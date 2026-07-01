"""Tool infrastructure: channels, the progress sink, workspace overrides, the tool
registry, per-tool enable/disable state, and dispatch. The tools themselves live in
the sibling domain modules; the package __init__ re-exports everything."""
import contextlib
import json
import threading
from pathlib import Path

import config
from oceano import atomicio

# --- channels --------------------------------------------------------------
# Oceano is driven from several places, and they don't share a screen. The live
# browser is ONE Chromium streamed to the WEB UI — so only the "web" channel may
# drive it. Telegram (the user can't see the browser) and unattended jobs
# (Researcher / scheduler / evals — nobody is watching) must NOT, or they'd hijack
# whatever the web view is showing. Off-web channels fall back to a plain HTTP
# fetch and decline the interactive browser tools. The channel is thread-local
# because each frontend/job runs on its own thread and drives tools synchronously.
#   web        → full interactive: live browser + screenshots
#   telegram   → attended chat, but no shared browser (HTTP fetch instead)
#   background → unattended job (Researcher/scheduler/evals): no browser
_local = threading.local()


def current_channel():
    return getattr(_local, "channel", "web")


def live_browser_available():
    """True only on the web channel — the only place a human can see the shared browser."""
    return current_channel() == "web"


def is_background():
    """An unattended job (no human in the loop) — distinct from an attended Telegram chat."""
    return current_channel() == "background"


@contextlib.contextmanager
def channel(name):
    """Run the enclosed agent work as a given channel (web/telegram/background)."""
    prev = getattr(_local, "channel", "web")
    _local.channel = name
    try:
        yield
    finally:
        _local.channel = prev


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
    """The workspace root for file/shell tools on THIS thread — a per-run override
    (set by the eval harness for isolation) or the global workspace by default."""
    return getattr(_local, "workspace", None) or config.WORKSPACE


@contextlib.contextmanager
def background_workspace(path):
    """Redirect this thread's file/shell tools to an isolated root (used by the eval
    harness so each case runs in a clean, throwaway workspace). Implies background()."""
    from pathlib import Path as _P
    root = _P(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prev_ws = getattr(_local, "workspace", None)
    prev_ch = getattr(_local, "channel", "web")
    _local.workspace = root
    _local.channel = "background"
    try:
        yield root
    finally:
        _local.workspace = prev_ws
        _local.channel = prev_ch


# --- registry --------------------------------------------------------------
_TOOLS = {}        # name -> python function
_SCHEMAS = []      # list of OpenAI tool schemas


def tool(schema):
    """Decorator: register a function as a tool with the given JSON schema."""
    def wrap(fn):
        _TOOLS[schema["function"]["name"]] = fn
        _SCHEMAS.append(schema)
        return fn
    return wrap


def register(name, schema, fn):
    """Register (or replace) a tool at runtime — used by the MCP client to expose
    external servers' tools alongside the built-in ones."""
    _TOOLS[name] = fn
    _SCHEMAS[:] = [s for s in _SCHEMAS if s["function"]["name"] != name] + [schema]


def unregister_prefix(prefix):
    """Drop all tools whose name starts with `prefix` (e.g. reconnecting MCP)."""
    for n in [n for n in _TOOLS if n.startswith(prefix)]:
        _TOOLS.pop(n, None)
    _SCHEMAS[:] = [s for s in _SCHEMAS if not s["function"]["name"].startswith(prefix)]


# --- per-tool enable/disable + chat-mode memory tools (Settings → Tools) -----
# Persisted so a user can hide tools from the model — turning one off removes it from
# the prompt, shrinking the context. Stored by NAME so it survives MCP reconnects
# (which re-register tools) and process restarts.
_STATE_PATH = config.WORKSPACE.parent / "data" / "tools.json"
_DISABLED = set()      # tools withheld from the model entirely (both modes)
_CHAT_OFF = set()      # memory tools the user turned OFF for plain chat mode specifically

# Memory tools that may be exposed in plain chat mode (Agent mode off), so the model can
# still recall/manage what it knows about the user without full tool access.
MEMORY_TOOLS = ("recall", "remember", "update_memory", "forget_memory")


def _load_state():
    global _DISABLED, _CHAT_OFF
    try:
        d = json.loads(_STATE_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    _DISABLED = set(d.get("disabled", []))
    _CHAT_OFF = set(d.get("chat_off", []))


def _save_state():
    try:
        atomicio.write_text(_STATE_PATH, json.dumps({"disabled": sorted(_DISABLED), "chat_off": sorted(_CHAT_OFF)}))
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


def run(name, arguments_json):
    """Execute a tool call and return its result as a string (always a string —
    that's what we feed back to the model)."""
    if name in _DISABLED:                         # the model can't see it, but never run it anyway
        return f"ERROR: tool {name!r} is disabled in Settings → Tools"
    fn = _TOOLS.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name!r}"
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return f"ERROR: bad arguments JSON: {e}"
    try:
        return str(fn(**args))
    except Exception as e:                       # tools should never crash the loop
        return f"ERROR running {name}: {e}"


def _resolve(path: str) -> Path:
    """Resolve a user/model-supplied path against the (possibly overridden) workspace."""
    root = _ws()
    p = (root / path).resolve()
    # is_relative_to (not startswith): a plain prefix match lets '/ws-evil' slip
    # past a workspace of '/ws'. root is already resolved.
    if config.CONFINE_TO_WORKSPACE and not p.is_relative_to(root):
        raise ValueError(f"path {path!r} escapes the workspace")
    return p
