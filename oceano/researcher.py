"""Researcher — recurring, configurable deep-dives the agent runs on a schedule.

Each topic owns one scheduler task (instruction prefixed '[ RESEARCH ]'). Those
tasks are visible in the Scheduler like any other run, but locked there — they
can only be created, edited, or deleted through the Researcher. Each run updates
a living markdown document under workspace/research/ and re-indexes it into RAG,
so both the user and the model can consult what has been learned so far.
"""
import re
import sqlite3
import threading
from datetime import datetime, timezone

import config

DB_PATH = config.WORKSPACE.parent / "data" / "research.db"
PREFIX = "[ RESEARCH ] "
DOC_DIR = "research"                      # workspace-relative folder for the docs

_RUNNING = set()                          # topic ids currently mid-run
_RUN_LOCK = threading.Lock()


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS topics ("
                "id INTEGER PRIMARY KEY, topic TEXT, focus TEXT, cron TEXT, "
                "enabled INTEGER DEFAULT 1, last_run TEXT, last_result TEXT, "
                "doc TEXT, task_id INTEGER)")
    return con


def _slug(topic):
    s = re.sub(r"[^a-z0-9]+", "-", (topic or "").lower()).strip("-")[:60]
    return s or "topic"


def _valid_cron(cron):
    try:
        from croniter import croniter
        return croniter.is_valid(cron)
    except ImportError:
        return bool(cron)


# ============================ CRUD ============================
def all_topics():
    con = _db()
    rows = con.execute("SELECT id, topic, focus, cron, enabled, last_run, last_result, doc, task_id "
                       "FROM topics ORDER BY id").fetchall()
    con.close()
    out = []
    for r in rows:
        out.append({"id": r[0], "topic": r[1], "focus": r[2], "cron": r[3],
                    "enabled": bool(r[4]), "last_run": r[5], "last_result": r[6],
                    "doc": r[7], "task_id": r[8], "running": r[0] in _RUNNING,
                    "doc_exists": (config.WORKSPACE / r[7]).exists() if r[7] else False})
    return out


def add_topic(topic, focus="", cron="0 8 * * *"):
    """Create a research topic + its locked scheduler entry. Returns id or None."""
    topic = (topic or "").strip()
    if not topic or not _valid_cron(cron):
        return None
    from oceano import scheduler
    doc = f"{DOC_DIR}/{_slug(topic)}.md"
    con = _db()
    cur = con.execute("INSERT INTO topics (topic, focus, cron, doc) VALUES (?,?,?,?)",
                      (topic, (focus or "").strip(), cron, doc))
    con.commit()
    rid = cur.lastrowid
    task_id = scheduler.add_task(cron, PREFIX + topic, source=f"research:{rid}")
    con.execute("UPDATE topics SET task_id=? WHERE id=?", (task_id, rid))
    con.commit()
    con.close()
    return rid


def update_topic(rid, topic=None, focus=None, cron=None, enabled=None):
    """Edit a topic and keep its scheduler entry in lockstep."""
    con = _db()
    row = con.execute("SELECT topic, cron, task_id FROM topics WHERE id=?", (rid,)).fetchone()
    if not row:
        con.close()
        return False
    if cron is not None and not _valid_cron(cron):
        con.close()
        return False
    if topic is not None and topic.strip():
        con.execute("UPDATE topics SET topic=? WHERE id=?", (topic.strip(), rid))
    if focus is not None:
        con.execute("UPDATE topics SET focus=? WHERE id=?", (focus.strip(), rid))
    if cron is not None:
        con.execute("UPDATE topics SET cron=? WHERE id=?", (cron, rid))
    if enabled is not None:
        con.execute("UPDATE topics SET enabled=? WHERE id=?", (1 if enabled else 0, rid))
    con.commit()
    con.close()
    from oceano import scheduler
    scheduler.update_task(row[2],
                          cron=cron,
                          instruction=(PREFIX + topic.strip()) if topic and topic.strip() else None,
                          enabled=enabled, allow_managed=True)
    return True


def note_schedule(rid, cron=None, enabled=None):
    """Mirror a schedule/on-off change the user made from the Scheduler back into the
    topic record, so the Researcher view stays in sync. Does NOT touch the scheduler
    (the change originated there) — avoids a sync loop."""
    sets, vals = [], []
    if cron is not None:
        sets.append("cron=?"); vals.append(cron)
    if enabled is not None:
        sets.append("enabled=?"); vals.append(1 if enabled else 0)
    if not sets:
        return
    vals.append(rid)
    con = _db()
    con.execute(f"UPDATE topics SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    con.close()


def delete_topic(rid):
    """Remove a topic + its scheduler entry. The research doc is kept (it's the
    user's accumulated knowledge — delete it from Files if unwanted)."""
    con = _db()
    row = con.execute("SELECT task_id FROM topics WHERE id=?", (rid,)).fetchone()
    con.execute("DELETE FROM topics WHERE id=?", (rid,))
    con.commit()
    con.close()
    if row and row[0]:
        from oceano import scheduler
        scheduler.delete_task(row[0], allow_managed=True)
    return True


# ============================ running ============================
_RUN_PROMPT = """You are running a scheduled RESEARCH job. Work autonomously.

TOPIC: {topic}
{focus_block}
You maintain a living research document at the workspace path: {doc}

Do this, in order:
1. read_file {doc} to see what is already documented (if it doesn't exist yet, you will create it).
2. Research the topic NOW: use web_search with 2-4 different queries, then OPEN the
   most relevant results with fetch_url and read the actual pages. Prefer recent sources.
3. Update the document with write_file (create the {doc_dir}/ folder first if needed). Structure:
   # <Topic>
   ## Summary  (a current, comprehensive overview — rewrite it to stay accurate)
   ## Findings — <today's date>  (what's new or changed since the last run)
   ...keep all previous dated "Findings" sections — never delete prior knowledge...
   ## Sources  (URLs you used, append new ones)
4. Keep it comprehensive, factual, and well-organized — it is documentation that will
   be consulted later, both by the user and by you in future conversations.

Finish with a 2-3 line summary of what you added or changed this run."""


def run_topic(rid):
    """One research run: drive the agent, then re-index the docs into RAG.
    Called by the scheduler when its [ RESEARCH ] task is due, or by Run-now."""
    con = _db()
    row = con.execute("SELECT topic, focus, doc FROM topics WHERE id=?", (rid,)).fetchone()
    con.close()
    if not row:
        return "(research topic no longer exists)"
    topic, focus, doc = row
    with _RUN_LOCK:
        if rid in _RUNNING:
            return "(this research is already running)"
        _RUNNING.add(rid)
    try:
        from oceano.agent import Agent
        from oceano import tools
        focus_block = f"FOCUS / GUIDANCE FROM THE USER: {focus}\n" if focus else ""
        with tools.background():       # unattended → never drive the user's live browser
            answer = Agent().run(_RUN_PROMPT.format(topic=topic, focus_block=focus_block,
                                                    doc=doc, doc_dir=DOC_DIR))
        try:                                  # re-embed only THIS topic's doc, not the whole folder
            from oceano import rag
            rag.index_docs(DOC_DIR, only=doc)
        except Exception:
            pass
        result = (answer or "").strip()[:500]
    except Exception as e:
        result = f"run failed: {type(e).__name__}: {e}"
    finally:
        _RUNNING.discard(rid)
    con = _db()
    con.execute("UPDATE topics SET last_run=?, last_result=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), result, rid))
    con.commit()
    con.close()
    return result


def run_topic_bg(rid):
    """Fire-and-forget run (the UI's Run-now button)."""
    threading.Thread(target=run_topic, args=(rid,), daemon=True).start()
