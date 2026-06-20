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
    for col in ("source", "model", "base_url"):      # migrate older DBs in place (column names are literals)
        if col not in cols:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
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
    """Push a notification through every channel you've enabled (ntfy and/or Telegram)."""
    from oceano import notifications
    return notifications.send(message, title)


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


def cron_preview(cron, n=5):
    """Validate a cron string and return its next `n` fire times — powers the task
    editor's live preview so the user sees *when* a schedule fires before saving.
    Times are UTC (the same base the scheduler/_next_run use). {valid, runs:[iso…]}
    or {valid: False, error}."""
    cron = (cron or "").strip()
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            return {"valid": False, "error": "not a valid cron — format: min hr day mon wkday"}
        it = croniter(cron, datetime.now(timezone.utc))
        return {"valid": True, "runs": [it.get_next(datetime).isoformat() for _ in range(max(1, min(int(n), 10)))]}
    except ImportError:
        return {"valid": True, "runs": []}            # croniter absent → can't preview, don't block
    except Exception as e:                            # noqa: BLE001
        return {"valid": False, "error": str(e)[:160]}


def all_tasks():
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, last_run, enabled, source, model, base_url "
                       "FROM tasks ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "cron": r[1], "instruction": r[2], "last_run": r[3],
             "enabled": bool(r[4]), "next_run": _next_run(r[1], r[3]),
             "source": r[5], "managed": bool(r[5]),
             "model": r[6], "base_url": r[7]} for r in rows]      # which model runs it ('' = system default)


def add_task(cron, instruction, source=None, model=None, base_url=None):
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            return None
    except ImportError:
        pass
    con = _db()
    cur = con.execute("INSERT INTO tasks (cron, instruction, source, model, base_url) VALUES (?,?,?,?,?)",
                      (cron, instruction, source, model or None, base_url or None))
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


def update_task(tid, cron=None, instruction=None, enabled=None, allow_managed=False,
                model=None, base_url=None):
    """Edit a task. A LOCKED job (one with a `source`, owned by the Researcher or the
    skills evaluator) can't be deleted and its instruction is owned by its manager —
    but the user may still retime it (cron) and toggle it on/off from the Scheduler.
    `allow_managed=True` is the owner's full-control path (used internally). `model`
    (pass "" to clear → system default) only applies to plain agent tasks."""
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
        model = base_url = None                  # so is which model runs it (e.g. evals run a whole matrix)
    if cron is not None:
        con.execute("UPDATE tasks SET cron=? WHERE id=?", (cron, tid))
    if instruction is not None:
        con.execute("UPDATE tasks SET instruction=? WHERE id=?", (instruction, tid))
    if enabled is not None:
        con.execute("UPDATE tasks SET enabled=? WHERE id=?", (1 if enabled else 0, tid))
    if model is not None:                        # "" clears the override → falls back to the default
        con.execute("UPDATE tasks SET model=?, base_url=? WHERE id=?", (model or None, base_url or None, tid))
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


def _dispatch(source, instruction, ref=None, model=None, base_url=None):
    """Run one task's action by its source tag and return the result string. Shared by
    the scheduled loop and the on-demand 'run now'. Always runs in the background channel —
    everything here is unattended, so it must never drive the user's shared live browser.

    The specialized jobs (research/skills/evals/memory/workflow) register themselves in the
    jobs registry (so 'run now' from their own panels is tracked too); only the plain agent
    task is wrapped here. `model`/`base_url` (a per-task override; empty → the system default)
    apply only to that plain agent task."""
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
            return memory.maintain()                         # delegates to the configured reviewer, applies the plan
        if source == "reindex:all":                          # locked index re-sync (docs/memories/skills/chats)
            from oceano import reindex
            return reindex.reindex_all()
        if source and source.startswith("workflow:"):        # a user-defined workflow
            from oceano import workflows
            return workflows.run_by_id(int(source.split(":", 1)[1]), trigger="schedule").get("summary", "workflow ran")
        with jobs.job("task", instruction, ref=ref) as jid:
            if model == "claude":              # run this task via the Claude mind (its own subscription)
                from oceano import delegate
                if delegate.available():
                    answer = Agent().run_claude(instruction)
                else:
                    answer = "⚠️ This task is set to run on 🧠 Claude, but the `claude` CLI isn't available on this host."
            else:
                ag = Agent()
                if model:                      # per-task model override (else Agent's configured default)
                    ag.model = model
                    if base_url:
                        ag.base_url = base_url
                        try:
                            from oceano.web import server
                            ag.api_key = server.endpoint_key(base_url)
                        except Exception:
                            pass
                answer = ag.run(instruction)
            jobs.set_result(jid, answer)       # so the activity log shows what the task actually produced
            return answer


def run_due_once():
    """Stamp the heartbeat, run every task that's due, push each result. Blocking.

    Returns the number of tasks run. A failing task is logged + skipped (its
    last_run still advances) so one bad task can't wedge the whole loop.
    """
    beat()                                  # tell the UI we're alive
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, last_run, enabled, source, model, base_url "
                       "FROM tasks").fetchall()
    now = datetime.now(timezone.utc)
    ran = 0
    for tid, cron, instruction, last_run, enabled, source, model, base_url in rows:
        if not (enabled and is_due(cron, last_run, now)):
            continue
        print(f"[scheduler] running #{tid}: {instruction}")
        try:
            answer = _dispatch(source, instruction, ref=source or f"task:{tid}", model=model, base_url=base_url)
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
    row = con.execute("SELECT instruction, source, model, base_url FROM tasks WHERE id=?", (tid,)).fetchone()
    con.close()
    if not row:
        return {"ok": False, "error": "no such task"}
    instruction, source, model, base_url = row
    print(f"[scheduler] manual run #{tid}: {instruction}")
    try:
        answer = _dispatch(source, instruction, ref=source or f"task:{tid}", model=model, base_url=base_url) or ""
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if advance:
        con = _db()
        con.execute("UPDATE tasks SET last_run=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), tid))
        con.commit()
        con.close()
    notify(f"{instruction}\n\n{answer[:600]}", title="Oceano task (manual)")
    return {"ok": True, "result": answer}
