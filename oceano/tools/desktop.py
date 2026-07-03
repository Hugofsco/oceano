"""Desktop-app-only tools: actions that need a real native OS process on the user's machine —
OceanoDesktop's Electron main process — not a browser tab. Even the web UI's own renderer has no
OS access by design (contextIsolation, no nodeIntegration), so these run over oceano.desktopbridge,
the request/response sibling of the uibridge push channel ui_open/ui_close/ui_arrange use, and block
briefly for the desktop app's answer (a chosen path, a notification actually shown).

Gated the same way ssh_run gates real-server access: only on a turn that came through the desktop
app (current_client() == "desktop", not just any "web" channel turn), AND never once this turn has
read untrusted content (a web page, email, or document) — an injected instruction must not be able
to pop a native dialog or notification on the user's real computer.
"""
import base64
import time
from pathlib import Path

import config
from oceano import safety
from oceano.tools.core import current_client, is_desktop_client, tool  # noqa: F401 (is_desktop_client re-exported)


def _desktop_gate():
    """Full gate for anything that DOES something on the user's real computer (a dialog, a
    notification, a clipboard write, opening/revealing a path, a screenshot)."""
    if not is_desktop_client():
        return "desktop tools are only available when chatting through the OceanoDesktop app."
    if safety.untrusted_seen() or safety.bridge_untrusted_seen():
        return ("Blocked for safety: this turn already read external content (a web page, email, or "
                "document), so triggering a native action on the user's computer is disabled this "
                "turn. Ask them to send a fresh message to use a desktop tool.")
    return None


def _client_gate():
    """Lighter gate for a pure READ (desktop_clipboard_read): no reason to block it just because
    this turn already read untrusted content — it's not destructive — but its OWN result gets
    fenced as untrusted content below, same as mail_read/fetch_url do for what THEY read."""
    if not is_desktop_client():
        return "desktop tools are only available when chatting through the OceanoDesktop app."
    return None


@tool({
    "type": "function",
    "function": {
        "name": "desktop_notify",
        "description": "Show a native OS notification on the user's computer via OceanoDesktop — "
                       "use it for something they should notice even if they've switched away from "
                       "Oceano (a finished job, something time-sensitive). Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["title", "body"]},
    },
})
def desktop_notify(title, body):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("notify", timeout=8, title=str(title)[:200], body=str(body)[:2000])
    if not ok:
        return f"couldn't show the notification: {result}"
    return "notification shown"


@tool({
    "type": "function",
    "function": {
        "name": "desktop_pick_file",
        "description": "Open a native file/folder picker on the user's computer via OceanoDesktop "
                       "and return the REAL absolute path they choose — use this instead of asking "
                       "them to type a path, or when you need to keep reading/writing the same file "
                       "on their disk (a browser upload only gives you a sandboxed copy). Blocks "
                       "until they choose or cancel. Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "dialog title, e.g. 'Choose a file to import'"},
            "kind": {"type": "string", "description": "'file' (default) or 'folder'"},
        }},
    },
})
def desktop_pick_file(title="Choose a file", kind="file"):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    kind = "folder" if str(kind).strip().lower() == "folder" else "file"
    ok, result = desktopbridge.call("pick-file", timeout=120, title=str(title)[:200], kind=kind)
    if not ok:
        return f"couldn't open the file picker: {result}"
    if not result:
        return "the user cancelled the picker"
    return f"chosen path: {result}"


@tool({
    "type": "function",
    "function": {
        "name": "desktop_save_file",
        "description": "Open a native 'Save As' dialog on the user's computer via OceanoDesktop and "
                       "return the REAL absolute path they choose — use this when you have content "
                       "ready to save (a report, an export) and want them to pick where. Blocks until "
                       "they choose or cancel. Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "dialog title, e.g. 'Save report'"},
            "default_name": {"type": "string", "description": "suggested filename, e.g. 'report.pdf'"},
        }},
    },
})
def desktop_save_file(title="Save file", default_name=""):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("save-file", timeout=120, title=str(title)[:200],
                                     default_name=str(default_name)[:200])
    if not ok:
        return f"couldn't open the save dialog: {result}"
    if not result:
        return "the user cancelled the save dialog"
    return f"save path: {result}"


@tool({
    "type": "function",
    "function": {
        "name": "desktop_reveal_path",
        "description": "Open the native file manager on the user's computer via OceanoDesktop, "
                       "highlighting the given file or folder — use this to show them where "
                       "something is, without opening it. Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
    },
})
def desktop_reveal_path(path):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("reveal-path", timeout=10, path=str(path))
    if not ok:
        return f"couldn't reveal {path!r}: {result}"
    return f"revealed {path!r} in the file manager"


@tool({
    "type": "function",
    "function": {
        "name": "desktop_open_path",
        "description": "Open a file or folder on the user's computer with its default application, "
                       "via OceanoDesktop (e.g. open a PDF in their PDF viewer). Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
    },
})
def desktop_open_path(path):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("open-path", timeout=10, path=str(path))
    if not ok:
        return f"couldn't open {path!r}: {result}"
    return f"opened {path!r}"


@tool({
    "type": "function",
    "function": {
        "name": "desktop_clipboard_read",
        "description": "Read the user's current clipboard text via OceanoDesktop — use when they say "
                       "something like 'I just copied this' or ask you to work with what they copied. "
                       "Desktop app only.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def desktop_clipboard_read():
    guard = _client_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("clipboard-read", timeout=8)
    if not ok:
        return f"couldn't read the clipboard: {result}"
    if not result:
        return "the clipboard is empty (or isn't plain text)"
    return safety.wrap_untrusted("clipboard", result)


@tool({
    "type": "function",
    "function": {
        "name": "desktop_clipboard_write",
        "description": "Copy text to the user's clipboard via OceanoDesktop, so they can paste it "
                       "somewhere else. Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
        }, "required": ["text"]},
    },
})
def desktop_clipboard_write(text):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("clipboard-write", timeout=8, text=str(text)[:20000])
    if not ok:
        return f"couldn't write to the clipboard: {result}"
    return "copied to clipboard"


@tool({
    "type": "function",
    "function": {
        "name": "desktop_screenshot",
        "description": "Capture a screenshot of what's actually showing on the user's screen right "
                       "now, via OceanoDesktop — saved into the workspace so it renders inline in "
                       "chat. Use this to see their real screen; for a page you're browsing, use "
                       "browser_screenshot instead. Desktop app only.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "filename to save as, e.g. 'screen.png' "
                     "(defaults to a timestamped name)"},
        }},
    },
})
def desktop_screenshot(name=""):
    guard = _desktop_gate()
    if guard:
        return guard
    from oceano import desktopbridge
    ok, result = desktopbridge.call("screenshot", timeout=15)
    if not ok:
        return f"couldn't capture the screen: {result}"
    fname = Path((name or f"desktop-screenshot-{int(time.time())}").strip()).name   # no path traversal via a crafted name
    if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
        fname += ".png"
    path = config.WORKSPACE / fname
    try:
        path.write_bytes(base64.b64decode(result))
    except Exception as e:
        return f"captured the screen but couldn't save it: {type(e).__name__}: {e}"
    return safety.wrap_untrusted("desktop-screenshot", f"saved a screenshot of the user's screen to "
                                  f"{fname}\n\n![screenshot]({fname})")
