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
import os
import queue
import threading

VIEWPORT = {"width": 1280, "height": 800}
MAX_TABS = 8                       # cap so a big research run can't pile up pages
LATEST = {"frame": None, "v": 0, "url": "about:blank", "tabs": []}

# Headless Chromium ships three dead giveaways every anti-bot check looks for: a
# "HeadlessChrome" User-Agent, navigator.webdriver===true, and missing
# languages/plugins/window.chrome. A normal browser on this box has none, which is
# why a blocked page loads fine when the user opens it. We launch the REAL Chrome
# channel when present (genuine fingerprint), drop the automation flags, and run an
# init script to paper over the rest. This is to browse like the user's own browser
# for research — not to defeat logins, captchas, or paywalls. Set OCEANO_BROWSER_STEALTH=0
# to go back to a vanilla headless launch.
STEALTH = os.environ.get("OCEANO_BROWSER_STEALTH", "1") == "1"
ACCEPT_LANGUAGE = os.environ.get("OCEANO_BROWSER_LANG", "en-US,en;q=0.9")

# Runs before any page script — removes the leftover automation tells that the
# launch flags don't cover.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
if (!window.chrome) { window.chrome = {runtime: {}}; }
const _q = navigator.permissions && navigator.permissions.query;
if (_q) { navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p); }
"""


def _launch(p):
    """Launch the most believable Chrome we can: the real Chrome channel if it's
    installed (genuine fingerprint), else bundled Chromium. Either way, strip the
    automation flags. Returns the browser."""
    args = ["--disable-blink-features=AutomationControlled"] if STEALTH else []
    ignore = ["--enable-automation"] if STEALTH else []
    if STEALTH:
        try:
            return p.chromium.launch(headless=True, channel="chrome",
                                     args=args, ignore_default_args=ignore)
        except Exception:
            pass                       # real Chrome not usable here → bundled Chromium
    return p.chromium.launch(headless=True, args=args, ignore_default_args=ignore)


def _new_context(br):
    """A context that looks like a normal desktop browser: real UA (the bundled
    build's UA minus the 'Headless' token), a locale, and an Accept-Language header."""
    opts = {"viewport": VIEWPORT}
    if STEALTH:
        try:                           # derive UA from this very browser so it tracks its version
            probe = br.new_context()
            ua = probe.new_page().evaluate("navigator.userAgent")
            probe.close()
            opts["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
        except Exception:
            pass
        opts["locale"] = "en-US"
        opts["extra_http_headers"] = {"Accept-Language": ACCEPT_LANGUAGE}
    ctx = br.new_context(**opts)
    if STEALTH:
        try:
            ctx.add_init_script(_STEALTH_JS)
        except Exception:
            pass
    return ctx

_CMD = queue.Queue()
_started = False
_lock = threading.Lock()


def ensure_started():
    global _started
    with _lock:
        if not _started:
            _started = True
            threading.Thread(target=_worker, name="livebrowser", daemon=True).start()


def _worker():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = _launch(p)
        ctx = _new_context(br)
        tabs = []                  # [{page, id, title, fresh}]
        st = {"active": 0, "seq": 0, "armed": False}

        def cur():
            return tabs[st["active"]]["page"] if tabs else None

        def grab():               # screenshot the active tab → LATEST (one live frame)
            pg = cur()
            if pg is None:
                return
            try:
                data = pg.screenshot(type="jpeg", quality=55, timeout=4000)
                if data != LATEST["frame"]:    # static page → identical JPEG → don't
                    LATEST["frame"] = data     # re-push it (stops the text shimmer and
                    LATEST["v"] += 1           # the pointless ~10/s redecodes downstream)
            except Exception:
                pass

        def make_tab():
            st["seq"] += 1
            return {"page": ctx.new_page(), "id": st["seq"], "title": "new tab", "fresh": True}

        def refresh():
            if tabs:                           # keep the ACTIVE tab's title current —
                t = tabs[st["active"]]         # click-through navigation changes it
                try:
                    t["title"] = (t["page"].title() or t["title"] or t["page"].url)[:48]
                except Exception:
                    pass
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
            tab["page"].wait_for_timeout(250)
            tab["fresh"] = False
            try:
                tab["title"] = (tab["page"].title() or url)[:48]
            except Exception:
                tab["title"] = url[:48]

        def clamp_active():
            st["active"] = max(0, min(st["active"], len(tabs) - 1))

        tabs.append(make_tab()); refresh()

        def coalesce(batch):
            """Merge bursts of fire-and-forget input that queued up faster than
            Chromium executes them: consecutive scrolls sum their deltas, consecutive
            type commands concatenate. Commands awaiting a response never merge."""
            out = []
            for cmd, arg, resp in batch:
                if (out and resp is None and out[-1][2] is None
                        and cmd == out[-1][0] and cmd in ("scroll", "type")):
                    out[-1] = (cmd, out[-1][1] + arg, None)
                else:
                    out.append((cmd, arg, resp))
            return out

        closing = False
        while not closing:
            try:
                first = _CMD.get(timeout=0.15)     # the timeout IS the idle frame pacing
            except queue.Empty:
                try:                               # idle: stream a frame of the active tab
                    if cur():
                        grab()
                        LATEST["url"] = cur().url
                except Exception:
                    pass
                continue
            batch = [first]
            while True:                            # drain whatever piled up behind it
                try:
                    batch.append(_CMD.get_nowait())
                except queue.Empty:
                    break
            for cmd, arg, resp in coalesce(batch):
                out = {"ok": True}
                try:
                    if cmd == "__quit__":              # clean shutdown — close Chrome, then exit
                        closing = True
                    elif cmd == "research_reset":      # a web_search → fresh research group
                        for t in tabs[1:]:
                            try: t["page"].close()
                            except Exception: pass
                        del tabs[1:]
                        try: tabs[0]["page"].goto("about:blank")
                        except Exception: pass
                        tabs[0]["title"] = "new tab"; tabs[0]["fresh"] = True
                        st["active"] = 0; st["armed"] = True
                    elif cmd == "open":                # agent fetch/open a source
                        if st["armed"]:
                            c = tabs[st["active"]]
                            if c.get("fresh"):
                                nav(c, arg)            # fill the blank tab first
                            else:
                                if len(tabs) >= MAX_TABS:
                                    try: tabs.pop(0)["page"].close()
                                    except Exception: pass
                                    clamp_active()
                                t = make_tab(); tabs.append(t); st["active"] = len(tabs) - 1; nav(t, arg)
                        else:
                            nav(tabs[st["active"]], arg)   # not researching → single-page behavior
                    elif cmd == "navigate":            # manual address bar → reuse active tab
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
                        cur().mouse.click(arg[0], arg[1]); cur().wait_for_timeout(300)
                    elif cmd == "click_text":
                        try:
                            cur().click(f"text={arg}", timeout=5000)
                        except Exception:
                            cur().get_by_role("link", name=arg, exact=False).first.click(timeout=5000)
                        cur().wait_for_timeout(500)
                    elif cmd == "scroll":
                        cur().mouse.wheel(0, arg); cur().wait_for_timeout(40)
                    elif cmd == "type":
                        cur().keyboard.type(arg); cur().wait_for_timeout(30)
                    elif cmd == "key":
                        cur().keyboard.press(arg); cur().wait_for_timeout(200)
                    elif cmd == "read":
                        out = {"ok": True, "text": cur().inner_text("body")[:6000]}
                    elif cmd == "screenshot":
                        cur().screenshot(path=arg, full_page=True)
                    if cur():
                        out["url"] = cur().url
                except Exception as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    import traceback
                    print(f"[livebrowser] {cmd}({arg!r}) failed: {type(e).__name__}: {e}",
                          flush=True)
                    traceback.print_exc()      # full trace lands in the journal for diagnosis
                if resp is not None:
                    resp.put(out)
            if closing:
                break
            grab()                                 # ONE frame + tab-bar update per batch,
            refresh()                              # not per command — bursts stay snappy

        for t in tabs:                             # tear Chrome down ON this thread (it's
            try: t["page"].close()                 # thread-bound) so the subprocess exits
            except Exception: pass                 # cleanly instead of segfaulting at exit
        try: ctx.close()
        except Exception: pass
        try: br.close()
        except Exception: pass


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


def shutdown(timeout=8):
    """Close Chrome cleanly on the worker thread (Playwright objects are thread-
    bound, so the browser MUST be torn down there, not from the caller). Call this
    on process shutdown to avoid a Chromium segfault at interpreter exit."""
    global _started
    with _lock:
        if not _started:
            return
    for th in threading.enumerate():
        if th.name == "livebrowser":
            _CMD.put(("__quit__", None, None))
            th.join(timeout)
            break
    with _lock:
        _started = False


def capture(url, path, full_page=True, timeout=30000):
    """One-off headless screenshot of `url` to `path`, on a THROWAWAY browser — does
    NOT use the shared worker, so it won't disturb the web UI's live view. For off-web
    channels (e.g. Telegram) that want a screenshot delivered as a photo. Returns the
    path on success, or raises. Blocking — call from a worker thread."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = _launch(p)
        try:
            ctx = _new_context(br)
            pg = ctx.new_page()
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=timeout)
            except Exception:
                pass
            pg.wait_for_timeout(600)
            pg.screenshot(path=str(path), full_page=full_page)
        finally:
            try:
                br.close()
            except Exception:
                pass
    return str(path)


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
