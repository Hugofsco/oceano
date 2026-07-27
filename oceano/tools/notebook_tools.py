"""Notebook tools — the agent's access to the user's Notebook (oceano.notebook), the
longer-form Markdown sibling of the Kanban board. Free-text search only, no embeddings —
this isn't a second memory store (see oceano/notebook.py); use remember/recall for that."""
from oceano.tools.core import tool


def _note_line(n):
    tags = f" [{', '.join(n['tags'])}]" if n["tags"] else ""
    pin = "📌 " if n["pinned"] else ""
    preview = n["body"][:140].replace("\n", " ")
    return f"  {pin}#{n['id']}{tags} {n['title']}" + (f" — {preview}" if preview else "")


@tool({
    "type": "function",
    "function": {
        "name": "search_notebook",
        "description": "Search the user's Notebook (longer-form Markdown notes) by free-text "
                       "substring and/or an exact tag — both empty lists everything, "
                       "pinned-first then most-recently-updated. Returns a short preview of "
                       "each match; use get_note for the full body.",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "substring to match in title or body"},
            "tag": {"type": "string", "description": "an exact tag to filter by"},
        }},
    },
})
def search_notebook(q="", tag=""):
    from oceano import notebook
    notes = notebook.list_all(q=q, tag=tag)
    if not notes:
        return "(no matching notes)"
    return "\n".join(_note_line(n) for n in notes)


@tool({
    "type": "function",
    "function": {
        "name": "get_note",
        "description": "Read one Notebook note's full title, body, and tags by id (from "
                       "search_notebook).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the note id shown by search_notebook"},
        }, "required": ["id"]},
    },
})
def get_note(id):
    from oceano import notebook
    n = notebook.get(int(id))
    if not n:
        return f"no note #{id} — use search_notebook to see current ids"
    tags = f" [{', '.join(n['tags'])}]" if n["tags"] else ""
    pin = " 📌" if n["pinned"] else ""
    return f"#{n['id']}{tags}{pin} {n['title']}\n\n{n['body']}"


@tool({
    "type": "function",
    "function": {
        "name": "add_note",
        "description": "Add a new note to the user's Notebook.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "body": {"type": "string", "description": "Markdown body"},
            "tags": {"type": "array", "items": {"type": "string"}},
        }, "required": ["title"]},
    },
})
def add_note(title, body="", tags=None):
    from oceano import notebook
    n = notebook.create(title=title, body=body, tags=tags)
    return f"Added note '{n['title']}' (id {n['id']})."


@tool({
    "type": "function",
    "function": {
        "name": "update_note",
        "description": "Edit a Notebook note's title/body/tags, and/or pin/unpin it (pinned "
                       "notes sort first). Pass only the fields you want to change. Get the "
                       "id from search_notebook.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the note id shown by search_notebook"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "pinned": {"type": "boolean"},
        }, "required": ["id"]},
    },
})
def update_note(id, title=None, body=None, tags=None, pinned=None):
    from oceano import notebook
    ok = notebook.update(int(id), title=title, body=body, tags=tags, pinned=pinned)
    if not ok:
        return f"no note #{id} — use search_notebook to see current ids"
    return f"Updated note #{id}."


@tool({
    "type": "function",
    "function": {
        "name": "delete_note",
        "description": "Delete a Notebook note by id (from search_notebook).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the note id shown by search_notebook"},
        }, "required": ["id"]},
    },
})
def delete_note(id):
    from oceano import notebook
    nid = int(id)
    if not notebook.get(nid):
        return f"no note #{nid} — use search_notebook to see current ids"
    notebook.remove(nid)
    return f"Deleted note #{nid}."
