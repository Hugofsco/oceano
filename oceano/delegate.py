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
  • 'default' — the agent's delegate_to_claude tool (interactive "use Claude / delegate").
  • 'improve' — the SELF-IMPROVING jobs: skills review, eval judging, memory maintenance.
'improve' may be set to 'inherit', meaning "use whatever 'default' is set to".

Containment (CLI): the subprocess runs with cwd inside the workspace (or a caller-chosen
folder) and ONLY the tools listed in `tools` are allowed — no Bash unless a caller
explicitly grants it. Claude Code itself blocks edits outside its working directory.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import config

DEFAULT_TOOLS = "Read,Glob,Grep,Write,Edit"
_CONFIG_PATH = config.WORKSPACE.parent / "data" / "delegation.json"
ROLES = ("default", "improve")


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
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(allcfg))
    except OSError:
        pass
    return allcfg[role]


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


def to_claude(instructions, cwd=None, tools=DEFAULT_TOOLS, timeout=600, max_turns=30):
    """Run one headless Claude Code task. Returns {ok, output, error}."""
    binary = find_claude()
    if not binary:
        return {"ok": False, "output": "",
                "error": "claude CLI not found — install Claude Code, or set OCEANO_CLAUDE_BIN"}
    cmd = [binary, "-p", instructions, "--output-format", "text",
           "--max-turns", str(int(max_turns))]
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
def to_api(instructions, cwd=None, role="default"):
    """Delegate to the configured cloud model by running it through OUR agent loop — the
    SAME machinery local models use, so it has the full (enabled) tool surface: it can read,
    write, run shell, browse, etc. Scoped to `cwd` (a throwaway/working folder) when given.
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
    try:
        from oceano.agent import Agent
        from oceano import tools as _tools
        # learn=False + inject_context=False: a delegate gets a self-contained task, not the
        # user's persona/memories; exclude delegate_to_claude so it can't delegate to itself.
        ag = Agent(model=model, base_url=base_url, api_key=api_key, learn=False,
                   inject_context=False, exclude_tools={"delegate_to_claude"})
        ctx = _tools.background_workspace(cwd) if cwd else _tools.background()
        with ctx:
            out = ag.run(instructions)
        return {"ok": True, "output": (out or "").strip(), "error": ""}
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
def run(instructions, cwd=None, tools=DEFAULT_TOOLS, timeout=600, max_turns=30, role="default"):
    """Delegate per the role's effective provider. Both can read AND act on files:
      claude_cli → the Claude Code CLI (its own tools; `tools=` limits --allowedTools);
      api        → the cloud model run through OUR agent loop with OUR tools.
    `cwd` scopes the working folder for both. role='improve' for self-improving jobs."""
    if resolve(role)["provider"] == "api":
        return to_api(instructions, cwd=cwd, role=role)
    return to_claude(instructions, cwd=cwd, tools=tools, timeout=timeout, max_turns=max_turns)


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

    return {"claude": claude, "default": role_status("default"), "improve": role_status("improve")}


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
