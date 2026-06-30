"""Scheduled tasks + notifications.

This module stores/queries cron jobs, decides what's due, runs it, and sends ntfy
pushes. The always-on loop that calls run_due_once() lives in the engine
(oceano.engine), so there's a single daemon for everything.

ntfy: set OCEANO_NTFY_TOPIC to a private, hard-to-guess topic. Defaults to the
public ntfy.sh server; point OCEANO_NTFY_URL at a self-hosted ntfy for privacy.
"""
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone

import requests

import config

DB_PATH = config.WORKSPACE.parent / "data" / "tasks.db"
HEARTBEAT = config.WORKSPACE.parent / "data" / "heartbeat"
NTFY_URL = os.environ.get("OCEANO_NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("OCEANO_NTFY_TOPIC", "")


# --- timezone: cron is evaluated in the host's LOCAL time, like system cron ---------------
# So '0 8 * * *' means 8am where the user is, not 8am UTC (the old behaviour, which silently
# offset every "run at 8am" by the host's UTC delta). Override with OCEANO_TZ (an IANA name
# like 'America/New_York') for exact DST handling.
def _tz():
    name = os.environ.get("OCEANO_TZ", "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now(timezone.utc).astimezone().tzinfo   # host-local offset (re-read each call → tracks DST)


def _now():
    return datetime.now(_tz())


def _as_local(dt):
    """Interpret a stored timestamp (older UTC-ISO rows, or newer local-ISO ones) in the schedule tz."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz())


def _parse_when(s):
    """Parse a one-shot date/time (e.g. '2026-07-01 15:00' or full ISO). A bare time with no
    offset is read in the schedule tz. Returns an aware datetime, or None if unparseable."""
    try:
        dt = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz())


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")    # wait (don't error) when another writer holds the db
    con.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer: web+telegram+scheduler+calendar
    con.execute("CREATE TABLE IF NOT EXISTS tasks ("
                "id INTEGER PRIMARY KEY, cron TEXT, instruction TEXT, "
                "last_run TEXT, enabled INTEGER DEFAULT 1, source TEXT)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)").fetchall()}
    # migrate older DBs in place (column names are literals): run_once_at = one-shot fire time
    # (cron empty); last_status/last_summary = the outcome of the most recent run.
    for col in ("source", "model", "base_url", "run_once_at", "last_status", "last_summary"):
        if col not in cols:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
    # Bootstrap legacy rows that never ran: a NULL last_run made is_due() compute a base of
    # 'now', so croniter's next fire was always in the future and the task NEVER triggered.
    # Seed it to now so they start firing at their next scheduled time (no mass catch-up).
    con.execute("UPDATE tasks SET last_run=? WHERE last_run IS NULL OR last_run=''",
                (datetime.now(timezone.utc).isoformat(),))
    con.commit()
    return con


def schedule_task(cron, instruction, run_once_at=None):
    """Schedule an instruction to run on a cron expression ('0 8 * * *'), or ONCE at a given
    date/time when `run_once_at` is set (cron is then ignored). Times are host-local."""
    if run_once_at:
        when = _parse_when(run_once_at)
        if not when:
            return f"invalid date/time: {run_once_at!r} (use e.g. '2026-07-01 15:00')"
        add_task("", instruction, run_once_at=when.isoformat())
        return f"scheduled one-shot '{instruction}' for {when.isoformat()}"
    if not _cron_ok(cron):
        return f"invalid cron expression: {cron!r} (format: 'min hour day month weekday')"
    add_task(cron, instruction)
    return f"scheduled '{instruction}' on cron '{cron}'"


def list_tasks():
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, enabled, run_once_at, last_status FROM tasks").fetchall()
    con.close()
    if not rows:
        return "(no scheduled tasks)"
    out = []
    for i, c, ins, en, once, status in rows:
        when = f"once @ {once}" if once else f"[{c}]"
        flag = "  ⚠️ last run FAILED" if status == "error" else ""
        out.append(f"#{i} {when} {'on' if en else 'off'}: {ins}{flag}")
    return "\n".join(out)


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
def _next_run(cron, last_run, run_once_at=None):
    if run_once_at:
        return run_once_at
    try:
        from croniter import croniter
        base = _as_local(datetime.fromisoformat(last_run)) if last_run else _now()
        return croniter(cron, base).get_next(datetime).isoformat()
    except Exception:
        return None


def cron_preview(cron, n=5):
    """Validate a cron string and return its next `n` fire times — powers the task
    editor's live preview so the user sees *when* a schedule fires before saving.
    Times are in the schedule timezone (host-local unless OCEANO_TZ is set), the same
    base the scheduler/_next_run use. {valid, runs:[iso…]} or {valid: False, error}."""
    cron = (cron or "").strip()
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            return {"valid": False, "error": "not a valid cron — format: min hr day mon wkday"}
        it = croniter(cron, _now())
        return {"valid": True, "runs": [it.get_next(datetime).isoformat() for _ in range(max(1, min(int(n), 10)))]}
    except ImportError:
        return {"valid": True, "runs": []}            # croniter absent → can't preview, don't block
    except Exception as e:                            # noqa: BLE001
        return {"valid": False, "error": str(e)[:160]}


def all_tasks():
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, last_run, enabled, source, model, base_url, "
                       "run_once_at, last_status, last_summary FROM tasks ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "cron": r[1], "instruction": r[2], "last_run": r[3],
             "enabled": bool(r[4]), "next_run": _next_run(r[1], r[3], r[8]),
             "source": r[5], "managed": bool(r[5]),
             "model": r[6], "base_url": r[7],                     # which model runs it ('' = system default)
             "run_once_at": r[8], "last_status": r[9], "last_summary": r[10]} for r in rows]


def add_task(cron, instruction, source=None, model=None, base_url=None, run_once_at=None):
    if not run_once_at:                                  # a cron task must have a valid cron; one-shots don't
        try:
            from croniter import croniter
            if not croniter.is_valid(cron):
                return None
        except ImportError:
            pass
    con = _db()
    # Seed last_run = now (creation baseline) so the task fires at its NEXT cron occurrence
    # instead of never — a NULL last_run made is_due() always look one interval into the future.
    cur = con.execute("INSERT INTO tasks (cron, instruction, source, model, base_url, last_run, run_once_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (cron or "", instruction, source, model or None, base_url or None,
                       _now().isoformat(), run_once_at))
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
    """Edit a task. Every field is editable from the Scheduler — schedule, instruction,
    which model runs it, and on/off — for ALL tasks, including the built-in ones (research,
    skills, evals, memory, reflect, reindex). Note those built-ins dispatch by their `source`
    tag, not by their instruction text, so editing the wording is cosmetic (and the bootstrap
    may re-canonicalize it on the next restart); the schedule, model, on/off and existence are
    what actually take effect. `model` (pass "" to clear → system default) applies to plain
    agent tasks. `allow_managed` is the owner module's own write path (researcher/skills/etc.
    syncing their generated label); it now only suppresses the research mirror-back below, so an
    owner-originated edit doesn't echo back to itself."""
    if cron is not None and not _cron_ok(cron):
        return False
    con = _db()
    row = con.execute("SELECT source FROM tasks WHERE id=?", (tid,)).fetchone()
    if not row:
        con.close()
        return False
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
    # A research job retimed/toggled from the Scheduler mirrors back into its topic record so the
    # Researcher view stays in sync. Skip when the Researcher itself is the caller (allow_managed)
    # — it already wrote the topic record, so an echo would be redundant.
    src = row[0]
    if not allow_managed and src and src.startswith("research:"):
        try:
            from oceano import researcher
            researcher.note_schedule(int(src.split(":", 1)[1]), cron=cron, enabled=enabled)
        except Exception:
            pass
    return True


def delete_task(tid, allow_managed=False):
    """Delete a task — any task can be removed from the Scheduler now. The built-in jobs
    (skills/evals/memory/reflect/reindex) are recreated on the next server start by their
    ensure_*() bootstrap, so deleting one only clears it until restart; toggle it OFF instead
    to keep it gone for good. `allow_managed` is retained for the owner modules' delete path."""
    con = _db()
    con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    con.commit()
    con.close()
    return True


# --- the actual runner (driven by the engine's loop) ----------------------
def is_due(cron, last_run, now=None, run_once_at=None):
    """True if a task should run by now. A one-shot (`run_once_at` set) is due once its time
    has arrived; run_due_once disables it on fire so it runs exactly once.

    For cron tasks, `last_run` is normally seeded at creation (see add_task/_db), so the usual
    path is: next fire AFTER the last run has already passed → due. The None branch is a
    defensive backstop: base it on the PREVIOUS scheduled time (get_prev, always ≤ now) so a
    task that somehow has no baseline still fires on the next tick rather than never. Cron is
    evaluated in the schedule timezone (host-local unless OCEANO_TZ is set)."""
    now = now or _now()
    if run_once_at:
        try:
            return _as_local(datetime.fromisoformat(run_once_at)) <= now
        except ValueError:
            return False
    from croniter import croniter
    if last_run:
        return croniter(cron, _as_local(datetime.fromisoformat(last_run))).get_next(datetime) <= now
    return croniter(cron, now).get_prev(datetime) <= now


# --- background drainer: scheduled tasks run OFF the engine's tick thread -------------------
# run_due_once() used to execute every due task inline, so one slow agent task (minutes) blocked
# the whole loop — no heartbeat, no new due-checks → the Scheduler looked dead/sluggish. Now the
# tick only STAMPS + ENQUEUES due tasks and returns instantly (so it beats every interval); a
# single daemon worker drains the queue serially (no local-model thrash) however long each takes.
_TASK_Q = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(target=_drain_loop, name="sched-drain", daemon=True).start()


def _set_run_status(tid, status, summary):
    """Record the outcome of a task's most recent run so list_tasks/all_tasks (and the UI)
    can show 'last run FAILED' instead of the failure being invisible."""
    if not tid:
        return
    try:
        con = _db()
        con.execute("UPDATE tasks SET last_status=?, last_summary=? WHERE id=?",
                    (status, (summary or "")[:500], tid))
        con.commit()
        con.close()
    except Exception:                                               # noqa: BLE001 - status is best-effort
        pass


def _drain_loop():
    while True:
        tid, source, instruction, ref, model, base_url = _TASK_Q.get()
        try:
            print(f"[scheduler] running {ref}: {instruction}")
            answer = _dispatch(source, instruction, ref=ref, model=model, base_url=base_url)
            _set_run_status(tid, "ok", answer)
            notify(f"{instruction}\n\n{answer}", title="Oceano task")   # full report; notify() chunks per channel
        except Exception as e:                                          # noqa: BLE001
            print(f"[scheduler] task {ref} failed: {e}")
            _set_run_status(tid, "error", f"{type(e).__name__}: {e}")
            notify(f"⚠️ Scheduled task failed:\n{instruction}\n\n{type(e).__name__}: {e}",
                   title="Oceano task failed")                          # silent failures → visible
        finally:
            _TASK_Q.task_done()


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
        if source == "skills:distill":                       # locked feeder: mine recent chats → learning skills
            from oceano import skills
            return skills.distill_recent()
        if source == "evals:run":                            # locked model-eval suite
            from oceano import evals
            evals.run_all_bg()                               # long → background, don't wedge the caller
            return "model eval suite started in the background"
        if source == "memory:maintain":                      # locked memory-hygiene job
            from oceano import memory
            return memory.maintain()                         # delegates to the configured reviewer, applies the plan
        if source == "self:reflect":                         # locked nightly self-reflection
            from oceano import reflect
            return reflect.reflect()
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
            elif model == "codex":             # run this task via the Codex mind (its own auth/session)
                from oceano import delegate
                if delegate.codex_available():
                    answer = Agent().run_codex(instruction)
                else:
                    answer = "⚠️ This task is set to run on 🧠 Codex, but the `codex` CLI isn't available on this host."
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
    """Stamp the heartbeat, then ENQUEUE every due task for the background drainer. Returns
    fast (just a DB scan + enqueue) so the engine's tick keeps beating on schedule even while a
    long task runs. Returns the number of tasks queued.

    last_run is advanced HERE, before the task runs, so a slow task can't be re-queued on the
    next tick while it's still in flight (and a failing task won't retry until its next cron) —
    same 'one bad task can't wedge the loop' guarantee as before, now without blocking.
    """
    beat()                                  # tell the UI we're alive
    _ensure_worker()
    con = _db()
    rows = con.execute("SELECT id, cron, instruction, last_run, enabled, source, model, base_url, run_once_at "
                       "FROM tasks").fetchall()
    now = _now()
    queued = 0
    for tid, cron, instruction, last_run, enabled, source, model, base_url, run_once_at in rows:
        if not (enabled and is_due(cron, last_run, now, run_once_at)):
            continue
        if run_once_at:                              # one-shot: fire exactly once, then disable
            con.execute("UPDATE tasks SET last_run=?, enabled=0 WHERE id=?", (now.isoformat(), tid))
        else:
            con.execute("UPDATE tasks SET last_run=? WHERE id=?", (now.isoformat(), tid))  # claim before dispatch
        con.commit()
        _TASK_Q.put((tid, source, instruction, source or f"task:{tid}", model, base_url))
        queued += 1
    con.close()
    return queued


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
        _set_run_status(tid, "error", f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _set_run_status(tid, "ok", answer)
    if advance:
        con = _db()
        con.execute("UPDATE tasks SET last_run=? WHERE id=?", (_now().isoformat(), tid))
        con.commit()
        con.close()
    notify(f"{instruction}\n\n{answer}", title="Oceano task (manual)")  # full report; notify() chunks per channel
    return {"ok": True, "result": answer}
