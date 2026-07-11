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
import re
import shutil
import subprocess
import time
from pathlib import Path

import config
from oceano import atomicio, secretcrypto

DEFAULT_TOOLS = "Read,Glob,Grep,Write,Edit"
# Delegation timeouts. The old model used ONE fixed wall-clock that killed long-but-active
# builds and lost all their output. Now we STREAM the run and use an IDLE timeout (reset on
# every event) — a productive run is never killed for "taking too long", only a stalled one —
# with a generous absolute cap as a backstop. All three are env-tunable for big builds.
_DELEGATE_IDLE = int(os.environ.get("OCEANO_DELEGATE_IDLE", "300"))       # secs with NO output → stalled
_DELEGATE_MAX = int(os.environ.get("OCEANO_DELEGATE_MAXTOTAL", "3600"))   # absolute cap (1h default)
_DELEGATE_TURNS = int(os.environ.get("OCEANO_DELEGATE_MAXTURNS", "60"))   # agent turns for a heavy build
_RL_MIN_WAIT = 15.0   # floor on the rate-limit wait — a "reset" already in the past still backs off a beat
_CONFIG_PATH = config.WORKSPACE.parent / "data" / "delegation.json"
_MODEL_KEY = "oceano_default_model"        # primary model id the agent uses everywhere
_BASE_KEY = "oceano_default_base_url"      # its endpoint (empty = the default local endpoint)
_KEY_KEY = "oceano_default_api_key"        # api key for that endpoint (empty = config default)
_ENABLED_KEY = "delegation_enabled"        # master on/off for delegation (run + delegate tool)
_CLAUDE_MODEL_KEY = "claude_model"         # which Claude model the CLI uses (alias/id); "" = CLI default
_CODEX_MODEL_KEY = "codex_model"           # which Codex model the CLI uses (alias/id); "" = CLI default
_CLAUDE_EFFORT_KEY = "claude_effort"       # Claude `--effort` reasoning level; "" = CLI default
_CODEX_EFFORT_KEY = "codex_effort"         # Codex model_reasoning_effort; "" = CLI default
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")   # accepted by `claude --effort`
CODEX_EFFORTS = ("minimal", "low", "medium", "high")         # codex model_reasoning_effort values
_ROUTE_KEY = "route_by_evals"              # un-pinned primary follows the eval leaderboard winner
# Legacy pre-roles flat keys: dropped on the next set_config write (they were migrated into the
# 'default' role by _load_all and would otherwise linger at the top level forever).
_LEGACY = ("provider", "base_url", "model")
# Claude models the user can pick for the CLI (mind + delegation). Aliases track the latest of each
# tier, so they stay valid across releases; "" means don't pass --model (use the CLI's own default).
CLAUDE_MODELS = (
    {"id": "", "label": "Default (subscription's default)"},
    {"id": "sonnet", "label": "Sonnet — balanced, recommended for the agent"},
    {"id": "opus", "label": "Opus — most capable, slower/costlier"},
    {"id": "haiku", "label": "Haiku — fastest, lightest"},
    {"id": "fable", "label": "Fable — newest addition to the Claude family"},
)
CODEX_MODELS = (
    {"id": "", "label": "Recommended default (currently GPT-5.5)"},
    {"id": "gpt-5.5", "label": "GPT-5.5 — strongest for complex coding and research"},
    {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini — faster and lower cost"},
    {"id": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark — near-instant coding iteration (preview)"},
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
        valid = ("claude_cli", "codex_cli", "api") + (("inherit",) if role != "default" else ())
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
    valid = ("claude_cli", "codex_cli", "api") + (("inherit",) if role != "default" else ())
    allcfg[role] = {"provider": prov if prov in valid else ("claude_cli" if role == "default" else "inherit"),
                    "base_url": (d.get("base_url", cur.get("base_url", "")) or "").strip(),
                    "model": (d.get("model", cur.get("model", "")) or "").strip()}
    # Preserve every non-role key (primary model, enabled, mind, model/effort pins, routing…).
    # This used to keep only a whitelist, so saving a role config silently WIPED any stored key
    # the list had fallen behind on — the mind reset to local, the effort pins vanished.
    out = {k: v for k, v in _raw().items() if k not in ROLES and k not in _LEGACY}
    out.update(allcfg)
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
            "api_key": secretcrypto.decrypt((d.get(_KEY_KEY) or "").strip())}


def set_primary(model, base_url="", api_key=""):
    """Persist the primary model + its endpoint. base_url/api_key empty → use the config
    defaults (local llama.cpp). Preserves the per-role delegation configs in the same file."""
    d = _raw()
    d[_MODEL_KEY] = (model or "").strip()
    d[_BASE_KEY] = (base_url or "").strip()
    d[_KEY_KEY] = secretcrypto.encrypt((api_key or "").strip())
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


def get_codex_model():
    """The Codex model id the CLI should use for the resident Codex mind. '' = CLI default."""
    return (_raw().get(_CODEX_MODEL_KEY) or "").strip()


def set_codex_model(model):
    """Persist which model the Codex CLI runs for the resident mind. '' clears it."""
    d = _raw()
    d[_CODEX_MODEL_KEY] = (model or "").strip()
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return get_codex_model()


def get_claude_effort():
    """The Claude `--effort` reasoning level (mind + delegation). '' = the CLI's own default."""
    e = (_raw().get(_CLAUDE_EFFORT_KEY) or "").strip()
    return e if e in CLAUDE_EFFORTS else ""


def set_claude_effort(effort):
    d = _raw()
    d[_CLAUDE_EFFORT_KEY] = (effort or "").strip()
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return get_claude_effort()


def get_codex_effort():
    """Codex's model_reasoning_effort (mind + delegation). '' = the CLI's own default."""
    e = (_raw().get(_CODEX_EFFORT_KEY) or "").strip()
    return e if e in CODEX_EFFORTS else ""


def set_codex_effort(effort):
    d = _raw()
    d[_CODEX_EFFORT_KEY] = (effort or "").strip()
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return get_codex_effort()


def _claude_model_args():
    """`--model <m>` for the claude CLI when the user pinned one, else [] (CLI default)."""
    m = get_claude_model()
    return ["--model", m] if m else []


def _claude_effort_args():
    """`--effort <level>` for the claude CLI when the user pinned one, else [] (CLI default)."""
    e = get_claude_effort()
    return ["--effort", e] if e else []


def _codex_effort_args():
    """`-c model_reasoning_effort="<level>"` for the codex CLI when pinned, else [] (CLI default)."""
    e = get_codex_effort()
    return ["-c", f'model_reasoning_effort="{e}"'] if e else []


def _codex_model_args():
    """`--model <m>` for the codex CLI when the user pinned one, else [] (CLI default).
    Codex delegation reuses the same global model pin as the Codex mind, just as Claude
    delegation reuses the global Claude model pin."""
    m = get_codex_model()
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


def get_route_by_evals():
    """When ON (default off) and no primary is pinned, resolve_primary() picks the eval
    leaderboard's top scorer among the served models instead of llama-swap file order —
    the eval suite's verdict actually steering which model answers."""
    return bool(_raw().get(_ROUTE_KEY, False))


def set_route_by_evals(on):
    d = _raw()
    d[_ROUTE_KEY] = bool(on)
    try:
        atomicio.write_text(_CONFIG_PATH, json.dumps(d, indent=2))
    except OSError:
        pass
    return get_route_by_evals()


def _eval_winner(served):
    """The leaderboard's best model among `served`, or None (no finished run / stale data /
    winner no longer served). Never raises — routing must degrade to file order, not break
    model resolution."""
    try:
        from oceano import evals                     # lazy: keep delegate import-light
        return evals.best_model(among=served)
    except Exception:
        return None


def resolve_primary():
    """Resolve the model + endpoint Oceano should use, in priority order:
      1. the user-set primary (Settings → Delegation, or Rivers 'set as default')
      2. an OCEANO_MODEL env override (config.MODEL), if one is pinned
      3. with route-by-evals ON: the eval leaderboard's top scorer among the served models
      4. a model served locally via Rivers (auto-picked, so Oceano just works once you've
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
        if get_route_by_evals():
            best = _eval_winner(served)
            if best:
                return {"model": best, "base_url": "", "api_key": "", "source": "evals"}
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
    cmd = [binary, "-p", "--output-format", "text",
           "--max-turns", str(int(max_turns))] + _claude_model_args() + _claude_effort_args()
    if tools:
        cmd += ["--allowedTools", tools]
    try:
        # Feed the prompt on stdin, NOT as a positional arg: Linux caps a single argv string at
        # MAX_ARG_STRLEN (128 KB), so a long prompt (e.g. continuing a big chat) overflows it and
        # execve fails with E2BIG ("Argument list too long"). `claude -p` reads the prompt from stdin
        # when none is given — the same reason codex_mind / to_codex feed stdin.
        r = subprocess.run(cmd, cwd=str(cwd or config.WORKSPACE), input=instructions,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"claude timed out after {timeout}s"}
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not launch claude: {e}"}
    if r.returncode != 0:
        return {"ok": False, "output": (r.stdout or "").strip(),
                "error": (r.stderr or f"claude exited {r.returncode}").strip()[:400]}
    return {"ok": True, "output": (r.stdout or "").strip(), "error": ""}


# A failed run that LOOKS like the provider's rate/usage limit (subscription window exhausted,
# API 429/529) — checked against the run's ERROR text only, never a successful result, so a task
# that merely talks about rate limits can't trip it.
_RL_ERROR = re.compile(r"usage limit|rate.?limit|too many requests|\b429\b|overloaded", re.I)


def _rl_reset_at(info):
    """Best-effort epoch-seconds reset time from a stream rate_limit_event payload. The schema
    has shifted across CLI releases, so scan for any *reset* key: a value > 1e9 is an absolute
    epoch (ms or s), a small positive one a relative 'resets in N seconds'."""
    for k, v in (info or {}).items():
        if "reset" not in str(k).lower():
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 1e12:
            return v / 1000.0
        if v > 1e9:
            return v
        if v > 0:
            return time.time() + v
    return None


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
                     mcp_config=None, disallow=None, cancel=None, skills=False):
    """Run a headless Claude Code task, STREAMING its events (--output-format stream-json).

    Three wins over the old blocking call:
      1. on_progress(ev) fires live as Claude works — ev is {kind:'text'|'tool', ...} — so a
         frontend can show what it's doing instead of a frozen spinner.
      2. an IDLE timeout (reset on every event) replaces the fixed wall-clock: a long build
         that's actively producing output is never killed; only a genuinely stalled one is.
      3. the final result is captured incrementally, so even a killed run keeps partial work.

    `skills=True` wires Oceano's "skills"-scoped MCP bridge (list_skills/load_skill only — never
    memory/the rest of the body) when the caller didn't already pass its own `mcp_config` (the
    resident Claude-mind path builds its own full-body one instead — see agent.py).

    Returns {ok, output, error, partial, turns, cost}."""
    import queue
    import threading
    idle_timeout = idle_timeout or _DELEGATE_IDLE
    max_total = max_total or _DELEGATE_MAX
    from oceano import tools as _tools   # lazy: avoid importing tools at delegate.py's module load
    max_turns = max_turns or _tools.get_max_delegate_turns() or _DELEGATE_TURNS
    binary = find_claude()
    if not binary:
        return {"ok": False, "output": "", "error": "claude CLI not found — install Claude Code, "
                "or set OCEANO_CLAUDE_BIN", "partial": False, "turns": 0, "cost": 0.0}
    if skills and not mcp_config:
        from oceano import mindbridge
        mcp_config = mindbridge.mcp_config_path(background=True, scope="skills")
        if mcp_config:
            names = mindbridge.tool_names(scope="skills")
            if names:
                tools = (tools + "," if tools else "") + ",".join("mcp__oceano__" + n for n in names)
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose",
           "--max-turns", str(int(max_turns))] + _claude_model_args() + _claude_effort_args()
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

    def _attempt(prompt, resume_id=None):
        """One CLI run. Returns the usual result dict plus three internal keys the retry loop
        consumes: _session (this run's session id, for --resume), _rl (the failure looks like
        the provider's rate/usage limit) and _reset (epoch when that limit lifts, or None)."""
        try:
            # Feed the prompt on stdin, NOT as a positional arg: Linux caps a single argv string at
            # MAX_ARG_STRLEN (128 KB), so a long transcript (e.g. continuing a big chat, or one grown
            # under the Codex mind before switching to Claude) overflows it and execve fails with E2BIG
            # ("Argument list too long"). `claude -p` reads the prompt from stdin when none is given.
            proc = subprocess.Popen(cmd + (["--resume", resume_id] if resume_id else []),
                                    cwd=str(cwd or config.WORKSPACE), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            return {"ok": False, "output": "", "error": f"could not launch claude: {e}", "partial": False,
                    "turns": 0, "cost": 0.0, "_session": resume_id, "_rl": False, "_reset": None}

        # Write the prompt on its own thread, then close stdin: a multi-hundred-KB transcript can exceed
        # the OS pipe buffer, and a single blocking write here would deadlock against claude (which
        # interleaves reading stdin with writing the stdout the reader below drains). Mirrors codex_mind.
        def feed():
            try:
                proc.stdin.write(prompt)
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        threading.Thread(target=feed, daemon=True).start()

        q = queue.Queue()

        def reader():
            try:
                for line in proc.stdout:             # blocks in this thread, never the main loop
                    q.put(line)
            finally:
                q.put(None)                          # EOF sentinel
        threading.Thread(target=reader, daemon=True).start()

        errbuf = []                                  # drained continuously: a chatty stderr (>64KB) would
        def errreader():                             # otherwise fill the OS pipe, block the child's writes,
            try:                                     # stall its stdout, and trip the idle-timeout on a
                for line in proc.stderr:             # perfectly healthy run.
                    errbuf.append(line)
                    if len(errbuf) > 400:            # bounded memory — keep the tail, drop old noise
                        del errbuf[:200]
            except Exception:
                pass
        threading.Thread(target=errreader, daemon=True).start()

        final, is_error, turns, cost, cancelled = "", False, 0, 0.0, False
        session_id, rl_rejected, rl_reset = resume_id, False, None
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
            if now - last_evt > idle_timeout:        # genuinely idle (the clock resets on every event)
                stalled = True
                break
            try:
                line = q.get(timeout=poll)
            except queue.Empty:
                continue                             # loop back to re-check cancel / cap / idle
            last_evt = time.monotonic()
            if line is None:
                break                                # process finished, stream closed
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            session_id = ev.get("session_id") or session_id
            t = ev.get("type")
            if t == "assistant":
                for block in (ev.get("message", {}).get("content") or []):
                    bt = block.get("type")
                    if bt == "text" and block.get("text"):
                        emit({"kind": "text", "text": block["text"]})
                    elif bt == "tool_use":
                        emit({"kind": "tool", "tool": block.get("name", "tool"),
                              "detail": _tool_detail(block.get("input") or {})})
            elif t == "user":                          # tool results come back as a 'user' message
                for block in (ev.get("message", {}).get("content") or []):
                    if block.get("type") == "tool_result":
                        c = block.get("content")
                        if isinstance(c, list):        # content can be a list of text blocks or a string
                            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                        emit({"kind": "tool_result", "text": (c or "").strip()})
            elif t == "result":
                final = ev.get("result") or final
                is_error = bool(ev.get("is_error"))
                turns = ev.get("num_turns") or turns
                cost = ev.get("total_cost_usd") or cost
            elif t == "rate_limit_event":              # the subscription window ran out mid-run
                info = ev.get("rate_limit") if isinstance(ev.get("rate_limit"), dict) else ev
                st = str((info or {}).get("status") or "").lower()
                if st in ("rejected", "blocked", "exceeded", "limit_reached"):
                    rl_rejected = True
                rl_reset = _rl_reset_at(info) or rl_reset
            # system / hook_* → not surfaced

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
            # Claude Code's own "result" event often DOES explain why (e.g. hitting --max-turns on a
            # large multi-file build) — that detail used to be discarded here in favor of a canned
            # phrase, which made an already-hard-to-diagnose failure (a workflow agent node's ONLY
            # trace of what happened) untraceable. Surface it, with turn count for context, and fall
            # back to captured stderr if the CLI's own result text was empty.
            detail = (final or "").strip() or ("".join(errbuf)).strip()[:400]
            err = f"the delegate reported an error after {turns} turn(s)" + (f": {detail[:500]}" if detail else "")
        elif not final:
            try:
                err = ("".join(errbuf)).strip()[:400] or "the delegate returned no output"
            except Exception:
                err = "the delegate returned no output"
        ok = bool(final) and not is_error and not stalled and not capped
        rl = not ok and not cancelled and (rl_rejected or bool(_RL_ERROR.search(err)))
        if rl and rl_reset is None:                    # "…usage limit reached|<epoch>" error format
            m = re.search(r"\|(\d{10,13})\b", err)
            if m:
                v = float(m.group(1))
                rl_reset = v / 1000.0 if v > 1e12 else v
        return {"ok": ok, "output": (final or "").strip(), "error": "" if ok else err,
                "partial": bool(final) and not ok, "turns": turns, "cost": cost,
                "_session": session_id, "_rl": rl, "_reset": rl_reset}

    # Retry loop: a run killed by the provider's rate/usage limit (routine on a subscription —
    # and fatal to a whole night of unattended jobs if we just give up) waits for the window to
    # reset, then RESUMES the same session (--resume) so completed work isn't redone. Bounded:
    # at most _RL_RETRIES waits, each no longer than _RL_WAIT — a reset further out fails fast
    # with the reset time in the error so the caller/scheduler can decide. Read per call so the
    # env can be tuned without a restart (and tests can patch it).
    retries = max(0, int(os.environ.get("OCEANO_DELEGATE_RL_RETRIES", "2")))
    wait_cap = int(os.environ.get("OCEANO_DELEGATE_RL_WAIT", "1800"))
    attempt, turns_total, cost_total, best, sid = 0, 0, 0.0, "", None
    while True:
        if attempt == 0:
            r = _attempt(instructions)
        elif sid:
            r = _attempt("You were interrupted by a rate limit. Continue the task exactly where "
                         "you left off; if it was already complete, restate the final result.",
                         resume_id=sid)
        else:
            r = _attempt(instructions)                 # no session captured → start over
        turns_total += r["turns"]
        cost_total += r["cost"]
        best = r["output"] or best
        sid = r.pop("_session") or sid
        rl, reset = r.pop("_rl"), r.pop("_reset")
        if r["ok"] or not rl or attempt >= retries:
            break
        wait = max(_RL_MIN_WAIT, (reset - time.time() + 10) if reset else 60.0 * (attempt + 1))
        if wait > wait_cap:
            resets = time.strftime("%H:%M", time.localtime(time.time() + wait))
            r["error"] += (f" — the usage window doesn't reset until ~{resets}, beyond the "
                           f"{wait_cap}s wait cap (OCEANO_DELEGATE_RL_WAIT), so not retrying")
            break
        attempt += 1
        emit({"kind": "text", "text": f"⏳ hit the provider's usage/rate limit — waiting "
                                      f"~{max(1, int(wait // 60))}m, then resuming (retry {attempt}/{retries})"})
        end = time.monotonic() + wait
        while time.monotonic() < end:                  # cancel-aware sleep: Stop works mid-wait
            if cancel is not None and cancel.is_set():
                r["error"] = "stopped by the user"
                break
            time.sleep(0.5)
        if cancel is not None and cancel.is_set():
            break
    r["turns"], r["cost"] = turns_total, round(cost_total, 4)
    if not r["ok"] and best and not r["output"]:       # keep partial work from an earlier attempt
        r["output"], r["partial"] = best, True
    return r


def claude_version():
    binary = find_claude()
    if not binary:
        return None
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


# --- Codex CLI provider (delegate + vision) --------------------------------
def _codex_sandbox(tools_spec):
    """Map a Claude-CLI --allowedTools spec onto a Codex sandbox mode — Codex's containment
    is the sandbox, not a per-tool allowlist. Anything that writes (Write/Edit) or runs shell
    (Bash) needs workspace-write; a pure-read task (or none) stays read-only."""
    spec = tools_spec or ""
    if any(t in spec for t in ("Write", "Edit", "Bash")):
        return "workspace-write"
    return "read-only"


# Codex sandboxes its file/shell tools with bubblewrap. On hosts that lock down unprivileged
# user namespaces — a non-setuid bwrap + kernel.apparmor_restrict_unprivileged_userns=1, the
# Ubuntu 24.04+/recent-kernel default — bwrap can't set up ANY namespace, so EVERY Codex sandbox
# mode dies before the command runs ("setting up uid map: Permission denied" / "loopback: Failed
# RTM_NEWADDR"). That breaks Codex's native read/shell tools (the MCP-bridge tools are unaffected).
# Probe once; if bwrap is broken, run Codex un-sandboxed and lean on Oceano's OWN confinement —
# the systemd unit's ProtectHome=read-only + ReadWritePaths, exactly like the Claude mind.
_BWRAP_OK = None


def _bwrap_works():
    global _BWRAP_OK
    if _BWRAP_OK is None:
        bw = shutil.which("bwrap")
        if not bw:
            _BWRAP_OK = True                 # no bwrap → Codex uses its own landlock path; let it try
        else:
            try:
                r = subprocess.run([bw, "--ro-bind", "/", "/", "--unshare-net", "true"],
                                   capture_output=True, timeout=10)
                _BWRAP_OK = (r.returncode == 0)
            except Exception:
                _BWRAP_OK = False
    return _BWRAP_OK


def codex_sandbox_mode(desired="workspace-write"):
    """The Codex `sandbox_mode` to actually use. Honours an explicit OCEANO_CODEX_SANDBOX override
    (read-only / workspace-write / danger-full-access); otherwise returns `desired` when bwrap can
    sandbox here, or falls back to 'danger-full-access' (no nested sandbox — relies on Oceano's
    external systemd confinement) when bwrap is broken on this host."""
    forced = (os.environ.get("OCEANO_CODEX_SANDBOX") or "").strip()
    if forced in ("read-only", "workspace-write", "danger-full-access"):
        return forced
    if _bwrap_works():
        return desired
    if not getattr(codex_sandbox_mode, "_warned", False):
        codex_sandbox_mode._warned = True
        import sys
        print("[codex] bubblewrap can't create a sandbox on this host (restricted unprivileged user "
              "namespaces) — running Codex un-sandboxed under Oceano's own confinement (systemd "
              "ProtectHome/ReadWritePaths). Set OCEANO_CODEX_SANDBOX to override.", file=sys.stderr, flush=True)
    return "danger-full-access"


def to_codex(instructions, cwd=None, tools=DEFAULT_TOOLS, timeout=600, images=None, skills=False):
    """Run one headless Codex task as a CONTAINED worker, mirroring to_claude. `--ignore-user-config`
    means it loads only the user's auth from our CODEX_HOME, NOT the resident mind's MCP-bridge config
    — so a delegate gets Codex's own file/shell tools (confined by the sandbox mapped from `tools`),
    never Oceano's body (memory/mail/ssh). `images` attaches files to the prompt for vision (-i).

    `skills=True` swaps in a SEPARATE CODEX_HOME (codex_mind.SUBAGENT_HOME) whose config.toml wires
    a "skills"-scoped MCP bridge (list_skills/load_skill only — see mindbridge._SCOPES) — never the
    resident mind's full-body one, which lives in a different CODEX_HOME entirely. --ignore-user-config
    is dropped in that case since this dedicated config IS what we want Codex to load.
    Returns {ok, output, error}."""
    import tempfile
    from oceano import codex_mind
    binary = find_codex()
    if not binary:
        return {"ok": False, "output": "",
                "error": "codex CLI not found — install Codex, or set OCEANO_CODEX_BIN"}
    if skills:
        prep = codex_mind.ensure_subagent_home()
        if not prep.get("ok"):
            return {"ok": False, "output": "", "error": prep.get("error") or "could not prepare Codex"}
        home = codex_mind.SUBAGENT_HOME
    else:
        ok, err = codex_mind.ensure_auth()
        if not ok:
            return {"ok": False, "output": "", "error": err}
        home = codex_mind.HOME
    sandbox = codex_sandbox_mode(_codex_sandbox(tools))
    fd, out_path = tempfile.mkstemp(prefix="codex-deleg-", suffix=".txt")
    os.close(fd)
    cmd = [binary, "exec"] + ([] if skills else ["--ignore-user-config"]) + \
          ["--skip-git-repo-check", "--ephemeral",
           "-c", 'approval_policy="never"', "-c", f'sandbox_mode="{sandbox}"',
           "-o", out_path] + _codex_model_args() + _codex_effort_args()
    for img in (images or []):
        cmd += ["-i", str(img)]
    if cwd:
        cmd += ["--cd", str(cwd)]
    # Pass the prompt on stdin, NOT as a positional: `-i <FILE>...` is greedy and would otherwise
    # swallow a trailing prompt argument. Codex reads instructions from stdin when none is given.
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    try:
        r = subprocess.run(cmd, cwd=str(cwd or config.WORKSPACE), env=env, input=instructions,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _unlink_quiet(out_path)
        return {"ok": False, "output": "", "error": f"codex timed out after {timeout}s"}
    except OSError as e:
        _unlink_quiet(out_path)
        return {"ok": False, "output": "", "error": f"could not launch codex: {e}"}
    try:
        out = Path(out_path).read_text().strip()        # the agent's final message (-o)
    except OSError:
        out = ""
    _unlink_quiet(out_path)
    if not out and r.returncode != 0:
        return {"ok": False, "output": "",
                "error": (r.stderr or f"codex exited {r.returncode}").strip()[:400]}
    return {"ok": bool(out), "output": out, "error": "" if out else "codex returned no output"}


def _unlink_quiet(p):
    try:
        os.unlink(p)
    except OSError:
        pass


# --- cloud API provider ----------------------------------------------------
# How a Claude-CLI --allowedTools spec maps onto OUR tool names, so the api provider
# honours the same containment callers ask of the CLI. Grep has no local tool —
# read_file/list_files cover that ground. Unknown CLI names grant nothing.
# code_search rides on Read/Grep (it's pure ripgrep, no side effects — same trust level as
# read_file); run_tests/git ride on Write (verifying/versioning what you just wrote is the
# point of write access, not a bigger ask than the Write/Edit it already sits alongside — git
# itself refuses push/remote ops, see oceano/tools/dev.py). Purely additive to what the CLI
# providers (claude/codex) already do on their own with native Bash — this dict only affects
# the api/local providers, which otherwise had no path to these tools at all.
_API_TOOL_MAP = {
    "Read": ("read_file", "list_files", "code_search"),
    "Glob": ("list_files",),
    "Grep": ("read_file", "list_files", "code_search"),
    "Write": ("write_file", "make_folder", "run_tests", "git"),
    "Edit": ("edit_file",),
    "Bash": ("run_shell", "python_exec"),
}

# Granted on top of the CLI-style map when a caller opts into skill-reuse (skills=True below) —
# NOT part of _API_TOOL_MAP itself, because that map is keyed by write-tier tokens (Read/Write/
# Bash) and skill-reuse is orthogonal to file-access tier: it should reach a contained sub-agent
# even at the read-only default. Mirrors mindbridge._SCOPES["skills"] — list_skills/load_skill
# only, never learn_skill (that's left to the full-body bridge, e.g. an Instructions node).
_SKILLS_TOOLS = ("list_skills", "load_skill")


def _api_only_tools(tools_spec, skills=False):
    """Translate a CLI tools spec into an allowlist of our tool names.
    None → no narrowing (the full enabled surface). `skills=True` additionally grants
    list_skills/load_skill regardless of tier (see _SKILLS_TOOLS)."""
    if tools_spec is None:
        return None
    names = set()
    for t in (x.strip() for x in tools_spec.split(",")):
        if t:
            names.update(_API_TOOL_MAP.get(t, ()))
    if skills:
        names.update(_SKILLS_TOOLS)
    return names


def to_api(instructions, cwd=None, role="default", tools=DEFAULT_TOOLS, timeout=600, on_progress=None,
           exclude=None, model="", base_url="", skills=False):
    """Delegate to the configured cloud model by running it through OUR agent loop — the
    SAME machinery local models use. `tools` (a Claude-CLI-style spec) is translated to
    the equivalent local tools and enforced, and `timeout` puts a wall-clock deadline on
    the loop, so this provider honours the same containment as the CLI. Scoped to `cwd`
    (a throwaway/working folder) when given. on_progress(ev) surfaces its tool calls live.
    `model`/`base_url` override the role config (a workflow agent node pinned to a specific
    registered endpoint + model); empty → the role's configured pair as before. `skills=True`
    additionally grants list_skills/load_skill (see _SKILLS_TOOLS) — never memory.
    Returns {ok, output, error}. (learn=False so the task prompt is never mined into memory.)"""
    cfg = resolve(role)
    base_url, model = (base_url or cfg["base_url"]), (model or cfg["model"])
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
        # to itself in an infinite loop. `exclude` widens that set (agentjobs adds spawn_agent
        # and run_workflow, so a spawned agent can't fan out further).
        ag = Agent(model=model, base_url=base_url, api_key=api_key, learn=False,
                   inject_context=False, exclude_tools=(exclude or {"delegate", "delegate_to_claude"}),
                   only_tools=_api_only_tools(tools, skills=skills), on_event=_on_ev)
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
        role="default", on_progress=None, skills=False):
    """Delegate per the role's effective provider, STREAMING progress via on_progress(ev).
      claude_cli → the Claude Code CLI (its own tools; `tools=` limits --allowedTools),
                   streamed with an idle timeout so long active builds aren't killed;
      api        → the cloud model run through OUR agent loop with OUR tools.
    `cwd` scopes the working folder for both. role='improve' for self-improving jobs.
    `timeout` is the absolute cap (None → the generous default); idle is handled internally.
    `skills=True` additionally grants list_skills/load_skill (reuse Oceano's published skills) —
    never memory. Used by workflow Delegate/Agent-spawn nodes; see mindbridge._SCOPES."""
    if not enabled():
        return {"ok": False, "output": "", "error": "Delegation is turned off (Settings → Delegation)."}
    prov = resolve(role)["provider"]
    if prov == "api":
        return to_api(instructions, cwd=cwd, role=role, tools=tools,
                      timeout=timeout or _DELEGATE_MAX, on_progress=on_progress, skills=skills)
    if prov == "codex_cli":                       # contained Codex worker (no live progress yet → blocking)
        return to_codex(instructions, cwd=cwd, tools=tools, timeout=timeout or _DELEGATE_MAX, skills=skills)
    return to_claude_stream(instructions, cwd=cwd, tools=tools, max_total=timeout,
                            max_turns=max_turns, on_progress=on_progress, skills=skills)


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
    if cfg["provider"] == "codex_cli":
        if not find_codex():
            return {"ok": False, "output": "",
                    "error": "codex CLI not found — install Codex or configure a cloud vision model in Settings → Delegation"}
        # Codex is multimodal: attach the image to the prompt directly (-i), no file-reading turns.
        return to_codex(q, cwd=config.WORKSPACE, tools="Read", timeout=300, images=[image_path])
    if not find_claude():
        return {"ok": False, "output": "",
                "error": "claude CLI not found — install Claude Code or configure a cloud vision model in Settings → Delegation"}
    # Claude Code can open and 'see' image files via its Read tool (needs a few turns:
    # read the file, then answer — keep a little headroom).
    return to_claude(f"Open and look at the image file `{image_path}`. {q}",
                     cwd=config.WORKSPACE, tools="Read", timeout=300, max_turns=10)


# --- readiness (Settings → Delegation) -------------------------------------
def status_all():
    """Claude Code + Codex readiness (shared) plus per-role provider + readiness, for the UI.
    Auth is only proven by probe(role)."""
    binary = find_claude()
    cbin = find_codex()
    claude = {"installed": bool(binary), "path": binary or "",
              "version": claude_version() if binary else None}
    codex = {"installed": bool(cbin), "path": cbin or "",
             "version": codex_version() if cbin else None}

    def role_status(role):
        raw, eff = get_config(role), resolve(role)
        inherits = role != "default" and raw["provider"] == "inherit"
        if eff["provider"] == "api":
            ready = bool(eff["base_url"] and eff["model"])
        elif eff["provider"] == "codex_cli":
            ready = bool(cbin)
        else:                                          # claude_cli
            ready = bool(binary)
        return {"provider": raw["provider"], "base_url": raw["base_url"], "model": raw["model"],
                "effective_provider": eff["provider"], "inherits": inherits, "ready": ready}

    return {"claude": claude, "codex": codex, "default": role_status("default"),
            "improve": role_status("improve"), "vision": role_status("vision")}


def probe(role="default"):
    """Actually test a role's effective provider with a tiny live request. Returns {ok,
    provider, detail}. For claude_cli this proves authentication (a logged-out CLI fails)."""
    cfg = resolve(role)
    if cfg["provider"] == "api":
        r = _api_ping(role)
        return {"ok": r["ok"], "provider": "api", "detail": r["detail"]}
    if cfg["provider"] == "codex_cli":
        if not find_codex():
            return {"ok": False, "provider": "codex_cli",
                    "detail": "codex CLI not found — install Codex, or set OCEANO_CODEX_BIN to its path, "
                              "then restart Oceano."}
        r = to_codex("Reply with the single word: READY", tools="", timeout=60)
        if r["ok"] and "ready" in (r["output"] or "").lower():
            return {"ok": True, "provider": "codex_cli", "detail": codex_version() or "authenticated"}
        return {"ok": False, "provider": "codex_cli",
                "detail": (r["error"] or r["output"] or "not authenticated").strip()[:300]}
    if not find_claude():
        return {"ok": False, "provider": "claude_cli",
                "detail": "claude CLI not found — install Claude Code (npm i -g @anthropic-ai/claude-code), "
                          "or set OCEANO_CLAUDE_BIN to its path, then restart Oceano."}
    r = to_claude("Reply with the single word: READY", tools="", timeout=60, max_turns=1)
    if r["ok"] and "ready" in (r["output"] or "").lower():
        return {"ok": True, "provider": "claude_cli", "detail": claude_version() or "authenticated"}
    return {"ok": False, "provider": "claude_cli",
            "detail": (r["error"] or r["output"] or "not authenticated").strip()[:300]}
