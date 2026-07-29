"""Tool infrastructure: channels, the progress sink, workspace overrides, the tool
registry, per-tool enable/disable state, and dispatch. The tools themselves live in
the sibling domain modules; the package __init__ re-exports everything."""
import contextlib
import contextvars
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import config
from oceano import atomicio, policies, traces, turnctx

# --- channels --------------------------------------------------------------
# Oceano is driven from several places, and they don't share a screen. The live
# browser is ONE Chromium streamed to the WEB UI — so only the "web" channel may
# drive it. Telegram (the user can't see the browser) and unattended jobs
# (Researcher / scheduler / evals — nobody is watching) must NOT, or they'd hijack
# whatever the web view is showing. Off-web channels fall back to a plain HTTP
# fetch and decline the interactive browser tools. The channel (and the workspace
# override below) live on the ONE per-turn TurnContext (oceano.turnctx) — each
# frontend/job brackets its work, and turnctx.carry() hands the whole context to a
# worker thread instead of the old thread-locals silently reverting to defaults.
#   web        → full interactive: live browser + screenshots
#   telegram   → attended chat, but no shared browser (HTTP fetch instead)
#   background → unattended job (Researcher/scheduler/evals): no browser
_local = threading.local()     # now only the progress sink — see below


def current_channel():
    return turnctx.get().channel


def live_browser_available():
    """True only on the web channel — the only place a human can see the shared browser."""
    return current_channel() == "web"


def is_background():
    """An unattended job (no human in the loop) — distinct from an attended Telegram chat."""
    return current_channel() == "background"


@contextlib.contextmanager
def channel(name):
    """Run the enclosed agent work as a given channel (web/telegram/background)."""
    with turnctx.push(channel=name):
        yield


# --- client (which app made this HTTP request, orthogonal to channel) ------
# channel says WHERE the turn runs (web/telegram/background); client says which app the "web"
# channel actually came through — a plain browser tab, or OceanoDesktop (Electron, tagged via the
# X-Oceano-Client header in routes_chat.py). Only OceanoDesktop has a real OS process to run native
# actions in (see oceano.desktopbridge), so desktop-only tools gate on this, not on channel.
def current_client():
    return turnctx.get().client


def is_desktop_client():
    """True only when this turn came through the OceanoDesktop app (not a plain browser tab)."""
    return current_client() == "desktop"


# --- progress sink ---------------------------------------------------------
# A long-running tool (the streaming delegate) can push live progress to whoever is driving
# it. The agent sets a sink before running such a tool (on the same thread the tool runs on)
# and drains it into its event stream; emit_progress is a no-op when nobody's listening.
def set_progress_sink(fn):
    _local.progress = fn


def clear_progress_sink():
    _local.progress = None


def emit_progress(ev):
    fn = getattr(_local, "progress", None)
    if fn:
        try:
            fn(ev)
        except Exception:
            pass


@contextlib.contextmanager
def background():
    """Run unattended agent work — no shared live browser (Researcher/scheduler/evals)."""
    with channel("background"):
        yield


def _ws():
    """The workspace root for file/shell tools on this turn — a per-run override
    (set by the eval harness for isolation) or the global workspace by default."""
    return turnctx.get().workspace or config.WORKSPACE


@contextlib.contextmanager
def background_workspace(path):
    """Redirect this turn's file/shell tools to an isolated root (used by the eval
    harness so each case runs in a clean, throwaway workspace). Implies background()."""
    from pathlib import Path as _P
    root = _P(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with turnctx.push(workspace=root, channel="background"):
        yield root


# --- registry --------------------------------------------------------------
_TOOLS = {}        # name -> python function
_SCHEMAS = []      # list of OpenAI tool schemas
_TOOL_SPECS = {}   # name -> ToolSpec (execution/security metadata; schemas stay provider-neutral)
_TOOL_OVERRIDES = contextvars.ContextVar("oceano_tool_overrides", default=None)


@dataclass(frozen=True)
class ToolSpec:
    """Runtime contract for a registered tool.

    The JSON schema answers "how may the model call this?". This metadata answers
    "what may the runtime permit and expect?". Defaults preserve the historical
    behavior; built-ins inherit the existing capability map unless a decorator or
    an MCP schema explicitly declares richer ``x-oceano`` metadata.
    """

    name: str
    capability: str = ""
    risk: str = "low"
    side_effecting: bool = False
    idempotent: bool = True
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ToolResult:
    """Structured internal result with a backward-compatible model-facing string."""

    ok: bool
    summary: str = ""
    data: Any = None
    error: str | None = None
    retryable: bool = False
    side_effects: tuple[str, ...] = field(default_factory=tuple)
    verification: tuple[str, ...] = field(default_factory=tuple)
    code: str = ""

    WIRE_PROTOCOL: ClassVar[str] = "oceano.tool-result.v1"

    def text(self) -> str:
        if self.ok:
            if self.summary:
                return self.summary
            if self.data is None:
                return ""
            if isinstance(self.data, str):
                return self.data
            return json.dumps(self.data, ensure_ascii=False, default=str)
        message = self.error or self.summary or "tool execution failed"
        return message if message.lstrip().upper().startswith("ERROR") else f"ERROR: {message}"

    def to_wire(self) -> dict:
        """Versioned transport envelope used by the resident MCP bridge."""
        data = json.loads(json.dumps(self.data, ensure_ascii=False, default=str))
        return {
            "protocol": self.WIRE_PROTOCOL,
            "ok": self.ok,
            "summary": self.summary,
            "data": data,
            "error": self.error,
            "retryable": self.retryable,
            "side_effects": list(self.side_effects),
            "verification": list(self.verification),
            "code": self.code,
        }

    @classmethod
    def from_wire(cls, value):
        """Decode a bridge envelope, returning None for legacy/unstructured values."""
        payload = value
        if isinstance(value, str):
            try:
                payload = json.loads(value)
            except (TypeError, ValueError):
                return None
        if not isinstance(payload, dict) or payload.get("protocol") != cls.WIRE_PROTOCOL:
            return None
        return cls(
            ok=bool(payload.get("ok")),
            summary=str(payload.get("summary") or ""),
            data=payload.get("data"),
            error=(str(payload["error"]) if payload.get("error") is not None else None),
            retryable=bool(payload.get("retryable")),
            side_effects=tuple(str(effect) for effect in payload.get("side_effects") or ()),
            verification=tuple(str(item) for item in payload.get("verification") or ()),
            code=str(payload.get("code") or ""),
        )

    @classmethod
    def from_value(cls, value, *, spec=None):
        if isinstance(value, cls):
            return value
        text = str(value)
        low = text.lstrip().lower()
        failed = low.startswith("error") or "traceback (most recent call last)" in low
        effects = (f"capability:{spec.capability}",) if spec and spec.side_effecting and not failed else ()
        return cls(ok=not failed, summary=text if not failed else "",
                   data=value if not failed and not isinstance(value, str) else None,
                   error=text if failed else None, side_effects=effects)


def _schema_metadata(schema):
    raw = schema.get("x-oceano") if isinstance(schema, dict) else None
    return raw if isinstance(raw, dict) else {}


_EXIT = re.compile(r"\(exit\s+(\d+)(?:,|\))", re.IGNORECASE)


def _normalize_result(name, args, value, spec):
    """Enrich legacy string-returning tools without changing their public API."""
    result = ToolResult.from_value(value, spec=spec)
    text = result.text()
    low = text.lower()
    if result.ok and name in {"run_tests", "run_shell", "python_exec"}:
        match = _EXIT.search(text)
        if match and int(match.group(1)) != 0:
            code = "tests_failed" if name == "run_tests" else "command_failed"
            return ToolResult(False, error=text, retryable=True, code=code)
        # shell.py emits this marker as the first line only when Oceano itself kills a
        # command at the configured deadline. Command output may legitimately discuss
        # something that "timed out" while still exiting successfully.
        if low.lstrip().startswith("(timed out after "):
            return ToolResult(False, error=text, retryable=True, code="timeout")
    if result.ok and name in {"list_files", "edit_file"} and low.startswith("(no such"):
        return ToolResult(False, error=text, retryable=True, code="not_found")
    if result.ok and name in {"run_tests", "run_shell", "python_exec"}:
        result = ToolResult(
            True, summary=result.summary, data=result.data,
            side_effects=result.side_effects,
            verification=result.verification + (f"{name}:ok",), code=result.code)
    if not result.ok and not result.code:
        retryable = any(term in low for term in ("not found", "timed out", "temporar", "try again"))
        code = "not_found" if "not found" in low else "tool_error"
        return ToolResult(False, error=result.error or text, retryable=retryable, code=code)
    if result.ok:
        path = str(args.get("path") or ".")
        effects = {
            "write_file": (f"file:{path}",),
            "edit_file": (f"file:{path}",),
            "make_folder": (f"directory:{path}",),
        }.get(name)
        if effects:
            return ToolResult(True, summary=result.summary, data=result.data,
                              side_effects=effects, verification=result.verification,
                              code=result.code)
    return result


def _exception_result(name, exc):
    if isinstance(exc, FileNotFoundError):
        return ToolResult(False, error=f"running {name}: {exc}", retryable=True,
                          code="not_found")
    if isinstance(exc, PermissionError):
        return ToolResult(False, error=f"running {name}: {exc}", code="permission_denied")
    if isinstance(exc, IsADirectoryError):
        return ToolResult(False, error=f"running {name}: {exc}", code="invalid_target")
    if isinstance(exc, ValueError):
        return ToolResult(False, error=f"running {name}: {exc}", code="invalid_input")
    return ToolResult(False, error=f"running {name}: {exc}", code="execution_error")


_SIDE_EFFECT_CAPABILITIES = {
    "workspace_write", "shell_exec", "python_exec", "background_job", "http_request",
    "browser_control", "remote_access", "mail_manage", "mail_send", "calendar_write",
    "memory_write", "schedule_write", "notes_write", "desktop_control",
}


def _make_spec(name, schema, metadata=None):
    values = {**_schema_metadata(schema), **(metadata or {})}
    capability = str(values.get("capability") or policies.capability_for_tool(name) or "")
    side_effecting = bool(values.get("side_effecting", capability in _SIDE_EFFECT_CAPABILITIES))
    return ToolSpec(
        name=name, capability=capability,
        risk=str(values.get("risk") or ("high" if capability in {"remote_access", "mail_send"}
                                        else "medium" if side_effecting else "low")),
        side_effecting=side_effecting,
        idempotent=bool(values.get("idempotent", not side_effecting)),
        requires_confirmation=bool(values.get("requires_confirmation", False)),
    )


@contextlib.contextmanager
def tool_overrides(mapping):
    """Temporarily replace selected tool implementations for this execution context.

    The eval harness uses this to give models deterministic fixtures for personal or
    external services (calendar, mail, web) without touching the user's real state.
    Values may be callables accepting the decoded arguments, or fixed result strings.
    """
    token = _TOOL_OVERRIDES.set(dict(mapping or {}))
    try:
        yield
    finally:
        _TOOL_OVERRIDES.reset(token)


def tool(schema, **metadata):
    """Decorator: register a function as a tool with the given JSON schema."""
    def wrap(fn):
        name = schema["function"]["name"]
        _TOOLS[name] = fn
        _TOOL_SPECS[name] = _make_spec(name, schema, metadata)
        _SCHEMAS.append(schema)
        return fn
    return wrap


def register(name, schema, fn, **metadata):
    """Register (or replace) a tool at runtime — used by the MCP client to expose
    external servers' tools alongside the built-in ones."""
    _TOOLS[name] = fn
    _TOOL_SPECS[name] = _make_spec(name, schema, metadata)
    _SCHEMAS[:] = [s for s in _SCHEMAS if s["function"]["name"] != name] + [schema]


def unregister_prefix(prefix):
    """Drop all tools whose name starts with `prefix` (e.g. reconnecting MCP)."""
    for n in [n for n in _TOOLS if n.startswith(prefix)]:
        _TOOLS.pop(n, None)
        _TOOL_SPECS.pop(n, None)
    _SCHEMAS[:] = [s for s in _SCHEMAS if not s["function"]["name"].startswith(prefix)]


# --- per-tool enable/disable + chat-mode memory tools (Settings → Tools) -----
# Persisted so a user can hide tools from the model — turning one off removes it from
# the prompt, shrinking the context. Stored by NAME so it survives MCP reconnects
# (which re-register tools) and process restarts.
_STATE_PATH = config.WORKSPACE.parent / "data" / "tools.json"
_DISABLED = set()      # tools withheld from the model entirely (both modes)
_CHAT_OFF = set()      # memory tools the user turned OFF for plain chat mode specifically
# User-configurable tool-call budgets (Settings → Tools). 0 = unset → the env-var default
# (config.MAX_STEPS / delegate's OCEANO_DELEGATE_MAXTURNS) applies, unchanged from before
# these existed. Kept here (not oceano.web.state) since this IS the tools store, and giving
# the user this knob is what closes the gap where a large workflow build (many file edits,
# each needing its own turn) silently hit a hardcoded ceiling with no way to raise it.
_MAX_STEPS = 0             # tool-call loop cap per turn: chat + background api/local agents
_MAX_DELEGATE_TURNS = 0    # Claude/Codex CLI's own --max-turns, for CLI-provider delegation

# Memory tools that may be exposed in plain chat mode (Agent mode off), so the model can
# still recall/manage what it knows about the user without full tool access.
MEMORY_TOOLS = ("recall", "remember", "update_memory", "forget_memory")


def _load_state():
    global _DISABLED, _CHAT_OFF, _MAX_STEPS, _MAX_DELEGATE_TURNS
    try:
        d = json.loads(_STATE_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    _DISABLED = set(d.get("disabled", []))
    _CHAT_OFF = set(d.get("chat_off", []))
    try:
        _MAX_STEPS = max(0, min(int(d.get("max_steps", 0)), 500))
    except (TypeError, ValueError):
        _MAX_STEPS = 0
    try:
        _MAX_DELEGATE_TURNS = max(0, min(int(d.get("max_delegate_turns", 0)), 500))
    except (TypeError, ValueError):
        _MAX_DELEGATE_TURNS = 0


def _save_state():
    try:
        atomicio.write_text(_STATE_PATH, json.dumps({"disabled": sorted(_DISABLED), "chat_off": sorted(_CHAT_OFF),
                                                      "max_steps": _MAX_STEPS, "max_delegate_turns": _MAX_DELEGATE_TURNS}))
    except OSError:
        pass


_load_state()


def all_schemas():
    """Every registered tool schema, enabled or not — for the Settings → Tools list."""
    return list(_SCHEMAS)


def schemas():
    """Tool schemas EXPOSED to the model. Disabled tools (Settings → Tools) are withheld,
    so turning a tool off removes it from the prompt and lowers the context cost."""
    return [s for s in _SCHEMAS if s["function"]["name"] not in _DISABLED]


def is_enabled(name):
    return name not in _DISABLED


def tool_spec(name):
    """Return immutable execution metadata for a registered tool, if known."""
    return _TOOL_SPECS.get(name)


def set_enabled(name, on):
    _DISABLED.discard(name) if on else _DISABLED.add(name)
    _save_state()


def set_all(on):
    """Enable or disable every currently-registered tool at once."""
    global _DISABLED
    _DISABLED = set() if on else {s["function"]["name"] for s in _SCHEMAS}
    _save_state()


def chat_tools():
    """Tool names available in plain chat mode (Agent mode off): the user-kept memory tools,
    intersected with globally-enabled tools. Empty list → chat mode is fully tool-free."""
    return [m for m in MEMORY_TOOLS if m not in _CHAT_OFF and m not in _DISABLED]


def chat_tool_state():
    """For the Settings UI: each memory tool with its chat-mode + global state + description."""
    by_name = {s["function"]["name"]: s["function"].get("description", "") for s in _SCHEMAS}
    return [{"name": m, "description": by_name.get(m, ""), "in_chat": m not in _CHAT_OFF,
             "enabled": m not in _DISABLED} for m in MEMORY_TOOLS if m in by_name]


def set_chat_tool(name, on):
    """Toggle whether a memory tool is offered in plain chat mode."""
    if name not in MEMORY_TOOLS:
        return
    _CHAT_OFF.discard(name) if on else _CHAT_OFF.add(name)
    _save_state()


def get_max_steps():
    """Tool-call loop cap per turn — how many rounds of (LLM reply + its tool calls) a single
    turn may take before Oceano forces a wrap-up. Applies to the interactive mind AND every
    background api/local agent (workflow nodes included). 0/unset → config.MAX_STEPS."""
    return _MAX_STEPS or config.MAX_STEPS


def set_max_steps(n):
    """0 (or negative) clears the override (falls back to config.MAX_STEPS / OCEANO_MAX_STEPS)."""
    global _MAX_STEPS
    n = int(n or 0)
    _MAX_STEPS = min(n, 500) if n > 0 else 0
    _save_state()


def get_max_steps_override():
    """Raw override (0 = unset) — for the Settings UI to distinguish "using the built-in
    default" from "explicitly set to N". Agents should call get_max_steps() instead."""
    return _MAX_STEPS


def get_max_delegate_turns():
    """User override for Claude/Codex CLI delegation's own --max-turns budget. 0/unset → the
    caller's own default (delegate._DELEGATE_TURNS / OCEANO_DELEGATE_MAXTURNS) — kept as a bare
    int here (not delegate._DELEGATE_TURNS itself) so this module never imports oceano.delegate."""
    return _MAX_DELEGATE_TURNS


def set_max_delegate_turns(n):
    """0 (or negative) clears the override (falls back to delegate's own default)."""
    global _MAX_DELEGATE_TURNS
    n = int(n or 0)
    _MAX_DELEGATE_TURNS = min(n, 500) if n > 0 else 0
    _save_state()


def run_result(name, arguments_json):
    """Execute a tool and return a structured result.

    ``run()`` below remains the compatibility API used by integrations and tests.
    New orchestration code should consume this result instead of parsing error text.
    """
    if name in _DISABLED:
        return ToolResult(False, error=f"tool {name!r} is disabled in Settings → Tools",
                          code="disabled")
    fn = _TOOLS.get(name)
    if fn is None:
        return ToolResult(False, error=f"unknown tool {name!r}", code="unknown_tool")
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return ToolResult(False, error=f"bad arguments JSON: {e}", code="bad_arguments")
    if not isinstance(args, dict):
        return ToolResult(False, error="tool arguments must be a JSON object", code="bad_arguments")
    spec = _TOOL_SPECS.get(name) or _make_spec(name, {}, {})
    override = (_TOOL_OVERRIDES.get() or {}).get(name)
    if override is not None:
        traces.record("tool_call", tool=name, capability="eval-fixture", args=args)
        try:
            value = override(**args) if callable(override) else override
            result = _normalize_result(name, args, value, spec)
            traces.record("tool_result", tool=name, capability="eval-fixture", ok=result.ok,
                          result=result.text()[:500])
            return result
        except Exception as e:
            traces.record("tool_result", tool=name, capability="eval-fixture", ok=False, error=str(e))
            return ToolResult(False, error=f"running eval fixture {name}: {e}", code="fixture_error")
    cap = spec.capability
    mode = policies.get().get(cap, "allow") if cap else "allow"
    if mode == "block":
        return ToolResult(False, error=f"tool {name!r} is blocked by policy ({cap})",
                          code="policy_blocked")
    if (mode == "confirm" or spec.requires_confirmation) and not policies.is_permitted(cap):
        return ToolResult(False,
                          error=(f"tool {name!r} requires approval by policy ({cap}). Run it from a "
                                 "workflow approval step or set the capability to allow."),
                          code="approval_required")
    traces.record("tool_call", tool=name, capability=cap or None, args=args)
    try:
        result = _normalize_result(name, args, fn(**args), spec)
        traces.record("tool_result", tool=name, capability=cap or None, ok=result.ok,
                      result=result.text()[:500], code=result.code or None,
                      side_effects=list(result.side_effects))
        return result
    except Exception as e:
        traces.record("tool_result", tool=name, capability=cap or None, ok=False, error=str(e))
        return _exception_result(name, e)


def run(name, arguments_json):
    """Execute a tool call and return the historical model-facing string."""
    return run_result(name, arguments_json).text()


def _resolve(path: str) -> Path:
    """Resolve a user/model-supplied path against the (possibly overridden) workspace."""
    root = _ws()
    p = (root / path).resolve()
    # is_relative_to (not startswith): a plain prefix match lets '/ws-evil' slip
    # past a workspace of '/ws'. root is already resolved.
    if config.CONFINE_TO_WORKSPACE and not p.is_relative_to(root):
        raise ValueError(f"path {path!r} escapes the workspace")
    return p
