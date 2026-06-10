"""Persistent, interactive, MULTI-TAB headless browser — shared by user + agent.

ONE long-lived Chromium owned by a SINGLE worker thread (Playwright is thread-bound;
the web server is multi-threaded, so every action funnels through this thread's
command queue). It holds several tabs (Playwright pages). The ACTIVE tab is streamed
to LATEST as periodic JPEG screenshots (relayed by /api/browser/stream) — simple and
reliable across tab switches (CDP screencast only fires on visual change, so switching
to an already-loaded tab would leave the view stale).

Research lifecycle:
  • a web_search arms "research mode" and clears the previous research's tabs
  • each fetch_url / browser_open while armed opens the source in a NEW tab, and the
    view follows the newest one
  • manual navigation (address bar) reuses the active tab; a one-off open with no
    preceding search just navigates — tabs are left alone
"""
import queue
import threading

VIEWPORT = {"width": 1280, "height": 800}
MAX_TABS = 8                       # cap so a big research run can't pile up pages
LATEST = {"frame": None, "v": 0, "url": "about:blank", "tabs": []}

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
        ctx = br.new_context(viewport=VIEWPORT)
        tabs = []                  # [{page, id, title, fresh}]
        st = {"active": 0, "seq": 0, "armed": False}

        def cur():
            return tabs[st["active"]]["page"] if tabs else None

        def grab():               # screenshot the active tab → LATEST (one live frame)
            pg = cur()
            if pg is None:
                return
            try:
                LATEST["frame"] = pg.screenshot(type="jpeg", quality=55, timeout=4000)
                LATEST["v"] += 1
            except Exception:
                pass

        def make_tab():
            st["seq"] += 1
            return {"page": ctx.new_page(), "id": st["seq"], "title": "new tab", "fresh": True}

        def refresh():
            out = []
            for idx, t in enumerate(tabs):
                try:
                    u = t["page"].url
                except Exception:
                    u = ""
                out.append({"id": t["id"], "title": t["title"], "url": u, "active": idx == st["active"]})
            LATEST["tabs"] = out
            if cur():
                try:
                    LATEST["url"] = cur().url
                except Exception:
                    pass

        def nav(tab, url):
            try:
                tab["page"].goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            tab["page"].wait_for_timeout(500)
            tab["fresh"] = False
            try:
                tab["title"] = (tab["page"].title() or url)[:48]
            except Exception:
                tab["title"] = url[:48]

        def clamp_active():
            st["active"] = max(0, min(st["active"], len(tabs) - 1))

        tabs.append(make_tab()); refresh()

        while True:
            try:
                cmd, arg, resp = _CMD.get(timeout=0.05)
            except queue.Empty:
                try:                       # idle: let the active page paint, then stream a frame
                    if cur():
                        cur().wait_for_timeout(150)
                        grab()
                        LATEST["url"] = cur().url
                except Exception:
                    pass
                continue
            out = {"ok": True}
            try:
                if cmd == "research_reset":            # a web_search → fresh research group
                    for t in tabs[1:]:
                        try: t["page"].close()
                        except Exception: pass
                    del tabs[1:]
                    try: tabs[0]["page"].goto("about:blank")
                    except Exception: pass
                    tabs[0]["title"] = "new tab"; tabs[0]["fresh"] = True
                    st["active"] = 0; st["armed"] = True
                elif cmd == "open":                    # agent fetch/open a source
                    if st["armed"]:
                        c = tabs[st["active"]]
                        if c.get("fresh"):
                            nav(c, arg)                # fill the blank tab first
                        else:
                            if len(tabs) >= MAX_TABS:
                                try: tabs.pop(0)["page"].close()
                                except Exception: pass
                                clamp_active()
                            t = make_tab(); tabs.append(t); st["active"] = len(tabs) - 1; nav(t, arg)
                    else:
                        nav(tabs[st["active"]], arg)   # not researching → single-page behavior
                elif cmd == "navigate":                # manual address bar → reuse active tab
                    nav(tabs[st["active"]], arg)
                elif cmd == "switch_tab":
                    for idx, t in enumerate(tabs):
                        if t["id"] == arg:
                            st["active"] = idx; break
                elif cmd == "close_tab":
                    if len(tabs) > 1:
                        for idx, t in enumerate(tabs):
                            if t["id"] == arg:
                                try: t["page"].close()
                                except Exception: pass
                                tabs.pop(idx); clamp_active(); break
                elif cmd == "click":
                    cur().mouse.click(arg[0], arg[1]); cur().wait_for_timeout(400)
                elif cmd == "click_text":
                    try:
                        cur().click(f"text={arg}", timeout=5000)
                    except Exception:
                        cur().get_by_role("link", name=arg, exact=False).first.click(timeout=5000)
                    cur().wait_for_timeout(600)
                elif cmd == "scroll":
                    cur().mouse.wheel(0, arg); cur().wait_for_timeout(120)
                elif cmd == "type":
                    cur().keyboard.type(arg); cur().wait_for_timeout(80)
                elif cmd == "key":
                    cur().keyboard.press(arg); cur().wait_for_timeout(300)
                elif cmd == "read":
                    out = {"ok": True, "text": cur().inner_text("body")[:6000]}
                elif cmd == "screenshot":
                    cur().screenshot(path=arg, full_page=True)
                grab()                                 # reflect the result immediately
                if cur():
                    out["url"] = cur().url
                refresh()
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
def start_research():
    """A web_search begins a fresh research tab-group (clears the previous one)."""
    submit("research_reset")


def open(url, read=False):
    """Agent-driven open: a new tab while researching, else the active tab."""
    submit("open", url, wait=True)
    return read_text() if read else {"ok": True, "url": LATEST["url"]}


def navigate(url, read=False):
    """Manual / one-off navigation of the ACTIVE tab."""
    submit("navigate", url, wait=True)
    return read_text() if read else {"ok": True, "url": LATEST["url"]}


def read_text():
    return submit("read", wait=True).get("text", "")


def click_text(text):
    return submit("click_text", text, wait=True)


def save_screenshot(path):
    return submit("screenshot", str(path), wait=True)
