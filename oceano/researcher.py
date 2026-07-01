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
    con.execute("PRAGMA busy_timeout=5000")    # wait (don't error) when another writer holds the db
    con.execute("PRAGMA journal_mode=WAL")     # the drain worker writes while the Researcher view reads
    con.execute("CREATE TABLE IF NOT EXISTS topics ("
                "id INTEGER PRIMARY KEY, topic TEXT, focus TEXT, cron TEXT, "
                "enabled INTEGER DEFAULT 1, last_run TEXT, last_result TEXT, "
                "doc TEXT, task_id INTEGER)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(topics)").fetchall()}
    for col in ("model", "base_url"):     # per-topic model override (which model runs this deep-dive)
        if col not in cols:
            con.execute(f"ALTER TABLE topics ADD COLUMN {col} TEXT")
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
    rows = con.execute("SELECT id, topic, focus, cron, enabled, last_run, last_result, doc, task_id, "
                       "model, base_url FROM topics ORDER BY id").fetchall()
    con.close()
    out = []
    for r in rows:
        out.append({"id": r[0], "topic": r[1], "focus": r[2], "cron": r[3],
                    "enabled": bool(r[4]), "last_run": r[5], "last_result": r[6],
                    "doc": r[7], "task_id": r[8], "running": r[0] in _RUNNING,
                    "doc_exists": (config.WORKSPACE / r[7]).exists() if r[7] else False,
                    "model": r[9] or "", "base_url": r[10] or ""})
    return out


def add_topic(topic, focus="", cron="0 8 * * *", model="", base_url=""):
    """Create a research topic + its locked scheduler entry. `model` (with `base_url`) picks which
    model runs the deep-dive: '' = system default, 'claude'/'codex' = that mind, else an endpoint
    model id. Returns id or None."""
    topic = (topic or "").strip()
    if not topic or not _valid_cron(cron):
        return None
    from oceano import scheduler
    doc = f"{DOC_DIR}/{_slug(topic)}.md"
    model = (model or "").strip() or None
    base_url = (base_url or "").strip() or None
    con = _db()
    cur = con.execute("INSERT INTO topics (topic, focus, cron, doc, model, base_url) VALUES (?,?,?,?,?,?)",
                      (topic, (focus or "").strip(), cron, doc, model, base_url))
    con.commit()
    rid = cur.lastrowid
    task_id = scheduler.add_task(cron, PREFIX + topic, source=f"research:{rid}",
                                 model=model, base_url=base_url)   # mirror onto the scheduler entry too
    con.execute("UPDATE topics SET task_id=? WHERE id=?", (task_id, rid))
    con.commit()
    con.close()
    return rid


def update_topic(rid, topic=None, focus=None, cron=None, enabled=None, model=None, base_url=None):
    """Edit a topic and keep its scheduler entry in lockstep. For `model`/`base_url`: None leaves
    them unchanged, "" clears the override (back to the system default), a value pins that model."""
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
    if model is not None:                     # "" clears the override → back to the system default
        con.execute("UPDATE topics SET model=?, base_url=? WHERE id=?",
                    ((model or "").strip() or None, (base_url or "").strip() or None, rid))
    con.commit()
    con.close()
    from oceano import scheduler
    scheduler.update_task(row[2],
                          cron=cron,
                          instruction=(PREFIX + topic.strip()) if topic and topic.strip() else None,
                          enabled=enabled, allow_managed=True,
                          model=model, base_url=base_url)         # keep the scheduler entry in sync
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
_RUN_PROMPT = """You are running a RECURRING research job whose whole purpose is to DEEPEN a body of
knowledge over many runs — to DRILL DOWN, not to re-summarize the same overview each time. Work
autonomously.

TOPIC: {topic}
{focus_block}
You maintain a living research document at the workspace path: {doc}

Do this, in order:
1. read_file {doc} to see what is ALREADY known (if it doesn't exist yet, you will create it). Look
   especially at its "Open questions / Next to investigate" list — that backlog is what drives this run.
2. CHOOSE ONE SPECIFIC ANGLE to drill into this run. Do NOT re-research the whole topic broadly. Pick
   the single most valuable under-explored thread: an item from the Open-questions list, a gap in the
   current coverage, a claim worth verifying, or a genuinely new development. Rotate the angle across
   runs so different facets get deep coverage over time — do not repeat the angle of the last run.
3. Research THAT angle deeply: web_search with specific, narrow queries (not generic overview ones),
   then OPEN the best results with fetch_url and read the actual pages; follow citations toward primary
   sources. Aim for new specifics — mechanisms, numbers, caveats, concrete examples — depth over breadth.
4. Update the document so it GROWS richer (create the {doc_dir}/ folder first if needed). NEVER lose
   earlier content — for a long doc, prefer edit_file to amend it in place rather than rewriting the
   whole file. Keep this structure:
   # <Topic>
   ## Summary  — a tight current synthesis; revise it to fold in what you just learned.
   ## Open questions / Next to investigate
       a live bullet list that steers future runs: REMOVE questions you answered this run, and ADD the
       new questions and threads this run opened. Never leave it empty — good research always raises
       new questions, and this list is the memory that lets the next run go deeper instead of repeating.
   ## Deep dives
       an accumulating set of dated, focused subsections. ADD one for the angle you drilled into today
       ("### Deep dive — <today's date>: <angle>"). This is where the depth accumulates over time.
   ## Sources  — append the new URLs you actually read.
5. Keep it accurate, specific, and well-cited — it is documentation consulted later by you and the user.

Finish with a 2-3 line summary: which angle you drilled into, the key new specifics you learned, and
what open questions you recorded for next time."""


def run_topic(rid):
    """One research run, registered as a background job so the UI can show it running."""
    from oceano import jobs
    with jobs.job("research", f"research #{rid}", ref=f"research:{rid}") as jid:
        r = _run_topic(rid)
        jobs.set_result(jid, r)                          # surface the research result in the activity log
        return r


def _run_with_model(prompt, model, base_url):
    """Run the research prompt on the topic's chosen model — '' = system default, 'claude'/'codex'
    drive that resident mind, anything else is an endpoint model id (resolved via base_url). Mirrors
    the scheduler's per-task model dispatch so research honours the same choice."""
    from oceano.agent import Agent
    model = (model or "").strip()
    if model == "claude":
        from oceano import delegate
        if not delegate.available():
            return "⚠️ This topic is set to run on 🧠 Claude, but the `claude` CLI isn't available on this host."
        return Agent().run_claude(prompt)
    if model == "codex":
        from oceano import delegate
        if not delegate.codex_available():
            return "⚠️ This topic is set to run on 🧠 Codex, but the `codex` CLI isn't available on this host."
        return Agent().run_codex(prompt)
    ag = Agent()
    if model:                              # an endpoint model id (else Agent's configured default)
        ag.model = model
        if base_url:
            ag.base_url = base_url
            try:
                from oceano.web import server
                ag.api_key = server.endpoint_key(base_url)
            except Exception:
                pass
    return ag.run(prompt)


def _run_topic(rid):
    """Drive the agent, then re-index the docs into RAG. Called by the scheduler when its
    [ RESEARCH ] task is due, or by Run-now."""
    con = _db()
    row = con.execute("SELECT topic, focus, doc, model, base_url FROM topics WHERE id=?", (rid,)).fetchone()
    con.close()
    if not row:
        return "(research topic no longer exists)"
    topic, focus, doc, model, base_url = row
    with _RUN_LOCK:
        if rid in _RUNNING:
            return "(this research is already running)"
        _RUNNING.add(rid)
    try:
        from oceano import tools
        focus_block = f"FOCUS / GUIDANCE FROM THE USER: {focus}\n" if focus else ""
        prompt = _RUN_PROMPT.format(topic=topic, focus_block=focus_block, doc=doc, doc_dir=DOC_DIR)
        with tools.background():       # unattended → never drive the user's live browser
            answer = _run_with_model(prompt, model, base_url)
        try:                                  # re-embed only THIS topic's doc, not the whole folder
            from oceano import rag
            rag.index_docs(DOC_DIR, only=doc)
        except Exception:
            pass
        # The agent's final answer IS the run summary (the prompt tells it to end with one), and the
        # full findings already live in the doc. So report that summary in FULL plus a pointer to the
        # doc — don't crop the notification. Only the copy kept as last_result (the compact status line
        # in the Researcher UI list) is truncated.
        summary = (answer or "").strip()
        report = f"🔬 {topic} → workspace/{doc}\n\n{summary}" if summary else f"🔬 {topic} — (no summary produced)"
    except Exception as e:
        summary = report = f"run failed: {type(e).__name__}: {e}"
    finally:
        _RUNNING.discard(rid)
    con = _db()
    con.execute("UPDATE topics SET last_run=?, last_result=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), summary[:500], rid))   # cap only the UI status
    con.commit()
    con.close()
    return report


def run_topic_bg(rid):
    """Fire-and-forget run (the UI's Run-now button)."""
    threading.Thread(target=run_topic, args=(rid,), daemon=True).start()
