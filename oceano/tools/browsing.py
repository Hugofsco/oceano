"""The interactive headless-browser tools (web channel only)."""
import json

from oceano import browser, livebrowser, safety
from oceano.tools.core import _resolve, is_background, live_browser_available, tool
from oceano.tools.web import _http_fetch

# --- headless browser ------------------------------------------------------
_BG_BROWSER_NOTE = ("(the interactive/visual browser is only available in the web UI — the "
                    "user on this channel can't see it. Use fetch_url to read pages instead.)")


@tool({
    "type": "function",
    "function": {
        "name": "browser_open",
        "description": "Open a URL in a real headless browser and return the rendered text. "
                       "Use for JavaScript-heavy pages that fetch_url can't read.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}
        }, "required": ["url"]},
    },
})
def browser_open(url):
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    if not live_browser_available():  # off-web channel → plain HTTP, never the shared browser
        return safety.wrap_untrusted(url, _http_fetch(url))
    return safety.wrap_untrusted(url, browser.open_url(url))


@tool({
    "type": "function",
    "function": {
        "name": "browser_screenshot",
        "description": "Open a URL in a headless browser and save a full-page screenshot to the "
                       "workspace (it then shows in chat). Pass the URL to capture.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "name": {"type": "string", "description": "file name, default screenshot.png"}
        }, "required": ["url"]},
    },
})
def browser_screenshot(url, name="screenshot.png"):
    # Unattended jobs have no one to show a screenshot to; everyone else gets one —
    # the web UI watches the shared browser, other channels get a throwaway capture.
    if is_background():
        return _BG_BROWSER_NOTE
    return browser.screenshot(url, name, shared=live_browser_available())


@tool({
    "type": "function",
    "function": {
        "name": "browser_click",
        "description": "Click an element on the CURRENT browser page — by its [ref] number from "
                       "browser_snapshot (most reliable), or by its visible text. Use after "
                       "browser_open/fetch_url to interact with a page step by step.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "a [ref] number from browser_snapshot, or the "
                                                      "visible text of the link/button to click"}
        }, "required": ["text"]},
    },
})
def browser_click(text):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    t = str(text).strip()
    r = livebrowser.click_ref(t) if t.isdigit() else livebrowser.click_text(text)
    if not r.get("ok"):
        return f"could not click {text!r}: {r.get('error')}"
    return safety.wrap_untrusted(r.get("url", ""), livebrowser.read_text())


@tool({
    "type": "function",
    "function": {
        "name": "browser_scroll",
        "description": "Scroll the current browser page (positive = down, negative = up).",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "integer", "description": "pixels to scroll, default 600"}
        }},
    },
})
def browser_scroll(amount=600):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    livebrowser.submit("scroll", int(amount))
    return f"scrolled {amount}px"


@tool({
    "type": "function",
    "function": {
        "name": "browser_snapshot",
        "description": "Map the interactive elements on the CURRENT browser page — links, buttons, "
                       "inputs, textareas, dropdowns — each with a numbered [ref]. Use the [ref] with "
                       "browser_click / browser_fill / browser_select to act on an element RELIABLY "
                       "(instead of guessing text). Re-run it after the page changes — refs go stale "
                       "on navigation.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def browser_snapshot():
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    items = (livebrowser.snapshot() or {}).get("items") or []
    if not items:
        return "(no interactive elements found — load a page first with browser_open/fetch_url)"
    lines = []
    for it in items:
        parts = [f"[{it['ref']}]", it.get("role", "")]
        if it.get("label"):
            parts.append(f'"{it["label"]}"')
        if it.get("type"):
            parts.append(f'({it["type"]})')
        if it.get("value"):
            parts.append(f'= {it["value"]!r}')
        if it.get("href"):
            parts.append(f'→ {it["href"]}')
        if it.get("options"):
            parts.append("options: " + ", ".join(it["options"]))
        lines.append(" ".join(str(p) for p in parts if p))
    return safety.wrap_untrusted(livebrowser.LATEST.get("url", ""), "\n".join(lines))


@tool({
    "type": "function",
    "function": {
        "name": "browser_fill",
        "description": "Type text into a form field on the current page (search box, login, etc.). "
                       "Target the field by its [ref] from browser_snapshot (most reliable) or by its "
                       "label/placeholder text. Set enter=true to press Enter after — e.g. to run a "
                       "search or submit a form.",
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string", "description": "a [ref] number from browser_snapshot, or the field's label/placeholder"},
            "text": {"type": "string", "description": "the text to type into the field"},
            "enter": {"type": "boolean", "description": "press Enter after filling (submit)"},
        }, "required": ["field", "text"]},
    },
})
def browser_fill(field, text, enter=False):
    # This is EGRESS, not navigation. `text` is arbitrary model-controlled content and enter=True
    # submits the form in the same call, so an injected page can have the conversation pasted into
    # a field it controls. Classifying it with browser_open/click as "the read path" was wrong:
    # clicking a link is reading, typing a payload into someone else's form is sending. Navigation
    # and GET fetching stay open, so multi-page research still works.
    refusal = safety.egress_blocked()
    if refusal:
        return refusal
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.fill(field, text, enter=enter)
    if not r.get("ok"):
        return f"could not fill {field!r}: {r.get('error')}"
    if enter:                                        # submitted → show the resulting page
        return safety.wrap_untrusted(r.get("url", ""), livebrowser.read_text())
    return f"filled {field!r}" + (f" — at {r.get('url')}" if r.get("url") else "")


@tool({
    "type": "function",
    "function": {
        "name": "browser_select",
        "description": "Choose an option in a dropdown (<select>) on the current page. Target the "
                       "dropdown by its [ref] from browser_snapshot or by label; `option` is the "
                       "visible option text.",
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string", "description": "a [ref] number from browser_snapshot, or the dropdown's label"},
            "option": {"type": "string", "description": "the visible text of the option to choose"},
        }, "required": ["field", "option"]},
    },
})
def browser_select(field, option):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.select(field, option)
    if not r.get("ok"):
        return f"could not select {option!r} in {field!r}: {r.get('error')}"
    return f"selected {option!r} in {field!r}"


@tool({
    "type": "function",
    "function": {
        "name": "browser_press",
        "description": "Press a keyboard key on the current page — 'Enter' to submit, 'Escape' to "
                       "dismiss, 'Tab' to move between fields, 'PageDown', etc. (To type text into a "
                       "field, use browser_fill.)",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "the key to press, e.g. Enter, Escape, Tab, PageDown"},
        }, "required": ["key"]},
    },
})
def browser_press(key):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.press(key)
    if not r.get("ok"):
        return f"could not press {key!r}: {r.get('error')}"
    if str(key).lower() in ("enter", "return"):      # likely submitted/navigated → show the page
        return safety.wrap_untrusted(r.get("url", ""), livebrowser.read_text())
    return f"pressed {key}" + (f" — at {r.get('url')}" if r.get("url") else "")


@tool({
    "type": "function",
    "function": {
        "name": "browser_wait",
        "description": "Wait for the current page to be ready before acting — for JS/SPA pages that "
                       "load content after navigation. mode='text' waits for visible text to appear, "
                       "'selector' for a CSS selector, 'load' for the page to settle (networkidle), "
                       "'time' for a fixed pause. Times out (default 8s) and reports if not ready.",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "description": "text | selector | load | time"},
            "value": {"type": "string", "description": "the text or CSS selector to wait for (mode text/selector)"},
            "timeout_ms": {"type": "integer", "description": "max wait in ms, default 8000 (cap 25000)"},
        }},
    },
})
def browser_wait(mode="text", value="", timeout_ms=8000):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.wait_for(mode, value, int(timeout_ms))
    return "ready" if r.get("ok") else f"still not ready ({mode} {value!r}) — {r.get('error')}"


@tool({
    "type": "function",
    "function": {
        "name": "browser_extract",
        "description": "Pull structured data from the current page by CSS selector — returns each "
                       "matching element's text, or a given attribute (e.g. href). Use for "
                       "lists/tables/search results instead of reading the whole page. Examples: "
                       "selector='h3' ; selector='a.result' attr='href' ; selector='table tr td'.",
        "parameters": {"type": "object", "properties": {
            "selector": {"type": "string", "description": "a CSS selector"},
            "attr": {"type": "string", "description": "optional attribute to return instead of text (e.g. href, src)"},
            "limit": {"type": "integer", "description": "max matches to return, default 30 (cap 100)"},
        }, "required": ["selector"]},
    },
})
def browser_extract(selector, attr=None, limit=30):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.extract(selector, attr=attr, limit=int(limit))
    if not r.get("ok"):
        return f"could not extract {selector!r}: {r.get('error')}"
    results = r.get("results") or []
    if not results:
        return f"(no elements matched {selector!r})"
    return safety.wrap_untrusted(livebrowser.LATEST.get("url", ""), "\n".join(f"- {x}" for x in results))


@tool({
    "type": "function",
    "function": {
        "name": "browser_read",
        "description": "Read the CURRENT browser page as clean markdown — headings marked, links "
                       "inlined as 'text <url>', scripts/boilerplate stripped. Better than a raw text "
                       "dump when you need the page's structure and link targets after clicking or "
                       "navigating (fetch_url is still best for opening a fresh URL).",
        "parameters": {"type": "object", "properties": {}},
    },
})
def browser_read():
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.read_markdown()
    return safety.wrap_untrusted(livebrowser.LATEST.get("url", ""), r.get("text") or "(empty page)")


@tool({
    "type": "function",
    "function": {
        "name": "browser_eval",
        "description": "Run JavaScript in the CURRENT page and return its result — a power tool for "
                       "reading or manipulating anything the other browser tools can't. Return a value "
                       "from the JS to get it back, e.g. 'document.title' or '[...document.images].map("
                       "i=>i.src)'. WEB-UI ONLY — blocked in background/scheduled runs.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "JavaScript expression/function to evaluate in the page"},
        }, "required": ["code"]},
    },
})
def browser_eval(code):
    refusal = safety.egress_blocked()      # arbitrary JS in a profile holding live logins
    if refusal:
        return refusal
    if not live_browser_available():
        return _BG_BROWSER_NOTE      # inherently web-only → no arbitrary JS in unattended runs
    r = livebrowser.evaluate_js(code)
    if not r.get("ok"):
        return f"eval error: {r.get('error')}"
    try:
        s = json.dumps(r.get("result"), default=str, ensure_ascii=False)
    except Exception:
        s = str(r.get("result"))
    return safety.wrap_untrusted(livebrowser.LATEST.get("url", ""), s[:4000])


@tool({
    "type": "function",
    "function": {
        "name": "browser_hover",
        "description": "Hover the mouse over an element to reveal a dropdown menu, tooltip, or hidden "
                       "controls. Target by [ref] from browser_snapshot or by visible text.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "a [ref] number from browser_snapshot, or visible text"},
        }, "required": ["target"]},
    },
})
def browser_hover(target):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.hover(target)
    return f"hovered {target!r}" if r.get("ok") else f"could not hover {target!r}: {r.get('error')}"


@tool({
    "type": "function",
    "function": {
        "name": "browser_upload",
        "description": "Upload a workspace file to a file-input on the current page. Target the file "
                       "input by [ref] from browser_snapshot or by label; `path` is a workspace file path.",
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string", "description": "a [ref] number from browser_snapshot, or the input's label"},
            "path": {"type": "string", "description": "workspace path of the file to upload"},
        }, "required": ["field", "path"]},
    },
})
def browser_upload(field, path):
    refusal = safety.egress_blocked()      # pushing a workspace file into someone else's form
    if refusal:
        return refusal
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    try:
        p = _resolve(path)
    except Exception as e:                           # noqa: BLE001 - path confinement refusal
        return f"(refused: {e})"
    if not p.is_file():
        return f"(no such file in the workspace: {path})"
    r = livebrowser.upload(field, [str(p)])
    return f"uploaded {p.name} to {field!r}" if r.get("ok") else f"could not upload: {r.get('error')}"


@tool({
    "type": "function",
    "function": {
        "name": "browser_dialog",
        "description": "Set how the page's JavaScript dialogs (alert/confirm/prompt) are handled from "
                       "now on: 'accept' to accept them (e.g. to confirm an action before the click "
                       "that triggers it), or 'dismiss' to cancel (the default). For a prompt, `text` "
                       "is the answer submitted.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "accept | dismiss"},
            "text": {"type": "string", "description": "answer for a prompt dialog (when accepting)"},
        }, "required": ["action"]},
    },
})
def browser_dialog(action, text=""):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    livebrowser.dialog(action, text)
    return "dialogs will be accepted" if action == "accept" else "dialogs will be dismissed"


@tool({
    "type": "function",
    "function": {
        "name": "browser_tab",
        "description": "Manage browser tabs: action='list' shows the open tabs with their ids, 'new' "
                       "opens a blank tab, 'switch' (with id) focuses a tab, 'close' (with id) closes "
                       "one. Tab ids come from 'list'.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "list | new | switch | close"},
            "id": {"type": "integer", "description": "tab id (for switch/close), from action=list"},
        }, "required": ["action"]},
    },
})
def browser_tab(action, id=None):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    action = (action or "list").lower()
    if action == "list":
        tabs = livebrowser.LATEST.get("tabs") or []
        if not tabs:
            return "(no tabs)"
        return "\n".join(f"[{t['id']}]{' *' if t.get('active') else ''} {t.get('title', '')} — {t.get('url', '')}"
                         for t in tabs)
    if action == "new":
        livebrowser.submit("new_tab")
        return "opened a new tab"
    if action in ("switch", "close"):
        if id is None:
            return f"{action} needs a tab id (from browser_tab list)"
        livebrowser.submit("switch_tab" if action == "switch" else "close_tab", int(id))
        return f"{'switched to' if action == 'switch' else 'closed'} tab {id}"
    return "action must be one of: list, new, switch, close"
