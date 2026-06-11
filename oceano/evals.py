"""Eval harness — measure how well each model performs agentic tasks on this box.

A CASE is a task plus graders. A RUN executes every enabled case against one or
more target models, producing one RESULT per (case × model). Cheap deterministic
graders (file exists / answer contains / tool was called) run in code; quality is
scored by an INDEPENDENT judge — Claude Code via oceano.delegate — never the model
under test. Aggregates feed the leaderboard (and, later, model routing).

Each case runs in a throwaway workspace (tools.background_workspace) so cases can't
pollute each other or the real workspace, and so file checks are deterministic.
Runs are grouped BY MODEL so llama-swap swaps once per model, not once per case.
"""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

import config

DB_PATH = config.WORKSPACE.parent / "data" / "evals.db"
RUN_DIR = config.WORKSPACE / ".eval-runs"          # throwaway per-case workspaces
EVAL_SOURCE = "evals:run"                          # the locked scheduler entry's source tag
CASE_TIMEOUT = 240                                 # default per-case wall-clock cap (seconds)
GRADER_TYPES = ("judge", "contains", "file_exists", "tool_called")
CATEGORIES = ("qa", "research", "code", "file", "reasoning", "tool-use")

_STATE = {"running": False, "phase": "", "done": 0, "total": 0, "last": None}


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS cases ("
                "id INTEGER PRIMARY KEY, name TEXT, category TEXT, prompt TEXT, rubric TEXT, "
                "graders TEXT, seed TEXT, timeout INTEGER, weight REAL DEFAULT 1.0, enabled INTEGER DEFAULT 1)")
    con.execute("CREATE TABLE IF NOT EXISTS runs ("
                "id INTEGER PRIMARY KEY, ts TEXT, models TEXT, status TEXT, summary TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS results ("
                "id INTEGER PRIMARY KEY, run_id INTEGER, case_id INTEGER, case_name TEXT, model TEXT, "
                "score REAL, passed INTEGER, tokens INTEGER, ms INTEGER, steps INTEGER, "
                "tools TEXT, error TEXT, verdict TEXT, answer TEXT)")
    return con


# ============================ cases CRUD ============================
def _row_to_case(r):
    return {"id": r[0], "name": r[1], "category": r[2], "prompt": r[3], "rubric": r[4],
            "graders": json.loads(r[5] or "[]"), "seed": json.loads(r[6] or "{}"),
            "timeout": r[7] or CASE_TIMEOUT, "weight": r[8], "enabled": bool(r[9])}


def all_cases():
    con = _db()
    rows = con.execute("SELECT id, name, category, prompt, rubric, graders, seed, timeout, weight, enabled "
                       "FROM cases ORDER BY id").fetchall()
    con.close()
    return [_row_to_case(r) for r in rows]


def _valid_graders(graders):
    out = []
    for g in graders or []:
        if isinstance(g, dict) and g.get("type") in GRADER_TYPES:
            out.append(g)
    return out or [{"type": "judge"}]              # default: judge against the rubric


def save_case(case_id, name, category, prompt, rubric, graders, seed=None, timeout=None,
              weight=1.0, enabled=True):
    g = json.dumps(_valid_graders(graders))
    s = json.dumps(seed or {})
    cat = category if category in CATEGORIES else "qa"
    to = int(timeout or CASE_TIMEOUT)
    con = _db()
    if case_id:
        con.execute("UPDATE cases SET name=?, category=?, prompt=?, rubric=?, graders=?, seed=?, "
                    "timeout=?, weight=?, enabled=? WHERE id=?",
                    (name, cat, prompt, rubric, g, s, to, weight, 1 if enabled else 0, case_id))
        rid = case_id
    else:
        cur = con.execute("INSERT INTO cases (name, category, prompt, rubric, graders, seed, timeout, weight, enabled) "
                          "VALUES (?,?,?,?,?,?,?,?,?)",
                          (name, cat, prompt, rubric, g, s, to, weight, 1 if enabled else 0))
        rid = cur.lastrowid
    con.commit()
    con.close()
    return rid


def delete_case(case_id):
    con = _db()
    con.execute("DELETE FROM cases WHERE id=?", (case_id,))
    con.commit()
    con.close()
    return True


# ============================ target models ============================
def available_models():
    """Served local models (llama-swap), newest registration first — the default
    targets for a run. Falls back to the configured default model."""
    try:
        from oceano import rivers
        served = [m["served"] for m in rivers.installed() if m.get("served")]
        seen, out = set(), []
        for name in served:
            if name not in seen:
                seen.add(name); out.append(name)
        if out:
            return out
    except Exception:
        pass
    return [config.MODEL]


# ============================ running a single case ============================
def _run_case(case, model):
    """Drive the agent through one case in an isolated workspace; capture answer,
    tools used, tokens, steps, wall-time, created files. Returns a result dict."""
    from oceano.agent import Agent
    from oceano import tools
    safe = re.sub(r"[^a-z0-9]+", "-", case["name"].lower()).strip("-")[:40] or "case"
    scratch = RUN_DIR / f"{safe}-{case['id']}"
    answer, tools_used, tokens, steps, err = "", [], 0, 0, None
    t0 = time.time()
    try:
        with tools.background_workspace(scratch) as root:
            for fn, content in (case.get("seed") or {}).items():
                try:
                    p = (root / fn)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(str(content), encoding="utf-8")
                except OSError:
                    pass
            ag = Agent(model=model)
            deadline = t0 + (case.get("timeout") or CASE_TIMEOUT)
            for ev in ag.run_stream(case["prompt"]):
                kind = ev.get("type")
                if kind == "token":
                    answer += ev.get("text", "")
                elif kind == "answer":
                    answer = ev.get("text", answer)
                elif kind == "tool_call":
                    tools_used.append(ev.get("name", "")); steps += 1
                elif kind == "stats":
                    tokens = ev.get("tokens", 0)
                if time.time() > deadline:
                    err = "timeout"
                    break
            files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    except Exception as e:
        files = []
        err = f"{type(e).__name__}: {e}"
    return {"case": case, "model": model, "answer": answer.strip(), "tools": tools_used,
            "tokens": tokens, "steps": steps, "ms": int((time.time() - t0) * 1000),
            "error": err, "scratch": str(scratch), "files": files}


# ============================ grading ============================
def _grade_deterministic(case, run):
    """Run the non-judge graders in code. Returns (all_passed, [notes])."""
    ok, notes = True, []
    root = config.WORKSPACE / ".eval-runs"
    from pathlib import Path
    scratch = Path(run["scratch"])
    answer_l = (run["answer"] or "").lower()
    for g in case["graders"]:
        t = g.get("type")
        if t == "contains":
            needle = g.get("value", "")
            try:
                hit = bool(re.search(needle, run["answer"] or "", re.IGNORECASE)) if g.get("regex") \
                    else needle.lower() in answer_l
            except re.error:
                hit = needle.lower() in answer_l
            ok &= hit; notes.append(f"contains {needle!r}: {'✓' if hit else '✗'}")
        elif t == "file_exists":
            fp = scratch / g.get("path", "")
            hit = fp.is_file() and (not g.get("nonempty") or fp.stat().st_size > 0)
            ok &= hit; notes.append(f"file {g.get('path')!r}: {'✓' if hit else '✗'}")
        elif t == "tool_called":
            hit = g.get("name") in run["tools"]
            ok &= hit; notes.append(f"used {g.get('name')!r}: {'✓' if hit else '✗'}")
    return ok, notes


_JUDGE_PROMPT = """You are grading an AI agent's attempt at a task. Be a fair but strict judge.

TASK GIVEN TO THE AGENT:
{prompt}

WHAT A GOOD RESULT LOOKS LIKE (rubric):
{rubric}

The agent's run produced files in this folder (read them if relevant to judging): {scratch}
Files created: {files}

THE AGENT'S FINAL ANSWER:
{answer}

Score how well the agent accomplished the task. Output ONLY a JSON object:
{{"score": <0-100 integer>, "pass": <true|false>,
  "dimensions": {{"correctness": <0-100>, "completeness": <0-100>, "efficiency": <0-100>, "safety": <0-100>}},
  "reasoning": "<one or two sentences>"}}"""


def _judge(case, run):
    """Independent quality score from Claude Code. Returns a verdict dict or None."""
    from oceano import delegate
    from pathlib import Path
    prompt = _JUDGE_PROMPT.format(
        prompt=case["prompt"], rubric=case.get("rubric") or "(use your judgment)",
        scratch=run["scratch"], files=", ".join(run["files"]) or "(none)",
        answer=(run["answer"] or "(no answer produced)")[:4000])
    r = delegate.to_claude(prompt, cwd=Path(run["scratch"]), tools="Read,Glob,Grep", timeout=600)
    if not r["ok"]:
        return {"score": 0, "pass": False, "reasoning": f"judge unavailable: {r['error']}", "judge_error": True}
    m = re.search(r"\{.*\}", r["output"], re.DOTALL)
    if not m:
        return {"score": 0, "pass": False, "reasoning": "judge returned no parsable verdict", "judge_error": True}
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return {"score": 0, "pass": False, "reasoning": "judge verdict was not valid JSON", "judge_error": True}
    v["score"] = max(0, min(100, int(v.get("score", 0))))
    v["pass"] = bool(v.get("pass"))
    return v


def _grade(case, run):
    """Combine deterministic graders with the judge. A deterministic failure caps the
    score at 0; the judge (if present) supplies the graded score otherwise."""
    det_ok, notes = _grade_deterministic(case, run)
    has_judge = any(g.get("type") == "judge" for g in case["graders"])
    verdict = {"deterministic": notes, "deterministic_pass": det_ok}
    if run["error"]:
        return {"score": 0.0, "passed": False, "verdict": {**verdict, "reasoning": f"run error: {run['error']}"}}
    if not has_judge:
        return {"score": 100.0 if det_ok else 0.0, "passed": det_ok, "verdict": verdict}
    j = _judge(case, run)
    verdict.update(j)
    score = float(j["score"]) if det_ok else 0.0
    passed = det_ok and j.get("pass", False)
    return {"score": score, "passed": passed, "verdict": verdict}


# ============================ orchestration ============================
def state():
    return dict(_STATE)


def run_all(models=None):
    """Run every enabled case against each target model (default: served local
    models), grouped by model so llama-swap swaps once per model. Stores a run +
    its results, returns a short summary string. Blocking — call in a thread."""
    if _STATE["running"]:
        return "(an eval run is already in progress)"
    cases = [c for c in all_cases() if c["enabled"]]
    models = [m for m in (models or available_models()) if m]
    if not cases:
        return "(no eval cases defined yet — add some in Brain → Evals)"
    if not models:
        return "(no target models available)"

    _STATE.update({"running": True, "phase": "starting", "done": 0,
                   "total": len(cases) * len(models), "last": None})
    con = _db()
    cur = con.execute("INSERT INTO runs (ts, models, status, summary) VALUES (?,?,?,?)",
                      (datetime.now(timezone.utc).isoformat(), json.dumps(models), "running", ""))
    run_id = cur.lastrowid
    con.commit()
    con.close()

    per_model = {m: [] for m in models}
    try:
        for model in models:                       # group by model → one llama-swap per model
            for case in cases:
                _STATE["phase"] = f"{model} · {case['name']}"
                run = _run_case(case, model)
                graded = _grade(case, run)
                per_model[model].append(graded["score"])
                con = _db()
                con.execute("INSERT INTO results (run_id, case_id, case_name, model, score, passed, "
                            "tokens, ms, steps, tools, error, verdict, answer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, case["id"], case["name"], model, graded["score"],
                             1 if graded["passed"] else 0, run["tokens"], run["ms"], run["steps"],
                             json.dumps(run["tools"]), run["error"], json.dumps(graded["verdict"]),
                             (run["answer"] or "")[:4000]))
                con.commit()
                con.close()
                _STATE["done"] += 1
        ranked = sorted(((sum(v) / len(v), m) for m, v in per_model.items() if v), reverse=True)
        summary = " · ".join(f"{m}: {avg:.0f}" for avg, m in ranked) or "no results"
        status = "done"
    except Exception as e:
        summary = f"run failed: {type(e).__name__}: {e}"
        status = "error"
    finally:
        con = _db()
        con.execute("UPDATE runs SET status=?, summary=? WHERE id=?", (status, summary, run_id))
        con.commit()
        con.close()
        _STATE.update({"running": False, "phase": "", "last": summary})
        _cleanup_scratch()
    return summary


def _cleanup_scratch():
    import shutil
    try:
        if RUN_DIR.exists():
            shutil.rmtree(RUN_DIR)
    except OSError:
        pass


def run_all_bg(models=None):
    import threading
    threading.Thread(target=run_all, args=(models,), daemon=True).start()


# ============================ reports for the UI ============================
def runs(limit=20):
    con = _db()
    rows = con.execute("SELECT id, ts, models, status, summary FROM runs ORDER BY id DESC LIMIT ?",
                       (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "models": json.loads(r[2] or "[]"),
             "status": r[3], "summary": r[4]} for r in rows]


def leaderboard(run_id=None):
    """Per-model aggregate for a run (default: the latest finished run)."""
    con = _db()
    if run_id is None:
        row = con.execute("SELECT id FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        run_id = row[0] if row else None
    if run_id is None:
        con.close()
        return {"run_id": None, "rows": []}
    rows = con.execute(
        "SELECT model, AVG(score), AVG(passed)*100, SUM(tokens), AVG(ms), AVG(steps), COUNT(*) "
        "FROM results WHERE run_id=? GROUP BY model", (run_id,)).fetchall()
    con.close()
    board = [{"model": r[0], "score": round(r[1], 1), "pass_rate": round(r[2], 0),
              "tokens": int(r[3] or 0), "avg_ms": int(r[4] or 0), "avg_steps": round(r[5], 1),
              "cases": r[6]} for r in rows]
    board.sort(key=lambda x: x["score"], reverse=True)
    return {"run_id": run_id, "rows": board}


def results(run_id):
    con = _db()
    rows = con.execute("SELECT case_name, model, score, passed, tokens, ms, steps, tools, error, verdict, answer "
                       "FROM results WHERE run_id=? ORDER BY case_name, model", (run_id,)).fetchall()
    con.close()
    return [{"case": r[0], "model": r[1], "score": r[2], "passed": bool(r[3]), "tokens": r[4],
             "ms": r[5], "steps": r[6], "tools": json.loads(r[7] or "[]"), "error": r[8],
             "verdict": json.loads(r[9] or "{}"), "answer": r[10]} for r in rows]


# ============================ scheduled job + seeds ============================
def ensure_eval_task():
    """Make sure the locked '[ EVAL ]' schedule exists (visible in the Scheduler;
    its schedule + on/off are user-editable there, but it can't be deleted). Weekly."""
    from oceano import scheduler
    if any(t.get("source") == EVAL_SOURCE for t in scheduler.all_tasks()):
        return
    scheduler.add_task("0 4 * * 1",          # Mondays 04:00
                       "[ EVAL ] Run the model eval suite (judged by Claude Code)",
                       source=EVAL_SOURCE)


_SEED_CASES = [
    {"name": "capital-of-japan", "category": "qa",
     "prompt": "What is the capital of Japan? Answer in one word.",
     "rubric": "The answer is Tokyo.",
     "graders": [{"type": "contains", "value": "tokyo"}, {"type": "judge"}]},
    {"name": "arithmetic-chain", "category": "reasoning",
     "prompt": "A shop sells pens at 3 for $2. How much do 12 pens cost? Give the number only.",
     "rubric": "The answer is $8 (12 pens = 4 sets × $2).",
     "graders": [{"type": "contains", "value": "8"}, {"type": "judge"}]},
    {"name": "write-haiku-file", "category": "file",
     "prompt": "Write a haiku about the ocean and save it to ocean.txt in the workspace.",
     "rubric": "ocean.txt exists and contains a three-line haiku about the ocean.",
     "graders": [{"type": "file_exists", "path": "ocean.txt", "nonempty": True}, {"type": "judge"}]},
    {"name": "python-script", "category": "code",
     "prompt": "Write a Python script primes.py that prints the first 10 prime numbers, then run it.",
     "rubric": "primes.py exists and, when run, prints 2 3 5 7 11 13 17 19 23 29.",
     "graders": [{"type": "file_exists", "path": "primes.py", "nonempty": True}, {"type": "judge"}]},
    {"name": "research-fetch", "category": "research",
     "prompt": "Find out what the SI base unit of electric current is and cite where you read it.",
     "rubric": "States the ampere, and actually fetched a page (didn't answer from memory alone).",
     "graders": [{"type": "tool_called", "name": "fetch_url"}, {"type": "contains", "value": "ampere"}, {"type": "judge"}]},
    {"name": "tool-choice-calendar", "category": "tool-use",
     "prompt": "What's on my calendar in the next 7 days?",
     "rubric": "Calls the calendar_events tool rather than guessing or refusing.",
     "graders": [{"type": "tool_called", "name": "calendar_events"}, {"type": "judge"}]},
]


def seed_cases():
    """Install the starter cases if the case set is empty. Returns count added."""
    if all_cases():
        return 0
    for c in _SEED_CASES:
        save_case(None, c["name"], c["category"], c["prompt"], c["rubric"], c["graders"])
    return len(_SEED_CASES)
