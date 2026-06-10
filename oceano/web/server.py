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
import json
import os
import shutil
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
from oceano import browser, livebrowser, memory, safety, scheduler, skills
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


def load():
    if STORE.exists():
        data = json.loads(STORE.read_text())
        if "telegram" not in data:          # migrate older stores in place
            data["telegram"] = _telegram_seed()
            save(data)
        return data
    seed = {"endpoints": [{"name": "Local (llama.cpp)",
                           "base_url": "http://127.0.0.1:8081/v1", "api_key": ""}],
            "prefs": {"agent_mode": False},
            "telegram": _telegram_seed()}
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
    yield
    await telegram_runtime.stop()


app = FastAPI(title="Oceano", lifespan=lifespan)
_sessions = {}  # session_id -> Agent


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


@app.get("/api/status")
def system_status():
    """Live state of the consolidated daemons, for the Settings → Services panel."""
    import time as _t
    try:                                        # embed server (:8082) reachable?
        from oceano.embeddings import EMBED_URL
        requests.get(EMBED_URL.rstrip("/") + "/models", timeout=2)
        embed_ok = True
    except requests.RequestException:
        embed_ok = False
    beat = scheduler.last_beat()
    return {"embed": embed_ok,
            "scheduler_beat_ago": (_t.time() - beat) if beat else None,
            "telegram": telegram_runtime.status()}


@app.get("/api/models")
def models():
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

    def worker():
        try:
            stream = ag.run_stream(message) if agent_mode else ag.chat_stream(message)
            for ev in stream:
                put(ev)
            put({"type": "done"})
        except Exception as ex:
            traceback.print_exc()   # so it actually lands in the journal, not just the UI
            put({"type": "error", "message": f"{type(ex).__name__}: {ex}"})
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
        except Exception:
            # never let the response generator die silently — log it and try to
            # send a clean error frame so the client shows a real message.
            traceback.print_exc()
            try:
                yield _sse({"type": "error", "message": "stream closed unexpectedly (see server logs)"})
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.delete("/api/session/{sid}")
def end_session(sid: str):
    _sessions.pop(sid, None)
    return {"ok": True}


# ---------------- skills ----------------
@app.get("/api/skills")
def get_skills():
    return skills.all_skills()


@app.post("/api/skills")
async def post_skill(req: Request):
    b = await req.json()
    slug = skills.save_skill(b["name"], b.get("description", ""), b.get("body", ""), b.get("dir"))
    return {"ok": True, "dir": slug}


@app.delete("/api/skills/{dir}")
def remove_skill(dir: str):
    return {"ok": skills.delete_skill(dir)}


# ---------------- memories ----------------
@app.get("/api/memories")
def get_memories():
    return memory.list_all()


@app.post("/api/memories")
async def post_memory(req: Request):
    b = await req.json()
    memory.remember(b["text"], b.get("tags", ""))
    return {"ok": True}


@app.delete("/api/memories/{mid}")
def remove_memory(mid: int):
    return {"ok": memory.forget(mid)}


# ---------------- workspace files (fenced) ----------------
def _wresolve(path):
    p = (config.WORKSPACE / (path or "")).resolve()
    if not str(p).startswith(str(config.WORKSPACE)):
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
    scheduler.update_task(tid, b.get("cron"), b.get("instruction"), b.get("enabled"))
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
def delete_task_api(tid: int):
    scheduler.delete_task(tid)
    return {"ok": True}


@app.get("/api/browser/stream")
async def browser_stream():
    """Live JPEG frames of the agent's headless browser (the 'what Oceano sees' window)."""
    async def gen():
        last_v, idle = -1, 0
        while True:
            v = livebrowser.LATEST["v"]
            if v != last_v and livebrowser.LATEST["frame"]:
                last_v, idle = v, 0
                b64 = base64.b64encode(livebrowser.LATEST["frame"]).decode()
                yield _sse({"url": livebrowser.LATEST["url"], "frame": "data:image/jpeg;base64," + b64})
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
