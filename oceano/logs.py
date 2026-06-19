"""Activity log — a durable record of every UNATTENDED run (scheduled tasks, workflows, research,
eval suite, memory upkeep, reindex, skills review). Answers "what ran, when, did it work, and what
did the agent actually produce?" — especially for scheduled tasks you weren't watching.

Written by jobs.job() when a background job finishes (interactive chat is deliberately excluded);
surfaced in the web UI's Logs window via /api/logs. SQLite, capped to the most recent _MAX rows.
"""
import sqlite3
import subprocess
from datetime import datetime, timezone

import config

# The systemd units we'll surface (allowlisted — never read an arbitrary unit's journal).
_UNITS = {"oceano": "oceano.service", "llama-swap": "oceano-llama-swap.service"}

DB_PATH = config.WORKSPACE.parent / "data" / "logs.db"
_MAX = 1000                       # keep the most recent N runs; older ones are pruned on write


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
                "kind TEXT, title TEXT, status TEXT, summary TEXT, duration REAL, ref TEXT)")
    return con


def log_run(kind, title, status="ok", summary="", duration=None, ref=None):
    """Record one finished run. summary = the agent's result / output (or the error message).
    Best-effort: never raise into the job that's finishing."""
    try:
        con = _db()
        con.execute("INSERT INTO runs (ts, kind, title, status, summary, duration, ref) VALUES (?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), kind, (title or "")[:200],
                     status, (summary or "")[:8000], duration, ref))
        con.execute("DELETE FROM runs WHERE id NOT IN "
                    "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (_MAX,))
        con.commit()
        con.close()
    except Exception:
        pass


def recent(limit=200, kind=None):
    """The newest runs (optionally filtered to one kind), newest first."""
    try:
        con = _db()
        q = ("SELECT id, ts, kind, title, status, summary, duration, ref FROM runs "
             + ("WHERE kind=? " if kind else "") + "ORDER BY id DESC LIMIT ?")
        rows = con.execute(q, ((kind, limit) if kind else (limit,))).fetchall()
        con.close()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "title": r[3], "status": r[4],
                 "summary": r[5], "duration": r[6], "ref": r[7]} for r in rows]
    except Exception:
        return []


def system_log(unit="oceano", lines=400):
    """The tail of a daemon's systemd journal — so a user can see if Oceano (and its model server)
    are actually healthy: startup, errors, tracebacks. Unit is allowlisted; reads via journalctl
    (the daemon's user can read its own journal). Returns {ok, unit, text} or {ok: False, error}."""
    svc = _UNITS.get(unit)
    if not svc:
        return {"ok": False, "error": f"unknown unit {unit!r}"}
    try:
        n = max(20, min(int(lines), 2000))
    except (TypeError, ValueError):
        n = 400
    try:
        r = subprocess.run(["journalctl", "-u", svc, "-n", str(n), "--no-pager", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return {"ok": True, "unit": svc, "text": (r.stdout or "")[-120000:]}
        err = (r.stderr or "").strip().splitlines()
        msg = err[-1] if err else "journalctl failed"
        if "permission" in msg.lower() or "not seen" in msg.lower():
            msg += " — add this user to the 'systemd-journal' or 'adm' group to read the journal"
        return {"ok": False, "error": msg}
    except FileNotFoundError:
        return {"ok": False, "error": "journalctl not available on this host"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "journalctl timed out"}


def kinds():
    """Distinct run kinds present, for the UI filter."""
    try:
        con = _db()
        rows = con.execute("SELECT DISTINCT kind FROM runs").fetchall()
        con.close()
        return sorted(r[0] for r in rows if r[0])
    except Exception:
        return []
