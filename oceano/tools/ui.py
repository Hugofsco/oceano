"""Web-UI control: open / close / arrange the floating-window desktop."""
from oceano.tools.core import live_browser_available, tool

# ============================ web-UI control (open / arrange windows) ============================
# Drive the floating-window desktop on the WEB channel — open files/windows and lay them out — by
# pushing commands to the connected browser (oceano/uibridge). Gated to "web" like the live browser
# (a human is watching); the vocabulary is allowlisted, so a command can only reach the UI's known,
# safe window openers — never arbitrary code.
_UI_WINDOWS = {"files", "explorer", "preview", "calendar", "brain", "memory", "knowledge", "skills",
               "rivers", "evals", "memory-graph", "scheduler", "researcher", "notes", "health",
               "search", "voice", "workflows", "live", "logs", "hosts", "terminal", "settings"}
# whole-desktop modes + single-window modes (the positional ones snap to a half/quarter/maximize)
_UI_POS = {"left", "right", "top", "bottom", "maximize",
           "top-left", "top-right", "bottom-left", "bottom-right"}
_UI_ARRANGE = {"tile", "cascade", "focus", "center", "minimize"} | _UI_POS


def _ui_push(action, **payload):
    """Push a UI command to the browser; returns a guard message if it can't, else None (pushed)."""
    if not live_browser_available():
        return "(window control is only available in the web UI — not on this channel)"
    from oceano import uibridge
    if not uibridge.listener_count():
        return "(no web UI is connected right now, so there's nothing to act on)"
    uibridge.push({"type": "ui", "action": action, **payload})
    return None


@tool({
    "type": "function",
    "function": {
        "name": "ui_open",
        "description": "Open a window or a file in the user's web UI so they SEE it — e.g. pop a "
                       "Preview of a file you just wrote, or open the Calendar before discussing their "
                       "schedule. Pass a `window` name OR a `path` to a workspace file/folder. (Web UI "
                       "only — does nothing on Telegram or background jobs.)",
        "parameters": {"type": "object", "properties": {
            "window": {"type": "string", "description": "one of: files, preview, calendar, brain, "
                       "memory, knowledge, skills, rivers, evals, memory-graph, scheduler, researcher, "
                       "notes, health, search, voice, workflows, live, logs (activity & system journal), "
                       "hosts (SSH servers), terminal (a workspace shell, or a live SSH session if you "
                       "pass `host`), settings"},
            "path": {"type": "string", "description": "a workspace file (opens a preview if renderable, "
                     "else the editor) or a folder (opens the Files explorer there)"},
            "host": {"type": "string", "description": "for window='terminal': a registered host name to "
                     "open a LIVE SSH session into (it must be armed/trusted in the Hosts panel). Omit "
                     "for a local workspace shell."},
        }},
    },
})
def ui_open(window="", path="", host=""):
    if path:
        return _ui_push("open", path=str(path)) or f"opened {path} in the web UI"
    window = (window or "").strip().lower()
    if window not in _UI_WINDOWS:
        return f"unknown window {window!r}. Use one of: {', '.join(sorted(_UI_WINDOWS))} — or pass a file path."
    payload = {"window": window}
    if window == "terminal" and host:
        payload["host"] = str(host)
    guard = _ui_push("open", **payload)
    if guard:
        return guard
    return f"opened the {window} window" + (f" (live SSH session into {host})" if window == "terminal" and host else "")


@tool({
    "type": "function",
    "function": {
        "name": "ui_close",
        "description": "Close one of the user's open web-UI windows by name (same names as ui_open).",
        "parameters": {"type": "object", "properties": {
            "window": {"type": "string"},
        }, "required": ["window"]},
    },
})
def ui_close(window):
    window = (window or "").strip().lower()
    if window not in _UI_WINDOWS:
        return f"unknown window {window!r}. Use one of: {', '.join(sorted(_UI_WINDOWS))}."
    return _ui_push("close", window=window) or f"closed the {window} window"


@tool({
    "type": "function",
    "function": {
        "name": "ui_arrange",
        "description": "Arrange the user's web-UI windows. Whole desktop: 'tile' or 'cascade' all of "
                       "them. A SINGLE window — snap it to a side ('left'/'right'/'top'/'bottom'), a "
                       "corner ('top-left'/'top-right'/'bottom-left'/'bottom-right'), or 'maximize' / "
                       "'center' / 'focus' / 'minimize'. The `window` is optional for these — omit it "
                       "to act on the front (active) window, e.g. 'move it to the right'.",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "description": "tile | cascade | left | right | top | bottom | "
                     "top-left | top-right | bottom-left | bottom-right | maximize | center | focus | minimize"},
            "window": {"type": "string", "description": "target window name; omit for the active window"},
        }, "required": ["mode"]},
    },
})
def ui_arrange(mode, window=""):
    mode = (mode or "").strip().lower()
    if mode not in _UI_ARRANGE:
        return f"unknown mode {mode!r}. Use one of: {', '.join(sorted(_UI_ARRANGE))}."
    if mode in ("tile", "cascade"):
        return _ui_push("arrange", mode=mode) or f"arranged windows ({mode})"
    window = (window or "").strip().lower()
    if window and window not in _UI_WINDOWS:           # window is optional → defaults to the active one
        return f"unknown window {window!r}. Use one of: {', '.join(sorted(_UI_WINDOWS))}."
    payload = {"mode": mode}
    if window:
        payload["window"] = window
    return _ui_push("arrange", **payload) or (f"{mode} → {window}" if window else f"{mode} the active window")
