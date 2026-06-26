"""Headless Codex resident-mind runner.

Uses `codex exec --json` (and `resume`) with a dedicated CODEX_HOME so Oceano can keep
its own MCP bridge config without inheriting the user's broader Codex setup.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import config
from oceano import atomicio, mindbridge

_HOME = config.WORKSPACE.parent / "data" / "codex-home"
_CONFIG = _HOME / "config.toml"
_SESSIONS = _HOME / "mind-sessions.json"


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


def _load_sessions():
    try:
        d = json.loads(_SESSIONS.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sessions(d):
    try:
        _SESSIONS.parent.mkdir(parents=True, exist_ok=True)
        atomicio.write_text(_SESSIONS, json.dumps(d, indent=2))
    except OSError:
        pass


def session_for(key):
    return (_load_sessions().get(key) or "").strip() if key else ""


def remember_session(key, sid):
    if not (key and sid):
        return
    d = _load_sessions()
    d[key] = sid
    _save_sessions(d)


def clear_session(key):
    if not key:
        return
    d = _load_sessions()
    if key in d:
        d.pop(key, None)
        _save_sessions(d)


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


def run_stream(prompt, session_key="", cwd=None, cancel=None, model="", on_event=None):
    from oceano import delegate
    binary = delegate.find_codex()
    if not binary:
        return {"ok": False, "output": "", "error": "codex CLI not found — install Codex or set OCEANO_CODEX_BIN", "session_id": ""}
    prep = ensure_home()
    if not prep.get("ok"):
        return {"ok": False, "output": "", "error": prep.get("error") or "could not prepare Codex", "session_id": ""}

    sid = session_for(session_key)
    cmd = [binary, "exec"]
    if model:
        cmd += ["--model", str(model)]
    cmd += ["--json", "--sandbox", "workspace-write", "-c", 'approval_policy="never"']
    if not session_key:
        cmd.append("--ephemeral")
    if cwd:
        cmd += ["--cd", str(cwd)]
    if sid:
        cmd += ["resume", sid, prompt]
    else:
        cmd.append(prompt)

    env = dict(os.environ)
    env["CODEX_HOME"] = str(_HOME)

    def emit(ev):
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd or config.WORKSPACE), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not launch codex: {e}", "session_id": sid}

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
    cancelled = False
    while True:
        if cancel is not None and cancel.is_set():
            cancelled = True
            try:
                proc.kill()
            except Exception:
                pass
            break
        line = q.get()
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
        if typ == "thread.started":
            sid = ev.get("thread_id") or sid
            remember_session(session_key, sid)
        elif typ == "item.started":
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

    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    err = (proc.stderr.read() or "").strip()[:1000] if proc.stderr else ""
    answer = ''.join(parts).strip()
    ok = bool(answer) and not cancelled and proc.returncode == 0
    if ok and err.startswith("Reading additional input from stdin"):
        err = ""
    if not ok and not err and cancelled:
        err = "stopped by the user"
    if not ok and not err and not answer:
        err = f"codex exited {proc.returncode}"
    return {"ok": ok, "output": answer, "error": err, "session_id": sid}
