"""Scheduled tasks + notifications.

This module stores/queries cron jobs, decides what's due, runs it, and sends ntfy
pushes. The always-on loop that calls run_due_once() lives in the engine
(oceano.engine), so there's a single daemon for everything.

ntfy: set OCEANO_NTFY_TOPIC to a private, hard-to-guess topic. Defaults to the
public ntfy.sh server; point OCEANO_NTFY_URL at a self-hosted ntfy for privacy.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

import config

DB_PATH = config.WORKSPACE.parent / "data" / "tasks.db"
HEARTBEAT = config.WORKSPACE.parent / "data" / "heartbeat"
NTFY_URL = os.environ.get("OCEANO_NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("OCEANO_NTFY_TOPIC", "")


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")    # wait (don't error) when another writer holds the db
    con.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer: web+telegram+scheduler+calendar
    con.execute("CREATE TABLE IF NOT EXISTS tasks ("
                "id INTEGER PRIMARY KEY, cron TEXT, instruction TEXT, "
                "last_run TEXT, enabled INTEGER DEFAULT 1, source TEXT)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
    if "source" not in cols:                         # migrate an older DB in place
        con.execute("ALTER TABLE tasks ADD COLUMN source TEXT")
        con.commit()
    return con


def _managed(con, tid):
    """A task with a source (e.g. 'research:3') is owned by another module — the
    Scheduler shows it but must not edit it."""
    row = con.execute("SELECT source FROM tasks WHERE id=?", (tid,)).fetchone()
    return bool(row and row[0])


def schedule_task(cron, instruction):
    """Schedule an instruction to run on a cron expression, e.g. '0 8 * * *'."""
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            return f"invalid cron expression: {cron!r} (format: 'min hour day month weekday')"
    except ImportError:
        pass
    con = _db()
    con.execute("INSERT INTO tasks (cron, instruction) VALUES (?,?)", (cron, instruction))
    con.commit()
    con.close()
    return f"scheduled '{instruction}' on cron '{cron}'"


def list_tasks():
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, enabled FROM tasks").fetchall()
    con.close()
    if not rows:
        return "(no scheduled tasks)"
    return "\n".join(f"#{i} [{c}] {'on' if en else 'off'}: {ins}" for i, c, ins, en in rows)


def notify(message, title="Oceano"):
    """Push a notification to your phone via ntfy."""
    if not NTFY_TOPIC:
        return "(ntfy topic not set — export OCEANO_NTFY_TOPIC=your-private-topic)"
    try:
        requests.post(f"{NTFY_URL}/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers={"Title": title}, timeout=10)
        return "notified"
    except requests.RequestException as e:
        return f"notify failed: {e}"


# --- heartbeat: the runner stamps this every tick; the UI reads it ---------
def beat():
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(str(time.time()))


def last_beat():
    try:
        return float(HEARTBEAT.read_text())
    except (OSError, ValueError):
        return None


# --- structured task CRUD for the UI --------------------------------------
def _next_run(cron, last_run):
    try:
        from croniter import croniter
        base = datetime.fromisoformat(last_run) if last_run else datetime.now(timezone.utc)
        return croniter(cron, base).get_next(datetime).isoformat()
    except Exception:
        return None


def all_tasks():
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, last_run, enabled, source FROM tasks ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "cron": r[1], "instruction": r[2], "last_run": r[3],
             "enabled": bool(r[4]), "next_run": _next_run(r[1], r[3]),
             "source": r[5], "managed": bool(r[5])} for r in rows]


def add_task(cron, instruction, source=None):
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            return None
    except ImportError:
        pass
    con = _db()
    cur = con.execute("INSERT INTO tasks (cron, instruction, source) VALUES (?,?,?)",
                      (cron, instruction, source))
    con.commit()
    tid = cur.lastrowid
    con.close()
    return tid


def _cron_ok(cron):
    try:
        from croniter import croniter
        return croniter.is_valid(cron)
    except ImportError:
        return bool(cron)


def update_task(tid, cron=None, instruction=None, enabled=None, allow_managed=False):
    """Edit a task. A LOCKED job (one with a `source`, owned by the Researcher or the
    skills evaluator) can't be deleted and its instruction is owned by its manager —
    but the user may still retime it (cron) and toggle it on/off from the Scheduler.
    `allow_managed=True` is the owner's full-control path (used internally)."""
    if cron is not None and not _cron_ok(cron):
        return False
    con = _db()
    managed = _managed(con, tid)
    row = con.execute("SELECT source FROM tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        con.close()
        return False
    if managed and not allow_managed:
        instruction = None                       # instruction is owned by the manager
    if cron is not None:
        con.execute("UPDATE tasks SET cron=? WHERE id=?", (cron, tid))
    if instruction is not None:
        con.execute("UPDATE tasks SET instruction=? WHERE id=?", (instruction, tid))
    if enabled is not None:
        con.execute("UPDATE tasks SET enabled=? WHERE id=?", (1 if enabled else 0, tid))
    con.commit()
    con.close()
    # user retimed/toggled a research job from the Scheduler → mirror it into the
    # topic record so the Researcher view stays in sync (skip on the owner's own path)
    src = row[0]
    if managed and not allow_managed and src and src.startswith("research:"):
        try:
            from oceano import researcher
            researcher.note_schedule(int(src.split(":", 1)[1]), cron=cron, enabled=enabled)
        except Exception:
            pass
    return True


def delete_task(tid, allow_managed=False):
    con = _db()
    if not allow_managed and _managed(con, tid):
        con.close()
        return False
    con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    con.commit()
    con.close()
    return True


# --- the actual runner (driven by the engine's loop) ----------------------
def is_due(cron, last_run, now=None):
    """True if a task on `cron` whose last run was `last_run` should run by now."""
    from croniter import croniter
    now = now or datetime.now(timezone.utc)
    base = datetime.fromisoformat(last_run) if last_run else now
    return croniter(cron, base).get_next(datetime) <= now


def _dispatch(source, instruction, ref=None):
    """Run one task's action by its source tag and return the result string. Shared by
    the scheduled loop and the on-demand 'run now'. Always runs in the background channel —
    everything here is unattended, so it must never drive the user's shared live browser.

    The specialized jobs (research/skills/evals/memory/workflow) register themselves in the
    jobs registry (so 'run now' from their own panels is tracked too); only the plain agent
    task is wrapped here."""
    from oceano.agent import Agent
    from oceano import tools, jobs
    with tools.background():
        if source and source.startswith("research:"):        # Researcher-owned entry
            from oceano import researcher
            return researcher.run_topic(int(source.split(":", 1)[1]))
        if source == "skills:eval":                          # locked skills-evaluation entry
            from oceano import skills
            return skills.evaluate_all()
        if source == "evals:run":                            # locked model-eval suite
            from oceano import evals
            evals.run_all_bg()                               # long → background, don't wedge the caller
            return "model eval suite started in the background"
        if source == "memory:maintain":                      # locked memory-hygiene job
            from oceano import memory
            return memory.maintain()                         # delegates to Claude Code, applies the plan
        if source == "reindex:all":                          # locked index re-sync (docs/memories/skills/chats)
            from oceano import reindex
            return reindex.reindex_all()
        if source and source.startswith("workflow:"):        # a user-defined workflow
            from oceano import workflows
            return workflows.run_by_id(int(source.split(":", 1)[1]), trigger="schedule").get("summary", "workflow ran")
        with jobs.job("task", instruction, ref=ref):
            return Agent().run(instruction)


def run_due_once():
    """Stamp the heartbeat, run every task that's due, push each result. Blocking.

    Returns the number of tasks run. A failing task is logged + skipped (its
    last_run still advances) so one bad task can't wedge the whole loop.
    """
    beat()                                  # tell the UI we're alive
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, last_run, enabled, source FROM tasks").fetchall()
    now = datetime.now(timezone.utc)
    ran = 0
    for tid, cron, instruction, last_run, enabled, source in rows:
        if not (enabled and is_due(cron, last_run, now)):
            continue
        print(f"[scheduler] running #{tid}: {instruction}")
        try:
            answer = _dispatch(source, instruction, ref=source or f"task:{tid}")
            notify(f"{instruction}\n\n{answer[:600]}", title="Oceano task")
        except Exception as e:
            print(f"[scheduler] task #{tid} failed: {e}")
        con.execute("UPDATE tasks SET last_run=? WHERE id=?", (now.isoformat(), tid))
        ran += 1
    con.commit()
    con.close()
    return ran


def run_task(tid, advance=True):
    """Run a scheduled task right now, on demand — ignores the cron. Returns
    {ok, result} or {ok: False, error}. advance=True stamps last_run so the heartbeat
    won't immediately re-fire a task that happened to be due. Blocking (call off the
    event loop); long jobs (evals) already detach themselves."""
    con = _db()
    row = con.execute("SELECT instruction, source FROM tasks WHERE id=?", (tid,)).fetchone()
    con.close()
    if not row:
        return {"ok": False, "error": "no such task"}
    instruction, source = row
    print(f"[scheduler] manual run #{tid}: {instruction}")
    try:
        answer = _dispatch(source, instruction, ref=source or f"task:{tid}") or ""
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if advance:
        con = _db()
        con.execute("UPDATE tasks SET last_run=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), tid))
        con.commit()
        con.close()
    notify(f"{instruction}\n\n{answer[:600]}", title="Oceano task (manual)")
    return {"ok": True, "result": answer}
