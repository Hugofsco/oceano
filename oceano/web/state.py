"""Shared state + helpers for the Oceano web app (FastAPI).

Everything here is used by more than one router module (or by server.py's
lifespan/middleware): the endpoints store (web.json) with its auth/TOTP
material, the session-cookie helpers, the per-session Agent registry and its
locks, the workspace path fence, and the spawn_job completion hook. Routers
import from here — never from oceano.web.server — so nothing touches the
partially-initialized app module at import time. server.py re-exports the
externally-used names (endpoint_key, list_models, load, _TOOL_CATEGORY, …) so
`oceano.web.server.X` keeps resolving for the rest of the codebase.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

import requests
from fastapi import HTTPException

import config
from oceano import atomicio, chats, embeddings
from oceano.agent import Agent
from oceano.web import telegram_runtime


STATIC = Path(__file__).parent / "static"
STORE = config.WORKSPACE.parent / "data" / "web.json"

# Pre-built OpenAI-compatible endpoints. `console` is where the user gets an API key —
# the UI links to it when a provider needs a key. base_url must end where the OpenAI SDK
# expects (…/v1 for most), since model listing hits base_url + "/models".
PROVIDERS = [
    {"name": "Local (llama.cpp)", "base_url": "http://127.0.0.1:8081/v1", "needs_key": False, "console": ""},
    {"name": "OpenAI",        "base_url": "https://api.openai.com/v1",        "needs_key": True,  "console": "https://platform.openai.com/api-keys"},
    {"name": "xAI (Grok)",    "base_url": "https://api.x.ai/v1",              "needs_key": True,  "console": "https://console.x.ai"},
    {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "needs_key": True, "console": "https://aistudio.google.com/apikey"},
    {"name": "OpenRouter",    "base_url": "https://openrouter.ai/api/v1",     "needs_key": True,  "console": "https://openrouter.ai/keys"},
    {"name": "Groq",          "base_url": "https://api.groq.com/openai/v1",   "needs_key": True,  "console": "https://console.groq.com/keys"},
    {"name": "DeepSeek",      "base_url": "https://api.deepseek.com/v1",      "needs_key": True,  "console": "https://platform.deepseek.com/api_keys"},
    {"name": "Mistral",       "base_url": "https://api.mistral.ai/v1",        "needs_key": True,  "console": "https://console.mistral.ai/api-keys"},
    {"name": "Together",      "base_url": "https://api.together.xyz/v1",      "needs_key": True,  "console": "https://api.together.ai/settings/api-keys"},
    {"name": "Fireworks",     "base_url": "https://api.fireworks.ai/inference/v1", "needs_key": True, "console": "https://fireworks.ai/account/api-keys"},
    {"name": "Cerebras",      "base_url": "https://api.cerebras.ai/v1",       "needs_key": True,  "console": "https://cloud.cerebras.ai"},
    {"name": "Perplexity",    "base_url": "https://api.perplexity.ai",        "needs_key": True,  "console": "https://www.perplexity.ai/settings/api"},
    {"name": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1",       "needs_key": False, "console": ""},
]


def _telegram_seed():
    """Default Telegram block, seeded from oceano.env so existing setups keep working."""
    return {"enabled": bool(config.TELEGRAM_TOKEN),
            "token": config.TELEGRAM_TOKEN,
            "allowed": sorted(config.TELEGRAM_ALLOWED)}


def _notify_seed():
    """Default notification config, seeded from the legacy OCEANO_NTFY_* env vars."""
    return {"ntfy_url": os.environ.get("OCEANO_NTFY_URL", "https://ntfy.sh"),
            "ntfy_topic": os.environ.get("OCEANO_NTFY_TOPIC", ""),
            "telegram": True}


def _hash_pw(password, salt):
    """PBKDF2-SHA256 — stdlib only, no bcrypt/passlib dependency."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


# ---------------- optional TOTP 2FA (RFC 6238, stdlib) ----------------
# Standard authenticator-app TOTP: SHA1 · 6 digits · 30s (max app compatibility). The secret lives
# in data/web.json alongside the password hash (gitignored, chmod 600, atomic-written). 2FA is OFF
# unless the user enables it in Settings → Account.
import struct  # noqa: E402  (kept local to the auth block)


def _totp_secret():
    """A fresh base32 TOTP secret (20 random bytes), the form authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret, counter):
    """The 6-digit code for a given 30s time-step (RFC 6238 / HOTP over SHA1)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _totp_verify(secret, code, window=1, now=None):
    """Return the matched time-step if `code` is valid within ±window steps, else None. The step is
    used for replay protection (a code can't be reused once its step is recorded)."""
    code = (code or "").strip().replace(" ", "")
    if not (secret and code.isdigit() and len(code) == 6):
        return None
    step = int((now if now is not None else time.time()) // 30)
    for w in range(-window, window + 1):
        if hmac.compare_digest(_totp_at(secret, step + w), code):
            return step + w
    return None


def _totp_uri(secret, account, issuer="Oceano"):
    """The otpauth:// URI an authenticator app reads from the QR code."""
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer)}:{quote(account or 'user')}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


def _auth_seed():
    """Default login: admin / admin. Secret signs session cookies (persisted so
    logins survive restarts). Override the default password in Settings → Account."""
    salt = secrets.token_hex(16)
    return {"user": "admin", "salt": salt, "pwhash": _hash_pw("admin", salt),
            "secret": secrets.token_hex(32)}


def _is_default_pw(auth):
    """True while the password is still the shipped default ('admin'). Stateless — the
    UI uses it to force a password change before letting the user in. Self-clears the
    moment the password is changed to anything else."""
    try:
        return hmac.compare_digest(_hash_pw("admin", auth.get("salt", "")), auth.get("pwhash", ""))
    except Exception:
        return False


def load():
    if STORE.exists():
        data = json.loads(STORE.read_text())
        changed = False
        if "telegram" not in data:           # migrate older stores in place
            data["telegram"] = _telegram_seed(); changed = True
        if "auth" not in data:
            data["auth"] = _auth_seed(); changed = True
        if "notify" not in data:
            data["notify"] = _notify_seed(); changed = True
        if changed:
            save(data)
        return data
    seed = {"endpoints": [{"name": "Local (llama.cpp)",
                           "base_url": "http://127.0.0.1:8081/v1", "api_key": ""}],
            "prefs": {"agent_mode": False},
            "telegram": _telegram_seed(),
            "notify": _notify_seed(),
            "auth": _auth_seed()}
    save(seed)
    return seed


def save(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: this file holds the password hash+salt, the cookie-signing secret, and
    # every endpoint API key — a crash / full disk mid-write must never leave it half-written.
    # (A corrupt web.json fails json.loads on boot, taking the whole UI down and locking the
    # user out, since load() only re-seeds when a key is *absent*, not when the file is broken.)
    atomicio.write_text(STORE, json.dumps(data, indent=2))
    try:
        STORE.chmod(0o600)
    except OSError:
        pass


_BOOT_TS = time.time()          # process start, for the health dashboard's uptime readout
_sessions = {}  # session_id -> Agent
_cancels = {}   # session_id -> threading.Event (set to abort an in-flight query)
_locks = {}     # session_id -> threading.Lock serialising turn/compact on one Agent
# per-session chat state for the composer's slash-commands (/context, /compact, /status)
_ctx_cap = {}      # session_id -> auto-compact threshold (messages), or absent
_compactions = {}  # session_id -> how many times the context was compacted this session
_last_ctx = {}     # session_id -> real prompt-token count from the last turn's stats
_chat_live = {}    # session_id -> {running, message, events:[...]} — buffers the in-flight turn so a
                   # browser refresh can RECONNECT to it (the agent keeps running server-side)
_CHAT_LIVE_KEEP = 600   # seconds a finished turn stays reconnectable

SESSION_COOKIE = "oceano_sess"
SESSION_TTL = 30 * 24 * 3600        # 30 days
# /api paths reachable without a session (everything else under /api is gated).
_PUBLIC_API = {"/api/login", "/api/me"}


def _make_token(user, secret):
    # Domain-separate the HMAC ("sess:") so a session cookie and a preview-capability token
    # (same secret, same envelope shape) can never be cross-validated as one another — without
    # this, a preview token minted for a folder named like the user doubles as a login cookie.
    msg = f"{user}:{int(time.time())}"
    sig = hmac.new(secret.encode(), f"sess:{msg}".encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}:{sig}".encode()).decode()


def _token_user(token, auth):
    """Return the username a cookie authenticates, or None if invalid/expired."""
    try:
        user, ts, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit(":", 2)
        good = hmac.new(auth["secret"].encode(), f"sess:{user}:{ts}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        if time.time() - int(ts) > SESSION_TTL:
            return None
        if user != auth.get("user"):          # username changed → old tokens die
            return None
        return user
    except Exception:
        return None


def _current_user(request):
    return _token_user(request.cookies.get(SESSION_COOKIE, ""), load().get("auth", {}))


def _set_session_cookie(response, user, secret):
    response.set_cookie(SESSION_COOKIE, _make_token(user, secret), httponly=True,
                        samesite="lax", max_age=SESSION_TTL, path="/")


def _agent(sid):
    if sid not in _sessions:
        ag = Agent()
        ag.session_id = sid          # so the mind's per-turn bridge can route a spawn_job back to this chat
        # Rehydrate the conversation so continuing an existing chat — or any chat after a
        # server restart — has its real history, not a blank slate. (Bare Agent() starts with
        # only the system message; without this the model has no memory of the chat it's in.)
        try:
            hist = chats.history_messages(sid)
            if hist:
                ag.messages.extend(hist)
        except Exception:
            pass
        _sessions[sid] = ag
    return _sessions[sid]


def _session_lock(sid):
    """One lock per session: anything that mutates that session's Agent.messages
    (a streaming turn, /compact, auto-compact) must hold it — two tabs can share a
    session id, so client-side guards don't cover this."""
    return _locks.setdefault(sid, threading.Lock())


def _drop_session_state(sid):
    """Forget ALL per-session state. Every session-removal path goes through here —
    a dict missed in one path leaks stale state into a reused session id."""
    for d in (_sessions, _cancels, _ctx_cap, _compactions, _last_ctx, _locks):
        d.pop(sid, None)


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def _apply_telegram(data=None):
    """Start or stop the bot so the running state matches the saved settings."""
    tg = (data or load()).get("telegram", {})
    if tg.get("enabled") and tg.get("token"):
        try:
            user = await telegram_runtime.start(tg["token"], tg.get("allowed", []))
            return {"running": True, "username": user}
        except Exception as e:
            return {"running": False, "error": f"{type(e).__name__}: {e}"}
    await telegram_runtime.stop()
    return {"running": False}


def _embed_reachable():
    try:                                        # embed server (:8082) reachable?
        requests.get(embeddings.EMBED_URL.rstrip("/") + "/models", timeout=2)
        return True
    except requests.RequestException:
        return False


def _inject_session_message(sid, content):
    """Inject a message into the live in-memory Agent for conversation `sid`, so the MIND has it
    as context on its next turn. The user-visible transcript delivery is client-driven (the
    browser polls /api/bgjobs and prints the result), so we deliberately DON'T write chats.json
    here — the client stays the sole persister, which keeps the two from clobbering each other's
    message list. No-op if the session isn't loaded (it rehydrates from the client's chat later)."""
    ag = _sessions.get(sid)
    if ag is None:
        return
    lock = _session_lock(sid)
    if lock.acquire(timeout=30):    # wait out an in-flight turn, but never wedge the caller forever
        try:
            ag.messages.append({"role": "assistant", "content": content})
        finally:
            lock.release()


def _deliver_job_to_session(rec):
    """Fired on a reaper thread when a spawn_job process ends: inject its result into the
    spawning conversation's live Agent."""
    from oceano import bgjobs
    sid = rec.get("sid")
    if not sid:
        return
    label, state, code = rec.get("label", "job"), rec.get("state"), rec.get("exit_code")
    head = (f'Background job "{label}" finished (exit {code}).' if state == "done"
            else f'Background job "{label}" failed (exit {code}).' if state == "failed"
            else f'Background job "{label}" was lost (Oceano restarted while it ran).')
    tail = bgjobs._tail_file(rec["log_path"], 2000) if rec.get("log_path") else ""
    _inject_session_message(sid, head + (("\n\n" + tail) if tail else ""))


def _deliver_agent_to_session(rec):
    """Fired on an agent worker thread when a spawn_agent run ends: same delivery, but the
    result lives bounded in the record itself (no log read needed)."""
    sid = rec.get("sid")
    if not sid:
        return
    label, prov, state = rec.get("label", "agent"), rec.get("provider", ""), rec.get("state")
    head = (f'Background agent "{label}" ({prov}) finished.' if state == "done"
            else f'Background agent "{label}" ({prov}) failed: {rec.get("error") or "no output"}.'
            if state == "failed"
            else f'Background agent "{label}" was lost (Oceano restarted while it ran).')
    body = rec.get("output") or ""
    _inject_session_message(sid, head + (("\n\n" + body) if body else ""))


from oceano import bgjobs as _bgjobs      # noqa: E402 - register the completion hooks once, at import
from oceano import agentjobs as _agentjobs  # noqa: E402
_bgjobs.set_on_complete(_deliver_job_to_session)
_agentjobs.set_on_complete(_deliver_agent_to_session)


_TOOL_CATEGORY = {
    "list_files": "workspace", "read_file": "workspace", "write_file": "workspace",
    "edit_file": "workspace", "make_folder": "workspace", "run_shell": "workspace",
    "python_exec": "workspace", "spawn_job": "workspace", "job_status": "workspace",
    "web_search": "web", "fetch_url": "web",
    "browser_open": "browser", "browser_screenshot": "browser",
    "browser_click": "browser", "browser_scroll": "browser",
    "browser_snapshot": "browser", "browser_fill": "browser",
    "browser_select": "browser", "browser_press": "browser",
    "browser_wait": "browser", "browser_extract": "browser", "browser_read": "browser",
    "browser_eval": "browser", "browser_hover": "browser", "browser_upload": "browser",
    "browser_dialog": "browser", "browser_tab": "browser",
    "remember": "memory", "recall": "memory", "update_memory": "memory", "forget_memory": "memory",
    "index_docs": "documents", "search_docs": "documents", "search_chats": "memory",
    "list_skills": "skills", "load_skill": "skills", "learn_skill": "skills", "evaluate_skill": "skills",
    "delegate": "delegate", "delegate_to_claude": "delegate",
    "spawn_agent": "delegate", "agent_status": "delegate",
    "schedule_task": "scheduler", "list_tasks": "scheduler", "notify": "scheduler",
    "update_task": "scheduler", "cancel_task": "scheduler",
    "list_suggestions": "evolve", "accept_suggestion": "evolve", "dismiss_suggestion": "evolve",
    "run_workflow": "workflow", "list_workflows": "workflow",
    "calendar_events": "calendar", "add_calendar_event": "calendar",
    "update_calendar_event": "calendar", "delete_calendar_event": "calendar",
    "add_calendar_events": "calendar", "find_free_slots": "calendar",
    "manage_calendar": "calendar",
    "transcribe_media": "media", "speak_to_file": "media", "fetch_media": "media", "convert": "media",
    "git": "dev", "code_search": "dev", "run_tests": "dev",
    "http_request": "web", "rss": "web", "sql_query": "data",
    "ui_open": "ui", "ui_close": "ui", "ui_arrange": "ui",
    "desktop_notify": "desktop", "desktop_pick_file": "desktop", "desktop_save_file": "desktop",
    "desktop_reveal_path": "desktop", "desktop_open_path": "desktop",
    "desktop_clipboard_read": "desktop", "desktop_clipboard_write": "desktop",
    "desktop_screenshot": "desktop",
    "mail_accounts": "mail", "mail_folders": "mail", "mail_list": "mail", "mail_read": "mail",
    "mail_move": "mail", "mail_delete": "mail", "mail_flag": "mail", "mail_send": "mail",
    "mail_reply": "mail", "mail_folder": "mail", "mail_save_attachment": "mail",
    "list_hosts": "servers", "ssh_run": "servers", "sftp": "servers",
}


def _effective_model():
    """The model Oceano actually uses: resolved from the user-set primary, an OCEANO_MODEL
    pin, or a model served via Rivers (delegate.resolve_primary) — '' if nothing is set up."""
    from oceano import delegate
    return delegate.get_default_model()


def list_models():
    """Models aggregated across all configured endpoints. Reusable (the web /api/models
    route and the Telegram bot both call it)."""
    data, out = load(), []
    for e in data["endpoints"]:
        try:
            headers = {"Authorization": f"Bearer {e['api_key']}"} if e.get("api_key") else {}
            r = requests.get(e["base_url"].rstrip("/") + "/models", headers=headers, timeout=8)
            for m in r.json().get("data", []):
                out.append({"id": m["id"], "endpoint": e["name"], "base_url": e["base_url"]})
        except requests.RequestException:
            out.append({"id": f"⚠ {e['name']} unreachable", "endpoint": e["name"],
                        "base_url": e["base_url"], "error": True})
    return out


def endpoint_key(base_url):
    """The API key configured for the endpoint serving `base_url` (or '')."""
    return next((e.get("api_key", "") for e in load()["endpoints"]
                 if e["base_url"] == base_url), "")


# ---------------- workspace files (fenced) ----------------
def _wresolve(path):
    p = (config.WORKSPACE / (path or "")).resolve()
    # is_relative_to, not startswith: a prefix match lets a sibling like
    # '<workspace>-evil' escape the fence. config.WORKSPACE is already resolved.
    if not p.is_relative_to(config.WORKSPACE):
        raise HTTPException(400, "path escapes workspace")
    return p
