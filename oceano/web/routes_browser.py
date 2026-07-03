"""Live-browser routes: user-driven control of the shared browser session
(navigate / click / type / drag / clipboard / tabs / resize), its settings,
the frame streams (SSE + WebSocket), and the server→browser UI command stream."""
import asyncio
import base64
import json

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import StreamingResponse

from oceano import desktopbridge, livebrowser, safety, uibridge
from oceano.web.state import SESSION_COOKIE, _sse, _token_user, load, save

router = APIRouter()


@router.post("/api/browser/go")
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


@router.post("/api/browser/click")
async def browser_click_ep(req: Request):
    b = await req.json()
    livebrowser.submit("click", (b["x"], b["y"]))
    return {"ok": True}


@router.post("/api/browser/scroll")
async def browser_scroll_ep(req: Request):
    livebrowser.submit("scroll", (await req.json()).get("dy", 300))
    return {"ok": True}


# Live drag: press → move → release, streamed as the user drags, so they can solve slider /
# drag-to-verify captchas and bot checks by hand (the movement IS their real mouse path).
@router.post("/api/browser/mousedown")
async def browser_mousedown_ep(req: Request):
    b = await req.json()
    try:
        livebrowser.submit("mousedown", (int(b["x"]), int(b["y"])))
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "x,y required"}
    return {"ok": True}


@router.post("/api/browser/mousemove")
async def browser_mousemove_ep(req: Request):
    b = await req.json()
    try:
        livebrowser.submit("mousemove", (int(b["x"]), int(b["y"])))
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "x,y required"}
    return {"ok": True}


@router.post("/api/browser/mouseup")
async def browser_mouseup_ep(req: Request):
    b = await req.json()
    try:
        arg = (int(b["x"]), int(b["y"])) if ("x" in b and "y" in b) else None
    except (TypeError, ValueError):
        arg = None
    livebrowser.submit("mouseup", arg)
    return {"ok": True}


@router.post("/api/browser/drag")
async def browser_drag_ep(req: Request):
    """A whole drag gesture in one call — a path of [x,y] points (viewport coords)."""
    b = await req.json()
    pts = [(int(p[0]), int(p[1])) for p in (b.get("path") or [])
           if isinstance(p, (list, tuple)) and len(p) >= 2][:200]
    if len(pts) >= 2:
        livebrowser.submit("drag", pts)
    return {"ok": len(pts) >= 2}


@router.post("/api/browser/type")
async def browser_type_ep(req: Request):
    livebrowser.submit("type", (await req.json()).get("text", ""))
    return {"ok": True}


@router.post("/api/browser/key")
async def browser_key_ep(req: Request):
    livebrowser.submit("key", (await req.json()).get("key", ""))
    return {"ok": True}


@router.post("/api/browser/paste")
async def browser_paste_ep(req: Request):
    """Insert the user's local clipboard text into the page's focused field (clipboard bridge in)."""
    livebrowser.submit("paste", (await req.json()).get("text", ""))
    return {"ok": True}


@router.post("/api/browser/copy")
async def browser_copy_ep():
    """Return the page's current text selection so the client can put it on the local clipboard (out)."""
    res = livebrowser.submit("copy", wait=True)
    return {"ok": True, "text": (res or {}).get("text", "")}


@router.post("/api/browser/tab")
async def browser_tab_switch(req: Request):
    livebrowser.submit("switch_tab", (await req.json()).get("id"))
    return {"ok": True}


@router.post("/api/browser/tab/close")
async def browser_tab_close(req: Request):
    livebrowser.submit("close_tab", (await req.json()).get("id"))
    return {"ok": True}


@router.post("/api/browser/back")
async def browser_back_ep():
    livebrowser.submit("back")
    return {"ok": True}


@router.post("/api/browser/forward")
async def browser_forward_ep():
    livebrowser.submit("forward")
    return {"ok": True}


@router.post("/api/browser/reload")
async def browser_reload_ep():
    livebrowser.submit("reload")
    return {"ok": True}


@router.post("/api/browser/stop")
async def browser_stop_ep():
    livebrowser.submit("stop")
    return {"ok": True}


@router.post("/api/browser/newtab")
async def browser_newtab_ep():
    livebrowser.submit("new_tab")
    return {"ok": True}


@router.post("/api/browser/resize")
async def browser_resize_ep(req: Request):
    """Match the browser viewport to the LIVE window size (responsive layout, no letterbox)."""
    b = await req.json()
    try:
        livebrowser.submit("resize", (int(b["width"]), int(b["height"])))
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "width,height required"}
    return {"ok": True}


@router.get("/api/browser/settings")
def get_browser_settings():
    return {"real_chrome": bool(load().get("prefs", {}).get("real_chrome"))}


@router.post("/api/browser/settings")
async def set_browser_settings(req: Request):
    """Toggle whether the live browser drives a real, persistent Chrome vs the throwaway headless
    Chromium. Restarts the browser worker so the new mode takes effect on the next action."""
    b = await req.json()
    data = load()
    data.setdefault("prefs", {})["real_chrome"] = bool(b.get("real_chrome"))
    save(data)
    try:
        livebrowser.shutdown()      # drop the current browser; next navigation relaunches in the new mode
    except Exception:
        pass
    return {"ok": True, "real_chrome": data["prefs"]["real_chrome"]}


@router.get("/api/browser/stream")
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


@router.websocket("/api/browser/ws")
async def browser_ws(ws: WebSocket):
    """Live browser frames over a WebSocket: BINARY JPEG frames + TEXT metadata (url/tabs). The
    client renders frames via createImageBitmap→canvas — far lighter than the SSE+base64 data-URL
    fallback. Auth-gated here (the HTTP _require_auth middleware doesn't cover WS handshakes):
    same-origin (anti-CSWSH) + a valid session cookie, matching /api/terminal/ws."""
    host = ws.headers.get("host", "")
    origin = ws.headers.get("origin", "")
    if not host or origin not in (f"http://{host}", f"https://{host}"):
        await ws.close(code=1008)
        return
    if not _token_user(ws.cookies.get(SESSION_COOKIE, ""), load().get("auth", {})):
        await ws.close(code=1008)
        return
    await ws.accept()
    from starlette.websockets import WebSocketDisconnect
    last_v, last_tabs = -1, None
    try:
        while True:
            L = livebrowser.LATEST
            if L["v"] != last_v and L["frame"]:
                last_v = L["v"]
                await ws.send_bytes(L["frame"])               # raw JPEG — no base64 inflation
            tabs = L.get("tabs", [])
            tabs_sig = json.dumps([[t["id"], t["url"], t["active"], t["title"]] for t in tabs])
            if tabs_sig != last_tabs:
                last_tabs = tabs_sig
                await ws.send_text(json.dumps({"url": L["url"], "tabs": tabs}))
            await asyncio.sleep(0.03)                          # ~30fps poll of LATEST
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    try:
        await ws.close()
    except Exception:
        pass


@router.get("/api/ui/stream")
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


@router.get("/api/desktop/stream")
async def desktop_stream():
    """Server→OceanoDesktop native-action requests (oceano/tools/desktop.py's calls land here). Only
    OceanoDesktop's main process ever holds this open — it's the one place a native OS action
    (notification, file picker) can actually run. Auth-gated by the middleware like /api/ui/stream."""
    loop = asyncio.get_running_loop()
    q = desktopbridge.subscribe(loop)

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
            desktopbridge.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@router.post("/api/desktop/result")
async def desktop_result(req: Request):
    """OceanoDesktop's answer to a pending desktopbridge.call() — matched by the `id` it was given
    on /api/desktop/stream. A stray or duplicate post (e.g. after a timeout already gave up) is a
    harmless no-op, so this never needs to error the desktop app's side."""
    body = await req.json()
    rid = body.get("id")
    if rid is None:
        return {"ok": False, "error": "missing id"}
    matched = desktopbridge.resolve(rid, bool(body.get("ok")), body.get("result"))
    return {"ok": True, "matched": matched}
