"""Delegate a bounded subtask to a trusted, more-capable assistant, running headless.

Why: the local model must not validate its own work (skill review, eval judging,
memory maintenance), and some subtasks benefit from a stronger model.

TWO PROVIDERS (configurable in Settings → Delegation, via get_config/set_config):
  • claude_cli — the `claude` CLI. Agentic: reads AND edits files in a working dir.
    Uses the user's Claude Code subscription, so no API key. The default.
  • api — an OpenAI-compatible cloud model (reusing a configured endpoint + model).
    Run through OUR agent loop with OUR tools — exactly how local models work — so it
    can read, write, run shell, browse, etc. Just a stronger brain on the same harness.

Use delegate.run(...) to honour the configured provider. to_claude(...) forces the CLI.

ROLES — delegation is configured separately per role, so the user can point different
work at different models:
  • 'default' — the agent's `delegate` tool (interactive "use Claude / delegate").
  • 'improve' — the SELF-IMPROVING jobs: skills review, eval judging, memory maintenance.
'improve' may be set to 'inherit', meaning "use whatever 'default' is set to".

Containment (BOTH providers): the caller's `tools` spec and `timeout` are honoured
whichever provider runs. CLI → cwd inside the workspace, --allowedTools, subprocess
timeout. api → the spec is translated to the equivalent local tools and enforced by
the Agent at execution time, with a wall-clock deadline on the loop. No Bash/shell
unless a caller explicitly grants it.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import config
from oceano import atomicio

DEFAULT_TOOLS = "Read,Glob,Grep,Write,Edit"
# Delegation timeouts. The old model used ONE fixed wall-clock that killed long-but-active
# builds and lost all their output. Now we STREAM the run and use an IDLE timeout (reset on
# every event) — a productive run is never killed for "taking too long", only a stalled one —
# with a generous absolute cap as a backstop. All three are env-tunable for big builds.
_DELEGATE_IDLE = int(os.environ.get("OCEANO_DELEGATE_IDLE", "300"))       # secs with NO output → stalled
_DELEGATE_MAX = int(os.environ.get("OCEANO_DELEGATE_MAXTOTAL", "3600"))   # absolute cap (1h default)
_DELEGATE_TURNS = int(os.environ.get("OCEANO_DELEGATE_MAXTURNS", "60"))   # agent turns for a heavy build
_CONFIG_PATH = config.WORKSPACE.parent / "data" / "delegation.json"
_MODEL_KEY = "oceano_default_model"        # primary model id the agent uses everywhere
_BASE_KEY = "oceano_default_base_url"      # its endpoint (empty = the default local endpoint)
_KEY_KEY = "oceano_default_api_key"        # api key for that endpoint (empty = config default)
_ENABLED_KEY = "delegation_enabled"        # master on/off for delegation (run + delegate tool)
_CLAUDE_MODEL_KEY = "claude_model"         # which Claude model the CLI uses (alias/id); "" = CLI default
_RESERVED = ("oceano_default_model", "oceano_default_base_url", "oceano_default_api_key",
             "delegation_enabled", "claude_model")
# Claude models the user can pick for the CLI (mind + delegation). Aliases track the latest of each
# tier, so they stay valid across releases; "" means don't pass --model (use the CLI's own default).
CLAUDE_MODELS = (
    {"id": "", "label": "Default (subscription's default)"},
    {"id": "sonnet", "label": "Sonnet — balanced, recommended for the agent"},
    {"id": "opus", "label": "Opus — most capable, slower/costlier"},
    {"id": "haiku", "label": "Haiku — fastest, lightest"},
)
# 'default' = the agent's delegate tool · 'improve' = self-improving jobs · 'vision' = image
# recognition (the local chat model is text-only, so images are routed to this target).
ROLES = ("default", "improve", "vision")


# --- provider config, per role (Settings → Delegation) ---------------------
def _load_all():
    """All roles, normalised. Migrates the old flat {provider,base_url,model} shape →
    the 'default' role. 'improve' defaults to 'inherit' (follow default)."""
    try:
        d = json.loads(_CONFIG_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    if "provider" in d and "default" not in d:          # migrate legacy flat config
        d = {"default": {k: d.get(k, "") for k in ("provider", "base_url", "model")}}
    out = {}
    for role in ROLES:
        c = d.get(role) or {}
        prov = c.get("provider") or ("claude_cli" if role == "default" else "inherit")
        valid = ("claude_cli", "api") + (("inherit",) if role != "default" else ())
        out[role] = {"provider": prov if prov in valid else ("claude_cli" if role == "default" else "inherit"),
                     "base_url": c.get("base_url", "") or "", "model": c.get("model", "") or ""}
    return out


def get_config(role="default"):
    """Raw stored config for a role: {provider, base_url, model}. 'improve' may read
    provider=='inherit'. Use resolve() for the EFFECTIVE config a run should use."""
    return _load_all().get(role, {"provider": "claude_cli", "base_url": "", "model": ""})


def resolve(role="default"):
    """Effective config for a role — resolves 'inherit' to the default role's config."""
    cfg = get_config(role)
    if role != "default" and cfg["provider"] == "inherit":
        return get_config("default")
    return cfg


def set_config(d, role="default"):
    allcfg = _load_all()
    cur = allcfg.get(role, {})
    prov = d.get("provider", cur.get("provider"))
    valid = ("claude_cli", "api") + (("inherit",) if role != "default" else ())
    allcfg[role] = {"provider": prov if prov in valid else ("claude_cli" if role == "default" else "inherit"),
                    "base_url": (d.get("base_url", cur.get("base_url", "")) or "").strip(),
                    "model": (d.get("model", cur.get("model", "")) or "").strip()}
    out = dict(allcfg)
    raw = _raw()                                     # don't clobber the primary-model / enabled keys
    for k in _RESERVED:
        if k in raw:
            out[k] = raw[k]
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(out))
    except OSError:
        pass
    return allcfg[role]


def _raw():
    try:
        d = json.loads(_CONFIG_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def get_primary():
    """The user's EXPLICIT primary model + endpoint (Settings → Delegation), as stored —
    no resolution. An empty model means 'none pinned'; resolve_primary() then decides what
    Oceano actually uses (env pin or a Rivers-served model). Empty base_url = local endpoint."""
    d = _raw()
    return {"model": (d.get(_MODEL_KEY) or "").strip(),
            "base_url": (d.get(_BASE_KEY) or "").strip(),
            "api_key": (d.get(_KEY_KEY) or "").strip()}


def set_primary(model, base_url="", api_key=""):
    """Persist the primary model + its endpoint. base_url/api_key empty → use the config
    defaults (local llama.cpp). Preserves the per-role delegation configs in the same file."""
    d = _raw()
    d[_MODEL_KEY] = (model or "").strip()
    d[_BASE_KEY] = (base_url or "").strip()
    d[_KEY_KEY] = (api_key or "").strip()
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return get_primary()


def get_claude_model():
    """The Claude model id/alias the CLI should use (mind + delegation). '' = the CLI's own default."""
    return (_raw().get(_CLAUDE_MODEL_KEY) or "").strip()


def set_claude_model(model):
    """Persist which Claude model the CLI runs (e.g. 'sonnet', 'opus', or a full id). '' clears it."""
    d = _raw()
    d[_CLAUDE_MODEL_KEY] = (model or "").strip()
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return get_claude_model()


def _claude_model_args():
    """`--model <m>` for the claude CLI when the user pinned one, else [] (CLI default)."""
    m = get_claude_model()
    return ["--model", m] if m else []


def served_models():
    """Model ids currently wired into llama-swap — i.e. what Brain → Rivers has set up to
    serve on the default local endpoint. An offline read of llama-swap.yaml (insertion order),
    so it works without the endpoint being up. [] if the config is missing/unreadable."""
    try:
        import yaml
        d = yaml.safe_load(config.LLAMA_SWAP_CFG.read_text()) or {}
        return list((d.get("models") or {}).keys())
    except Exception:
        return []


def resolve_primary():
    """Resolve the model + endpoint Oceano should use, in priority order:
      1. the user-set primary (Settings → Delegation, or Rivers 'set as default')
      2. an OCEANO_MODEL env override (config.MODEL), if one is pinned
      3. a model served locally via Rivers (auto-picked, so Oceano just works once you've
         served one — no separate "make it primary" step)
    Returns {model, base_url, api_key, source}. There is NO hardcoded model: model == '' means
    nothing is configured at all, and the caller should tell the user to download/serve a model
    in Brain → Rivers (or pick a primary) rather than calling an endpoint with no model."""
    p = get_primary()
    if p["model"]:
        return {**p, "source": "primary"}
    if config.MODEL:
        return {"model": config.MODEL, "base_url": "", "api_key": "", "source": "env"}
    served = served_models()
    if served:
        return {"model": served[0], "base_url": "", "api_key": "", "source": "served"}
    return {"model": "", "base_url": "", "api_key": "", "source": "none"}


def get_default_model():                             # back-compat: the RESOLVED model id
    return resolve_primary()["model"]


def enabled():
    """Master delegation switch (default ON). When OFF, run() refuses and the delegate tool
    is withheld from the agent — so delegation can be fully turned off."""
    v = _raw().get(_ENABLED_KEY, True)
    return v if isinstance(v, bool) else str(v).lower() not in ("0", "false", "off", "no", "")


def set_enabled(on):
    d = _raw()
    d[_ENABLED_KEY] = bool(on)
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass


def get_mind():
    """Which mind drives the PRIMARY chat turn: 'local' (the served local model — fully offline,
    default), 'claude' (Claude Code via the user's subscription), or 'codex' (the Codex CLI via
    the user's OpenAI/Codex auth). Oceano is the body; this picks the mind."""
    m = (_raw().get("mind") or "local").strip().lower()
    return m if m in ("local", "claude", "codex") else "local"


def set_mind(mind):
    d = _raw()
    want = str(mind).strip().lower()
    d["mind"] = want if want in ("claude", "codex") else "local"
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return d["mind"]


def mind_is_claude():
    return get_mind() == "claude"


def mind_is_codex():
    return get_mind() == "codex"


def find_claude():
    """Locate the `claude` binary. PATH first, then common install dirs — because the
    engine runs under systemd with a minimal PATH that omits ~/.local/bin (where the
    official installer puts it), so shutil.which() alone reports it 'not installed'."""
    found = shutil.which("claude") or (os.environ.get("OCEANO_CLAUDE_BIN") or None)
    if found and os.access(found, os.X_OK):
        return found
    home = Path.home()
    for c in (home / ".local/bin/claude", Path("/usr/local/bin/claude"),
              Path("/usr/bin/claude"), home / ".npm-global/bin/claude",
              home / ".local/share/claude/bin/claude"):
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def available():
    return find_claude() is not None


def find_codex():
    """Locate the `codex` binary. PATH first, then common install dirs — mirroring the Claude
    lookup because the daemon may run under systemd with a reduced PATH."""
    found = shutil.which("codex") or (os.environ.get("OCEANO_CODEX_BIN") or None)
    if found and os.access(found, os.X_OK):
        return found
    home = Path.home()
    for c in (home / ".local/bin/codex", Path("/usr/local/bin/codex"),
              Path("/usr/bin/codex"), home / ".npm-global/bin/codex",
              home / ".local/share/codex/bin/codex"):
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def codex_available():
    return find_codex() is not None


def codex_version():
    binary = find_codex()
    if not binary:
        return None
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def to_claude(instructions, cwd=None, tools=DEFAULT_TOOLS, timeout=600, max_turns=30):
    """Run one headless Claude Code task. Returns {ok, output, error}."""
    binary = find_claude()
    if not binary:
        return {"ok": False, "output": "",
                "error": "claude CLI not found — install Claude Code, or set OCEANO_CLAUDE_BIN"}
    cmd = [binary, "-p", instructions, "--output-format", "text",
           "--max-turns", str(int(max_turns))] + _claude_model_args()
    if tools:
        cmd += ["--allowedTools", tools]
    try:
        r = subprocess.run(cmd, cwd=str(cwd or config.WORKSPACE),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"claude timed out after {timeout}s"}
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not launch claude: {e}"}
    if r.returncode != 0:
        return {"ok": False, "output": (r.stdout or "").strip(),
                "error": (r.stderr or f"claude exited {r.returncode}").strip()[:400]}
    return {"ok": True, "output": (r.stdout or "").strip(), "error": ""}


def _tool_detail(inp):
    """A short human label for a Claude tool_use input (a file path / command / pattern)."""
    if not isinstance(inp, dict):
        return ""
    for k in ("file_path", "path", "command", "pattern", "query", "url", "prompt", "description"):
        v = inp.get(k)
        if v:
            return str(v).replace("\n", " ")[:90]
    return ""


def to_claude_stream(instructions, cwd=None, tools=DEFAULT_TOOLS, idle_timeout=None,
                     max_total=None, max_turns=None, on_progress=None, append_system=None,
                     mcp_config=None, disallow=None, cancel=None):
    """Run a headless Claude Code task, STREAMING its events (--output-format stream-json).

    Three wins over the old blocking call:
      1. on_progress(ev) fires live as Claude works — ev is {kind:'text'|'tool', ...} — so a
         frontend can show what it's doing instead of a frozen spinner.
      2. an IDLE timeout (reset on every event) replaces the fixed wall-clock: a long build
         that's actively producing output is never killed; only a genuinely stalled one is.
      3. the final result is captured incrementally, so even a killed run keeps partial work.

    Returns {ok, output, error, partial, turns, cost}."""
    import queue
    import threading
    idle_timeout = idle_timeout or _DELEGATE_IDLE
    max_total = max_total or _DELEGATE_MAX
    max_turns = max_turns or _DELEGATE_TURNS
    binary = find_claude()
    if not binary:
        return {"ok": False, "output": "", "error": "claude CLI not found — install Claude Code, "
                "or set OCEANO_CLAUDE_BIN", "partial": False, "turns": 0, "cost": 0.0}
    cmd = [binary, "-p", instructions, "--output-format", "stream-json", "--verbose",
           "--max-turns", str(int(max_turns))] + _claude_model_args()
    if tools:
        cmd += ["--allowedTools", tools]
    if append_system:
        cmd += ["--append-system-prompt", append_system]   # Oceano's persona + memory ride on top
    if mcp_config:
        cmd += ["--mcp-config", mcp_config, "--strict-mcp-config"]   # only Oceano's tool-bridge, not the user's other MCP servers
    if disallow:
        cmd += ["--disallowedTools", disallow]      # block native write/shell so it acts through Oceano + can't touch ~/.claude

    def emit(ev):
        if on_progress:
            try:
                on_progress(ev)
            except Exception:
                pass

    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd or config.WORKSPACE),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not launch claude: {e}",
                "partial": False, "turns": 0, "cost": 0.0}

    q = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:                 # blocks in this thread, never the main loop
                q.put(line)
        finally:
            q.put(None)                              # EOF sentinel
    threading.Thread(target=reader, daemon=True).start()

    final, is_error, turns, cost, cancelled = "", False, 0, 0.0, False
    started = last_evt = time.monotonic()
    stalled, capped = False, False
    poll = 0.5 if cancel is not None else idle_timeout   # short polls so a Stop is honoured promptly
    while True:
        now = time.monotonic()
        if cancel is not None and cancel.is_set():   # the user hit Stop → kill the run now
            cancelled = True
            break
        if now - started > max_total:
            capped = True
            break
        if now - last_evt > idle_timeout:            # genuinely idle (the clock resets on every event)
            stalled = True
            break
        try:
            line = q.get(timeout=poll)
        except queue.Empty:
            continue                                 # loop back to re-check cancel / cap / idle
        last_evt = time.monotonic()
        if line is None:
            break                                    # process finished, stream closed
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "assistant":
            for block in (ev.get("message", {}).get("content") or []):
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    emit({"kind": "text", "text": block["text"]})
                elif bt == "tool_use":
                    emit({"kind": "tool", "tool": block.get("name", "tool"),
                          "detail": _tool_detail(block.get("input") or {})})
        elif t == "user":                              # tool results come back as a 'user' message
            for block in (ev.get("message", {}).get("content") or []):
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):            # content can be a list of text blocks or a string
                        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                    emit({"kind": "tool_result", "text": (c or "").strip()})
        elif t == "result":
            final = ev.get("result") or final
            is_error = bool(ev.get("is_error"))
            turns = ev.get("num_turns") or turns
            cost = ev.get("total_cost_usd") or cost
        # system / hook_* / rate_limit_event / user(tool_result) → not surfaced

    if stalled or capped or cancelled:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass

    err = ""
    if cancelled:
        err = "stopped by the user"
    elif stalled:
        err = f"the delegate produced no output for {idle_timeout}s and was stopped (looked stalled)"
    elif capped:
        err = f"the delegate hit the {max_total}s time cap and was stopped"
    elif is_error:
        err = "the delegate reported an error"
    elif not final:
        try:
            err = (proc.stderr.read() or "").strip()[:400] or "the delegate returned no output"
        except Exception:
            err = "the delegate returned no output"
    ok = bool(final) and not is_error and not stalled and not capped
    return {"ok": ok, "output": (final or "").strip(), "error": "" if ok else err,
            "partial": bool(final) and not ok, "turns": turns, "cost": round(cost, 4)}


def claude_version():
    binary = find_claude()
    if not binary:
        return None
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


# --- cloud API provider ----------------------------------------------------
# How a Claude-CLI --allowedTools spec maps onto OUR tool names, so the api provider
# honours the same containment callers ask of the CLI. Grep has no local tool —
# read_file/list_files cover that ground. Unknown CLI names grant nothing.
_API_TOOL_MAP = {
    "Read": ("read_file", "list_files"),
    "Glob": ("list_files",),
    "Grep": ("read_file", "list_files"),
    "Write": ("write_file", "make_folder"),
    "Edit": ("edit_file",),
    "Bash": ("run_shell", "python_exec"),
}


def _api_only_tools(tools_spec):
    """Translate a CLI tools spec into an allowlist of our tool names.
    None → no narrowing (the full enabled surface)."""
    if tools_spec is None:
        return None
    names = set()
    for t in (x.strip() for x in tools_spec.split(",")):
        if t:
            names.update(_API_TOOL_MAP.get(t, ()))
    return names


def to_api(instructions, cwd=None, role="default", tools=DEFAULT_TOOLS, timeout=600, on_progress=None):
    """Delegate to the configured cloud model by running it through OUR agent loop — the
    SAME machinery local models use. `tools` (a Claude-CLI-style spec) is translated to
    the equivalent local tools and enforced, and `timeout` puts a wall-clock deadline on
    the loop, so this provider honours the same containment as the CLI. Scoped to `cwd`
    (a throwaway/working folder) when given. on_progress(ev) surfaces its tool calls live.
    Returns {ok, output, error}. (learn=False so the task prompt is never mined into memory.)"""
    cfg = resolve(role)
    base_url, model = cfg["base_url"], cfg["model"]
    if not (base_url and model):
        return {"ok": False, "output": "",
                "error": "no delegate model configured — pick one in Settings → Delegation"}
    try:
        from oceano.web import server          # lazy: avoid an import cycle at module load
        api_key = server.endpoint_key(base_url) or "sk-no-key-needed"
    except Exception:
        api_key = "sk-no-key-needed"

    def _on_ev(kind, data):                       # map the cloud agent's loop events to progress
        if not on_progress:
            return
        if kind == "tool_call":
            on_progress({"kind": "tool", "tool": (data or {}).get("name", "tool"), "detail": ""})

    try:
        from oceano.agent import Agent
        from oceano import tools as _tools
        # learn=False + inject_context=False: a delegate gets a self-contained task, not the
        # user's persona/memories; exclude the delegate tool (both names) so it can't delegate
        # to itself in an infinite loop.
        ag = Agent(model=model, base_url=base_url, api_key=api_key, learn=False,
                   inject_context=False, exclude_tools={"delegate", "delegate_to_claude"},
                   only_tools=_api_only_tools(tools), on_event=_on_ev)
        deadline = (time.monotonic() + timeout) if timeout else None
        ctx = _tools.background_workspace(cwd) if cwd else _tools.background()
        with ctx:
            out = ag.run(instructions, deadline=deadline)
        return {"ok": True, "output": (out or "").strip(), "error": ""}
    except TimeoutError:
        return {"ok": False, "output": "", "error": f"delegate (cloud agent) timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "output": "", "error": f"delegate (cloud agent) error: {type(e).__name__}: {e}"}


def _api_ping(role="default", timeout=60):
    """Lightweight single completion to confirm the endpoint+model+key work (used by probe(),
    so a connectivity check doesn't spin up a whole agent loop)."""
    cfg = resolve(role)
    if not (cfg["base_url"] and cfg["model"]):
        return {"ok": False, "detail": "no delegate model configured (Settings → Delegation)"}
    try:
        from oceano.web import server
        key = server.endpoint_key(cfg["base_url"]) or "sk-no-key-needed"
    except Exception:
        key = "sk-no-key-needed"
    try:
        from openai import OpenAI
        c = OpenAI(base_url=cfg["base_url"], api_key=key, timeout=timeout)
        r = c.chat.completions.create(model=cfg["model"],
                                      messages=[{"role": "user", "content": "Reply with the single word: READY"}])
        out = (r.choices[0].message.content or "").strip()
        return {"ok": "ready" in out.lower(), "detail": out[:200] or "(empty reply)"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


# --- unified entry: honour the configured provider, per role ---------------
def run(instructions, cwd=None, tools=DEFAULT_TOOLS, timeout=None, max_turns=None,
        role="default", on_progress=None):
    """Delegate per the role's effective provider, STREAMING progress via on_progress(ev).
      claude_cli → the Claude Code CLI (its own tools; `tools=` limits --allowedTools),
                   streamed with an idle timeout so long active builds aren't killed;
      api        → the cloud model run through OUR agent loop with OUR tools.
    `cwd` scopes the working folder for both. role='improve' for self-improving jobs.
    `timeout` is the absolute cap (None → the generous default); idle is handled internally."""
    if not enabled():
        return {"ok": False, "output": "", "error": "Delegation is turned off (Settings → Delegation)."}
    if resolve(role)["provider"] == "api":
        return to_api(instructions, cwd=cwd, role=role, tools=tools,
                      timeout=timeout or _DELEGATE_MAX, on_progress=on_progress)
    return to_claude_stream(instructions, cwd=cwd, tools=tools, max_total=timeout,
                            max_turns=max_turns, on_progress=on_progress)


# --- vision: analyze an image via the configured target (the local chat model is text-only) ---
def _vision_api(image_path, question, cfg):
    """Direct multimodal completion (image_url) to a configured cloud vision model."""
    import base64
    import mimetypes
    if not (cfg["base_url"] and cfg["model"]):
        return {"ok": False, "output": "", "error": "no vision model configured (Settings → Delegation)"}
    try:
        data = Path(image_path).read_bytes()
    except OSError as e:
        return {"ok": False, "output": "", "error": f"can't read image: {e}"}
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    url = f"data:{mime};base64," + base64.b64encode(data).decode()
    try:
        from oceano.web import server
        key = server.endpoint_key(cfg["base_url"]) or "sk-no-key-needed"
    except Exception:
        key = "sk-no-key-needed"
    try:
        from openai import OpenAI
        c = OpenAI(base_url=cfg["base_url"], api_key=key, timeout=120)
        r = c.chat.completions.create(model=cfg["model"], messages=[{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": url}}]}])
        return {"ok": True, "output": (r.choices[0].message.content or "").strip(), "error": ""}
    except Exception as e:
        return {"ok": False, "output": "", "error": f"vision model error: {type(e).__name__}: {e}"}


def describe_image(image_path, question="", role="vision"):
    """Analyze an image with the configured vision target and return {ok, output, error}.
      claude_cli → Claude Code reads the image file directly (it's multimodal);
      api        → a direct image_url completion to the configured vision model.
    The text result is fed back to the (text-only) local chat model as context."""
    cfg = resolve(role)
    q = (question or "").strip() or "Describe this image in detail."
    q = (f"{q}\n\nDescribe only what is actually visible, concisely and factually.")
    if cfg["provider"] == "api":
        return _vision_api(image_path, q, cfg)
    if not find_claude():
        return {"ok": False, "output": "",
                "error": "claude CLI not found — install Claude Code or configure a cloud vision model in Settings → Delegation"}
    # Claude Code can open and 'see' image files via its Read tool (needs a few turns:
    # read the file, then answer — keep a little headroom).
    return to_claude(f"Open and look at the image file `{image_path}`. {q}",
                     cwd=config.WORKSPACE, tools="Read", timeout=300, max_turns=10)


# --- readiness (Settings → Delegation) -------------------------------------
def status_all():
    """Claude Code readiness (shared) plus per-role provider + readiness, for the UI.
    Auth is only proven by probe(role)."""
    binary = find_claude()
    claude = {"installed": bool(binary), "path": binary or "",
              "version": claude_version() if binary else None}

    def role_status(role):
        raw, eff = get_config(role), resolve(role)
        inherits = role != "default" and raw["provider"] == "inherit"
        ready = bool(eff["base_url"] and eff["model"]) if eff["provider"] == "api" else bool(binary)
        return {"provider": raw["provider"], "base_url": raw["base_url"], "model": raw["model"],
                "effective_provider": eff["provider"], "inherits": inherits, "ready": ready}

    return {"claude": claude, "default": role_status("default"),
            "improve": role_status("improve"), "vision": role_status("vision")}


def probe(role="default"):
    """Actually test a role's effective provider with a tiny live request. Returns {ok,
    provider, detail}. For claude_cli this proves authentication (a logged-out CLI fails)."""
    cfg = resolve(role)
    if cfg["provider"] == "api":
        r = _api_ping(role)
        return {"ok": r["ok"], "provider": "api", "detail": r["detail"]}
    if not find_claude():
        return {"ok": False, "provider": "claude_cli",
                "detail": "claude CLI not found — install Claude Code (npm i -g @anthropic-ai/claude-code), "
                          "or set OCEANO_CLAUDE_BIN to its path, then restart Oceano."}
    r = to_claude("Reply with the single word: READY", tools="", timeout=60, max_turns=1)
    if r["ok"] and "ready" in (r["output"] or "").lower():
        return {"ok": True, "provider": "claude_cli", "detail": claude_version() or "authenticated"}
    return {"ok": False, "provider": "claude_cli",
            "detail": (r["error"] or r["output"] or "not authenticated").strip()[:300]}
