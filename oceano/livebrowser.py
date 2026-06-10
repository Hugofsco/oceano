"""Persistent, interactive headless browser — shared by the user AND the agent.

ONE long-lived Chromium owned by a SINGLE worker thread. Playwright objects are
thread-bound and the web server runs many threads, so every browser action funnels
through this thread's command queue. CDP screencast streams continuous JPEG frames
to LATEST (relayed by /api/browser/stream). The Live window drives it (navigate /
click / scroll / type) and the agent acts on the SAME page via browser_* tools —
human + model on one browser.
"""
import base64
import queue
import threading

VIEWPORT = {"width": 1280, "height": 800}
LATEST = {"frame": None, "v": 0, "url": "about:blank"}

_CMD = queue.Queue()
_started = False
_lock = threading.Lock()


def ensure_started():
    global _started
    with _lock:
        if not _started:
            _started = True
            threading.Thread(target=_worker, daemon=True).start()


def _worker():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page(viewport=VIEWPORT)
        cdp = pg.context.new_cdp_session(pg)

        def on_frame(params):
            try:
                LATEST["frame"] = base64.b64decode(params["data"])
                LATEST["v"] += 1
                cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 50,
                 "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"], "everyNthFrame": 1})

        while True:
            try:
                cmd, arg, resp = _CMD.get(timeout=0.05)
            except queue.Empty:
                try:
                    pg.wait_for_timeout(80)     # pump → frames keep flowing while idle
                    LATEST["url"] = pg.url       # keep the URL label fresh (e.g. after a click navigates)
                except Exception:
                    pass
                continue
            out = {"ok": True}
            try:
                if cmd == "navigate":
                    try:
                        pg.goto(arg, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    pg.wait_for_timeout(700)
                elif cmd == "click":
                    pg.mouse.click(arg[0], arg[1]); pg.wait_for_timeout(500)
                elif cmd == "click_text":
                    try:
                        pg.click(f"text={arg}", timeout=5000)            # smallest element containing the text
                    except Exception:
                        pg.get_by_role("link", name=arg, exact=False).first.click(timeout=5000)
                    pg.wait_for_timeout(700)
                elif cmd == "scroll":
                    pg.mouse.wheel(0, arg); pg.wait_for_timeout(120)
                elif cmd == "type":
                    pg.keyboard.type(arg); pg.wait_for_timeout(80)
                elif cmd == "key":
                    pg.keyboard.press(arg); pg.wait_for_timeout(300)
                elif cmd == "read":
                    out = {"ok": True, "text": pg.inner_text("body")[:6000]}
                elif cmd == "screenshot":
                    pg.screenshot(path=arg, full_page=True)
                LATEST["url"] = pg.url
                out["url"] = pg.url
            except Exception as e:
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if resp is not None:
                resp.put(out)


def submit(cmd, arg=None, wait=False, timeout=40):
    ensure_started()
    resp = queue.Queue() if wait else None
    _CMD.put((cmd, arg, resp))
    if wait:
        try:
            return resp.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "timeout"}
    return {"ok": True}


# --- convenience wrappers ---
def navigate(url, read=False):
    submit("navigate", url, wait=True)
    return read_text() if read else {"ok": True, "url": LATEST["url"]}


def read_text():
    return submit("read", wait=True).get("text", "")


def click_text(text):
    return submit("click_text", text, wait=True)


def save_screenshot(path):
    return submit("screenshot", str(path), wait=True)
