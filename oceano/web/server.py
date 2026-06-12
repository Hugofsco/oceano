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
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
from oceano import browser, calsync, chats, evals, researcher, rivers, embeddings, livebrowser, mcp_client, memory, rag, safety, scheduler, skills
from oceano.agent import Agent
from oceano.web import telegram_runtime

STATIC = Path(__file__).parent / "static"
STORE = config.WORKSPACE.parent / "data" / "web.json"

PROVIDERS = [
    {"name": "Local (llama.cpp)", "base_url": "http://127.0.0.1:8081/v1", "needs_key": False},
    {"name": "OpenAI",     "base_url": "https://api.openai.com/v1",       "needs_key": True},
    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",    "needs_key": True},
    {"name": "Groq",       "base_url": "https://api.groq.com/openai/v1",  "needs_key": True},
    {"name": "Together",   "base_url": "https://api.together.xyz/v1",     "needs_key": True},
    {"name": "DeepSeek",   "base_url": "https://api.deepseek.com/v1",     "needs_key": True},
    {"name": "Mistral",    "base_url": "https://api.mistral.ai/v1",       "needs_key": True},
]


def _telegram_seed():
    """Default Telegram block, seeded from oceano.env so existing setups keep working."""
    return {"enabled": bool(config.TELEGRAM_TOKEN),
            "token": config.TELEGRAM_TOKEN,
            "allowed": sorted(config.TELEGRAM_ALLOWED)}


def _hash_pw(password, salt):
    """PBKDF2-SHA256 — stdlib only, no bcrypt/passlib dependency."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


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
    STORE.write_text(json.dumps(data, indent=2))
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
    yield
    await telegram_runtime.stop()
    try:
        await asyncio.to_thread(livebrowser.shutdown)   # close Chrome on its own thread
    except Exception:
        traceback.print_exc()


app = FastAPI(title="Oceano", lifespan=lifespan)
_sessions = {}  # session_id -> Agent
_cancels = {}   # session_id -> threading.Event (set to abort an in-flight query)

SESSION_COOKIE = "oceano_sess"
SESSION_TTL = 30 * 24 * 3600        # 30 days
# /api paths reachable without a session (everything else under /api is gated).
_PUBLIC_API = {"/api/login", "/api/me"}


def _make_token(user, secret):
    msg = f"{user}:{int(time.time())}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}:{sig}".encode()).decode()


def _token_user(token, auth):
    """Return the username a cookie authenticates, or None if invalid/expired."""
    try:
        user, ts, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit(":", 2)
        good = hmac.new(auth["secret"].encode(), f"{user}:{ts}".encode(), hashlib.sha256).hexdigest()
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
    if path.startswith("/api/") and path not in _PUBLIC_API:
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
    auth = load().get("auth", {})
    user = (body.get("user") or "").strip()
    pw = body.get("password") or ""
    ok = (user == auth.get("user")
          and hmac.compare_digest(_hash_pw(pw, auth["salt"]), auth["pwhash"]))
    if not ok:
        raise HTTPException(401, "invalid username or password")
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


def _agent(sid):
    if sid not in _sessions:
        _sessions[sid] = Agent()
    return _sessions[sid]


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


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


@app.get("/api/status")
def system_status():
    """Live state of the consolidated daemons, for the Settings → Services panel."""
    beat = scheduler.last_beat()
    return {"embed": _embed_reachable(),
            "scheduler_beat_ago": (time.time() - beat) if beat else None,
            "telegram": telegram_runtime.status()}


# ---------------- agent tools (read-only list for Settings → Tools) ----------
_TOOL_CATEGORY = {
    "list_files": "workspace", "read_file": "workspace", "write_file": "workspace",
    "edit_file": "workspace", "make_folder": "workspace", "run_shell": "workspace",
    "python_exec": "workspace",
    "web_search": "web", "fetch_url": "web",
    "browser_open": "browser", "browser_screenshot": "browser",
    "browser_click": "browser", "browser_scroll": "browser",
    "remember": "memory", "recall": "memory", "update_memory": "memory", "forget_memory": "memory",
    "index_docs": "documents", "search_docs": "documents",
    "list_skills": "skills", "load_skill": "skills", "learn_skill": "skills",
    "delegate_to_claude": "delegate",
    "schedule_task": "scheduler", "list_tasks": "scheduler", "notify": "scheduler",
    "calendar_events": "calendar",
}


@app.get("/api/tools")
def list_tools():
    """Each agent tool with its verifiable capability surface — the parameters it
    actually accepts (read straight from the registered JSON schema)."""
    from oceano import tools
    out = []
    for s in tools.schemas():
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
            "params": [{"name": k, "type": v.get("type", "any"),
                        "required": k in required, "description": v.get("description", "")}
                       for k, v in props.items()],
        })
    return out


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
    """Semantic search over memories or indexed docs (uses the embedding engine)."""
    b = await request.json()
    query = (b.get("query") or "").strip()
    scope = b.get("scope", "memory")
    if not query:
        return {"results": []}
    fn = memory.search if scope == "memory" else rag.search
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


@app.post("/api/rivers/serve")
async def rivers_serve(request: Request):
    b = await request.json()
    return rivers.serve(b.get("filename", ""), b.get("name"),
                          b.get("ngl", 99), b.get("ctx", 8192),
                          fa=b.get("fa", True), kv=b.get("kv", "f16"), ttl=b.get("ttl", 600))


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
    base_url = body.get("base_url")
    data = load()
    api_key = next((e.get("api_key", "") for e in data["endpoints"]
                    if e["base_url"] == base_url), "")

    ag = _agent(sid)
    ag.model = body.get("model") or ag.model
    ag.base_url = base_url
    ag.api_key = api_key
    agent_mode = bool(body.get("agent_mode"))

    # The agent is blocking (a single LLM step or a slow tool can take 20s+ with no
    # output). Run it in a worker thread and feed events through a queue, so the
    # response generator can emit a keep-alive during any silent gap — otherwise an
    # idle proxy / VS Code port-forward / Tailscale hop drops the stream.
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    put = lambda ev: loop.call_soon_threadsafe(q.put_nowait, ev)

    cancel = threading.Event()      # set by /api/chat/stop OR a client disconnect
    _cancels[sid] = cancel

    def worker():
        stream = None
        try:
            stream = ag.run_stream(message) if agent_mode else ag.chat_stream(message)
            for ev in stream:
                if cancel.is_set():
                    break               # stop feeding — query was aborted
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
            cancel.set()                # client went away (aborted/closed) → stop the agent
            raise
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


@app.delete("/api/session/{sid}")
def end_session(sid: str):
    _sessions.pop(sid, None)
    return {"ok": True}


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
    ok = chats.save(cid, b.get("title", ""), b.get("messages", []), b.get("created"))
    return {"ok": ok}


@app.delete("/api/chats/{cid}")
def chats_delete(cid: str):
    _sessions.pop(cid, None)        # also free the in-memory Agent
    _cancels.pop(cid, None)
    return {"ok": chats.delete(cid)}


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
    return {"models": evals.available_models()}


@app.post("/api/evals/run")
async def evals_run(req: Request):
    if evals.state()["running"]:
        return {"ok": False, "running": True, "error": "an eval run is already in progress"}
    b = await req.json()
    evals.run_all_bg(b.get("models") or None)
    return {"ok": True, "running": True}


@app.get("/api/evals/state")
def evals_state():
    return evals.state()


@app.get("/api/evals/leaderboard")
def evals_leaderboard(run_id: int = None):
    return evals.leaderboard(run_id)


@app.get("/api/evals/runs")
def evals_runs():
    return {"runs": evals.runs()}


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


# ---------------- memory injection policy (Settings → Memory) ----------------
@app.get("/api/memory/policy")
def get_memory_policy():
    return {"policy": memory.get_policy(), "categories": memory.CATEGORIES}


@app.post("/api/memory/policy")
async def set_memory_policy(req: Request):
    return {"ok": True, "policy": memory.set_policy(await req.json())}


# ---------------- workspace files (fenced) ----------------
def _wresolve(path):
    p = (config.WORKSPACE / (path or "")).resolve()
    # is_relative_to, not startswith: a prefix match lets a sibling like
    # '<workspace>-evil' escape the fence. config.WORKSPACE is already resolved.
    if not p.is_relative_to(config.WORKSPACE):
        raise HTTPException(400, "path escapes workspace")
    return p


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


@app.get("/api/raw")
def raw_file(path: str):
    """Serve a workspace file with its real content-type (for images in chat, downloads)."""
    p = _wresolve(path)
    if not p.is_file():
        raise HTTPException(404, "not a file")
    return FileResponse(str(p))


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


# ---------------- calendar (local copy, synced from ICS feeds) ----------------
@app.get("/api/calendar")
def get_calendar(days: int = 30):
    return {"feeds": calsync.feeds(), "events": calsync.upcoming(max(1, min(days, 365)))}


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


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    host = os.environ.get("OCEANO_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("OCEANO_WEB_PORT", "8800"))
    print(f"Oceano web UI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
