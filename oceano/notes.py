"""A Kanban board — JSON-persisted, local-first.

Columns are user-defined (default todo/doing/done, but can be renamed/added/removed/
reordered). A card has a title, an optional longer body, and free-form tags, plus
timestamps. Backs the Notes window in the web UI. Deliberately minimal: no embeddings,
no DB — this is a place for the user (and, later, the agent) to jot and move things, not
a second memory store. Atomic writes so the web + scheduler threads can't corrupt it.
"""
import json
from datetime import datetime, timezone

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "notes.json"
DEFAULT_COLUMNS = ("todo", "doing", "done")
_MAX_COLUMNS = 12
_MAX_TAGS = 12


def _now():
    return datetime.now(timezone.utc).isoformat()


def _norm_card(card):
    if not isinstance(card, dict) or not isinstance(card.get("id"), int):
        return None
    title = str(card.get("title", card.get("text", ""))).strip()   # "text" = pre-rework field
    tags = card.get("tags")
    tags = [str(t).strip() for t in tags if str(t).strip()][:_MAX_TAGS] if isinstance(tags, list) else []
    ts = card.get("ts") or _now()
    return {"id": card["id"], "title": title, "body": str(card.get("body", "")),
            "tags": tags, "ts": ts, "updated": card.get("updated") or ts}


def _load():
    try:
        raw = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    columns, cards_raw = raw.get("columns"), raw.get("cards")
    if not (isinstance(columns, list) and isinstance(cards_raw, dict)):
        # pre-column-management shape was a flat {col: [cards]} dict (fixed todo/doing/done)
        columns, cards_raw = list(DEFAULT_COLUMNS), raw
    columns = [str(c).strip() for c in columns if str(c).strip()][:_MAX_COLUMNS] or list(DEFAULT_COLUMNS)
    cards = {}
    for col in columns:
        cards[col] = [c for c in (_norm_card(k) for k in cards_raw.get(col, [])
                                  if isinstance(cards_raw.get(col), list)) if c]
    return {"columns": columns, "cards": cards}


def _save(state):
    atomicio.write_text(STORE, json.dumps(state, indent=2))


def board():
    """The whole board: {columns: [...], cards: {col: [...]}}."""
    return _load()


def _next_id(state):
    ids = [c["id"] for col in state["cards"].values() for c in col]
    return (max(ids) + 1) if ids else 1


def add(title, body="", tags=None, col=None):
    """Add a card to a column (newest first). Returns the created card, or None if the
    board somehow has no columns."""
    s = _load()
    col = col if col in s["columns"] else (s["columns"][0] if s["columns"] else None)
    if col is None:
        return None
    now = _now()
    card = {"id": _next_id(s), "title": (title or "").strip(), "body": (body or "").strip(),
            "tags": [str(t).strip() for t in (tags or []) if str(t).strip()][:_MAX_TAGS],
            "ts": now, "updated": now}
    s["cards"][col].insert(0, card)
    _save(s)
    return card


def update(cid, title=None, body=None, tags=None, col=None):
    """Edit a card's fields and/or move it to another column. Returns True if found."""
    s = _load()
    found = cur = None
    for c, cards in s["cards"].items():
        for k in cards:
            if k["id"] == cid:
                found, cur = k, c
                break
        if found:
            break
    if not found:
        return False
    edited = False
    if title is not None:
        found["title"] = str(title).strip(); edited = True
    if body is not None:
        found["body"] = str(body).strip(); edited = True
    if tags is not None:
        found["tags"] = [str(t).strip() for t in tags if str(t).strip()][:_MAX_TAGS]; edited = True
    if edited:
        found["updated"] = _now()
    if col and col in s["columns"] and col != cur:
        s["cards"][cur].remove(found)
        s["cards"][col].insert(0, found)
    _save(s)
    return True


def remove(cid):
    """Delete a card by id. Returns True (idempotent)."""
    s = _load()
    for col in s["cards"]:
        s["cards"][col] = [k for k in s["cards"][col] if k["id"] != cid]
    _save(s)
    return True


# ---------------- columns ----------------
def add_column(name, after=None):
    """Append a new (empty) column, optionally right after an existing one. Returns the
    board, or None if the name is blank/duplicate or the board is already at the cap."""
    name = (name or "").strip()
    s = _load()
    if not name or name in s["columns"] or len(s["columns"]) >= _MAX_COLUMNS:
        return None
    if after in s["columns"]:
        s["columns"].insert(s["columns"].index(after) + 1, name)
    else:
        s["columns"].append(name)
    s["cards"][name] = []
    _save(s)
    return s


def rename_column(old, new):
    """Rename a column in place, keeping its cards. Returns True if renamed."""
    new = (new or "").strip()
    s = _load()
    if old not in s["columns"] or not new:
        return False
    if new == old:
        return True
    if new in s["columns"]:
        return False
    s["columns"][s["columns"].index(old)] = new
    s["cards"][new] = s["cards"].pop(old)
    _save(s)
    return True


def remove_column(name, move_to=None):
    """Delete a column. If it still holds cards, `move_to` (another existing column) is
    required — cards move there rather than vanish. Refuses to drop the last column.
    Returns True if removed."""
    s = _load()
    if name not in s["columns"] or len(s["columns"]) <= 1:
        return False
    cards = s["cards"][name]
    if cards:
        if move_to not in s["columns"] or move_to == name:
            return False
        s["cards"][move_to] = cards + s["cards"][move_to]
    s["columns"].remove(name)
    del s["cards"][name]
    _save(s)
    return True


def move_column(name, direction):
    """Shift a column left (direction < 0) or right (direction > 0) in display order.
    Returns True if moved."""
    s = _load()
    if name not in s["columns"]:
        return False
    i = s["columns"].index(name)
    j = i + (1 if direction > 0 else -1)
    if not (0 <= j < len(s["columns"])):
        return False
    s["columns"][i], s["columns"][j] = s["columns"][j], s["columns"][i]
    _save(s)
    return True
