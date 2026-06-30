"""Agent suggestions queue — the ACTION half of Oceano's self-evolution loop.

Nightly reflection (reflect.py) used to only journal its "next steps" as inert prose. Now it
files them here as PENDING suggestions the user can review and ACCEPT — and accepting one
auto-creates the real artifact (a research topic, a workflow draft, a saved memory). So
"self-improving" means the system actually changes itself, with a human approving each step.

Kinds we can safely auto-create on accept: research, workflow, memory. Others (skill, setting)
are kept as accepted notes for manual follow-up — we don't silently change settings or fabricate
a skill body from a one-line idea.
"""
import sqlite3
from datetime import datetime, timezone

import config

DB_PATH = config.WORKSPACE.parent / "data" / "suggestions.db"
KINDS = ("research", "workflow", "memory", "skill", "setting", "other")


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS suggestions ("
                "id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, title TEXT, detail TEXT, "
                "status TEXT DEFAULT 'pending', source TEXT, result TEXT)")
    return con


def _row(r):
    return {"id": r[0], "ts": r[1], "kind": r[2], "title": r[3], "detail": r[4],
            "status": r[5], "source": r[6], "result": r[7]}


_COLS = "id, ts, kind, title, detail, status, source, result"


def add(kind, title, detail="", source=""):
    """File a pending suggestion. De-dupes against an identical pending one (same kind+title) so
    repeated nightly reflections don't pile up the same idea. Returns the id, or None if no title."""
    kind = (kind or "other").strip().lower()
    if kind not in KINDS:
        kind = "other"
    title = (title or "").strip()
    if not title:
        return None
    con = _db()
    dup = con.execute("SELECT id FROM suggestions WHERE status='pending' AND kind=? AND title=?",
                      (kind, title)).fetchone()
    if dup:
        con.close()
        return dup[0]
    cur = con.execute("INSERT INTO suggestions (ts, kind, title, detail, status, source) "
                      "VALUES (?,?,?,?, 'pending', ?)",
                      (datetime.now(timezone.utc).isoformat(), kind, title, (detail or "").strip(), source))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return sid


def all_suggestions(status="pending"):
    """List suggestions, default just the pending ones. Pass status=None (or 'all') for every status."""
    con = _db()
    if status and status != "all":
        rows = con.execute(f"SELECT {_COLS} FROM suggestions WHERE status=? ORDER BY id DESC",
                           (status,)).fetchall()
    else:
        rows = con.execute(f"SELECT {_COLS} FROM suggestions ORDER BY id DESC").fetchall()
    con.close()
    return [_row(r) for r in rows]


def get(sid):
    con = _db()
    r = con.execute(f"SELECT {_COLS} FROM suggestions WHERE id=?", (sid,)).fetchone()
    con.close()
    return _row(r) if r else None


def _set(sid, status, result=None):
    con = _db()
    if result is None:
        con.execute("UPDATE suggestions SET status=? WHERE id=?", (status, sid))
    else:
        con.execute("UPDATE suggestions SET status=?, result=? WHERE id=?", (status, result[:500], sid))
    con.commit()
    con.close()


def dismiss(sid):
    s = get(sid)
    if not s:
        return {"ok": False, "error": f"no suggestion #{sid}"}
    _set(sid, "dismissed")
    return {"ok": True, "status": "dismissed", "title": s["title"]}


def accept(sid):
    """Accept a pending suggestion and ACT on it — create the real artifact for the kinds we can
    safely auto-create. Returns {ok, action, result} or {ok: False, error}."""
    s = get(sid)
    if not s:
        return {"ok": False, "error": f"no suggestion #{sid}"}
    if s["status"] != "pending":
        return {"ok": False, "error": f"suggestion #{sid} is already {s['status']}"}
    kind, title, detail = s["kind"], s["title"], s["detail"]
    try:
        if kind == "research":
            from oceano import researcher
            rid = researcher.add_topic(title, focus=detail)
            if not rid:
                return {"ok": False, "error": "could not create the research topic (invalid title/cron?)"}
            result = f"created research topic #{rid}: {title}"
        elif kind == "workflow":
            from oceano import workflows
            wf = workflows.create(title, description=detail)
            result = f"created workflow draft #{wf['id']}: {title} — open the Workflows editor to build it out"
        elif kind == "memory":
            from oceano import memory
            memory.remember(detail or title, category="knowledge", source="reflection")
            result = f"saved memory: {title}"
        else:                                  # skill / setting / other — no safe auto-create
            _set(sid, "accepted", f"accepted for manual follow-up: {title}")
            return {"ok": True, "action": "noted",
                    "result": f"accepted #{sid} — '{kind}' needs manual follow-up, nothing was auto-created"}
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _set(sid, "done", result)
    return {"ok": True, "action": kind, "result": result}
