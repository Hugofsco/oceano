"""Headless Codex resident-mind runner.

Uses `codex exec --json` (and `resume`) with a dedicated CODEX_HOME so Oceano can keep
its own MCP bridge config without inheriting the user's broader Codex setup.
"""
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import config
from oceano import atomicio, mindbridge

_HOME = config.WORKSPACE.parent / "data" / "codex-home"
_CONFIG = _HOME / "config.toml"


def _j(s):
    return json.dumps(str(s))


def _auth_source_home():
    src = os.environ.get("OCEANO_CODEX_AUTH_HOME", "").strip()
    return Path(src).expanduser() if src else (Path.home() / ".codex")


def _sync_auth():
    src_home = _auth_source_home()
    src = src_home / "auth.json"
    if not src.is_file():
        return False, f"codex auth not found at {src} — run `codex login` on this host first"
    _HOME.mkdir(parents=True, exist_ok=True)
    dst = _HOME / "auth.json"
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
        'sandbox_mode = "workspace-write"',
        'web_search = "disabled"',
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
# get the auth from here but NOT the mind's body tools.
HOME = _HOME


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
    return None


def _tool_result(item):
    if not isinstance(item, dict):
        return ""
    err = item.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:2000]
    if isinstance(err, str) and err.strip():
        return err.strip()[:2000]
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
                return txt[:2000]
        for k in ("text", "summary", "result"):
            v = nested.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:2000]
    for k in ("output", "text", "summary", "result"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:2000]
    return ""


def run_stream(prompt, cwd=None, cancel=None, model="", on_event=None):
    """Run one stateless Codex turn. The caller passes the WHOLE conversation in `prompt` (Oceano's
    self.messages is the single source of truth, mirroring the Claude mind), so every turn is a fresh
    ephemeral `codex exec` — no server-side thread to resume, drift, or lose."""
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
    sandbox = delegate.codex_sandbox_mode("workspace-write")    # falls back off bwrap if it can't sandbox here
    cmd += ["--json", "--sandbox", sandbox, "-c", 'approval_policy="never"', "--ephemeral"]
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
                pending[item.get("id") or str(time.time())] = call[0]
                emit({"type": "tool_call", "name": call[0], "args": call[1]})
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
                name = pending.pop(iid, "") if iid else ""
                if not name:
                    call = _tool_call(item)
                    name = call[0] if call else ""
                if name:
                    emit({"type": "tool_result", "name": name, "result": _tool_result(item)})
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
    err = "" if killed else ((proc.stderr.read() or "").strip()[:1000] if proc.stderr else "")
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
