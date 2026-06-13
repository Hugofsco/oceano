"""The agent's tools. Each tool = a JSON schema (shown to the model) + a Python
function (run by us). Add a tool by writing a function and decorating it.

File/shell ops default to the WORKSPACE folder so the agent has a real place to
work, without roaming the whole disk. Set OCEANO_CONFINE=0 to lift the fence.
"""
import contextlib
import json
import subprocess
import sys
import threading
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import config
from oceano import memory, skills, rag, browser, scheduler, safety, livebrowser, atomicio

# --- channels --------------------------------------------------------------
# Oceano is driven from several places, and they don't share a screen. The live
# browser is ONE Chromium streamed to the WEB UI — so only the "web" channel may
# drive it. Telegram (the user can't see the browser) and unattended jobs
# (Researcher / scheduler / evals — nobody is watching) must NOT, or they'd hijack
# whatever the web view is showing. Off-web channels fall back to a plain HTTP
# fetch and decline the interactive browser tools. The channel is thread-local
# because each frontend/job runs on its own thread and drives tools synchronously.
#   web        → full interactive: live browser + screenshots
#   telegram   → attended chat, but no shared browser (HTTP fetch instead)
#   background → unattended job (Researcher/scheduler/evals): no browser
_local = threading.local()


def current_channel():
    return getattr(_local, "channel", "web")


def live_browser_available():
    """True only on the web channel — the only place a human can see the shared browser."""
    return current_channel() == "web"


def is_background():
    """An unattended job (no human in the loop) — distinct from an attended Telegram chat."""
    return current_channel() == "background"


@contextlib.contextmanager
def channel(name):
    """Run the enclosed agent work as a given channel (web/telegram/background)."""
    prev = getattr(_local, "channel", "web")
    _local.channel = name
    try:
        yield
    finally:
        _local.channel = prev


@contextlib.contextmanager
def background():
    """Run unattended agent work — no shared live browser (Researcher/scheduler/evals)."""
    with channel("background"):
        yield


def _ws():
    """The workspace root for file/shell tools on THIS thread — a per-run override
    (set by the eval harness for isolation) or the global workspace by default."""
    return getattr(_local, "workspace", None) or config.WORKSPACE


@contextlib.contextmanager
def background_workspace(path):
    """Redirect this thread's file/shell tools to an isolated root (used by the eval
    harness so each case runs in a clean, throwaway workspace). Implies background()."""
    from pathlib import Path as _P
    root = _P(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prev_ws = getattr(_local, "workspace", None)
    prev_ch = getattr(_local, "channel", "web")
    _local.workspace = root
    _local.channel = "background"
    try:
        yield root
    finally:
        _local.workspace = prev_ws
        _local.channel = prev_ch


# --- registry --------------------------------------------------------------
_TOOLS = {}        # name -> python function
_SCHEMAS = []      # list of OpenAI tool schemas


def tool(schema):
    """Decorator: register a function as a tool with the given JSON schema."""
    def wrap(fn):
        _TOOLS[schema["function"]["name"]] = fn
        _SCHEMAS.append(schema)
        return fn
    return wrap


def register(name, schema, fn):
    """Register (or replace) a tool at runtime — used by the MCP client to expose
    external servers' tools alongside the built-in ones."""
    _TOOLS[name] = fn
    _SCHEMAS[:] = [s for s in _SCHEMAS if s["function"]["name"] != name] + [schema]


def unregister_prefix(prefix):
    """Drop all tools whose name starts with `prefix` (e.g. reconnecting MCP)."""
    for n in [n for n in _TOOLS if n.startswith(prefix)]:
        _TOOLS.pop(n, None)
    _SCHEMAS[:] = [s for s in _SCHEMAS if not s["function"]["name"].startswith(prefix)]


# --- per-tool enable/disable + chat-mode memory tools (Settings → Tools) -----
# Persisted so a user can hide tools from the model — turning one off removes it from
# the prompt, shrinking the context. Stored by NAME so it survives MCP reconnects
# (which re-register tools) and process restarts.
_STATE_PATH = config.WORKSPACE.parent / "data" / "tools.json"
_DISABLED = set()      # tools withheld from the model entirely (both modes)
_CHAT_OFF = set()      # memory tools the user turned OFF for plain chat mode specifically

# Memory tools that may be exposed in plain chat mode (Agent mode off), so the model can
# still recall/manage what it knows about the user without full tool access.
MEMORY_TOOLS = ("recall", "remember", "update_memory", "forget_memory")


def _load_state():
    global _DISABLED, _CHAT_OFF
    try:
        d = json.loads(_STATE_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    _DISABLED = set(d.get("disabled", []))
    _CHAT_OFF = set(d.get("chat_off", []))


def _save_state():
    try:
        atomicio.write_text(_STATE_PATH, json.dumps({"disabled": sorted(_DISABLED), "chat_off": sorted(_CHAT_OFF)}))
    except OSError:
        pass


_load_state()


def all_schemas():
    """Every registered tool schema, enabled or not — for the Settings → Tools list."""
    return list(_SCHEMAS)


def schemas():
    """Tool schemas EXPOSED to the model. Disabled tools (Settings → Tools) are withheld,
    so turning a tool off removes it from the prompt and lowers the context cost."""
    return [s for s in _SCHEMAS if s["function"]["name"] not in _DISABLED]


def is_enabled(name):
    return name not in _DISABLED


def set_enabled(name, on):
    _DISABLED.discard(name) if on else _DISABLED.add(name)
    _save_state()


def set_all(on):
    """Enable or disable every currently-registered tool at once."""
    global _DISABLED
    _DISABLED = set() if on else {s["function"]["name"] for s in _SCHEMAS}
    _save_state()


def chat_tools():
    """Tool names available in plain chat mode (Agent mode off): the user-kept memory tools,
    intersected with globally-enabled tools. Empty list → chat mode is fully tool-free."""
    return [m for m in MEMORY_TOOLS if m not in _CHAT_OFF and m not in _DISABLED]


def chat_tool_state():
    """For the Settings UI: each memory tool with its chat-mode + global state + description."""
    by_name = {s["function"]["name"]: s["function"].get("description", "") for s in _SCHEMAS}
    return [{"name": m, "description": by_name.get(m, ""), "in_chat": m not in _CHAT_OFF,
             "enabled": m not in _DISABLED} for m in MEMORY_TOOLS if m in by_name]


def set_chat_tool(name, on):
    """Toggle whether a memory tool is offered in plain chat mode."""
    if name not in MEMORY_TOOLS:
        return
    _CHAT_OFF.discard(name) if on else _CHAT_OFF.add(name)
    _save_state()


def run(name, arguments_json):
    """Execute a tool call and return its result as a string (always a string —
    that's what we feed back to the model)."""
    if name in _DISABLED:                         # the model can't see it, but never run it anyway
        return f"ERROR: tool {name!r} is disabled in Settings → Tools"
    fn = _TOOLS.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name!r}"
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return f"ERROR: bad arguments JSON: {e}"
    try:
        return str(fn(**args))
    except Exception as e:                       # tools should never crash the loop
        return f"ERROR running {name}: {e}"


def _resolve(path: str) -> Path:
    """Resolve a user/model-supplied path against the (possibly overridden) workspace."""
    root = _ws()
    p = (root / path).resolve()
    # is_relative_to (not startswith): a plain prefix match lets '/ws-evil' slip
    # past a workspace of '/ws'. root is already resolved.
    if config.CONFINE_TO_WORKSPACE and not p.is_relative_to(root):
        raise ValueError(f"path {path!r} escapes the workspace")
    return p


# --- tools -----------------------------------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List files and folders in the workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "subdir relative to workspace, default '.'"}
        }},
    },
})
def list_files(path="."):
    base = _resolve(path)
    if not base.exists():
        return f"(no such path: {path})"
    return "\n".join(sorted(
        f"{'DIR ' if c.is_dir() else 'FILE'}  {c.relative_to(_ws())}"
        for c in base.iterdir()
    )) or "(empty)"


@tool({
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}
        }, "required": ["path"]},
    },
})
def read_file(path):
    return _resolve(path).read_text(encoding="utf-8", errors="replace")[:20000]


@tool({
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file in the workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    },
})
def write_file(path, content):
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {p.relative_to(_ws())}"


@tool({
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Edit part of an existing workspace text file by replacing an EXACT "
                       "substring — safer/cheaper than rewriting the whole file with write_file. "
                       "Read the file first and copy the exact text (including indentation) into "
                       "`find`. Fails if `find` isn't found verbatim.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "find": {"type": "string", "description": "exact text to replace (copy it verbatim from the file)"},
            "replace": {"type": "string", "description": "the new text"},
        }, "required": ["path", "find", "replace"]},
    },
})
def edit_file(path, find, replace):
    p = _resolve(path)
    if not p.is_file():
        return f"(no such file: {path} — use write_file to create it)"
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(find)
    if n == 0:
        return ("ERROR: the `find` text was not found verbatim. Read the file and copy the exact "
                "text (including whitespace) you want to replace.")
    p.write_text(text.replace(find, replace), encoding="utf-8")
    return f"edited {p.relative_to(_ws())}: replaced {n} occurrence(s)"


@tool({
    "type": "function",
    "function": {
        "name": "make_folder",
        "description": "Create a folder (directory) in the workspace, including any parent folders.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}
        }, "required": ["path"]},
    },
})
def make_folder(path):
    p = _resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"created folder {p.relative_to(_ws())}"


@tool({
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a bash command in the workspace and return its output. "
                       "Use for builds, scripts, git, etc.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}
        }, "required": ["command"]},
    },
})
def run_shell(command):
    refusal = safety.check_shell(command)
    if refusal:
        return refusal
    r = subprocess.run(
        command, shell=True, cwd=str(_ws()),
        capture_output=True, text=True, timeout=config.SHELL_TIMEOUT,
    )
    out = (r.stdout + r.stderr).strip()
    return f"(exit {r.returncode})\n{out[:8000]}" or f"(exit {r.returncode}, no output)"


_HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _http_fetch(url, max_redirects=4):
    """Fetch + extract readable text over plain HTTP — no shared browser, no live
    frames. Used by background jobs. Redirects are followed manually so every hop
    is re-checked against the SSRF guard (a fetched page could 302 to an internal
    address). Returns extracted text, or an error string."""
    for _ in range(max_redirects + 1):
        refusal = safety.check_url(url)
        if refusal:
            return refusal
        try:
            r = requests.get(url, timeout=25, allow_redirects=False, headers=_HTTP_HEADERS)
        except requests.RequestException as e:
            return f"(could not fetch {url}: {type(e).__name__})"
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            url = requests.compat.urljoin(url, loc)
            continue
        if r.status_code >= 400:
            return f"(HTTP {r.status_code} fetching {url})"
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            tag.decompose()
        text = "\n".join(line for line in (ln.strip() for ln in soup.get_text("\n").splitlines()) if line)
        return text[:6000] or "(page had no readable text)"
    return f"(too many redirects fetching {url})"


@tool({
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information. Returns top results.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def web_search(query):
    if live_browser_available():
        livebrowser.start_research()  # fresh research tab-group; results open as tabs via fetch_url
    r = requests.get(
        f"{config.SEARXNG_URL}/search",
        params={"q": query, "format": "json"},
        timeout=20,
    )
    r.raise_for_status()
    hits = r.json().get("results", [])[:5]
    if not hits:
        return "(no results)"
    body = "\n\n".join(
        f"{h.get('title','')}\n{h.get('url','')}\n{h.get('content','')}" for h in hits
    )
    return safety.wrap_untrusted(f"web_search:{query}", body)


@tool({
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch a web page and return its readable text. Use after "
                       "web_search to actually read a result.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}
        }, "required": ["url"]},
    },
})
def fetch_url(url):
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    if not live_browser_available():  # off-web channel → plain HTTP, never the shared browser
        return safety.wrap_untrusted(url, _http_fetch(url))
    # Web channel: read the page in the real headless browser — renders JS the plain
    # path can't, AND streams frames to the Live browser window so you watch it read.
    text = browser.open_url(url)
    return safety.wrap_untrusted(url, text)


@tool({
    "type": "function",
    "function": {
        "name": "python_exec",
        "description": "Run a Python snippet in the workspace and return stdout/stderr. "
                       "Good for calculations, data wrangling, quick scripts.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}
        }, "required": ["code"]},
    },
})
def python_exec(code):
    r = subprocess.run(
        [sys.executable, "-"], input=code, cwd=str(_ws()),
        capture_output=True, text=True, timeout=config.SHELL_TIMEOUT,
    )
    out = (r.stdout + r.stderr).strip()
    return out[:8000] or f"(exit {r.returncode}, no output)"


@tool({
    "type": "function",
    "function": {
        "name": "remember",
        "description": "Save a durable fact, preference, or note to long-term memory "
                       "so you recall it in future conversations. Pick the category that "
                       "fits best: identity (who the user is), preference (what they like/"
                       "want/prefer), project (ongoing work or goals), task (something to "
                       "do), fact (anything else durable).",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "category": {"type": "string", "enum": memory.CATEGORIES,
                         "description": "memory category — controls when it is injected"},
            "tags": {"type": "string", "description": "optional comma-separated tags"},
        }, "required": ["text", "category"]},
    },
})
def remember(text, category="fact", tags=""):
    return memory.remember(text, tags, category=category)


@tool({
    "type": "function",
    "function": {
        "name": "recall",
        "description": "Search long-term memory for facts relevant to a query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def recall(query):
    return memory.recall(query)


@tool({
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": "Correct a stored memory when something you know becomes wrong or "
                       "out of date. Describe the existing memory in `about`; it's replaced "
                       "with `new_text`. If nothing close is stored, `new_text` is saved as new.",
        "parameters": {"type": "object", "properties": {
            "about": {"type": "string", "description": "what the old/wrong memory is about"},
            "new_text": {"type": "string", "description": "the corrected fact to store"},
        }, "required": ["about", "new_text"]},
    },
})
def update_memory(about, new_text):
    m = memory.best_match(about)
    if not m or m["score"] < 0.5:
        memory.remember(new_text)
        return f"no close existing memory — saved as new: {new_text!r}"
    memory.update(m["id"], new_text)
    return f"updated memory → {new_text!r}  (was: {m['text']!r})"


@tool({
    "type": "function",
    "function": {
        "name": "forget_memory",
        "description": "Delete a stored memory that is no longer true or relevant. Describe "
                       "it in `about`; the closest-matching memory is removed.",
        "parameters": {"type": "object", "properties": {
            "about": {"type": "string", "description": "what the memory to forget is about"},
        }, "required": ["about"]},
    },
})
def forget_memory(about):
    m = memory.best_match(about)
    if not m or m["score"] < 0.5:
        return f"no clearly-matching memory found for {about!r} — nothing forgotten"
    memory.forget(m["id"])
    return f"forgot: {m['text']!r}"


# --- skills ----------------------------------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "list_skills",
        "description": "List available skills (reusable procedures) with their descriptions. "
                       "Check this when a task might match a known skill.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_skills():
    return skills.list_skills()


@tool({
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "Load the full step-by-step instructions for a named skill, then follow them.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}
        }, "required": ["name"]},
    },
})
def load_skill(name):
    return skills.load_skill(name)


# --- RAG over the user's documents -----------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "index_docs",
        "description": "Index a folder of the user's documents (txt/md/pdf/code) for later search.",
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "absolute path, or relative to workspace"}
        }, "required": ["folder"]},
    },
})
def index_docs(folder):
    return rag.index_docs(folder)


@tool({
    "type": "function",
    "function": {
        "name": "search_docs",
        "description": "Search the user's indexed documents by meaning and return relevant passages. "
                       "Use this to answer questions about their files.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def search_docs(query):
    return safety.wrap_untrusted("documents", rag.search_docs(query))


@tool({
    "type": "function",
    "function": {
        "name": "search_chats",
        "description": "Search the user's PAST conversations by meaning, to recall what was "
                       "discussed or decided before. Use this when the user refers to an earlier "
                       "chat ('what did we decide about…', 'the conversation where we…') or you "
                       "need context from prior sessions. Returns the closest conversations with a "
                       "title, date, and snippet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}
        }, "required": ["query"]},
    },
})
def search_chats(query):
    from oceano import chats
    res = chats.search(query, k=5)
    if not res:
        return "(no matching past conversations)"
    return "\n".join(f"- [{r['date']}] {r['title']}: {r['snippet'][:160]}" for r in res)


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
        "description": "Click an element (link/button) on the CURRENT browser page by its visible "
                       "text. Use after browser_open/fetch_url to interact with a page step by step.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "visible text of the link/button to click"}
        }, "required": ["text"]},
    },
})
def browser_click(text):
    if not live_browser_available():
        return _BG_BROWSER_NOTE
    r = livebrowser.click_text(text)
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


# --- scheduled tasks + notifications ---------------------------------------
@tool({
    "type": "function",
    "function": {
        "name": "schedule_task",
        "description": "Schedule an instruction to run automatically on a cron schedule. "
                       "Example cron: '0 8 * * *' = every day at 08:00.",
        "parameters": {"type": "object", "properties": {
            "cron": {"type": "string"},
            "instruction": {"type": "string"},
        }, "required": ["cron", "instruction"]},
    },
})
def schedule_task(cron, instruction):
    return scheduler.schedule_task(cron, instruction)


@tool({
    "type": "function",
    "function": {
        "name": "list_tasks",
        "description": "List the user's scheduled tasks.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_tasks():
    return scheduler.list_tasks()


@tool({
    "type": "function",
    "function": {
        "name": "run_workflow",
        "description": "Run one of the user's saved workflows (a named, multi-step recipe) "
                       "right now, by name or id. Use this when the user asks to run a workflow, "
                       "or when a task matches a workflow they've defined. You can RUN workflows "
                       "but not create them — the user authors workflows in the UI. To see what "
                       "exists, call list_workflows first.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "the workflow's name (or its numeric id)"},
        }, "required": ["name"]},
    },
})
def run_workflow(name):
    from oceano import workflows
    name = str(name or "").strip()
    wf = workflows.get_by_name(name)
    if not wf and name.isdigit():
        wf = workflows.get(int(name))
    if not wf:
        avail = ", ".join(w["name"] for w in workflows.list_all()) or "(none defined)"
        return f"no workflow named {name!r}. Available: {avail}"
    rec = workflows.run(wf, trigger="agent")
    lines = [f"Workflow '{wf['name']}' — {rec['summary']}"]
    for s in rec.get("steps", []):
        mark = "✓" if s["ok"] else "✗"
        lines.append(f"  {mark} {s['label']}: {(s['output'] or '').strip()[:240]}")
    return "\n".join(lines)


@tool({
    "type": "function",
    "function": {
        "name": "list_workflows",
        "description": "List the user's saved workflows (name, description, step count) so you "
                       "know which ones can be run with run_workflow.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_workflows():
    from oceano import workflows
    wfs = workflows.list_all()
    if not wfs:
        return "(no workflows defined yet — the user can create them in the Workflows window)"
    def _nodes(w):
        return len([n for n in w.get("graph", {}).get("nodes", []) if n.get("type") not in ("start", "end")])
    return "\n".join(f"- {w['name']} ({_nodes(w)} nodes)"
                     + (f": {w['description']}" if w.get("description") else "") for w in wfs)


@tool({
    "type": "function",
    "function": {
        "name": "notify",
        "description": "Send a push notification to the user's phone (ntfy). "
                       "Use to report when a long task is finished.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string"},
            "title": {"type": "string"},
        }, "required": ["message"]},
    },
})
def notify(message, title="Oceano"):
    return scheduler.notify(message, title)


# --- self-improvement: learned skills + delegation ---------------------------
@tool({
    "type": "function",
    "function": {
        "name": "learn_skill",
        "description": "Save a NEW reusable skill you worked out during this task, for your "
                       "future self. It is stored as 'learning' and reviewed by an independent "
                       "model before being published into your active skills. Use only for "
                       "genuinely reusable know-how, not one-off details.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "short kebab-case name, e.g. scrape-paginated-site"},
            "description": {"type": "string", "description": "one line: when should this skill be used?"},
            "body": {"type": "string", "description": "the instructions: short imperative steps"},
        }, "required": ["name", "description", "body"]},
    },
})
def learn_skill(name, description, body):
    return skills.learn_skill(name, description, body)


@tool({
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "Hand a self-contained subtask to the configured delegate — a stronger "
                       "assistant running headless in the workspace. WHO that is, is set by the user "
                       "in Settings → Delegation (Claude Code by default, or a cloud model run as a "
                       "full agent); you don't choose — just delegate. Either way it can read, write, "
                       "and run things to complete the task. Give precise, complete instructions — "
                       "the relevant file paths and exactly what it must produce — because it cannot "
                       "ask you questions. Returns its final report. CALL THIS whenever the user asks "
                       "you to 'delegate' or 'have the strong model do it', or for a heavy subtask "
                       "beyond you. The capability is available — don't claim you can't.",
        "parameters": {"type": "object", "properties": {
            "instructions": {"type": "string"},
        }, "required": ["instructions"]},
    },
})
def delegate_tool(instructions):
    from oceano import delegate
    r = delegate.run(instructions, cwd=config.WORKSPACE)   # honours Settings → Delegation (default role)
    if not r["ok"]:
        return f"delegation failed: {r['error']}"
    return r["output"][:8000] or "(the delegate finished but returned no text)"


# back-compat: the tool was once 'delegate_to_claude'. Keep the old name callable (not shown
# to the model) so any saved reference still routes to the generalized delegate.
_TOOLS["delegate_to_claude"] = delegate_tool


# --- calendar (local copy, synced from Google Calendar / any ICS feed) -------
@tool({
    "type": "function",
    "function": {
        "name": "calendar_events",
        "description": "Read the user's calendar: upcoming events for the next N days. "
                       "This is the local copy, kept in sync from their Google Calendar — "
                       "use it whenever the user asks about their schedule, availability, "
                       "appointments, or plans.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "how many days ahead to look (default 7)"},
        }},
    },
})
def calendar_events(days=7):
    from oceano import calsync
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 7
    return calsync.agenda(days)
