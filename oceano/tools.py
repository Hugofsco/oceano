"""The agent's tools. Each tool = a JSON schema (shown to the model) + a Python
function (run by us). Add a tool by writing a function and decorating it.

File/shell ops default to the WORKSPACE folder so the agent has a real place to
work, without roaming the whole disk. Set OCEANO_CONFINE=0 to lift the fence.
"""
import json
import subprocess
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import config
from oceano import memory, skills, rag, browser, scheduler, safety, livebrowser

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


def schemas():
    return _SCHEMAS


def run(name, arguments_json):
    """Execute a tool call and return its result as a string (always a string —
    that's what we feed back to the model)."""
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
    """Resolve a user/model-supplied path against the workspace, with a fence."""
    p = (config.WORKSPACE / path).resolve()
    # is_relative_to (not startswith): a plain prefix match lets '/ws-evil' slip
    # past a workspace of '/ws'. config.WORKSPACE is already resolved.
    if config.CONFINE_TO_WORKSPACE and not p.is_relative_to(config.WORKSPACE):
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
        f"{'DIR ' if c.is_dir() else 'FILE'}  {c.relative_to(config.WORKSPACE)}"
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
    return f"wrote {len(content)} chars to {p.relative_to(config.WORKSPACE)}"


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
    return f"edited {p.relative_to(config.WORKSPACE)}: replaced {n} occurrence(s)"


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
    return f"created folder {p.relative_to(config.WORKSPACE)}"


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
        command, shell=True, cwd=str(config.WORKSPACE),
        capture_output=True, text=True, timeout=config.SHELL_TIMEOUT,
    )
    out = (r.stdout + r.stderr).strip()
    return f"(exit {r.returncode})\n{out[:8000]}" or f"(exit {r.returncode}, no output)"


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
    # Read the page in the real headless browser: renders JS the requests/BS4 path
    # couldn't, AND streams frames to the Live browser window so you watch it read.
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
        [sys.executable, "-"], input=code, cwd=str(config.WORKSPACE),
        capture_output=True, text=True, timeout=config.SHELL_TIMEOUT,
    )
    out = (r.stdout + r.stderr).strip()
    return out[:8000] or f"(exit {r.returncode}, no output)"


@tool({
    "type": "function",
    "function": {
        "name": "remember",
        "description": "Save a durable fact, preference, or note to long-term memory "
                       "so you recall it in future conversations.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "tags": {"type": "string", "description": "optional comma-separated tags"},
        }, "required": ["text"]},
    },
})
def remember(text, tags=""):
    return memory.remember(text, tags)


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


# --- headless browser ------------------------------------------------------
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
    return browser.screenshot(url, name)


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
