"""A notebook — Markdown notes with a title, tags, and a pin flag, JSON-persisted,
local-first. Sibling to notes.py's Kanban board: same minimal philosophy (no embeddings,
no DB — free-text search only), but for longer-form writing rather than short cards.
Atomic writes so the web + scheduler threads can't corrupt it.
"""
import json
from datetime import datetime, timezone

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "notebook.json"
_MAX_TAGS = 12
_MAX_TITLE = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


def _norm_tags(tags):
    return [str(t).strip() for t in (tags or []) if str(t).strip()][:_MAX_TAGS] if isinstance(tags, list) else []


def _norm_note(n):
    if not isinstance(n, dict) or not isinstance(n.get("id"), int):
        return None
    ts = n.get("ts") or _now()
    return {"id": n["id"], "title": str(n.get("title", ""))[:_MAX_TITLE], "body": str(n.get("body", "")),
            "tags": _norm_tags(n.get("tags")), "pinned": bool(n.get("pinned")),
            "ts": ts, "updated": n.get("updated") or ts}


def _load():
    try:
        raw = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    notes_raw = raw.get("notes") if isinstance(raw, dict) else None
    if not isinstance(notes_raw, list):
        notes_raw = []
    return [n for n in (_norm_note(k) for k in notes_raw) if n]


def _save(notes):
    atomicio.write_text(STORE, json.dumps({"notes": notes}, indent=2))


def _next_id(notes):
    return (max((n["id"] for n in notes), default=0)) + 1


def _sort(notes):
    # pinned first, then most-recently-updated
    return sorted(notes, key=lambda n: (n["pinned"], n["updated"]), reverse=True)


def list_all(q="", tag=""):
    """Notes matching a free-text substring (title/body, case-insensitive) and/or an exact
    tag, pinned-first then most-recently-updated. Both filters empty = everything."""
    notes = _sort(_load())
    q = (q or "").strip().lower()
    tag = (tag or "").strip()
    if q:
        notes = [n for n in notes if q in n["title"].lower() or q in n["body"].lower()]
    if tag:
        notes = [n for n in notes if tag in n["tags"]]
    return notes


def all_tags():
    """Every tag in use, sorted, for a filter/autocomplete UI."""
    return sorted({t for n in _load() for t in n["tags"]})


def get(nid):
    return next((n for n in _load() if n["id"] == nid), None)


def create(title="", body="", tags=None):
    """Add a note. Returns the created note."""
    notes = _load()
    now = _now()
    note = {"id": _next_id(notes), "title": (title or "").strip()[:_MAX_TITLE], "body": (body or "").strip(),
            "tags": _norm_tags(tags), "pinned": False, "ts": now, "updated": now}
    notes.append(note)
    _save(notes)
    return note


def update(nid, title=None, body=None, tags=None, pinned=None):
    """Edit a note's fields. Returns True if found."""
    notes = _load()
    note = next((n for n in notes if n["id"] == nid), None)
    if not note:
        return False
    edited = False
    if title is not None:
        note["title"] = str(title).strip()[:_MAX_TITLE]; edited = True
    if body is not None:
        note["body"] = str(body).strip(); edited = True
    if tags is not None:
        note["tags"] = _norm_tags(tags); edited = True
    if pinned is not None:
        note["pinned"] = bool(pinned)   # a pin/unpin alone doesn't bump `updated` — it's not an edit
    if edited:
        note["updated"] = _now()
    _save(notes)
    return True


def remove(nid):
    """Delete a note by id. Returns True (idempotent)."""
    notes = _load()
    kept = [n for n in notes if n["id"] != nid]
    if len(kept) != len(notes):
        _save(kept)
    return True
