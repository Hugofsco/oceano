# Live Browser: CDP screencast + WebSocket rendering

Design doc to replace the sluggish screenshot-polling pipeline with proper
push-based rendering. **Not yet implemented** — needs a host with a live Chromium
to verify (can't be tested in the headless CI/agent sandbox). Build it behind a
fallback so it degrades to today's behaviour.

## 1. Why it's slow today

Pipeline (current):

```
worker thread (livebrowser._worker)
   _CMD.get(timeout=0.15)  → on idle, grab(): page.screenshot(jpeg, q=55)   ~6 fps
        ↑ SAME THREAD also runs every click/scroll/drag → they block each other
   → LATEST["frame"] = raw JPEG bytes
server (/api/browser/stream, SSE)
   base64(frame) → "data:image/jpeg;base64,…"  → SSE text, asyncio.sleep(0.1) ~10 fps
client (app.js)
   EventSource.onmessage → img.src = dataURL   → full re-decode + repaint, main thread
```

Three compounding bottlenecks:

1. **Capture** — `page.screenshot()` is a synchronous full-viewport encode + CDP
   round-trip (50–200 ms), polled ~6 fps, **on the same thread as input**. This is
   the dominant lag (drags/scrolls stall while a frame encodes, and vice-versa).
2. **Transport** — base64 inflates each JPEG ~33 % and rides a text SSE stream.
3. **Render** — `img.src = dataURL` forces a main-thread decode + repaint per frame.

## 2. Target architecture

```
worker thread
   CDP: Page.startScreencast (jpeg) → Page.screencastFrame events PUSHED on visual change
        handler: LATEST["frame"] = b64decode(data); LATEST["v"]++ ; ack
        no synchronous screenshot() in the hot path → input thread stays free
server (/api/browser/ws, WebSocket)
   on LATEST["v"] change → ws.send_bytes(raw JPEG)        (binary, no base64)
   on tab/url change     → ws.send_text(JSON metadata)
client
   ws.onmessage(binary) → createImageBitmap(blob)  (off-main-thread decode)
                        → ctx.drawImage(bmp, …)    (cheap canvas blit)
```

WebSocket transport is already proven in this stack — the terminal uses
`/api/terminal/ws` (app.js ~L5103), so the infra (systemd 0.0.0.0 bind, any proxy)
already passes WS upgrades.

## 3. Capture side — `oceano/livebrowser.py`

### 3a. Start/stop screencast on the active page

CDP session is bound to a page; screencast only the ACTIVE tab and restart it on
switch/open/navigate. `startScreencast` emits an initial frame for the current
page state, which also fixes the "stale view after switching to an already-loaded
tab" concern in the module docstring.

```python
import base64

SCREENCAST = os.environ.get("OCEANO_BROWSER_SCREENCAST", "1") == "1"

def _start_screencast(page):
    """Best-effort CDP screencast on `page`. Returns the cdp session or None."""
    try:
        cdp = page.context.new_cdp_session(page)
    except Exception:
        return None

    def on_frame(params):
        try:
            LATEST["frame"] = base64.b64decode(params["data"])
            LATEST["v"] += 1
        except Exception:
            pass
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass

    cdp.on("Page.screencastFrame", on_frame)
    try:
        cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 55,
            "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
            "everyNthFrame": 1,
        })
        return cdp
    except Exception:
        return None

def _stop_screencast(cdp):
    if not cdp:
        return
    try: cdp.send("Page.stopScreencast")
    except Exception: pass
    try: cdp.detach()
    except Exception: pass
```

### 3b. Event pumping — the key subtlety

In **sync** Playwright, `screencastFrame` handlers only fire while the connection
is being pumped, i.e. during a Playwright call. The current loop blocks on
`_CMD.get(timeout=0.15)` — a *Python queue*, which does **not** pump Playwright, so
frames would never arrive. Restructure the idle path to poll the queue
non-blocking and pump Playwright with a short `wait_for_timeout`:

```python
# inside _worker, replacing the blocking idle wait when screencast is active
cast = _start_screencast(cur()) if (SCREENCAST and cur()) else None
last_safety = 0.0  # wall-clock of last fallback grab()

while not closing:
    try:
        first = _CMD.get_nowait()          # don't block: we must keep pumping Playwright
        batch = [first]
        while True:
            try: batch.append(_CMD.get_nowait())
            except queue.Empty: break
        # … existing coalesce + command handling …
        # after a command batch that navigated/switched, restart the cast:
        #   _stop_screencast(cast); cast = _start_screencast(cur())
        grab() if not cast else None        # cast pushes frames; grab() only when no cast
        refresh()
    except queue.Empty:
        if cast and cur():
            try: cur().wait_for_timeout(33)  # ~30 fps PUMP → on_frame() fires here
            except Exception: pass
        else:
            grab()                           # fallback mode: old behaviour
            time.sleep(0.12)
        # safety net so the view can't freeze even if screencast stalls
        now = time.monotonic()
        if now - last_safety > 1.5:
            last_safety = now
            if cast: grab()                  # one slow poll/sec for robustness
            refresh()
```

Notes:
- Restart the cast whenever the active page changes: in the `open` / `navigate` /
  `switch_tab` / `close_tab` handlers, do
  `_stop_screencast(cast); cast = _start_screencast(cur())`.
- Keep `grab()` and the whole current loop intact as the `cast is None` path.
- Command latency becomes ≤33 ms (a queued click waits at most one pump) — fine,
  and far better than today's capture stalls.

### 3c. Fallback
- `OCEANO_BROWSER_SCREENCAST=0` → `cast` stays None → exact current behaviour.
- `_start_screencast` returning None (older Chromium / CDP error) → same.
- The 1.5 s safety `grab()` guarantees the view refreshes even if frames stall.

## 4. Transport — `oceano/web/server.py`

Add a WebSocket beside the existing SSE (keep SSE as the fallback). Imports:
`from fastapi import WebSocket, WebSocketDisconnect`.

```python
@app.websocket("/api/browser/ws")
async def browser_ws(ws: WebSocket):
    await ws.accept()
    last_v, last_tabs = -1, None
    try:
        while True:
            L = livebrowser.LATEST
            if L["v"] != last_v and L["frame"]:
                last_v = L["v"]
                await ws.send_bytes(L["frame"])               # raw JPEG, no base64
            tabs_sig = json.dumps([[t["id"], t["url"], t["active"], t["title"]]
                                   for t in L.get("tabs", [])])
            if tabs_sig != last_tabs:
                last_tabs = tabs_sig
                await ws.send_text(json.dumps({"url": L["url"], "tabs": L.get("tabs", [])}))
            await asyncio.sleep(0.03)                          # ~30 fps poll of LATEST
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
```

Auth: gate it the same way `/api/terminal/ws` is gated (copy that endpoint's
session/login check — don't leave the browser stream unauthenticated).

## 5. Render — `oceano/web/static/app.js`

Swap `#liveImg` for a `<canvas id="liveCanvas">` in the `.live-stage` markup
(~L2205) and replace the EventSource block (~L2264-2276):

```javascript
const canvas = $("#liveCanvas", body), cx = canvas.getContext("2d", { alpha: false });
const proto = location.protocol === "https:" ? "wss" : "ws";
let ws;
function connectLive() {
  ws = new WebSocket(`${proto}://${location.host}/api/browser/ws`);
  ws.binaryType = "blob";
  ws.onmessage = async (e) => {
    const win = document.getElementById("win-live");
    if (win && win.style.display === "none") return;          // minimized → skip decode
    if (typeof e.data === "string") {                         // metadata frame
      const d = JSON.parse(e.data);
      if (d.url) { /* update #liveUrl */ }
      if (d.tabs) renderLiveTabs(d.tabs);
      return;
    }
    const bmp = await createImageBitmap(e.data);              // off-main-thread decode
    if (canvas.width !== bmp.width)  canvas.width  = bmp.width;   // intrinsic 1280×800
    if (canvas.height !== bmp.height) canvas.height = bmp.height;
    cx.drawImage(bmp, 0, 0);
    bmp.close();
    /* hide #liveWait on first frame */
  };
  ws.onerror = () => { try { ws.close(); } catch {} ; fallbackToSSE(); };
}
```

- **Keep intrinsic canvas size at 1280×800** (the viewport) and let CSS scale the
  display — so the existing client→viewport coordinate mapping in the input
  handlers (click/drag/scroll, ~L2211-2262) is unchanged. Verify those handlers
  map against the canvas's displayed rect the same way they did for the img.
- `fallbackToSSE()` = today's `new EventSource("/api/browser/stream")` + `img.src`
  path, kept verbatim, used when the WS can't open.
- CSS: `#liveCanvas { width: 100%; height: auto; display: block; }` in
  `style.css` under `.live-stage`.

## 6. File-by-file change list

| File | Change |
|---|---|
| `oceano/livebrowser.py` | `import base64, time`; `_start_screencast`/`_stop_screencast`; restructure `_worker` idle loop to pump Playwright; restart cast on open/navigate/switch_tab/close_tab; keep `grab()` as fallback + 1.5 s safety poll; `OCEANO_BROWSER_SCREENCAST` env |
| `oceano/web/server.py` | `from fastapi import WebSocket, WebSocketDisconnect`; add `/api/browser/ws` (binary frames + text metadata), auth like `/api/terminal/ws`; keep `/api/browser/stream` SSE as fallback |
| `oceano/web/static/app.js` | `.live-stage`: `<img id="liveImg">` → `<canvas id="liveCanvas">`; WS connect + `createImageBitmap`→canvas render; SSE/img fallback; reuse input coordinate mapping |
| `oceano/web/static/style.css` | canvas sizing under `.live-stage` |

## 7. Expected result & what to verify on the host

Result: frames pushed on visual change at ~30 fps, input no longer fighting
capture, ~33 % less bandwidth, off-main-thread decode → smooth.

Verify live:
1. `screencastFrame` actually fires under sync Playwright with the pump loop
   (the one unproven assumption — confirm frames update while idle).
2. Tab switch shows a fresh frame immediately (initial frame on `startScreencast`).
3. Click/drag/scroll coordinates still land correctly on the canvas.
4. Fallback paths: `OCEANO_BROWSER_SCREENCAST=0` and WS-blocked both still render
   via the old SSE+img path.
5. Real Chrome channel (`STEALTH=1`) vs bundled Chromium both screencast.
