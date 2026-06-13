"""A tiny Kanban scratchpad — JSON-persisted, local-first.

Three columns (todo / doing / done); a card is just text + a timestamp + an id.
Backs the Notes window in the web UI. Deliberately minimal: no embeddings, no DB —
this is a place for the user (and, later, the agent) to jot and move things, not a
second memory store. Atomic writes so the web + scheduler threads can't corrupt it.
"""
import json
from datetime import datetime, timezone

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "notes.json"
COLUMNS = ("todo", "doing", "done")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load():
    try:
        data = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    # normalize to the known columns, dropping anything malformed
    out = {c: [] for c in COLUMNS}
    for c in COLUMNS:
        for card in data.get(c, []) if isinstance(data, dict) else []:
            if isinstance(card, dict) and isinstance(card.get("id"), int):
                out[c].append({"id": card["id"], "text": str(card.get("text", "")),
                               "ts": card.get("ts") or _now()})
    return out


def _save(board):
    atomicio.write_text(STORE, json.dumps(board, indent=2))


def board():
    """The whole board: {todo: [...], doing: [...], done: [...]}."""
    return _load()


def _next_id(board):
    ids = [c["id"] for col in COLUMNS for c in board[col]]
    return (max(ids) + 1) if ids else 1


def add(text, col="todo"):
    """Add a card to a column (newest first). Returns the created card."""
    col = col if col in COLUMNS else "todo"
    b = _load()
    card = {"id": _next_id(b), "text": (text or "").strip(), "ts": _now()}
    b[col].insert(0, card)
    _save(b)
    return card


def update(cid, text=None, col=None):
    """Edit a card's text and/or move it to another column. Returns True if found."""
    b = _load()
    found = cur = None
    for c in COLUMNS:
        for k in b[c]:
            if k["id"] == cid:
                found, cur = k, c
                break
        if found:
            break
    if not found:
        return False
    if text is not None:
        found["text"] = str(text).strip()
    if col and col in COLUMNS and col != cur:
        b[cur].remove(found)
        b[col].insert(0, found)
    _save(b)
    return True


def remove(cid):
    """Delete a card by id. Returns True (idempotent)."""
    b = _load()
    for c in COLUMNS:
        b[c] = [k for k in b[c] if k["id"] != cid]
    _save(b)
    return True
