"""File and folder tools over the (confined) workspace."""
from oceano.tools.core import _resolve, _ws, tool

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
