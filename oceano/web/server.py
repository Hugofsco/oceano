"""Oceano web UI backend (FastAPI).

Serves the SPA and exposes:
  GET  /api/providers          known provider presets (OpenAI, Groq, ...)
  GET  /api/config             configured endpoints (keys masked) + prefs
  POST /api/endpoints          add/update an endpoint {name, base_url, api_key}
  DEL  /api/endpoints/{name}   remove an endpoint
  GET  /api/models             models aggregated across all endpoints
  POST /api/prefs              persist UI prefs
  POST /api/chat               SSE stream: plain tokens OR agent tool-events

Bind stays on 127.0.0.1 by default — the agent can run shell commands, so do NOT
expose this without auth. Reach it over SSH tunnel or Tailscale.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

import config
from oceano import atomicio, browser, calsync, chats, evals, researcher, rivers, embeddings, livebrowser, mcp_client, memory, rag, safety, scheduler, skills, uibridge
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
        if changed:
            save(data)
        return data
    seed = {"endpoints": [{"name": "Local (llama.cpp)",
                           "base_url": "http://127.0.0.1:8081/v1", "api_key": ""}],
            "prefs": {"agent_mode": False},
            "telegram": _telegram_seed(),
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


@asynccontextmanager
async def lifespan(_app):
    try:
        await _apply_telegram()      # start the bot if it's enabled + has a token
    except Exception:
        traceback.print_exc()        # never let a bad token block the web UI from booting
    try:
        mcp_client.start()           # connect configured MCP servers + register their tools
    except Exception:
        traceback.print_exc()
    try:
        skills.ensure_eval_task()    # the locked '[ SKILLS ] evaluate' schedule must exist
    except Exception:
        traceback.print_exc()
    try:
        evals.ensure_eval_task()     # the locked '[ EVAL ]' suite schedule
        evals.seed_cases()           # install starter eval cases on first boot
    except Exception:
        traceback.print_exc()
    try:
        memory.ensure_maintenance_task()   # the locked '[ MEMORY ]' hygiene schedule
    except Exception:
        traceback.print_exc()
    try:
        from oceano import reindex
        reindex.ensure_task()              # the locked '[ INDEX ]' reindex schedule
    except Exception:
        traceback.print_exc()
    yield
    await telegram_runtime.stop()
    try:
        await asyncio.to_thread(livebrowser.shutdown)   # close Chrome on its own thread
    except Exception:
        traceback.print_exc()


app = FastAPI(title="Oceano", lifespan=lifespan)
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


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    webhook = path.startswith("/api/workflows/") and "/webhook/" in path   # gated by its secret token
    if path.startswith("/api/") and path not in _PUBLIC_API and not webhook:
        if not _current_user(request):
            return JSONResponse({"error": "authentication required"}, status_code=401)
    return await call_next(request)


# ---------------- auth ----------------
@app.get("/api/me")
def whoami(request: Request):
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "not authenticated")
    return {"user": user, "must_change": _is_default_pw(load().get("auth", {}))}


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    data = load()
    auth = data.get("auth", {})
    user = (body.get("user") or "").strip()
    pw = body.get("password") or ""
    ok = (user == auth.get("user")
          and hmac.compare_digest(_hash_pw(pw, auth.get("salt", "")), auth.get("pwhash", "")))
    if not ok:
        raise HTTPException(401, "invalid username or password")
    if auth.get("totp_enabled"):                        # second factor required
        code = (body.get("code") or "").strip()
        if not code:
            return {"ok": False, "need_code": True}     # password was right — UI now asks for the code
        step = _totp_verify(auth.get("totp_secret", ""), code)
        last = auth.get("totp_last_step")
        if step is None or (last is not None and step <= last):
            raise HTTPException(401, "invalid authentication code")
        auth["totp_last_step"] = step                   # replay guard — this step can't be reused
        data["auth"] = auth
        save(data)
    _set_session_cookie(response, user, auth["secret"])
    return {"ok": True, "user": user, "must_change": _is_default_pw(auth)}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/account")
async def change_account(request: Request, response: Response):
    """Change the username/password. Gated by the middleware; requires the current
    password too, so a hijacked open tab can't silently rotate credentials."""
    body = await request.json()
    data = load()
    auth = data["auth"]
    if not hmac.compare_digest(_hash_pw(body.get("current_password") or "", auth["salt"]), auth["pwhash"]):
        raise HTTPException(403, "current password is incorrect")
    new_user = (body.get("user") or auth["user"]).strip() or auth["user"]
    new_pw = body.get("new_password") or ""
    if new_pw.strip().lower() == "admin":          # don't let the forced change loop back to the default
        raise HTTPException(400, "choose a password other than the default 'admin'")
    if new_pw:
        auth["salt"] = secrets.token_hex(16)
        auth["pwhash"] = _hash_pw(new_pw, auth["salt"])
    auth["user"] = new_user
    data["auth"] = auth
    save(data)
    _set_session_cookie(response, new_user, auth["secret"])   # re-issue (username may have changed)
    return {"ok": True, "user": new_user}


# ---------------- two-factor auth (optional TOTP) ----------------
@app.get("/api/2fa/status")
def twofa_status():
    auth = load().get("auth", {})
    return {"enabled": bool(auth.get("totp_enabled")), "pending": bool(auth.get("totp_pending"))}


@app.post("/api/2fa/setup")
def twofa_setup():
    """Mint a pending TOTP secret and return the otpauth URI + a QR (SVG) to scan. Not active until
    confirmed with a valid code via /api/2fa/enable, so a mis-scan can't lock the user out."""
    data = load()
    auth = data["auth"]
    secret = _totp_secret()
    auth["totp_pending"] = secret
    data["auth"] = auth
    save(data)
    uri = _totp_uri(secret, auth.get("user", "user"))
    try:
        import segno
        svg = segno.make(uri, error="m").svg_inline(scale=5)
    except Exception:
        svg = ""                                        # UI falls back to showing the secret/URI
    return {"ok": True, "uri": uri, "secret": secret, "svg": svg}


@app.post("/api/2fa/enable")
async def twofa_enable(req: Request):
    """Turn 2FA on: confirm the pending secret with a current code AND the account password — so a
    hijacked session (which has the cookie but not the password) can't silently enroll its own
    authenticator. Matches the password gate on change_account / 2fa disable."""
    data = load()
    auth = data["auth"]
    pending = auth.get("totp_pending")
    if not pending:
        raise HTTPException(400, "no pending 2FA setup — start with Set up")
    body = await req.json()
    if not hmac.compare_digest(_hash_pw(body.get("current_password") or "", auth.get("salt", "")), auth.get("pwhash", "")):
        raise HTTPException(403, "current password is incorrect")
    code = (body.get("code") or "").strip()
    step = _totp_verify(pending, code)
    if step is None:
        raise HTTPException(400, "that code didn't match — check your authenticator and try again")
    auth["totp_secret"] = pending
    auth["totp_enabled"] = True
    auth["totp_last_step"] = step                       # the confirming code can't be replayed at login
    auth.pop("totp_pending", None)
    data["auth"] = auth
    save(data)
    return {"ok": True, "enabled": True}


@app.post("/api/2fa/disable")
async def twofa_disable(req: Request):
    """Turn 2FA off. Requires the current password (a deliberate action), like change_account."""
    data = load()
    auth = data["auth"]
    pw = (await req.json()).get("current_password") or ""
    if not hmac.compare_digest(_hash_pw(pw, auth.get("salt", "")), auth.get("pwhash", "")):
        raise HTTPException(403, "current password is incorrect")
    for k in ("totp_enabled", "totp_secret", "totp_pending", "totp_last_step"):
        auth.pop(k, None)
    data["auth"] = auth
    save(data)
    return {"ok": True, "enabled": False}


def _agent(sid):
    if sid not in _sessions:
        _sessions[sid] = Agent()
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


@app.get("/")
def index():
    # Cache-bust our own app.js/style.css by file mtime so a browser never serves a stale
    # build after an update; the HTML itself is no-cache so the version tokens are re-read.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        try:
            v = int((STATIC / asset).stat().st_mtime)
        except OSError:
            continue
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/providers")
def providers():
    return PROVIDERS


@app.get("/api/config")
def get_config():
    data = load()
    eps = [{"name": e["name"], "base_url": e["base_url"], "has_key": bool(e.get("api_key"))}
           for e in data["endpoints"]]
    tg = data.get("telegram", {})
    return {"endpoints": eps, "prefs": data.get("prefs", {}),
            "telegram": {"enabled": bool(tg.get("enabled")),
                         "has_token": bool(tg.get("token")),   # token itself is never sent down
                         "allowed": tg.get("allowed", []),
                         "status": telegram_runtime.status()}}


@app.post("/api/endpoints")
async def add_endpoint(req: Request):
    body = await req.json()
    data = load()
    data["endpoints"] = [e for e in data["endpoints"] if e["name"] != body["name"]]
    data["endpoints"].append({"name": body["name"], "base_url": body["base_url"].rstrip("/"),
                              "api_key": body.get("api_key", "")})
    save(data)
    return {"ok": True}


@app.delete("/api/endpoints/{name}")
def del_endpoint(name: str):
    data = load()
    data["endpoints"] = [e for e in data["endpoints"] if e["name"] != name]
    save(data)
    return {"ok": True}


@app.post("/api/prefs")
async def set_prefs(req: Request):
    data = load()
    data["prefs"] = {**data.get("prefs", {}), **(await req.json())}
    save(data)
    return {"ok": True}


# ---------------- telegram (folded into this daemon) ----------------
def _parse_ids(value):
    """Accept a list or a comma/space-separated string of Telegram user IDs -> [int]."""
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    out = []
    for x in value or []:
        try:
            out.append(int(str(x).strip()))
        except (TypeError, ValueError):
            pass
    return sorted(set(out))


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


@app.post("/api/telegram")
async def set_telegram(req: Request):
    body = await req.json()
    data = load()
    tg = data.get("telegram", _telegram_seed())
    if "enabled" in body:
        tg["enabled"] = bool(body["enabled"])
    if body.get("clear_token"):
        tg["token"] = ""
    elif body.get("token"):                     # blank token = "leave it unchanged"
        tg["token"] = body["token"].strip()
    if "allowed" in body:
        tg["allowed"] = _parse_ids(body["allowed"])
    data["telegram"] = tg
    save(data)
    result = await _apply_telegram(data)
    return {"ok": "error" not in result, **result, "status": telegram_runtime.status()}


def _embed_reachable():
    try:                                        # embed server (:8082) reachable?
        requests.get(embeddings.EMBED_URL.rstrip("/") + "/models", timeout=2)
        return True
    except requests.RequestException:
        return False


def _searxng_reachable():
    try:                                        # SearXNG (:8080) reachable?
        return requests.get(config.SEARXNG_URL, timeout=2).ok
    except requests.RequestException:
        return False


@app.get("/api/status")
def system_status():
    """Live state of the consolidated daemons, for the Settings → Services panel."""
    from oceano import voice
    beat = scheduler.last_beat()
    return {"embed": _embed_reachable(),
            "scheduler_beat_ago": (time.time() - beat) if beat else None,
            "telegram": telegram_runtime.status(),
            "llamaswap": _llamaswap_status(),
            "searxng": _searxng_reachable(),
            "voice": voice.status()}


@app.post("/api/services/restart")
async def services_restart(request: Request):
    """Restart an individual in-process service. External units (llama-swap :8081, SearXNG :8080) are
    NOT restartable from here — the daemon runs with NoNewPrivileges — so they return a manual hint."""
    name = ((await request.json()).get("service") or "").lower()
    if name == "embeddings":
        from oceano import engine
        ok = engine.restart_embed()
        return {"ok": ok, "msg": "embedding server restarting…" if ok
                else "embedding server isn't managed by the daemon here"}
    if name == "telegram":
        await telegram_runtime.stop()
        st = await _apply_telegram()                      # re-reads saved settings, starts if enabled
        return {"ok": "error" not in st,
                "msg": "Telegram restarted" if st.get("running") else "Telegram stopped (not enabled)",
                "error": st.get("error")}
    if name in ("tts", "stt", "voice"):
        from oceano import voice
        voice.reload()
        return {"ok": True, "msg": "voice models reloaded — the next utterance loads them fresh"}
    if name in ("llamaswap", "llama-swap", "chat-models"):
        # plain systemctl (NOT sudo — sudo would trip NoNewPrivileges); the polkit rule from
        # scripts/install.sh authorizes the daemon's user to manage this one unit.
        import subprocess, sys

        def _restart_swap():
            try:
                r = subprocess.run(["systemctl", "restart", "--no-block", "oceano-llama-swap.service"],
                                   capture_output=True, text=True, timeout=20)
                print(f"[services] systemctl restart oceano-llama-swap -> rc={r.returncode} "
                      f"err={(r.stderr or '').strip()!r}", file=sys.stderr, flush=True)
                if r.returncode == 0:
                    return {"ok": True, "msg": "chat model server restarting…"}
                err = (r.stderr or r.stdout or "").strip().splitlines()
                err = err[-1] if err else "systemctl restart failed"
                if "authentication" in err.lower() or "authorized" in err.lower():
                    err = "not authorized — install the polkit rule (re-run scripts/install.sh) to enable this"
                return {"ok": False, "error": err}
            except FileNotFoundError:
                return {"ok": False, "error": "systemctl not available on this host"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "restart timed out"}
        return await asyncio.to_thread(_restart_swap)
    return {"ok": False, "error": "managed by systemd — can't restart from here. "
            "Run on the host:  sudo systemctl restart oceano-llama-swap"}


def _llamaswap_status():
    """llama-swap reachability + which model it currently has loaded. The model list
    comes from /v1/models; the live-loaded model from llama-swap's /running admin route
    (best-effort — tolerant of shape/version differences, never raises)."""
    base = config.LLM_BASE_URL.rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base   # admin routes live off /v1
    out = {"ok": False, "loaded": None, "models": []}
    try:
        r = requests.get(base + "/models", timeout=2)
        out["ok"] = r.ok
        out["models"] = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except (requests.RequestException, ValueError):
        return out
    try:                                            # llama-swap: GET /running -> currently-up upstream(s)
        rr = requests.get(root + "/running", timeout=2)
        if rr.ok:
            data = rr.json()
            running = data.get("running") if isinstance(data, dict) else data
            if isinstance(running, list) and running and isinstance(running[0], dict):
                out["loaded"] = running[0].get("model") or running[0].get("id")
    except (requests.RequestException, ValueError):
        pass
    return out


@app.get("/api/health")
def health_dashboard():
    """Aggregated live health of the whole self-hosted stack, for the Health window:
    uptime, the inference + embedding servers, scheduler heartbeat, Telegram, the
    knowledge stores, and GPU/VRAM. Each piece degrades independently."""
    beat = scheduler.last_beat()
    try:
        tasks = len(scheduler.all_tasks())
    except Exception:
        tasks = None
    try:
        docs = rag.stats()
    except Exception:
        docs = {}
    try:
        hw = rivers.hw()
    except Exception:
        hw = {}
    return {
        "uptime_s": time.time() - _BOOT_TS,
        "model": _effective_model(),
        "llamaswap": _llamaswap_status(),
        "embed": {"ok": _embed_reachable(), "model": embeddings.EMBED_MODEL, "url": embeddings.EMBED_URL},
        "scheduler": {"beat_ago_s": (time.time() - beat) if beat else None, "tasks": tasks},
        "telegram": telegram_runtime.status(),
        "memory": {"count": memory.count()},
        "rag": docs,
        "hw": hw,
    }


# ---------------- background jobs: live registry + serialization (queue) toggle ----------
@app.get("/api/jobs")
def jobs_snapshot():
    """What background work is in flight right now + the serialize setting (for the
    running indicators and the Settings toggle)."""
    from oceano import jobs
    return jobs.snapshot()


@app.post("/api/jobs/serialize")
async def jobs_set_serialize(req: Request):
    """Turn the queue on/off. `enabled` → background jobs; `chat` → chat turns. Both run
    one-at-a-time through one shared gate instead of hitting the local model in parallel."""
    from oceano import jobs
    b = await req.json()
    if "enabled" in b:
        jobs.set_serialize(bool(b["enabled"]))
    if "chat" in b:
        jobs.set_serialize_chat(bool(b["chat"]))
    s = jobs.snapshot()
    return {"ok": True, "serialize": s["serialize"], "serialize_chat": s["serialize_chat"]}


# ---------------- agent tools (read-only list for Settings → Tools) ----------
_TOOL_CATEGORY = {
    "list_files": "workspace", "read_file": "workspace", "write_file": "workspace",
    "edit_file": "workspace", "make_folder": "workspace", "run_shell": "workspace",
    "python_exec": "workspace",
    "web_search": "web", "fetch_url": "web",
    "browser_open": "browser", "browser_screenshot": "browser",
    "browser_click": "browser", "browser_scroll": "browser",
    "remember": "memory", "recall": "memory", "update_memory": "memory", "forget_memory": "memory",
    "index_docs": "documents", "search_docs": "documents", "search_chats": "memory",
    "list_skills": "skills", "load_skill": "skills", "learn_skill": "skills", "evaluate_skill": "skills",
    "delegate": "delegate", "delegate_to_claude": "delegate",
    "schedule_task": "scheduler", "list_tasks": "scheduler", "notify": "scheduler",
    "run_workflow": "workflow", "list_workflows": "workflow",
    "calendar_events": "calendar", "add_calendar_event": "calendar",
    "update_calendar_event": "calendar", "delete_calendar_event": "calendar",
    "add_calendar_events": "calendar", "find_free_slots": "calendar",
    "manage_calendar": "calendar",
    "transcribe_media": "media", "speak_to_file": "media", "fetch_media": "media", "convert": "media",
    "git": "dev", "code_search": "dev", "run_tests": "dev",
    "http_request": "web", "rss": "web", "sql_query": "data",
    "ui_open": "ui", "ui_close": "ui", "ui_arrange": "ui",
}


@app.get("/api/tools")
def list_tools():
    """Each agent tool with its verifiable capability surface — the parameters it
    actually accepts (read straight from the registered JSON schema)."""
    from oceano import tools
    out = []
    for s in tools.all_schemas():                 # ALL tools (incl. disabled) so the toggles show
        fn = s["function"]
        params = fn.get("parameters", {}) or {}
        props = params.get("properties", {}) or {}
        required = set(params.get("required", []))
        name = fn["name"]
        cat = "mcp" if name.startswith("mcp__") else _TOOL_CATEGORY.get(name, "other")
        out.append({
            "name": name,
            "description": fn.get("description", ""),
            "category": cat,
            "enabled": tools.is_enabled(name),
            "params": [{"name": k, "type": v.get("type", "any"),
                        "required": k in required, "description": v.get("description", "")}
                       for k, v in props.items()],
        })
    return out


@app.post("/api/tools/toggle")
async def toggle_tool(req: Request):
    """Enable/disable a tool (or all of them) for the model. Disabled tools are dropped
    from the prompt, lowering context. body: {name, enabled} or {all: true|false}."""
    from oceano import tools
    b = await req.json()
    if "all" in b:
        tools.set_all(bool(b["all"]))
    elif b.get("name"):
        tools.set_enabled(b["name"], bool(b.get("enabled", True)))
    return {"ok": True, "enabled": len(tools.schemas()), "total": len(tools.all_schemas())}


@app.get("/api/tools/chat")
def chat_tools_state():
    """Which memory tools are offered in plain chat mode (Agent mode off)."""
    from oceano import tools
    return {"tools": tools.chat_tool_state()}


@app.post("/api/tools/chat")
async def chat_tools_set(req: Request):
    """Toggle a memory tool's availability in chat-only mode. body: {name, enabled}."""
    from oceano import tools
    b = await req.json()
    if b.get("name"):
        tools.set_chat_tool(b["name"], bool(b.get("enabled", True)))
    return {"ok": True, "tools": tools.chat_tool_state()}


# ---------------- delegation (Claude Code readiness + per-role provider config) ----------------
@app.get("/api/mind")
def mind_get():
    """Which mind drives the primary chat: 'local' (served model, offline) or 'claude' (Claude Code
    via the user's subscription, with Oceano's persona/memory/workspace). + whether Claude is present."""
    from oceano import delegate
    return {"mind": delegate.get_mind(), "claude_available": delegate.available()}


@app.post("/api/mind")
async def mind_set(req: Request):
    from oceano import delegate
    mind = (await req.json()).get("mind", "local")
    return {"mind": delegate.set_mind(mind), "claude_available": delegate.available()}


@app.get("/api/delegate")
def delegate_status():
    """Claude readiness (shared) + per-role config/readiness: 'default' (agent delegate tool)
    and 'improve' (self-improving jobs: skills, evals, memory)."""
    from oceano import delegate
    return {**delegate.status_all(), "enabled": delegate.enabled()}


@app.post("/api/delegate")
async def delegate_set(req: Request):
    from oceano import delegate
    b = await req.json()
    role = b.get("role", "default")
    if role not in delegate.ROLES:
        return {"ok": False, "error": "unknown role"}
    delegate.set_config(b, role=role)
    return {"ok": True, **delegate.status_all()}


@app.post("/api/delegate/test")
async def delegate_test(req: Request):
    """Live probe of a role's provider (proves Claude Code auth, or the API model works).
    Runs in a thread so the ~minute timeout can't block the event loop."""
    from oceano import delegate
    try:
        b = await req.json()
    except Exception:
        b = {}
    role = b.get("role", "default")
    role = role if role in delegate.ROLES else "default"
    return await asyncio.to_thread(delegate.probe, role)


def _effective_model():
    """The model Oceano actually uses: resolved from the user-set primary, an OCEANO_MODEL
    pin, or a model served via Rivers (delegate.resolve_primary) — '' if nothing is set up."""
    from oceano import delegate
    return delegate.get_default_model()


@app.get("/api/default-model")
def get_default_model_api():
    """The primary model + endpoint the agent uses everywhere. The picker lists ALL models
    (/api/models, any endpoint) — local-first is opt-in, so any model can be primary.
      model     the explicit primary the user pinned ('' = none → use the resolved default)
      current   what Oceano actually resolves to right now (primary > env > served)
      fallback  what the 'Default' (un-pinned) choice resolves to: env pin or first Rivers model
      source    where `current` came from: primary | env | served | none"""
    from oceano import delegate
    p = delegate.get_primary()
    r = delegate.resolve_primary()
    implicit = config.MODEL or (delegate.served_models()[:1] or [""])[0]
    return {"model": p["model"], "base_url": p["base_url"],
            "current": r["model"], "source": r["source"], "fallback": implicit}


@app.post("/api/default-model")
async def set_default_model_api(req: Request):
    """Set the primary model. base_url empty = the default local endpoint; otherwise resolve and
    store that endpoint's api key so the agent can reach it from Telegram/CLI/jobs too."""
    from oceano import delegate
    b = await req.json()
    model = (b.get("model") or "").strip()
    base_url = (b.get("base_url") or "").strip()
    api_key = endpoint_key(base_url) if base_url else ""
    delegate.set_primary(model, base_url, api_key)
    return {"ok": True, "current": delegate.get_default_model()}


@app.post("/api/delegate/enabled")
async def set_delegation_enabled(req: Request):
    """Master delegation switch. Off → run() refuses (background jobs + the tool) and the
    delegate tool is withheld from the agent. (Also toggleable per-tool under Settings → Tools.)"""
    from oceano import delegate
    b = await req.json()
    on = bool(b.get("enabled", True))
    delegate.set_enabled(on)
    tools.set_enabled("delegate", on)                # keep the agent's delegate tool in sync
    return {"ok": True, "enabled": on, **delegate.status_all()}


@app.get("/api/mcp")
def mcp_status():
    return mcp_client.status()


# ---------------- brain: embedding-engine stats + semantic search ------------
@app.get("/api/brain/stats")
def brain_stats():
    docs = rag.stats()
    return {"memories": memory.count(),
            "docs": docs,
            "embed": {"ok": _embed_reachable(), "model": embeddings.EMBED_MODEL,
                      "url": embeddings.EMBED_URL, "dims": docs.get("dims")}}


@app.post("/api/brain/search")
async def brain_search(request: Request):
    """Semantic search over memories, indexed docs, or past conversations."""
    b = await request.json()
    query = (b.get("query") or "").strip()
    scope = b.get("scope", "memory")
    if not query:
        return {"results": []}
    fn = {"memory": memory.search, "docs": rag.search, "chats": chats.search}.get(scope, memory.search)
    return {"results": await asyncio.to_thread(fn, query)}   # cosine scan off the event loop


@app.post("/api/brain/index")
async def brain_index(request: Request):
    """Index a folder of documents into the RAG store (embeds each chunk)."""
    folder = ((await request.json()).get("folder") or "").strip()
    if not folder:
        return {"ok": False, "result": "no folder given"}
    result = await asyncio.to_thread(rag.index_docs, folder)
    return {"ok": not result.startswith(("ERROR", "(no such")), "result": result}


# ---------------- rivers: HF model catalog → hwfit → download → serve -------
@app.get("/api/rivers/hw")
def rivers_hw():
    return rivers.hw()


@app.get("/api/rivers/recommended")
def rivers_recommended():
    return rivers.recommended()


@app.get("/api/rivers/search")
async def rivers_search(q: str = ""):
    try:
        return {"results": await asyncio.to_thread(rivers.search, q)}
    except Exception as e:
        return {"results": [], "error": f"{type(e).__name__}: {e}"}


@app.get("/api/rivers/files")
async def rivers_files(repo: str):
    try:
        return await asyncio.to_thread(rivers.files, repo)
    except Exception as e:
        return {"files": [], "error": f"{type(e).__name__}: {e}"}


@app.get("/api/rivers/installed")
def rivers_installed():
    return {"models": rivers.installed()}


@app.post("/api/rivers/download")
async def rivers_download(request: Request):
    b = await request.json()
    try:
        return rivers.start_download(b.get("repo", ""), b.get("filename", ""))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/api/rivers/jobs")
def rivers_jobs():
    return {"jobs": rivers.jobs()}


def _serve_params(b):
    """Pull the serving params out of a request body (shared by serve + update)."""
    return dict(ngl=b.get("ngl", 99), ctx=b.get("ctx", 8192), fa=b.get("fa", True),
                kv=b.get("kv", "f16"), kv_v=b.get("kv_v"), ttl=b.get("ttl", 600),
                threads=b.get("threads"), batch=b.get("batch"), ubatch=b.get("ubatch"),
                n_cpu_moe=b.get("n_cpu_moe"), parallel=b.get("parallel", 1), extra=b.get("extra", ""))


@app.post("/api/rivers/serve")
async def rivers_serve(request: Request):
    b = await request.json()
    return rivers.serve(b.get("filename", ""), b.get("name"), **_serve_params(b))


@app.get("/api/rivers/served")
def rivers_served():
    """Models currently wired into llama-swap, as structured params (for edit/unserve)."""
    return rivers.served()


@app.get("/api/rivers/estimate")
async def rivers_estimate(filename: str, ctx: int = 8192, kv: str = "f16",
                          kv_v: str = "", ngl: int = 99):
    """VRAM estimate for a serve config (reads the GGUF header off the event loop)."""
    return await asyncio.to_thread(rivers.estimate, filename, ctx, kv, ngl, kv_v or None)


@app.get("/api/rivers/recommend")
async def rivers_recommend(filename: str):
    """Suggested serving config for this model on this box (ngl/ctx/KV/threads/MoE), with reasons."""
    return await asyncio.to_thread(rivers.recommend, filename)


@app.post("/api/rivers/update")
async def rivers_update(request: Request):
    """Re-tune an already-served model in place (preserves comments + unknown flags)."""
    b = await request.json()
    return rivers.update_served(b.get("name", ""), **_serve_params(b))


@app.post("/api/rivers/unserve")
async def rivers_unserve(request: Request):
    """Remove a model from llama-swap.yaml."""
    return rivers.unserve((await request.json()).get("name", ""))


@app.post("/api/rivers/delete")
async def rivers_delete(request: Request):
    """Delete a model's .gguf from disk (refuses if served, or if it's the embedding model)."""
    return rivers.delete_model((await request.json()).get("filename", ""))


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


@app.get("/api/models")
def models():
    return list_models()


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    sid = body.get("session", "default")
    message = body.get("message", "")
    try:
        from oceano import workflows
        workflows.fire_keyword(message, "web")        # keyword-trigger workflows (runs in background)
    except Exception:
        pass
    base_url = body.get("base_url")
    data = load()
    api_key = next((e.get("api_key", "") for e in data["endpoints"]
                    if e["base_url"] == base_url), "")

    ag = _agent(sid)
    ag.model = body.get("model") or ag.model
    ag.base_url = base_url
    ag.api_key = api_key
    agent_mode = bool(body.get("agent_mode"))
    attachments = body.get("attachments") or []      # [{path, name, kind}] from /api/upload
    # so it's verifiable in the journal which mode a message actually ran in (tools
    # are only attached in agent mode) — settles "the toggle was on but it didn't use tools".
    print(f"[chat] model={ag.model!r} agent_mode={agent_mode}", flush=True)

    # The agent is blocking (a single LLM step or a slow tool can take 20s+ with no
    # output). Run it in a worker thread and feed events through a queue, so the
    # response generator can emit a keep-alive during any silent gap — otherwise an
    # idle proxy / VS Code port-forward / Tailscale hop drops the stream.
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    now = time.time()
    for _s in [k for k, v in _chat_live.items() if not v.get("running") and now - v.get("ts", now) > _CHAT_LIVE_KEEP]:
        _chat_live.pop(_s, None)                          # prune old finished turns
    _chat_live[sid] = {"running": True, "message": message, "events": [], "ts": now}

    def put(ev):
        if ev is not None:                                # buffer every event so a refresh can reconnect
            b = _chat_live.get(sid)
            if b is not None:
                b["events"].append(ev)
                if isinstance(ev, dict) and ev.get("type") == "tool_progress":
                    # progress events are ephemeral live updates; keep only the most recent so a
                    # long delegation (hundreds of them) can't bloat the reconnect buffer.
                    prog = [e for e in b["events"] if e.get("type") == "tool_progress"]
                    if len(prog) > 60:
                        b["events"].remove(prog[0])
                if isinstance(ev, dict) and ev.get("type") in ("done", "error"):
                    b["running"] = False
        loop.call_soon_threadsafe(q.put_nowait, ev)

    cancel = threading.Event()      # set ONLY by /api/chat/stop (a disconnect no longer cancels)
    _cancels[sid] = cancel

    def worker():
        stream = None
        try:
            # When the user adds chat to the queue (Settings → Execution), this turn waits on
            # the same global gate the background jobs use — so it won't hit the model in
            # parallel with running work. gate=False (default) → chat stays fully responsive.
            from oceano import jobs
            chat_gate = jobs.serialize_chat_enabled()
            if chat_gate and jobs.snapshot()["running"] > 0:
                put({"type": "notice", "text": "⏳ Queued — waiting for current work to finish (chat queue is on)."})
            with jobs.job("chat", (message or "chat")[:60], gate=chat_gate):
                # One turn at a time per session: another tab's turn or a /compact must not
                # mutate ag.messages while this stream is appending to it.
                with _session_lock(sid):
                    cap = _ctx_cap.get(sid)              # /context <n> → auto-compact before the turn
                    if cap and len(ag.messages) > cap:
                        dropped = ag.compact()
                        if dropped:
                            _compactions[sid] = _compactions.get(sid, 0) + 1
                            put({"type": "notice", "text": f"🗜 Auto-compacted {dropped} messages "
                                                            f"(context passed {cap})."})
                    # dropped files become context for the (text-only) local model: text is
                    # extracted inline; images are described by the configured vision target.
                    turn_msg = message
                    if attachments:
                        ctx = _attachment_context(attachments, message, put)
                        if ctx:
                            turn_msg = ctx + message
                    # chat mode still gets the user-chosen memory tools (Settings → Tools) so it can
                    # manage what it knows about you without full agent mode; agent mode → all tools.
                    from oceano import tools as _tools
                    stream = ag.run_stream(turn_msg) if agent_mode else ag.run_stream(turn_msg, only_tools=_tools.chat_tools())
                    for ev in stream:
                        if isinstance(ev, dict) and ev.get("type") == "stats" and ev.get("ctx"):
                            _last_ctx[sid] = ev["ctx"]   # remember real prompt tokens for /status
                        if cancel.is_set():
                            break           # stop feeding — query was aborted
                        put(ev)
                if not cancel.is_set():
                    put({"type": "done"})
        except Exception as ex:
            traceback.print_exc()   # so it actually lands in the journal, not just the UI
            put({"type": "error", "message": f"{type(ex).__name__}: {ex}"})
        finally:
            # closing the generator unwinds its try/finally → closes the upstream
            # LLM HTTP stream, so the local model stops generating too.
            if cancel.is_set() and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass
            b = _chat_live.get(sid)
            if b is not None:
                b["running"] = False                      # turn is over — reconnection stops polling
            put(None)  # sentinel: stream finished

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # SSE comment — ignored by the client parser
                    continue
                if ev is None:
                    break
                yield _sse(ev)
        except (asyncio.CancelledError, GeneratorExit):
            raise                       # client went away — let the turn finish server-side (reconnectable)
        except Exception:
            # never let the response generator die silently — log it and try to
            # send a clean error frame so the client shows a real message.
            traceback.print_exc()
            try:
                yield _sse({"type": "error", "message": "stream closed unexpectedly (see server logs)"})
            except Exception:
                pass
        finally:
            _cancels.pop(sid, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.post("/api/chat/stop")
async def chat_stop(request: Request):
    """Abort the in-flight query for a session — the Stop button calls this."""
    sid = (await request.json()).get("session", "default")
    ev = _cancels.get(sid)
    if ev:
        ev.set()
    return {"ok": bool(ev)}


@app.get("/api/chat/live/{sid}")
def chat_live(sid: str, since: int = 0):
    """The in-flight (or just-finished) turn for a session, so a reloaded page can reconnect to it
    and replay what it missed. `since` = how many events the client already has."""
    b = _chat_live.get(sid)
    if not b:
        return {"running": False, "message": "", "events": [], "total": 0}
    evs = b["events"]
    return {"running": b["running"], "message": b["message"], "events": evs[since:], "total": len(evs)}


@app.delete("/api/session/{sid}")
def end_session(sid: str):
    _drop_session_state(sid)
    return {"ok": True}


# ---------------- chat composer slash-commands (mirror Telegram /context /compact /status) ----------------
def _ctx_payload(sid):
    ag = _agent(sid)
    n, approx = ag.context_metrics()
    return {"model": ag.model, "messages": n, "approx_tokens": approx,
            "ctx_tokens": _last_ctx.get(sid), "compactions": _compactions.get(sid, 0),
            "cap": _ctx_cap.get(sid)}


@app.get("/api/chat/context")
def chat_context(session: str = "default"):
    return _ctx_payload(session)


@app.post("/api/chat/context")
async def chat_set_context(req: Request):
    """Set/clear the auto-compact threshold for a session. value: <n> | off."""
    b = await req.json()
    sid = b.get("session", "default")
    raw = str(b.get("value", "")).strip().lower()
    if raw in ("", "off", "0", "none"):
        _ctx_cap.pop(sid, None)
        return {"ok": True, **_ctx_payload(sid)}
    try:
        _ctx_cap[sid] = max(4, int(raw))
    except ValueError:
        return {"ok": False, "error": "usage: /context <n> (messages before auto-compact) or /context off"}
    return {"ok": True, **_ctx_payload(sid)}


@app.post("/api/chat/compact")
async def chat_compact(req: Request):
    b = await req.json()
    sid = b.get("session", "default")
    ag = _agent(sid)
    if len(ag.messages) <= 2:
        return {"ok": False, "error": "nothing to compact yet — the context is already small",
                **_ctx_payload(sid)}
    lock = _session_lock(sid)
    if not lock.acquire(blocking=False):   # a turn is streaming — compacting now would corrupt it
        return {"ok": False, "error": "busy — wait for the current reply to finish (or Stop it) first",
                **_ctx_payload(sid)}
    try:
        # summarising is a blocking LLM call — keep it off the event loop
        dropped = await asyncio.to_thread(ag.compact)
    finally:
        lock.release()
    if dropped:
        _compactions[sid] = _compactions.get(sid, 0) + 1
    return {"ok": True, "dropped": dropped, **_ctx_payload(sid)}


@app.get("/api/chat/status")
def chat_status(session: str = "default"):
    from oceano import tools, memory, rag
    ag = _agent(session)
    try:
        docs = rag.stats().get("files", 0)
    except Exception:
        docs = 0
    try:
        facts = memory.count()
    except Exception:
        facts = 0
    tool_names = sorted(s["function"]["name"] for s in tools.schemas())
    return {**_ctx_payload(session), "tools": tool_names, "tool_count": len(tool_names),
            "memory": facts, "docs": docs}


# ---------------- chats (server-side, dated-folder persistence) ----------------
@app.get("/api/chats")
def chats_list():
    return {"chats": chats.list_all()}


@app.get("/api/chats/{cid}")
def chats_get(cid: str):
    c = chats.get(cid)
    return c or {"id": cid, "title": "New voyage", "messages": []}


@app.post("/api/chats/{cid}")
async def chats_save(cid: str, req: Request):
    b = await req.json()
    # creation date is assigned server-side (never trust the client for a path component);
    # existing chats keep their original date inside chats.save().
    ok = chats.save(cid, b.get("title", ""), b.get("messages", []))
    return {"ok": ok}


@app.delete("/api/chats/{cid}")
def chats_delete(cid: str):
    _drop_session_state(cid)        # also free the in-memory Agent
    return {"ok": chats.delete(cid)}


@app.post("/api/chats/{cid}/to-skill")
async def chat_to_skill(cid: str):
    """Distill this conversation into a reusable skill (delegated to Claude / the improve
    model; saved as a LEARNING skill that enters the independent-review pipeline)."""
    text = chats.transcript(cid)
    if not text.strip():
        return {"ok": False, "error": "no conversation yet — chat a bit first"}
    return await asyncio.to_thread(skills.from_conversation, text)


# ---------------- wipe (Settings → destructive, per-target) ----------------
@app.post("/api/wipe/{target}")
def wipe(target: str):
    if target == "chats":
        return {"ok": True, "removed": chats.wipe(), "what": "chats"}
    if target == "documents":
        n = 0
        for c in config.WORKSPACE.iterdir():
            if c.name == ".gitkeep":
                continue
            try:
                shutil.rmtree(c) if c.is_dir() else c.unlink()
                n += 1
            except OSError:
                pass
        return {"ok": True, "removed": n, "what": "workspace items"}
    if target == "skills":                          # the agent's self-learned (non-published) skills
        learnt = [s for s in skills.all_skills() if s.get("status") != "published"]
        for s in learnt:
            skills.delete_skill(s["dir"])
        return {"ok": True, "removed": len(learnt), "what": "learnt skills"}
    if target == "memory":
        return {"ok": True, "removed": memory.wipe(), "what": "memories"}
    if target == "knowledge":
        return {"ok": True, "removed": rag.wipe(), "what": "indexed chunks"}
    raise HTTPException(400, f"unknown wipe target: {target}")


# ---------------- skills ----------------
@app.get("/api/skills")
def get_skills():
    return skills.all_skills()


@app.post("/api/skills")
async def post_skill(req: Request):
    b = await req.json()
    slug = skills.save_skill(b["name"], b.get("description", ""), b.get("body", ""), b.get("dir"),
                             status=b.get("status", "published"), notes=b.get("notes", ""))
    return {"ok": True, "dir": slug}


@app.patch("/api/skills/{dir}")
async def patch_skill(dir: str, req: Request):
    """Move a skill through the lifecycle (publish / send back to learning)."""
    b = await req.json()
    return {"ok": skills.set_status(dir, b.get("status", ""), b.get("notes"))}


@app.delete("/api/skills/{dir}")
def remove_skill(dir: str):
    return {"ok": skills.delete_skill(dir)}


@app.post("/api/skills/evaluate")
def evaluate_skills_api():
    """Kick off review → staging → publish in the background (it shells out to
    Claude Code, which can take minutes)."""
    if skills.eval_state()["running"]:
        return {"ok": False, "running": True, "error": "an evaluation is already running"}
    threading.Thread(target=skills.evaluate_all, daemon=True).start()
    return {"ok": True, "running": True}


@app.get("/api/skills-eval")
def skills_eval_state():
    return skills.eval_state()


# ---------------- evals: model eval harness ----------------
@app.get("/api/evals/cases")
def evals_cases():
    return {"cases": evals.all_cases(), "categories": list(evals.CATEGORIES),
            "grader_types": list(evals.GRADER_TYPES)}


@app.post("/api/evals/cases")
async def evals_save_case(req: Request):
    b = await req.json()
    rid = evals.save_case(b.get("id"), b.get("name", ""), b.get("category", "qa"),
                          b.get("prompt", ""), b.get("rubric", ""), b.get("graders", []),
                          b.get("seed"), b.get("timeout"), b.get("weight", 1.0),
                          bool(b.get("enabled", True)))
    return {"ok": True, "id": rid}


@app.delete("/api/evals/cases/{cid}")
def evals_delete_case(cid: int):
    return {"ok": evals.delete_case(cid)}


@app.get("/api/evals/models")
def evals_models():
    """Available local models + which are selected as eval targets (drives Run-now
    AND the scheduled run), plus the locked schedule for context."""
    return evals.models_config()


@app.post("/api/evals/models")
async def evals_set_models(req: Request):
    b = await req.json()
    return {"ok": True, "selected": evals.set_selected_models(b.get("models") or [])}


@app.post("/api/evals/run")
async def evals_run(req: Request):
    if evals.state()["running"]:
        return {"ok": False, "running": True, "error": "an eval run is already in progress"}
    b = await req.json()
    evals.run_all_bg(b.get("models") or None)   # None → use the saved selection
    return {"ok": True, "running": True}


@app.post("/api/evals/cancel")
def evals_cancel():
    """Stop an in-progress run (after the current case). The ✕ Cancel button calls this."""
    return {"ok": evals.cancel()}


@app.get("/api/evals/state")
def evals_state():
    return evals.state()


@app.get("/api/evals/leaderboard")
def evals_leaderboard(run_id: int = None):
    return evals.leaderboard(run_id)


@app.get("/api/evals/runs")
def evals_runs():
    return {"runs": evals.runs()}


@app.delete("/api/evals/runs/{run_id}")
def evals_delete_run(run_id: int):
    if not evals.delete_run(run_id):
        return {"ok": False, "error": "that run is still executing — cancel it first"}
    return {"ok": True}


@app.post("/api/evals/runs/clear")
def evals_clear_runs():
    removed = evals.clear_runs()
    if removed is None:
        return {"ok": False, "error": "an eval run is in progress — cancel it or let it finish first"}
    return {"ok": True, "removed": removed}


@app.get("/api/evals/results")
def evals_results(run_id: int):
    return {"results": evals.results(run_id)}


# ---------------- memories ----------------
@app.get("/api/memories")
def get_memories():
    return memory.list_all()


@app.post("/api/memories")
async def post_memory(req: Request):
    b = await req.json()
    memory.remember(b["text"], b.get("tags", ""), b.get("category", "fact"), bool(b.get("pinned")))
    return {"ok": True}


@app.patch("/api/memories/{mid}")
async def patch_memory(mid: int, req: Request):
    b = await req.json()
    if "pinned" in b:
        memory.set_pinned(mid, bool(b["pinned"]))
    if "category" in b:
        memory.set_category(mid, b["category"])
    return {"ok": True}


@app.delete("/api/memories/{mid}")
def remove_memory(mid: int):
    return {"ok": memory.forget(mid)}


# ---------------- memory graph (Memory Graph window) ----------------
@app.get("/api/memory/graph")
async def memory_graph(threshold: float = 0.62):
    """Memories as a similarity graph for the Memory Graph window. The cosine scan runs
    off the event loop so a large store can't stall the request."""
    th = min(max(threshold, 0.3), 0.95)
    return await asyncio.to_thread(memory.graph, th)


# ---------------- memory injection policy (Settings → Memory) ----------------
@app.get("/api/memory/policy")
def get_memory_policy():
    return {"policy": memory.get_policy(), "categories": memory.CATEGORIES}


@app.post("/api/memory/policy")
async def set_memory_policy(req: Request):
    return {"ok": True, "policy": memory.set_policy(await req.json())}


# ---------------- notes / kanban scratchpad ----------------
@app.get("/api/notes")
def notes_get():
    from oceano import notes
    return notes.board()


@app.post("/api/notes")
async def notes_add(req: Request):
    from oceano import notes
    b = await req.json()
    return {"ok": True, "card": notes.add(b.get("text", ""), b.get("col", "todo"))}


@app.patch("/api/notes/{cid}")
async def notes_update(cid: int, req: Request):
    from oceano import notes
    b = await req.json()
    return {"ok": notes.update(cid, b.get("text"), b.get("col"))}


@app.delete("/api/notes/{cid}")
def notes_delete(cid: int):
    from oceano import notes
    return {"ok": notes.remove(cid)}


# ---------------- voice console (web) — reuses the Telegram speech stack ----------------
@app.get("/api/voice/status")
def voice_status():
    from oceano import voice
    return voice.status()


@app.get("/api/voice/voices")
def voice_voices():
    """Available Kokoro voices, installed Piper voices, and current TTS settings — for the Voice tab."""
    from oceano import voice
    return {"voices": voice.list_voices(), "settings": voice.get_settings(),
            "piper_installed": voice.piper_installed()}


@app.post("/api/voice/settings")
async def voice_settings(req: Request):
    """Set the active TTS engine / voice / speed / Piper voice and the wake-word config (persisted;
    takes effect on the next utterance / the next time conversation mode is started)."""
    from oceano import voice
    b = await req.json()
    return {"ok": True, "settings": voice.set_settings(engine=b.get("engine"), voice=b.get("voice"),
                                                       speed=b.get("speed"), wake=b.get("wake"),
                                                       wake_word=b.get("wake_word"),
                                                       piper_voice=b.get("piper_voice"))}


@app.get("/api/voice/piper/languages")
def voice_piper_languages():
    """Languages available in the Piper voice catalog (+ what's already installed) — for Browse."""
    from oceano import voice
    return {"languages": voice.piper_languages(), "installed": voice.piper_installed()}


@app.get("/api/voice/piper/voices")
def voice_piper_voices(lang: str = ""):
    """Piper catalog voices, filtered to one language code (e.g. ?lang=en_US)."""
    from oceano import voice
    return {"voices": voice.piper_list(lang or None)}


@app.post("/api/voice/piper/download")
async def voice_piper_download(req: Request):
    """Download a Piper voice from the catalog into assets/voice/ (md5-verified). Runs in a worker
    thread so the (sync, ~tens-of-MB) download doesn't block the event loop."""
    from starlette.concurrency import run_in_threadpool
    from oceano import voice
    key = (await req.json()).get("key", "")
    if not key:
        return {"ok": False, "error": "no voice key"}
    return await run_in_threadpool(voice.piper_download, key)


@app.post("/api/voice/stt")
async def voice_stt(req: Request):
    """Transcribe an uploaded audio blob (the browser's MediaRecorder gives webm/opus;
    faster-whisper decodes it via ffmpeg). Body is the raw audio bytes."""
    from oceano import voice
    data = await req.body()
    if not data:
        return {"text": ""}
    fd, tmp = tempfile.mkstemp(suffix=".webm")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        text = await asyncio.to_thread(voice.transcribe, tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"text": text}


@app.post("/api/voice/tts")
async def voice_tts(req: Request):
    """Render text to an OGG/Opus clip the browser can play. The temp file is unlinked
    after the response is sent (BackgroundTask)."""
    from oceano import voice
    text = ((await req.json()).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "no text")
    path = await asyncio.to_thread(voice.synthesize, text)
    if not path:
        raise HTTPException(503, "TTS unavailable on this machine")

    def _cleanup():
        try:
            os.remove(path)
        except OSError:
            pass

    return FileResponse(path, media_type="audio/ogg", background=BackgroundTask(_cleanup))


# ---------------- workflows (named, schedulable multi-step recipes) ----------------
@app.get("/api/workflows")
def workflows_list():
    from oceano import workflows
    return [{**w, "schedule": workflows.schedule_info(w["id"])} for w in workflows.list_all()]


@app.get("/api/workflows/live")
def workflows_live():
    """In-progress (and just-finished) runs — lets the UI reconnect to a running workflow's
    live state after a browser refresh, and mark which workflows are running."""
    from oceano import workflows
    return {"running": workflows.live()}


@app.get("/api/workflows/{wid}/triggers")
def workflows_triggers_get(wid: int):
    from oceano import workflows
    return {"triggers": workflows.get_triggers(wid)}


@app.put("/api/workflows/{wid}/triggers")
async def workflows_triggers_set(wid: int, req: Request):
    from oceano import workflows
    b = await req.json()
    return {"ok": True, "triggers": workflows.set_triggers(wid, b.get("triggers", []))}


@app.post("/api/workflows/{wid}/webhook/{token}")
async def workflows_webhook(wid: int, token: str):
    """Fire a workflow from an external POST. Auth-exempt — the secret token IS the auth.
    The server is localhost-bound by default; only reachable remotely if you tunnel it."""
    from oceano import workflows
    wf = workflows.webhook_run(wid, token)
    if not wf:
        raise HTTPException(404, "no matching/enabled webhook trigger")
    return JSONResponse({"ok": True, "started": wf["name"]}, status_code=202)


@app.post("/api/workflows")
async def workflows_create(req: Request):
    from oceano import workflows
    b = await req.json()
    return {"ok": True, "workflow": workflows.create(b.get("name", "Untitled"),
                                                      b.get("description", ""), b.get("graph"))}


@app.patch("/api/workflows/{wid}")
async def workflows_update(wid: int, req: Request):
    from oceano import workflows
    b = await req.json()
    wf = workflows.update(wid, name=b.get("name"), description=b.get("description"), graph=b.get("graph"))
    return {"ok": wf is not None, "workflow": wf}


@app.delete("/api/workflows/{wid}")
def workflows_delete(wid: int):
    from oceano import workflows
    return {"ok": workflows.remove(wid)}


@app.post("/api/workflows/{wid}/schedule")
async def workflows_schedule(wid: int, req: Request):
    from oceano import workflows
    workflows.set_schedule(wid, ((await req.json()).get("cron") or "").strip())
    return {"ok": True, "schedule": workflows.schedule_info(wid)}


@app.get("/api/workflows/{wid}/runs")
def workflows_runs(wid: int):
    from oceano import workflows
    return workflows.runs(wid)


@app.post("/api/workflows/{wid}/run")
async def workflows_run(wid: int):
    """Run a workflow now, streaming step-by-step progress as SSE. The engine runs in a
    worker thread (it blocks on the local model + tools); events feed through a queue so
    the response can keep-alive during quiet steps — same shape as /api/chat."""
    from oceano import workflows
    wf = workflows.get(wid)
    if not wf:
        raise HTTPException(404, "no such workflow")
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    put = lambda ev: loop.call_soon_threadsafe(q.put_nowait, ev)

    def worker():
        try:
            workflows.run(wf, trigger="manual", on_step=put)
        except Exception as ex:
            traceback.print_exc()
            put({"event": "error", "message": f"{type(ex).__name__}: {ex}"})
        finally:
            put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=10)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if ev is None:
                break
            yield _sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ---------------- workspace files (fenced) ----------------
def _wresolve(path):
    p = (config.WORKSPACE / (path or "")).resolve()
    # is_relative_to, not startswith: a prefix match lets a sibling like
    # '<workspace>-evil' escape the fence. config.WORKSPACE is already resolved.
    if not p.is_relative_to(config.WORKSPACE):
        raise HTTPException(400, "path escapes workspace")
    return p


# ---------------- chat file/image drop ----------------
_UPLOAD_DIR = config.WORKSPACE / "uploads"
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Save a dropped file into workspace/uploads and classify it (image / text / other)."""
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "file too large (25 MB max)")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    raw = (file.filename or "file").replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in raw)[:80] or "file"
    dest = _UPLOAD_DIR / safe
    base, suf, n = dest.stem, dest.suffix, 1
    while dest.exists():                                  # don't clobber an existing upload
        dest = _UPLOAD_DIR / f"{base}_{n}{suf}"
        n += 1
    dest.write_bytes(data)
    ext = dest.suffix.lower()
    kind = "image" if ext in _IMG_EXT else ("text" if (ext in rag.TEXT_EXT or ext == ".pdf") else "other")
    return {"ok": True, "name": dest.name, "path": str(dest.relative_to(config.WORKSPACE)), "kind": kind}


@app.post("/api/upload-to")
async def upload_to(dir: str = Form(""), paths: list[str] = Form([]),
                    files: list[UploadFile] = File(...)):
    """Upload many files — or a whole folder (the browser sends each file with its relative path) —
    straight into the workspace. `dir` is the target subfolder (the explorer's current dir); each
    `paths[i]` recreates the picked folder's structure. Everything is confined to the workspace."""
    saved, skipped = [], []
    for i, f in enumerate(files):
        rel = (paths[i] if i < len(paths) else "") or (f.filename or "file")
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p and p not in (".", "..")]
        if not parts:
            skipped.append(str(rel)); continue
        safe = "/".join("".join(c if (c.isalnum() or c in "._- ()") else "_" for c in p)[:120] for p in parts)
        try:
            dest = _wresolve((dir.rstrip("/") + "/" + safe) if dir else safe)
        except HTTPException:
            skipped.append(safe); continue
        data = await f.read()
        if len(data) > 100 * 1024 * 1024:                    # 100 MB per-file cap
            skipped.append(f"{safe} (>100 MB)"); continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        saved.append(str(dest.relative_to(config.WORKSPACE)))
    return {"ok": True, "saved": len(saved), "skipped": skipped, "files": saved[:50]}


def _attachment_context(attachments, question, put=None):
    """Turn dropped files into text context for the (text-only) local model: text files inline,
    images described by the configured vision target. Returns a prefix string ('' if nothing)."""
    from oceano import rag, delegate
    parts = []
    for att in attachments or []:
        try:
            p = _wresolve(att.get("path", ""))
        except Exception:
            continue
        if not p.is_file():
            continue
        name = att.get("name") or p.name
        if att.get("kind") == "image":
            if put:
                put({"type": "notice", "text": f"🖼 analyzing {name} with the vision model…"})
            r = delegate.describe_image(str(p), question, role="vision")
            desc = (r.get("output") or "").strip() if r.get("ok") else f"(couldn't analyze: {r.get('error')})"
            parts.append(f"[Attached image “{name}” — what the vision model sees:]\n{desc}")
        else:
            text = rag._read(p)
            if text.strip():
                parts.append(f"[Attached file “{name}”:]\n{text[:6000]}")
    return ("\n\n".join(parts) + "\n\n") if parts else ""


@app.get("/api/files")
def list_dir(path: str = ""):
    base = _wresolve(path)
    if not base.exists():
        return {"path": "", "entries": []}
    if base.is_file():
        base = base.parent
    entries = [{"name": c.name, "dir": c.is_dir(),
                "path": str(c.relative_to(config.WORKSPACE)),
                "size": (c.stat().st_size if c.is_file() else 0)}
               for c in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))]
    rel = str(base.relative_to(config.WORKSPACE))
    return {"path": "" if rel == "." else rel, "entries": entries}


@app.get("/api/files/all")
def list_all_files():
    """Flat, recursive list of workspace files + dirs (relative posix paths), for the
    searchable file/folder pickers in the Workflows editor. Skips heavy/hidden dirs and
    caps the walk so a huge workspace can't stall the request."""
    base = config.WORKSPACE
    files, dirs, n = [], [], 0
    for root, ds, fs in os.walk(base):
        ds[:] = [d for d in ds if d not in _MTIME_SKIP_DIRS and not d.startswith(".")]
        relp = os.path.relpath(root, base)
        rel = "" if relp == "." else relp.replace(os.sep, "/")
        if rel:
            dirs.append(rel)
        for f in fs:
            files.append(f if not rel else rel + "/" + f)
            n += 1
            if n >= 4000:
                return {"files": sorted(files), "dirs": sorted(dirs), "capped": True}
    return {"files": sorted(files), "dirs": sorted(dirs)}


@app.get("/api/raw")
def raw_file(path: str):
    """Serve a workspace file with its real content-type (for images in chat, downloads)."""
    p = _wresolve(path)
    if not p.is_file():
        raise HTTPException(404, "not a file")
    return FileResponse(str(p))


# Folders never worth statting for app auto-reload — they're what blows a preview
# folder past the walk cap and hides the actual app files behind it.
_MTIME_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build"}


@app.get("/api/preview-mtime")
def preview_mtime(path: str):
    """Latest mtime among the files in the previewed app's folder. The Preview window
    polls this to auto-reload when the agent (or you) edits the app. Defined BEFORE the
    /api/preview/{path} catch-all so it isn't swallowed by it."""
    p = _wresolve(path)
    if not p.exists():
        return {"mtime": 0}         # deleted/renamed — never walk the whole workspace for it
    base = p.parent if p.is_file() else p
    latest, n = 0.0, 0
    if p.is_file():
        try:                        # the previewed file itself ALWAYS counts, cap or not —
            latest = p.stat().st_mtime   # edits to it must fire a reload even in a huge folder
        except OSError:
            pass
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _MTIME_SKIP_DIRS and not d.startswith(".")]
        for name in files:
            try:
                latest = max(latest, (Path(root) / name).stat().st_mtime)
            except OSError:
                pass
            n += 1
            if n >= 1000:           # cap the walk so a huge folder can't stall the poll
                return {"mtime": latest}
    return {"mtime": latest}


# Sandbox flags for previewed content — kept in sync with the iframe's sandbox attribute in
# app.js. Note the deliberate ABSENCE of allow-same-origin: that's what keeps the rendered
# page in an opaque origin so it can't reuse the session cookie against /api/*.
_PREVIEW_SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-pointer-lock"


# ---------------- artifact rendering (markdown / mermaid / chart / slides) ----------------
# The Preview iframe can render a handful of *source* artifact types — not just finished
# .html. We wrap the file in a self-contained page that pulls the renderer from /static/vendor
# (loads fine in the opaque sandbox — the CSP sandbox restricts the document's origin, not
# resource fetches) and decodes the file content from base64 (so nothing in it can break out
# of the HTML/JS context). Same security headers as a plain preview apply.
def _artifact_kind(name):
    n = (name or "").lower()
    if n.endswith(".slides.md") or n.endswith(".slides"):
        return "slides"
    if n.endswith(".chart.json"):
        return "chart"
    if n.endswith((".mmd", ".mermaid")):
        return "mermaid"
    if n.endswith((".md", ".markdown")):
        return "markdown"
    return None


_ARTIFACT_BASE_CSS = """
  :root{color-scheme:dark}*{box-sizing:border-box}
  body{margin:0;background:#0b1620;color:#e6edf3;font:15px/1.65 'Hanken Grotesk',-apple-system,system-ui,sans-serif}
  ::selection{background:#1f6feb55}a{color:#58a6ff}
  .wrap{max-width:860px;margin:0 auto;padding:30px 28px 80px}
  pre{background:#0d1117;border:1px solid #1c2733;border-radius:10px;padding:14px 16px;overflow:auto}
  code{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:.92em}
  :not(pre)>code{background:#1c2733;padding:.1em .4em;border-radius:5px}
  table{border-collapse:collapse;width:100%;margin:1em 0}
  th,td{border:1px solid #1c2733;padding:7px 11px;text-align:left}
  th{background:#101c27}
  blockquote{border-left:3px solid #2b7a78;margin:1em 0;padding:.2em 1em;color:#9fb3c8}
  img{max-width:100%;border-radius:8px}
  h1,h2,h3{font-family:'Fraunces',Georgia,serif;line-height:1.2}
  h1{font-size:2em}h2{font-size:1.5em;border-bottom:1px solid #1c2733;padding-bottom:.2em}
  hr{border:none;border-top:1px solid #1c2733;margin:2em 0}
  .art-err{color:#ff7b72;padding:22px;font-family:'JetBrains Mono',monospace;white-space:pre-wrap}
"""

_TPL_MARKDOWN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/vendor/atom-one-dark.min.css">
<style>__CSS__</style>
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script>
<script src="/static/vendor/highlight.min.js"></script></head>
<body><article class="wrap" id="doc"></article><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
try{marked.setOptions({gfm:true,breaks:false});
  document.getElementById('doc').innerHTML=DOMPurify.sanitize(marked.parse(RAW));
  document.querySelectorAll('pre code').forEach(b=>{try{hljs.highlightElement(b)}catch(e){}});
}catch(e){document.getElementById('doc').innerHTML='<div class="art-err">'+e+'</div>';}
</script></body></html>"""

_TPL_MERMAID = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__ .wrap{text-align:center}.mermaid{visibility:hidden}</style>
<script src="/static/vendor/mermaid.min.js"></script></head>
<body><div class="wrap"><pre class="mermaid" id="m"></pre></div><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
const el=document.getElementById('m');el.textContent=RAW;
try{mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'strict'});
  mermaid.run({nodes:[el]}).then(()=>{el.style.visibility='visible'})
   .catch(e=>{el.outerHTML='<div class="art-err">'+e+'</div>';});
}catch(e){el.outerHTML='<div class="art-err">'+e+'</div>';}
</script></body></html>"""

_TPL_CHART = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__ .wrap{max-width:780px;padding-top:40px}</style>
<script src="/static/vendor/chart.umd.min.js"></script></head>
<body><div class="wrap"><canvas id="c"></canvas><div class="art-err" id="err"></div></div><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
try{const cfg=JSON.parse(RAW);
  Chart.defaults.color='#9fb3c8';Chart.defaults.borderColor='#1c2733';
  Chart.defaults.font.family="'Hanken Grotesk',sans-serif";
  new Chart(document.getElementById('c'),cfg);
}catch(e){document.getElementById('err').textContent='Invalid chart spec (expects a Chart.js config JSON): '+e;}
</script></body></html>"""

_TPL_SLIDES = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__
 html,body{height:100%;overflow:hidden;background:#070f17}
 #deck{height:100vh;display:flex;align-items:center;justify-content:center;padding:6vh 8vw;cursor:pointer}
 .slide{max-width:920px;width:100%;animation:fade .25s ease}
 .slide h1{font-size:2.7em;margin-top:0}.slide h2{border:none}
 #hud{position:fixed;bottom:14px;right:18px;font:13px/1 'JetBrains Mono',monospace;color:#5b7287}
 #hint{position:fixed;bottom:14px;left:18px;font:12px 'JetBrains Mono',monospace;color:#3d4f5e}
 @keyframes fade{from{opacity:0;transform:translateY(7px)}to{opacity:1}}</style>
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script></head>
<body><div id="deck"></div><div id="hud"></div><div id="hint">← → / space · click to advance</div><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
const slides=RAW.split(/\\n-{3,}\\s*\\n/).map(s=>s.trim()).filter(Boolean);
let i=0;const deck=document.getElementById('deck'),hud=document.getElementById('hud');
function render(){deck.innerHTML='<section class="slide">'+DOMPurify.sanitize(marked.parse(slides[i]||'*empty deck*'))+'</section>';hud.textContent=(i+1)+' / '+Math.max(slides.length,1);}
function go(d){const n=Math.min(Math.max(i+d,0),slides.length-1);if(n!==i){i=n;render();}}
addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){e.preventDefault();go(1);}else if(e.key==='ArrowLeft'||e.key==='PageUp'){go(-1);}else if(e.key==='Home'){i=0;render();}else if(e.key==='End'){i=slides.length-1;render();}});
deck.addEventListener('click',e=>go(e.clientX<innerWidth*0.25?-1:1));
render();</script></body></html>"""

_ARTIFACT_TEMPLATES = {"markdown": _TPL_MARKDOWN, "mermaid": _TPL_MERMAID,
                       "chart": _TPL_CHART, "slides": _TPL_SLIDES}


def _artifact_html(kind, raw):
    b64 = base64.b64encode(raw.encode("utf-8")).decode()
    return _ARTIFACT_TEMPLATES[kind].replace("__CSS__", _ARTIFACT_BASE_CSS).replace("__B64__", b64)


# ---------------- preview capability tokens ----------------
# The Preview iframe is sandboxed WITHOUT allow-same-origin (see _PREVIEW_SANDBOX), so the rendered
# page sits in an opaque origin and can't send the SameSite=Lax session cookie — that's what stops a
# previewed page from calling /api/* with your session. The flip side: the page's OWN relative
# assets (./style.css, ./app.js), if served from the cookie-gated /api/preview/* path, would 401 —
# the opaque-origin sub-requests carry no cookie. So multi-file previews load through
# /preview/<token>/… instead: an unguessable, time-boxed, read-only capability minted by a logged-in
# user (via /api/preview-token) and confined to the previewed file's directory subtree. No cookie is
# involved, so the /api/* containment is fully preserved.
_PREVIEW_TOKEN_TTL = 12 * 3600        # long enough to outlast an editing session; re-minted on reload


def _preview_root(path):
    """The directory subtree a token authorizes: the previewed file's parent as a
    workspace-relative POSIX string ('' = workspace root)."""
    rel = (path or "").replace("\\", "/").strip("/")
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _make_preview_token(root, secret):
    # "prev:" domain tag (see _make_token) — keeps this capability token distinct from a session
    # cookie even though both are HMAC'd with the same auth secret in the same base64 envelope.
    msg = f"{root}:{int(time.time())}"
    sig = hmac.new(secret.encode(), f"prev:{msg}".encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}:{sig}".encode()).decode()


def _preview_token_root(token, secret):
    """Authorized root for a valid, unexpired token, else None. HMAC-signed, so root can't be
    tampered without the secret."""
    try:
        root, ts, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit(":", 2)
        good = hmac.new(secret.encode(), f"prev:{root}:{ts}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        if time.time() - int(ts) > _PREVIEW_TOKEN_TTL:
            return None
        return root
    except Exception:
        return None


def _serve_preview(p):
    """Build the sandboxed response for an already-resolved workspace path. Renders artifact source
    (.md/.mmd/.chart.json/.slides) to HTML; serves everything else raw. The CSP `sandbox` (no
    allow-same-origin) forces an opaque origin HOWEVER the response is loaded — so even opened
    directly (new tab, window.open, a crafted link) it can't act with the session. nosniff stops
    MIME confusion; no-store keeps auto-reload fetching fresh."""
    if p.is_dir():
        p = p / "index.html"
    if not p.is_file():
        raise HTTPException(404, "not found")
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"sandbox {_PREVIEW_SANDBOX}",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",       # keep the capability token out of the Referer header
    }
    kind = _artifact_kind(p.name)               # .md/.mmd/.chart.json/.slides → render, not raw
    if kind:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(500, str(e))
        return HTMLResponse(_artifact_html(kind, raw), headers=headers)
    return FileResponse(str(p), headers=headers)


@app.get("/api/preview-token")
def preview_token(path: str):
    """Mint a capability token scoped to the previewed file's folder. Cookie-gated by the
    middleware, so only a logged-in user can mint one; the token then rides in the
    /preview/<token>/… URL the sandboxed iframe loads."""
    _wresolve(path)                              # 400s if the path escapes the workspace
    root = _preview_root(path)
    secret = load().get("auth", {}).get("secret", "")
    return {"token": _make_preview_token(root, secret), "root": root}


@app.get("/api/preview/{path:path}")
def preview_file(path: str):
    """Cookie-gated single-file preview — the iframe's top-level navigation carries the Lax cookie,
    so this still works for a lone .html/.md/etc. A multi-file app whose relative assets must load
    goes through /preview/<token>/… instead (see the preview-capability note above)."""
    return _serve_preview(_wresolve(path))


@app.get("/preview/{token}/{path:path}")
def preview_capability(token: str, path: str):
    """Token-authed preview — deliberately NOT under /api/, so the middleware doesn't demand the
    cookie the sandboxed (opaque-origin) iframe can't send. Validates the capability token and
    confines the request to its authorized directory subtree (read-only). Same sandbox/nosniff/
    no-store headers as the cookie-gated route. NOTE: a token minted for a workspace-root file
    authorizes the whole workspace-root subtree — keep multi-file apps in their own folder for a
    tighter scope."""
    secret = load().get("auth", {}).get("secret", "")
    root = _preview_token_root(token, secret)
    if root is None:
        raise HTTPException(403, "invalid or expired preview token")
    p = _wresolve(path)
    if not p.is_relative_to(_wresolve(root)):    # confine to the token's subtree
        raise HTTPException(403, "path outside preview scope")
    return _serve_preview(p)


@app.get("/api/file")
def read_file_api(path: str):
    p = _wresolve(path)
    if not p.is_file():
        raise HTTPException(404, "not a file")
    try:
        return {"path": path, "content": p.read_text(encoding="utf-8")}
    except (UnicodeDecodeError, ValueError):
        return {"path": path, "content": None, "binary": True}


@app.post("/api/file")
async def write_file_api(req: Request):
    b = await req.json()
    p = _wresolve(b["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(b.get("content", ""), encoding="utf-8")
    return {"ok": True}


@app.delete("/api/file")
def delete_file_api(path: str):
    p = _wresolve(path)
    if p == config.WORKSPACE:        # an empty/'.' path resolves to the root — don't rmtree everything
        raise HTTPException(400, "refusing to delete the workspace root")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.is_file():
        p.unlink()
    return {"ok": True}


@app.post("/api/folder")
async def make_folder_api(req: Request):
    p = _wresolve((await req.json())["path"])
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@app.post("/api/rename")
async def rename_api(req: Request):
    b = await req.json()
    src, dst = _wresolve(b["path"]), _wresolve(b["to"])
    if config.WORKSPACE in (src, dst):               # never move/clobber the workspace root itself
        raise HTTPException(400, "refusing to move the workspace root")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True}


@app.post("/api/browser/go")
async def browser_go(req: Request):
    """User-driven navigation for the Live browser window (shared session)."""
    url = (await req.json()).get("url", "").strip()
    if not url:
        return {"ok": False, "error": "no url"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    refusal = safety.check_url(url)
    if refusal:
        return {"ok": False, "error": refusal}
    livebrowser.submit("navigate", url)              # fire-and-forget; result shows in the stream
    return {"ok": True, "url": url}


@app.post("/api/browser/click")
async def browser_click_ep(req: Request):
    b = await req.json()
    livebrowser.submit("click", (b["x"], b["y"]))
    return {"ok": True}


@app.post("/api/browser/scroll")
async def browser_scroll_ep(req: Request):
    livebrowser.submit("scroll", (await req.json()).get("dy", 300))
    return {"ok": True}


@app.post("/api/browser/type")
async def browser_type_ep(req: Request):
    livebrowser.submit("type", (await req.json()).get("text", ""))
    return {"ok": True}


@app.post("/api/browser/key")
async def browser_key_ep(req: Request):
    livebrowser.submit("key", (await req.json()).get("key", ""))
    return {"ok": True}


@app.post("/api/browser/tab")
async def browser_tab_switch(req: Request):
    livebrowser.submit("switch_tab", (await req.json()).get("id"))
    return {"ok": True}


@app.post("/api/browser/tab/close")
async def browser_tab_close(req: Request):
    livebrowser.submit("close_tab", (await req.json()).get("id"))
    return {"ok": True}


# ---------------- scheduler ----------------
@app.get("/api/scheduler")
def get_scheduler():
    import time as _t
    lb = scheduler.last_beat()
    return {"beat_ago": (_t.time() - lb) if lb else None, "tasks": scheduler.all_tasks()}


@app.post("/api/tasks")
async def add_task_api(req: Request):
    b = await req.json()
    tid = scheduler.add_task(b["cron"], b["instruction"])
    return {"ok": tid is not None, "id": tid}


@app.patch("/api/tasks/{tid}")
async def update_task_api(tid: int, req: Request):
    b = await req.json()
    ok = scheduler.update_task(tid, b.get("cron"), b.get("instruction"), b.get("enabled"))
    return {"ok": ok, **({} if ok else {"error": "invalid cron expression (format: min hr day mon wkday)"})}


@app.delete("/api/tasks/{tid}")
def delete_task_api(tid: int):
    ok = scheduler.delete_task(tid)
    return {"ok": ok, **({} if ok else {"error": "this entry is managed by the Researcher — delete the topic there"})}


@app.post("/api/tasks/{tid}/run")
async def run_task_api(tid: int):
    """Run a scheduled task right now, on demand. Off the event loop — a task can block
    (it may call the model, delegate, or run a workflow)."""
    return await asyncio.to_thread(scheduler.run_task, tid)


# ---------------- calendar (local copy, synced from ICS feeds) ----------------
@app.get("/api/calendar")
def get_calendar(days: int = 30, start: str = "", end: str = ""):
    # start+end (YYYY-MM-DD) → the month/week/day grid asks for an explicit range; otherwise
    # fall back to "next N days from today" (the agenda).
    events = calsync.range_events(start, end) if (start and end) else calsync.upcoming(max(1, min(days, 365)))
    return {"feeds": calsync.feeds(), "events": events}


@app.post("/api/calendar/feeds")
async def add_calendar_feed(req: Request):
    b = await req.json()
    refusal = safety.check_url((b.get("url") or "").strip().replace("webcal://", "https://", 1))
    if refusal:
        return {"ok": False, "error": refusal}
    fid = calsync.add_feed(b.get("name", ""), b.get("url", ""))
    if fid is None:
        return {"ok": False, "error": "invalid URL — paste the calendar's secret .ics address"}
    result = await asyncio.to_thread(calsync.sync_feed, fid)   # first sync right away
    return {"ok": True, "id": fid, "sync": result}


@app.delete("/api/calendar/feeds/{fid}")
def delete_calendar_feed(fid: int):
    return {"ok": calsync.delete_feed(fid)}


@app.post("/api/calendar/sync")
async def sync_calendar():
    results = await asyncio.to_thread(calsync.sync_all)
    return {"ok": all(r.get("ok") for r in results.values()) if results else True, "results": results}


# ---- local events: the editable layer (synced feed events stay read-only) ----
@app.post("/api/calendar/events")
async def add_calendar_event_api(req: Request):
    b = await req.json()
    return calsync.add_event(b.get("title", ""), b.get("start", ""), end=b.get("end"),
                             all_day=bool(b.get("all_day")), location=b.get("location", ""),
                             description=b.get("description", ""), category=b.get("category", ""))


@app.put("/api/calendar/events/{eid}")
async def update_calendar_event_api(eid: int, req: Request):
    b = await req.json()
    # only override fields the client actually sent (so omitted ones aren't wiped); `end`
    # uses calsync's sentinel default so it's only touched when present in the body.
    kw = {k: b[k] for k in ("title", "start", "all_day", "location", "description", "category") if k in b}
    if "end" in b:
        kw["end"] = b["end"]
    return calsync.update_event(eid, **kw)


@app.delete("/api/calendar/events/{eid}")
def delete_calendar_event_api(eid: int):
    return calsync.delete_event(eid)


# ---------------- researcher (scheduled deep-dives → living docs) -------------
@app.get("/api/research")
def get_research():
    return researcher.all_topics()


@app.post("/api/research")
async def add_research(req: Request):
    b = await req.json()
    rid = researcher.add_topic(b.get("topic", ""), b.get("focus", ""), b.get("cron", "0 8 * * *"))
    return {"ok": rid is not None, "id": rid,
            **({} if rid is not None else {"error": "topic and a valid cron are required"})}


@app.patch("/api/research/{rid}")
async def update_research(rid: int, req: Request):
    b = await req.json()
    ok = researcher.update_topic(rid, b.get("topic"), b.get("focus"), b.get("cron"), b.get("enabled"))
    return {"ok": ok}


@app.delete("/api/research/{rid}")
def delete_research(rid: int):
    return {"ok": researcher.delete_topic(rid)}


@app.post("/api/research/{rid}/run")
def run_research_now(rid: int):
    researcher.run_topic_bg(rid)        # long-running — fire in the background
    return {"ok": True, "started": True}


@app.get("/api/browser/stream")
async def browser_stream():
    """Live JPEG frames of the agent's headless browser (the 'what Oceano sees' window)."""
    async def gen():
        last_v, last_tabs, idle = -1, None, 0
        while True:
            L = livebrowser.LATEST
            v, tabs = L["v"], L.get("tabs", [])
            tabs_sig = json.dumps([[t["id"], t["url"], t["active"], t["title"]] for t in tabs])
            if v != last_v and L["frame"]:
                last_v, last_tabs, idle = v, tabs_sig, 0
                b64 = base64.b64encode(L["frame"]).decode()
                yield _sse({"url": L["url"], "frame": "data:image/jpeg;base64," + b64, "tabs": tabs})
            elif tabs_sig != last_tabs:
                last_tabs, idle = tabs_sig, 0    # tabs changed without a new frame → push the tab bar
                yield _sse({"url": L["url"], "tabs": tabs})
            else:
                idle += 1
                if idle >= 50:          # ~5s keepalive when idle
                    idle = 0
                    yield ": ka\n\n"
            await asyncio.sleep(0.1)     # ~10 fps relay
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.get("/api/ui/stream")
async def ui_stream():
    """Server→browser UI commands (the agent's ui_open/ui_close/ui_arrange land here). Auth-gated by
    the middleware; the browser holds this open and executes whatever the agent pushes."""
    loop = asyncio.get_running_loop()
    q = uibridge.subscribe(loop)

    async def gen():
        try:
            while True:
                try:
                    cmd = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ka\n\n"          # keep-alive
                    continue
                yield _sse(cmd)
        finally:
            uibridge.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    host = os.environ.get("OCEANO_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("OCEANO_WEB_PORT", "8800"))
    print(f"Oceano web UI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
