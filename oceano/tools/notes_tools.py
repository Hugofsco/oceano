"""Kanban board tools — the agent's access to the user's Kanban board (oceano.notes), the
free-form todo/doing/done tracker behind the "Kanban Board" window. Columns are user-defined
(renamed/added/removed/reordered from the UI), so every write here re-reads the board first
and resolves the caller's target column against what actually exists (case-insensitively)
instead of assuming a fixed todo/doing/done and silently landing on the wrong list."""
from oceano.tools.core import tool


def _find_column(board, column):
    """The board's real column name matching `column` case-insensitively, or None."""
    if not column:
        return None
    by_lower = {c.lower(): c for c in board["columns"]}
    return by_lower.get(column.strip().lower())


def _card_line(card):
    tags = f" [{', '.join(card['tags'])}]" if card["tags"] else ""
    body = f" — {card['body'][:140].replace(chr(10), ' ')}" if card["body"] else ""
    return f"  #{card['id']}{tags} {card['title']}{body}"


@tool({
    "type": "function",
    "function": {
        "name": "kanban_board",
        "description": "Read the user's Kanban board: every column and its cards. Call this "
                       "before add/update/delete so you know the real column names and card "
                       "ids — both are user-defined and can change.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def kanban_board():
    from oceano import notes
    b = notes.board()
    lines = [f"Kanban Board — columns: {', '.join(b['columns'])}"]
    for col in b["columns"]:
        cards = b["cards"].get(col, [])
        lines.append(f"\n{col}:")
        lines.extend(_card_line(c) for c in cards) if cards else lines.append("  (empty)")
    return "\n".join(lines)


@tool({
    "type": "function",
    "function": {
        "name": "add_kanban_card",
        "description": "Add a card to the user's Kanban board. Defaults to the first column "
                       "(usually 'todo') if `column` is omitted or unrecognized — call "
                       "kanban_board first to see the real column names.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "body": {"type": "string", "description": "optional longer note"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "column": {"type": "string", "description": "target column name (default: the first column)"},
        }, "required": ["title"]},
    },
})
def add_kanban_card(title, body="", tags=None, column=""):
    from oceano import notes
    b = notes.board()
    col = _find_column(b, column)
    card = notes.add(title, body=body, tags=tags, col=col)
    if not card:
        return "ERROR: the board has no columns"
    return f"Added '{card['title']}' to {col or b['columns'][0]} (id {card['id']})."


@tool({
    "type": "function",
    "function": {
        "name": "update_kanban_card",
        "description": "Edit a Kanban card's title/body/tags, and/or move it to another "
                       "column. Pass only the fields you want to change. Get the id and valid "
                       "column names from kanban_board.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the card id shown by kanban_board"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "column": {"type": "string", "description": "move the card to this column"},
        }, "required": ["id"]},
    },
})
def update_kanban_card(id, title=None, body=None, tags=None, column=None):
    from oceano import notes
    b = notes.board()
    col = None
    if column:
        col = _find_column(b, column)
        if not col:
            return f"ERROR: no column {column!r} — the board has: {', '.join(b['columns'])}"
    ok = notes.update(int(id), title=title, body=body, tags=tags, col=col)
    if not ok:
        return f"no card #{id} — use kanban_board to see current ids"
    return f"Updated card #{id}."


@tool({
    "type": "function",
    "function": {
        "name": "delete_kanban_card",
        "description": "Delete a Kanban card by id (from kanban_board).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the card id shown by kanban_board"},
        }, "required": ["id"]},
    },
})
def delete_kanban_card(id):
    from oceano import notes
    cid = int(id)
    b = notes.board()
    if cid not in {c["id"] for col in b["cards"].values() for c in col}:
        return f"no card #{cid} — use kanban_board to see current ids"
    notes.remove(cid)
    return f"Deleted card #{cid}."
