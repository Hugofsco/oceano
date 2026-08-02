"""Headless Codex resident-mind runner.

Uses `codex exec --json` (and `resume`) with a dedicated CODEX_HOME so Oceano can keep
its own MCP bridge config without inheriting the user's broader Codex setup.
"""
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import config
from oceano import atomicio, mindbridge

_HOME = config.WORKSPACE.parent / "data" / "codex-home"
_CONFIG = _HOME / "config.toml"
# A SEPARATE CODEX_HOME for contained delegate/agent-node runs that opt into skill-reuse (see
# ensure_subagent_home below) — never codex_mind.HOME, the resident mind's, so its full-body
# config.toml (written by ensure_home) can never end up loaded for a contained sub-agent.
_SUBAGENT_HOME = config.WORKSPACE.parent / "data" / "codex-home-subagent"
_SUBAGENT_CONFIG = _SUBAGENT_HOME / "config.toml"
# One-off homes for CONCURRENT skill-enabled runs (see new_subagent_home) live beside it, e.g.
# data/codex-home-subagent-<uuid>. Swept for staleness on every new_subagent_home() call so a
# crash mid-run (the only way discard_subagent_home's cleanup gets skipped) doesn't leak forever.
_SUBAGENT_HOME_GLOB = "codex-home-subagent-*"
_SUBAGENT_HOME_STALE_S = 3600


def _j(s):
    return json.dumps(str(s))


_DISABLED_RESIDENT_FEATURES = (
    "multi_agent", "apps", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "computer_use", "goals", "image_generation",
    "in_app_browser", "plugins", "plugin_sharing", "remote_plugin", "skill_search",
    "shell_tool", "unified_exec",
)


def _isolation_lines(home):
    lines = [f"{name} = false" for name in _DISABLED_RESIDENT_FEATURES]
    lines += ["", "[agents]", "enabled = false", ""]
    for skill in sorted(Path(home).glob("skills/**/SKILL.md")):
        lines += ["[[skills.config]]", f"path = {_j(skill)}", "enabled = false", ""]
    return lines


def _auth_source_home():
    src = os.environ.get("OCEANO_CODEX_AUTH_HOME", "").strip()
    return Path(src).expanduser() if src else (Path.home() / ".codex")


def _sync_auth(dst_home=None):
    dst_home = dst_home or _HOME
    src_home = _auth_source_home()
    src = src_home / "auth.json"
    if not src.is_file():
        return False, f"codex auth not found at {src} — run `codex login` on this host first"
    dst_home.mkdir(parents=True, exist_ok=True)
    dst = dst_home / "auth.json"
    try:
        if (not dst.exists()) or src.stat().st_mtime > dst.stat().st_mtime or src.stat().st_size != dst.stat().st_size:
            shutil.copy2(src, dst)
    except OSError as e:
        return False, f"could not prepare Codex auth: {e}"
    return True, ""


def _write_config():
    import sys
    lines = [
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        '',
        '[features]',
        'hooks = true',
    ] + _isolation_lines(_HOME) + [
        '[[hooks.PreToolUse]]',
        'matcher = "^(Bash|shell|exec_command|apply_patch|Edit|Write|write_file|edit_file|make_folder|run_shell|python_exec|run_tests|git|spawn_agent|send_input|resume_agent|wait_agent|close_agent)$"',
        '',
        '[[hooks.PreToolUse.hooks]]',
        'type = "command"',
        f'command = {_j(shlex.join([str(sys.executable), str(Path(__file__).with_name("codex_guard.py"))]))}',
        'timeout = 10',
        '',
        '[mcp_servers.oceano]',
        f'command = {_j(sys.executable)}',
        'args = ["-m", "oceano.mcp_bridge_server"]',
        'enabled = true',
        'required = true',
        'startup_timeout_sec = 15',
        'tool_timeout_sec = 600',
        'default_tools_approval_mode = "approve"',
        '',
        '[mcp_servers.oceano.env]',
        f'OCEANO_MCP_URL = {_j(mindbridge.daemon_url())}',
        f'OCEANO_MCP_TOKEN = {_j(mindbridge.token())}',
        f'PYTHONPATH = {_j(str(config.WORKSPACE.parent))}',
        '',
    ]
    atomicio.write_text(_CONFIG, "\n".join(lines))


def ensure_home():
    ok, err = _sync_auth()
    if not ok:
        return {"ok": False, "error": err}
    try:
        _HOME.mkdir(parents=True, exist_ok=True)
        _write_config()
    except OSError as e:
        return {"ok": False, "error": f"could not prepare Codex home: {e}"}
    return {"ok": True, "home": str(_HOME)}


# The CODEX_HOME our headless callers point at. The mind uses ensure_home() (auth + the MCP-bridge
# config.toml); contained delegates use ensure_auth() + `codex exec --ignore-user-config`, so they
# get the auth from here but NOT the mind's body tools. A delegate/agent-node run that opts into
# skill-reuse uses SUBAGENT_HOME + ensure_subagent_home() instead (its own "skills"-scoped bridge).
HOME = _HOME
SUBAGENT_HOME = _SUBAGENT_HOME


def ensure_auth():
    """Sync the user's Codex auth into our CODEX_HOME and return (ok, error). For headless callers
    (delegate/vision) that run with --ignore-user-config: they need the auth but not the mind's
    MCP config, so they skip _write_config()."""
    ok, err = _sync_auth()
    if ok:
        try:
            _HOME.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"could not prepare Codex home: {e}"
    return ok, err


def _write_subagent_config(dst):
    """Like _write_config, but the bridge is scoped to "skills" (list_skills/load_skill only —
    see mindbridge._SCOPES) and the turn is always marked background (a contained sub-agent is
    never attended). Written to `dst` (never _CONFIG, the resident mind's)."""
    import sys
    lines = [
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        'web_search = "disabled"',
        '',
        '[features]',
        'hooks = true',
    ] + _isolation_lines(Path(dst).parent) + [
        '[mcp_servers.oceano]',
        f'command = {_j(sys.executable)}',
        'args = ["-m", "oceano.mcp_bridge_server"]',
        'enabled = true',
        'required = true',
        'startup_timeout_sec = 15',
        'tool_timeout_sec = 600',
        'default_tools_approval_mode = "approve"',
        '',
        '[mcp_servers.oceano.env]',
        f'OCEANO_MCP_URL = {_j(mindbridge.daemon_url())}',
        f'OCEANO_MCP_TOKEN = {_j(mindbridge.token())}',
        f'PYTHONPATH = {_j(str(config.WORKSPACE.parent))}',
        'OCEANO_MCP_SCOPE = "skills"',
        'OCEANO_MCP_BACKGROUND = "1"',
        '',
    ]
    atomicio.write_text(dst, "\n".join(lines))


def ensure_subagent_home(home=None):
    """Auth + a "skills"-scoped MCP bridge config for a CONTAINED delegate/agent-node Codex run
    that opts into skill-reuse (list_skills/load_skill only — never memory/mail/ssh/the rest of
    the body). `home` defaults to the shared _SUBAGENT_HOME; pass a private path from
    new_subagent_home() to isolate ONE concurrent run instead (see there for why that matters).
    Kept separate from both the resident mind's (ensure_home) and the plain contained delegate's
    (ensure_auth), so this scoped bridge can never be confused with either."""
    home = Path(home) if home else _SUBAGENT_HOME
    ok, err = _sync_auth(home)
    if not ok:
        return {"ok": False, "error": err}
    try:
        home.mkdir(parents=True, exist_ok=True)
        _write_subagent_config(home / "config.toml")
    except OSError as e:
        return {"ok": False, "error": f"could not prepare Codex home: {e}"}
    return {"ok": True, "home": str(home)}


def _sweep_stale_subagent_homes():
    """Best-effort cleanup of one-off homes a crashed process never got to discard_subagent_home()
    — cheap (a glob + mtime check), bounded (there are never many at once: MAX_AGENTS caps live
    concurrency), and safe to skip on any error since it's purely disk hygiene."""
    try:
        cutoff = time.time() - _SUBAGENT_HOME_STALE_S
        for p in _SUBAGENT_HOME.parent.glob(_SUBAGENT_HOME_GLOB):
            try:
                if p.is_dir() and p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def new_subagent_home():
    """A fresh, private CODEX_HOME for ONE skill-enabled contained Codex run — never shared with
    a concurrently-running one. `codex exec` writes session/rollout state under CODEX_HOME, and
    two processes pointed at the same directory at the same time corrupt each other's state (this
    is why parallel orchestrate-node agent spawns used to fail almost immediately, right after
    printing their startup banner, with no usable error). Caller must discard_subagent_home() it
    once that process has exited, success or not."""
    _sweep_stale_subagent_homes()
    return _SUBAGENT_HOME.parent / f"codex-home-subagent-{uuid.uuid4().hex[:12]}"


def discard_subagent_home(home):
    """Remove a one-off home from new_subagent_home() now that its codex process has exited."""
    try:
        shutil.rmtree(home, ignore_errors=True)
    except OSError:
        pass


def _agent_text(item):
    if not isinstance(item, dict):
        return ""
    txt = item.get("text")
    if txt:
        return str(txt)
    msg = item.get("message")
    if isinstance(msg, dict):
        txt = msg.get("text") or msg.get("content")
        if isinstance(txt, str):
            return txt
    delta = item.get("delta")
    if isinstance(delta, str):
        return delta
    return ""


def _tool_call(item):
    if not isinstance(item, dict):
        return None
    t = item.get("type") or ""
    if t == "command_execution":
        return (item.get("command") and "shell", str(item.get("command") or ""))
    if t in ("mcp_tool_call", "mcp_tool_use"):
        name = item.get("tool_name") or item.get("tool") or item.get("name") or "tool"
        server = item.get("server_name") or item.get("server") or ""
        detail = item.get("arguments") or item.get("input") or ""
        if not isinstance(detail, str):
            try:
                detail = json.dumps(detail, ensure_ascii=False)
            except Exception:
                detail = str(detail)
        return (str(name), detail[:400])
    if t == "web_search":
        return ("web_search", str(item.get("query") or ""))
    if t == "collab_tool_call":
        name = item.get("tool") or item.get("tool_name") or item.get("name") or "collab_tool_call"
        detail = item.get("arguments") or item.get("input") or item.get("prompt") or ""
        if not isinstance(detail, str):
            try:
                detail = json.dumps(detail, ensure_ascii=False)
            except Exception:
                detail = str(detail)
        return (str(name), detail[:400])
    if t in ("dynamic_tool_call", "image_generation", "computer_use"):
        name = item.get("tool") or item.get("tool_name") or item.get("name") or t
        detail = item.get("arguments") or item.get("input") or ""
        return (str(name), str(detail)[:400])
    return None


def _bounded_result_text(value, limit=2000):
    text = str(value or "").strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("protocol") == "oceano.tool-result.v1":
        return text
    return text[:limit]


def _tool_result(item):
    if not isinstance(item, dict):
        return ""
    err = item.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return _bounded_result_text(msg)
    if isinstance(err, str) and err.strip():
        return _bounded_result_text(err)
    # Shell command (item.type == "command_execution"): codex puts the combined stdout/stderr in
    # `aggregated_output` and the status in `exit_code` — NOT in output/text/result — so the old
    # lookups below missed it entirely and shell chips showed a BLANK result. Keep the tail (a
    # command's errors/results print last), and prefix a non-zero exit so failures read clearly.
    agg = item.get("aggregated_output")
    if isinstance(agg, str) and agg.strip():
        code = item.get("exit_code")
        body = agg.strip()[-2000:]
        return f"(exit {code})\n{body}" if code not in (None, 0) else body
    nested = item.get("result")
    if isinstance(nested, dict):
        content = nested.get("content")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"].strip())
            txt = "\n".join(t for t in texts if t)
            if txt:
                return _bounded_result_text(txt)
        for k in ("text", "summary", "result"):
            v = nested.get(k)
            if isinstance(v, str) and v.strip():
                return _bounded_result_text(v)
    for k in ("output", "text", "summary", "result"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return _bounded_result_text(v)
    # A command that ran but printed nothing: surface its exit status instead of a blank chip.
    if "exit_code" in item:
        code = item.get("exit_code")
        return "(no output)" if code in (None, 0) else f"(exit {code}, no output)"
    return ""


def run_stream(prompt, cwd=None, cancel=None, model="", on_event=None, session=None,
               background=False, catalog_id=None, client="web"):
    """Run one stateless Codex turn. The caller passes the WHOLE conversation in `prompt` (Oceano's
    self.messages is the single source of truth, mirroring the Claude mind), so every turn is a fresh
    ephemeral `codex exec` — no server-side thread to resume, drift, or lose. `session` is the chat
    this turn drives and `background` marks an unattended turn (bridged tools then run on the
    background channel) — both ride to the MCP bridge per-turn via -c env overrides, never a global."""
    from oceano import delegate
    binary = delegate.find_codex()
    if not binary:
        return {"ok": False, "output": "", "error": "codex CLI not found — install Codex or set OCEANO_CODEX_BIN"}
    prep = ensure_home()
    if not prep.get("ok"):
        return {"ok": False, "output": "", "error": prep.get("error") or "could not prepare Codex"}

    cmd = [binary, "exec"]
    if model:
        cmd += ["--model", str(model)]
    # Resident mutation paths are denied by PreToolUse and run through MCP. Read-only is
    # defense in depth; codex_sandbox_mode retains its compatibility fallback on hosts where
    # nested Linux sandboxing is unavailable, while the pre-execution hook still applies.
    sandbox = delegate.codex_sandbox_mode("read-only")
    cmd += delegate._codex_effort_args()                        # honour the configured reasoning effort
    cmd += ["--json", "--sandbox", sandbox, "--skip-git-repo-check",
            "-c", 'approval_policy="never"', "--ephemeral"]
    if session:
        # Per-turn config override (merges one leaf into the shared config.toml's env table, so
        # concurrent Codex turns for different chats never share a sid): the MCP bridge subprocess
        # gets OCEANO_MCP_SESSION in its env and forwards it as X-Oceano-Session on each tool call.
        cmd += ["-c", f'mcp_servers.oceano.env.OCEANO_MCP_SESSION="{session}"']
    if background:
        # Same per-turn mechanism for the channel: forwarded as X-Oceano-Background, so an
        # unattended turn's bridged tools are gated off the live browser/UI without any global state.
        cmd += ["-c", 'mcp_servers.oceano.env.OCEANO_MCP_BACKGROUND="1"']
    if catalog_id:
        cmd += ["-c", f'mcp_servers.oceano.env.OCEANO_MCP_CATALOG="{catalog_id}"']
    if client and client != "web":
        cmd += ["-c", f'mcp_servers.oceano.env.OCEANO_MCP_CLIENT="{client}"']
    if cwd:
        cmd += ["--cd", str(cwd)]
    # Feed the WHOLE conversation on stdin, NOT as a positional argument: Linux caps a single argv
    # string at MAX_ARG_STRLEN (128 KB), so once the chat grows past that, execve fails with E2BIG
    # ("Argument list too long") and the mind can't launch at all. Codex reads instructions from
    # stdin when no prompt argument is given — the same pattern delegate.to_codex already uses.

    env = dict(os.environ)
    env["CODEX_HOME"] = str(_HOME)

    def emit(ev):
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    try:
        # Own session/process group so a stall/cancel can take down the WHOLE tree (codex + the MCP
        # bridge + any shells it spawned), not just the parent — otherwise a lingering grandchild
        # keeps the stdio pipes open and a teardown read would block.
        proc = subprocess.Popen(cmd, cwd=str(cwd or config.WORKSPACE), env=env,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                start_new_session=True)
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not launch codex: {e}"}

    # Write the prompt on its own thread and close stdin: a multi-hundred-KB transcript can exceed the
    # OS pipe buffer, and a single blocking write here would deadlock against codex (which interleaves
    # reading stdin with writing the stdout we drain below). A daemon thread keeps both pipes flowing.
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
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    errbuf = []                                  # drain stderr continuously so a chatty stderr can't fill
    def errreader():                             # the pipe, block codex's writes, and stall its stdout
        try:
            for line in proc.stderr:
                errbuf.append(line)
                if len(errbuf) > 400:            # bounded — keep the tail
                    del errbuf[:200]
        except Exception:
            pass

    threading.Thread(target=errreader, daemon=True).start()

    pending = {}
    parts = []
    cancelled = stalled = capped = False
    # Stall guards, mirroring the Claude mind (delegate.to_claude_stream): an IDLE timeout that
    # resets on every event (a busy run is never killed) plus an absolute wall-clock cap. Codex's
    # own tool_timeout_sec only bounds a single tool call, not a wedged/looping turn. Poll the queue
    # so a user Stop is honoured within 0.5s even when no output is flowing (a bare q.get() would
    # block until the next line, which may never come on a stall).
    idle_timeout = delegate._DELEGATE_IDLE
    max_total = delegate._DELEGATE_MAX
    started = last_evt = time.monotonic()
    poll = 0.5 if cancel is not None else idle_timeout
    while True:
        now = time.monotonic()
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if now - started > max_total:
            capped = True
            break
        if now - last_evt > idle_timeout:
            stalled = True
            break
        try:
            line = q.get(timeout=poll)
        except queue.Empty:
            continue                                 # re-check cancel / cap / idle
        last_evt = time.monotonic()
        if line is None:
            break
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        typ = ev.get("type") or ""
        if typ == "item.started":
            item = ev.get("item") or {}
            call = _tool_call(item)
            if call:
                source = ("mcp" if (item.get("type") or "") in
                          ("mcp_tool_call", "mcp_tool_use") else "native")
                iid = item.get("id") or str(time.time())
                pending[iid] = {"name": call[0], "source": source}
                # `id` rides through to the UI so a parallel batch's results land on their own
                # cards instead of all overwriting the most recently opened one.
                emit({"type": "tool_call", "name": call[0], "args": call[1],
                      "source": source, "id": iid})
        elif typ == "item.updated":
            item = ev.get("item") or {}
            if (item.get("type") or "") == "agent_message":
                txt = _agent_text(item)
                if txt:
                    parts.append(txt)
                    emit({"type": "token", "text": txt})
        elif typ == "item.completed":
            item = ev.get("item") or {}
            itype = item.get("type") or ""
            if itype == "agent_message":
                txt = _agent_text(item)
                if txt and (not parts or txt != ''.join(parts)):
                    parts = [txt]
                    emit({"type": "token", "text": txt})
            else:
                iid = item.get("id")
                pending_call = pending.pop(iid, None) if iid else None
                if pending_call:
                    name = pending_call["name"]
                    source = pending_call["source"]
                else:
                    call = _tool_call(item)
                    name = call[0] if call else ""
                    source = ("mcp" if itype in ("mcp_tool_call", "mcp_tool_use")
                              else "native")
                if name:
                    emit({"type": "tool_result", "name": name,
                          "result": _tool_result(item), "source": source, "id": iid})
        elif typ == "turn.failed":
            break

    killed = cancelled or stalled or capped
    if killed:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # whole tree, not just the parent
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    # Read stderr only on a NATURAL exit. After a kill, reading the pipe can block on a grandchild
    # that briefly outlives the group; we already synthesize a definitive error below, so skip it.
    err = "" if killed else ("".join(errbuf)).strip()[:1000]
    answer = ''.join(parts).strip()
    ok = bool(answer) and not cancelled and not stalled and not capped and proc.returncode == 0
    if ok and err.startswith("Reading additional input from stdin"):
        err = ""
    if not ok and not err:
        if cancelled:
            err = "stopped by the user"
        elif stalled:
            err = f"codex produced no output for {idle_timeout}s and was stopped (looked stalled)"
        elif capped:
            err = f"codex hit the {max_total}s time cap and was stopped"
        elif not answer:
            err = f"codex exited {proc.returncode}"
    return {"ok": ok, "output": answer, "error": err}
