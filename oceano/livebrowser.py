"""Persistent, interactive, MULTI-TAB headless browser — shared by user + agent.

ONE long-lived Chromium owned by a SINGLE worker thread (Playwright is thread-bound;
the web server is multi-threaded, so every action funnels through this thread's
command queue). It holds several tabs (Playwright pages). The ACTIVE tab is streamed
to LATEST as periodic JPEG screenshots (relayed by /api/browser/stream) — simple and
reliable across tab switches (CDP screencast only fires on visual change, so switching
to an already-loaded tab would leave the view stale).

Research lifecycle:
  • a web_search arms "research mode" — from then on each fetch_url / browser_open opens
    the source in a NEW tab and the view follows the newest one. Tabs PERSIST across
    searches (capped at MAX_TABS; the oldest is evicted) — a new search does NOT wipe them
  • manual navigation (address bar) reuses the active tab; a one-off open with no
    preceding search just navigates — tabs are left alone
"""
import base64
import os
import queue
import threading
import time

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

# CDP screencast: Chromium PUSHES JPEG frames on visual change, instead of the worker polling
# page.screenshot() on the input thread (slow, and it fights every click/scroll). Probed at
# startup — if frames don't actually flow we fall back to the screenshot poll, so the live view
# never breaks. Disable with OCEANO_BROWSER_SCREENCAST=0.
SCREENCAST = os.environ.get("OCEANO_BROWSER_SCREENCAST", "1") == "1"

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


def _install_ssrf_guard(ctx):
    """Re-apply the SSRF guard to every NAVIGATION the browser makes — not just the first URL
    a tool checked. A fetched page can 3xx / <meta refresh> / JS-redirect to an internal address
    (llama-swap :8081, embeddings :8082, SearXNG :8080, cloud metadata); without this the
    live-browser path (fetch_url / browser_open on the web channel) would follow the redirect and
    feed the internal response back to the model. We gate navigation requests only — each redirect
    hop is its own navigation request, so hops are re-checked too — and let subresources through
    (they don't return readable page text to the agent). Only http(s) is checked, so about:/data:
    navigations still work; a no-op when OCEANO_URL_GUARD is off (check_url returns None)."""
    from oceano import safety

    def _route(route):
        try:
            req = route.request
            if (req.is_navigation_request() and req.url.startswith(("http://", "https://"))
                    and safety.check_url(req.url)):
                route.abort()
                return
        except Exception:
            pass
        try:
            route.continue_()
        except Exception:
            pass

    try:
        ctx.route("**/*", _route)
    except Exception:
        pass


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
    _install_ssrf_guard(ctx)         # block redirects/JS-navigation to internal addresses
    return ctx

def _use_real_chrome():
    """Whether to drive a real, persistent Google Chrome (the Settings toggle) instead of the
    throwaway headless Chromium. OCEANO_REAL_CHROME=1/0 overrides the saved setting."""
    env = os.environ.get("OCEANO_REAL_CHROME")
    if env is not None:
        return env == "1"
    try:
        from oceano.web import server
        return bool(server.load().get("prefs", {}).get("real_chrome"))
    except Exception:
        return False


def _launch_persistent(p):
    """Launch a REAL, persistent Google Chrome (channel='chrome') with a profile dir under data/, so
    logins / cookies / extensions survive restarts and the fingerprint is genuine. Headless — it's
    viewed through the LIVE Browser via the screencast. Returns a persistent BrowserContext (which is
    its own browser); we apply the same SSRF guard + stealth patch as a normal context."""
    import config
    profile = config.WORKSPACE.parent / "data" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    opts = {"channel": "chrome", "headless": True, "viewport": VIEWPORT, "locale": "en-US",
            "args": ["--disable-blink-features=AutomationControlled"] if STEALTH else [],
            "ignore_default_args": ["--enable-automation"] if STEALTH else []}
    if STEALTH:
        opts["extra_http_headers"] = {"Accept-Language": ACCEPT_LANGUAGE}
    ctx = p.chromium.launch_persistent_context(str(profile), **opts)
    if STEALTH:
        try:
            ctx.add_init_script(_STEALTH_JS)
        except Exception:
            pass
    _install_ssrf_guard(ctx)             # block redirects/JS-navigation to internal addresses
    return ctx


def _start_screencast(page):
    """Best-effort CDP screencast on `page`: Chromium pushes JPEG frames into LATEST as the page
    changes, so the worker thread never blocks on a synchronous screenshot. Returns the CDP
    session, or None if it couldn't start. Frames arrive while the worker pumps Playwright."""
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

    try:
        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 55,
            "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"], "everyNthFrame": 1,
        })
        return cdp
    except Exception:
        return None


def _stop_screencast(cdp):
    if not cdp:
        return
    try:
        cdp.send("Page.stopScreencast")
    except Exception:
        pass
    try:
        cdp.detach()
    except Exception:
        pass


# Set-of-marks: tag every visible interactive element with data-oceano-ref=N and return a compact
# list, so the agent can target elements by a stable [ref] instead of guessing pixels/ambiguous text.
_SNAPSHOT_JS = r"""() => {
  const q = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],'
          + '[role=textbox],[role=combobox],[role=checkbox],[role=menuitem],[role=tab],'
          + '[contenteditable=""],[contenteditable=true],summary,[onclick]';
  const seen = new Set(); const out = []; let i = 0;
  for (const el of document.querySelectorAll(q)) {
    if (seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) continue;
    i++; el.setAttribute('data-oceano-ref', String(i));
    const tag = el.tagName.toLowerCase();
    let label = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
                 || (el.innerText || el.textContent || '').trim() || el.getAttribute('title')
                 || el.getAttribute('name') || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 90);
    const o = { ref: i, role: el.getAttribute('role') || tag };
    if (label) o.label = label;
    if (tag === 'a') { const h = el.getAttribute('href'); if (h) o.href = h.slice(0, 120); }
    if (tag === 'input') { o.type = el.getAttribute('type') || 'text'; if (el.value) o.value = String(el.value).slice(0, 40); }
    if (tag === 'textarea' && el.value) o.value = String(el.value).slice(0, 40);
    if (tag === 'select') o.options = Array.from(el.options).map(x => (x.text || '').trim()).filter(Boolean).slice(0, 25);
    out.push(o);
    if (i >= 200) break;
  }
  return out;
}"""


_EXTRACT_JS = r"""(arg) => {
  const els = Array.from(document.querySelectorAll(arg.sel)).slice(0, arg.lim || 30);
  return els.map(el => {
    const v = arg.attr ? (el.getAttribute(arg.attr) || '')
                       : ((el.innerText || el.textContent || '').trim());
    return (v || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  }).filter(Boolean);
}"""

# Read the current page as markdown-ish text: headings marked with #, links inlined as "text <url>",
# scripts/styles stripped — so the agent reads structure + link targets, not a flat blob.
_READ_MD_JS = r"""() => {
  const b = document.body.cloneNode(true);
  b.querySelectorAll('script,style,noscript,svg').forEach(e => e.remove());
  b.querySelectorAll('a[href]').forEach(a => {
    const h = a.getAttribute('href'), t = (a.innerText || '').trim();
    if (h && t && !h.startsWith('javascript:')) a.textContent = t + ' <' + h + '>';
  });
  b.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => {
    const n = +h.tagName[1]; h.textContent = '\n' + '#'.repeat(n) + ' ' + (h.innerText || '').trim() + '\n';
  });
  b.querySelectorAll('li').forEach(li => { li.textContent = '- ' + (li.innerText || '').trim(); });
  return (b.innerText || '').replace(/\n{3,}/g, '\n\n').trim().slice(0, 8000);
}"""


def _locate(page, target):
    """Resolve a target to a Playwright locator (or None): a snapshot ref number (the reliable path,
    from browser_snapshot), or a label / placeholder / role-name / text descriptor as a fallback."""
    t = str(target or "").strip()
    if not t:
        return None
    if t.isdigit():
        loc = page.locator(f'[data-oceano-ref="{t}"]')
        try:
            return loc if loc.count() else None
        except Exception:
            return loc
    for strat in (lambda: page.get_by_label(t, exact=False),
                  lambda: page.get_by_placeholder(t, exact=False),
                  lambda: page.get_by_role("textbox", name=t),
                  lambda: page.get_by_role("combobox", name=t),
                  lambda: page.get_by_text(t, exact=False)):
        try:
            loc = strat()
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
    return None


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
        br = ctx = None
        if _use_real_chrome():                     # Settings toggle: drive a real, persistent Chrome
            try:
                ctx = _launch_persistent(p)
                print("[livebrowser] driving real persistent Chrome (data/chrome-profile)", flush=True)
            except Exception as e:                 # real Chrome missing/failed → fall back to headless
                print(f"[livebrowser] real-chrome launch failed ({e!r}); using headless Chromium", flush=True)
                ctx = None
        if ctx is None:
            br = _launch(p)
            ctx = _new_context(br)
        tabs = []                  # [{page, id, title, fresh}]
        st = {"active": 0, "seq": 0, "armed": False, "dialog": "dismiss", "dialog_text": ""}

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

        def make_tab(page=None):
            st["seq"] += 1
            pg = page or ctx.new_page()
            try:    # JS dialogs: dismiss by default (Playwright's default too), accept when armed
                pg.on("dialog", lambda d: (d.accept(st.get("dialog_text") or "")
                                           if st.get("dialog") == "accept" else d.dismiss()))
            except Exception:
                pass
            return {"page": pg, "id": st["seq"], "title": "new tab", "fresh": True}

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

        if ctx.pages:                              # a persistent context ships an initial page — adopt it
            tabs.append(make_tab(ctx.pages[0]))
        else:
            tabs.append(make_tab())
        refresh()

        def coalesce(batch):
            """Merge bursts of fire-and-forget input that queued up faster than
            Chromium executes them: consecutive scrolls sum their deltas, consecutive
            type commands concatenate, and consecutive mousemoves collapse to the newest
            position (only where the pointer ended up matters). Commands awaiting a
            response never merge."""
            out = []
            for cmd, arg, resp in batch:
                mergeable = out and resp is None and out[-1][2] is None and cmd == out[-1][0]
                if mergeable and cmd in ("scroll", "type"):
                    out[-1] = (cmd, out[-1][1] + arg, None)
                elif mergeable and cmd in ("mousemove", "resize"):
                    out[-1] = (cmd, arg, None)              # only the latest position / size matters
                else:
                    out.append((cmd, arg, resp))
            return out

        # Screencast probe: if Chromium actually pushes frames under our pump, use it; otherwise
        # fall back to the screenshot poll so the view can't end up frozen.
        cast = None
        if SCREENCAST and cur():
            v0 = LATEST["v"]
            c = _start_screencast(cur())
            if c:
                try: cur().wait_for_timeout(300)       # let the initial frame land
                except Exception: pass
                if LATEST["v"] > v0:
                    cast = c                            # frames flow → push-based rendering
                else:
                    _stop_screencast(c)                 # silent → fall back to polling

        closing = False
        last_safety = 0.0
        while not closing:
            batch = []                                 # NON-blocking drain: we must keep pumping the
            try:                                       # cast, so we never block on the command queue
                batch.append(_CMD.get_nowait())
                while True:
                    try:
                        batch.append(_CMD.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if not batch:                              # idle
                if cast is not None and cur():
                    try: cur().wait_for_timeout(33)     # ~30fps PUMP → screencastFrame fires here
                    except Exception: pass
                else:
                    grab()                              # fallback: poll a frame like before
                    time.sleep(0.12)
                now = time.monotonic()
                if now - last_safety > 1.5:             # safety net: refresh url/tabs (+ a poll frame
                    last_safety = now                   # even with a cast) so the view can't freeze
                    try:
                        if cur():
                            if cast is not None:
                                grab()
                            LATEST["url"] = cur().url
                            refresh()
                    except Exception:
                        pass
                continue
            nav_in_batch = any(c in ("open", "navigate", "switch_tab", "close_tab",
                                     "back", "forward", "reload", "new_tab", "resize")
                               for c, _, _ in batch)   # → re-cast (resize: new frame size) the active page after
            for cmd, arg, resp in coalesce(batch):
                out = {"ok": True}
                try:
                    if cmd == "__quit__":              # clean shutdown — close Chrome, then exit
                        closing = True
                    elif cmd == "research_arm":         # a web_search → open results as tabs from here on
                        st["armed"] = True             # tabs PERSIST across searches (MAX_TABS evicts the oldest)
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
                    elif cmd == "new_tab":                 # open a fresh blank tab and focus it
                        if len(tabs) >= MAX_TABS:
                            try: tabs.pop(0)["page"].close()
                            except Exception: pass
                            clamp_active()
                        t = make_tab(); tabs.append(t); st["active"] = len(tabs) - 1
                    elif cmd in ("back", "forward", "reload"):   # history navigation of the active tab
                        pg = cur()
                        try:
                            if cmd == "back":
                                pg.go_back(wait_until="domcontentloaded", timeout=15000)
                            elif cmd == "forward":
                                pg.go_forward(wait_until="domcontentloaded", timeout=15000)
                            else:
                                pg.reload(wait_until="domcontentloaded", timeout=15000)
                        except Exception:
                            pass                           # no history / aborted load → no-op
                        pg.wait_for_timeout(200)
                        try:
                            tabs[st["active"]]["title"] = (pg.title() or pg.url)[:48]
                        except Exception:
                            pass
                    elif cmd == "stop":                    # stop the in-flight page load
                        try: cur().evaluate("window.stop()")
                        except Exception: pass
                    elif cmd == "resize":                  # match the browser viewport to the LIVE window
                        try:
                            w = max(320, min(int(arg[0]), 2560))
                            h = max(240, min(int(arg[1]), 1600))
                        except (TypeError, ValueError, IndexError):
                            w = h = 0
                        if w and h and (w, h) != (VIEWPORT["width"], VIEWPORT["height"]):
                            VIEWPORT["width"], VIEWPORT["height"] = w, h   # new tabs + the re-cast use this
                            for t in tabs:
                                try: t["page"].set_viewport_size({"width": w, "height": h})
                                except Exception: pass
                    elif cmd == "click":
                        cur().mouse.click(arg[0], arg[1]); cur().wait_for_timeout(300)
                    elif cmd == "mousedown":           # press-hold-release, streamed live — lets the
                        cur().mouse.move(arg[0], arg[1]); cur().mouse.down()   # user drag sliders /
                    elif cmd == "mousemove":           # solve drag-to-verify captchas by hand. The
                        cur().mouse.move(arg[0], arg[1])                       # path is the user's OWN
                    elif cmd == "mouseup":             # mouse, so it reads as human movement.
                        if arg:
                            cur().mouse.move(arg[0], arg[1])
                        cur().mouse.up(); cur().wait_for_timeout(150)
                    elif cmd == "drag":                # whole gesture in one call: a path of [x,y] points
                        pts = [p for p in (arg or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
                        if len(pts) >= 2:
                            c = cur()
                            c.mouse.move(pts[0][0], pts[0][1]); c.mouse.down()
                            for (x, y) in pts[1:]:
                                c.mouse.move(x, y); c.wait_for_timeout(16)
                            c.mouse.up(); c.wait_for_timeout(250)
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
                    elif cmd == "paste":               # your clipboard text → the page's focused field
                        cur().keyboard.insert_text(arg or ""); cur().wait_for_timeout(30)
                    elif cmd == "copy":                # hand the page's current selection back to the client
                        try:
                            sel = cur().evaluate("() => (window.getSelection && window.getSelection().toString()) || ''")
                        except Exception:
                            sel = ""
                        out = {"ok": True, "text": sel or ""}
                    elif cmd == "read":
                        out = {"ok": True, "text": cur().inner_text("body")[:6000]}
                    elif cmd == "snapshot":            # map interactive elements → numbered [ref]s
                        try:
                            out = {"ok": True, "items": cur().evaluate(_SNAPSHOT_JS)}
                        except Exception as e:
                            out = {"ok": False, "error": str(e), "items": []}
                    elif cmd == "fill":                # type text into a field (by ref or descriptor)
                        loc = _locate(cur(), arg.get("target"))
                        if not loc:
                            out = {"ok": False, "error": f"no field matching {arg.get('target')!r}"}
                        else:
                            loc.fill(arg.get("text", ""))
                            if arg.get("enter"):
                                loc.press("Enter"); cur().wait_for_timeout(400)
                            else:
                                cur().wait_for_timeout(60)
                            out = {"ok": True}
                    elif cmd == "select":              # choose a dropdown option
                        loc = _locate(cur(), arg.get("target"))
                        if not loc:
                            out = {"ok": False, "error": f"no dropdown matching {arg.get('target')!r}"}
                        else:
                            opt = arg.get("option", "")
                            try:
                                loc.select_option(label=opt)
                            except Exception:
                                loc.select_option(opt)          # fall back to value/index
                            cur().wait_for_timeout(80); out = {"ok": True}
                    elif cmd == "click_ref":           # click an element by its snapshot [ref]
                        loc = _locate(cur(), arg)
                        if not loc:
                            out = {"ok": False, "error": f"no element with ref {arg!r}"}
                        else:
                            loc.click(); cur().wait_for_timeout(300); out = {"ok": True}
                    elif cmd == "press":               # press a key on the page (Enter/Escape/Tab/…)
                        cur().keyboard.press(arg); cur().wait_for_timeout(300); out = {"ok": True}
                    elif cmd == "wait":                # wait for content to appear / the page to settle
                        pg = cur(); mode = arg.get("mode", "text"); val = arg.get("value", "")
                        to = max(500, min(int(arg.get("timeout", 8000)), 25000))
                        try:
                            if mode == "text":
                                pg.get_by_text(val, exact=False).first.wait_for(timeout=to)
                            elif mode == "selector":
                                pg.wait_for_selector(val, timeout=to)
                            elif mode == "load":
                                pg.wait_for_load_state(val or "networkidle", timeout=to)
                            else:
                                pg.wait_for_timeout(to)
                            out = {"ok": True}
                        except Exception as e:
                            out = {"ok": False, "error": f"timeout/none: {type(e).__name__}"}
                    elif cmd == "extract":             # pull matching elements' text or an attribute
                        out = {"ok": True, "results": cur().evaluate(_EXTRACT_JS, {
                            "sel": arg.get("selector", ""), "attr": arg.get("attr"),
                            "lim": max(1, min(int(arg.get("limit", 30)), 100))})}
                    elif cmd == "read_md":             # the current page as markdown-ish text + links
                        out = {"ok": True, "text": cur().evaluate(_READ_MD_JS)}
                    elif cmd == "eval":                # run arbitrary JS in the page (gated web-only tool)
                        out = {"ok": True, "result": cur().evaluate(arg)}
                    elif cmd == "hover":               # hover an element (reveal menus/tooltips)
                        loc = _locate(cur(), arg)
                        if not loc:
                            out = {"ok": False, "error": f"no element matching {arg!r}"}
                        else:
                            loc.hover(); cur().wait_for_timeout(120); out = {"ok": True}
                    elif cmd == "upload":              # set a file (workspace path) on a file input
                        loc = _locate(cur(), arg.get("target"))
                        if not loc:
                            out = {"ok": False, "error": f"no file input matching {arg.get('target')!r}"}
                        else:
                            loc.set_input_files(arg.get("paths") or []); out = {"ok": True}
                    elif cmd == "dialog":              # arm how the NEXT JS dialog(s) are handled
                        st["dialog"] = "accept" if arg.get("action") == "accept" else "dismiss"
                        st["dialog_text"] = arg.get("text", "") or ""
                        out = {"ok": True}
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
            if cast is not None and nav_in_batch:  # page navigated/tab switched → re-cast for a fresh
                _stop_screencast(cast)             # frame (startScreencast emits the current view)
                cast = _start_screencast(cur())
            if cast is None:
                grab()                             # no cast → poll one frame per batch (old behaviour)
            refresh()                              # tab-bar + url update per batch

        _stop_screencast(cast)
        for t in tabs:                             # tear Chrome down ON this thread (it's
            try: t["page"].close()                 # thread-bound) so the subprocess exits
            except Exception: pass                 # cleanly instead of segfaulting at exit
        try: ctx.close()
        except Exception: pass
        if br is not None:                         # persistent context has no separate browser handle
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
    """A web_search arms research mode so results open as tabs. Tabs persist across searches
    (capped at MAX_TABS — the oldest is evicted); they are NOT wiped on each search."""
    submit("research_arm")


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


def snapshot():
    return submit("snapshot", wait=True)


def fill(target, text, enter=False):
    return submit("fill", {"target": target, "text": text, "enter": bool(enter)}, wait=True)


def select(target, option):
    return submit("select", {"target": target, "option": option}, wait=True)


def click_ref(ref):
    return submit("click_ref", ref, wait=True)


def press(key):
    return submit("press", key, wait=True)


def wait_for(mode, value="", timeout=8000):
    return submit("wait", {"mode": mode, "value": value, "timeout": timeout}, wait=True, timeout=30)


def extract(selector, attr=None, limit=30):
    return submit("extract", {"selector": selector, "attr": attr, "limit": limit}, wait=True)


def read_markdown():
    return submit("read_md", wait=True)


def evaluate_js(code):
    return submit("eval", code, wait=True)


def hover(target):
    return submit("hover", target, wait=True)


def upload(target, paths):
    return submit("upload", {"target": target, "paths": paths}, wait=True)


def dialog(action, text=""):
    return submit("dialog", {"action": action, "text": text}, wait=True)


def save_screenshot(path):
    return submit("screenshot", str(path), wait=True)
