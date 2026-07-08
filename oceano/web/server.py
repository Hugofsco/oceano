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
import os
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from oceano import evals, livebrowser, mcp_client, memory, skills
from oceano.web import telegram_runtime

# Shared state + helpers live in oceano.web.state; re-export the names the rest of the
# codebase (and the tests) reach via `oceano.web.server.X` so the split is invisible
# to them (delegate/scheduler/researcher → endpoint_key, telegram_bot → list_models,
# notifications/livebrowser → load, test_smoke → _TOOL_CATEGORY, …).
from oceano.web.state import (  # noqa: F401
    PROVIDERS,
    SESSION_COOKIE,
    SESSION_TTL,
    STATIC,
    STORE,
    _PUBLIC_API,
    _TOOL_CATEGORY,
    _agent,
    _apply_telegram,
    _current_user,
    _drop_session_state,
    _effective_model,
    _is_default_pw,
    _session_lock,
    _sessions,
    _set_session_cookie,
    _sse,
    _token_user,
    _wresolve,
    endpoint_key,
    list_models,
    load,
    save,
)


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
        skills.ensure_distill_task()  # …and its feeder: distill recent chats into learning skills
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
    try:
        from oceano import reflect
        reflect.ensure_task()              # the locked '[ SELF ]' nightly-reflection schedule
    except Exception:
        traceback.print_exc()
    yield
    await telegram_runtime.stop()
    try:
        await asyncio.to_thread(livebrowser.shutdown)   # close Chrome on its own thread
    except Exception:
        traceback.print_exc()


app = FastAPI(title="Oceano", lifespan=lifespan)


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    webhook = path.startswith("/api/workflows/") and "/webhook/" in path   # gated by its secret token
    # Only the mind-bridge routes are gated by the mind token (_mcp_authed() in routes_delegate.py) —
    # every other /api/mcp/* route (server registration, presets) needs the normal session cookie
    # like any other authenticated endpoint.
    mcp = path in ("/api/mcp/tools", "/api/mcp/call")
    if path.startswith("/api/") and path not in _PUBLIC_API and not webhook and not mcp:
        auth = load().get("auth", {})
        if not _token_user(request.cookies.get(SESSION_COOKIE, ""), auth):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        # While the password is still the shipped default, confine EVERY session to the
        # change-password call — the API enforces this, not just the UI gate.
        if _is_default_pw(auth) and path != "/api/account":
            return JSONResponse({"error": "set a non-default password first"}, status_code=403)
    return await call_next(request)


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


# The route handlers live in domain routers (cut along this file's old section banners).
# They import shared state from oceano.web.state — not from this module — so including
# them here creates no import cycle. The _require_auth middleware above applies app-wide,
# so every routed path stays behind it exactly as before the split.
from oceano.web import (  # noqa: E402
    routes_auth,
    routes_brain,
    routes_browser,
    routes_chat,
    routes_content,
    routes_delegate,
    routes_files,
    routes_mail,
    routes_mcp,
    routes_ops,
    routes_system,
)

app.include_router(routes_auth.router)
app.include_router(routes_system.router)
app.include_router(routes_delegate.router)
app.include_router(routes_brain.router)
app.include_router(routes_chat.router)
app.include_router(routes_content.router)
app.include_router(routes_files.router)
app.include_router(routes_browser.router)
app.include_router(routes_mail.router)
app.include_router(routes_mcp.router)
app.include_router(routes_ops.router)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    host = os.environ.get("OCEANO_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("OCEANO_WEB_PORT", "8800"))
    print(f"Oceano web UI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
