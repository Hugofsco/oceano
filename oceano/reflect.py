"""Nightly self-reflection — the feedback half of Oceano's self-evolution loop.

The daemon gathers the day's REAL signal (the activity log incl. failures, how the skills
library is moving, fresh research, recent conversation topics), hands a compact digest to the
strong improve-delegate to reflect on and propose concrete next steps, then writes a dated
journal entry under workspace/journal/ and returns a short summary (the scheduler pushes it).

A locked scheduler entry (source `self:reflect`) — schedulable + toggleable in the Scheduler,
not editable/removable there. The local model never judges its own behaviour: reflection runs on
the configured 'improve' delegate, same as skill review and memory maintenance.
"""
import json
import re
from datetime import datetime

import config
from oceano import atomicio

SOURCE = "self:reflect"
PREFIX = "[ SELF ] "
CRON = "30 23 * * *"                      # nightly at 23:30 local
JOURNAL = config.WORKSPACE / "journal"
RESEARCH = config.WORKSPACE / "research"


def _digest():
    """A compact, factual digest of the last day for the reflector. Read-only — no judgement here."""
    from oceano import logs, skills, chats
    lines = []

    runs = logs.recent(limit=120)                 # most-recent first; unattended runs (tasks/workflows/…)
    if runs:
        ok = sum(1 for r in runs if r["status"] == "ok")
        err = [r for r in runs if r["status"] != "ok"]
        lines.append(f"## Recent runs ({len(runs)} logged · {ok} ok · {len(err)} failed)")
        for r in runs[:40]:
            mark = "OK " if r["status"] == "ok" else "ERR"
            summ = " ".join((r.get("summary") or "").split())[:160]
            lines.append(f"- [{mark}] {r.get('kind')}: {r.get('title')}" + (f" — {summ}" if summ else ""))

    sk = skills.all_skills()
    if sk:
        by = {}
        for s in sk:
            by.setdefault(s["status"], []).append(s["name"])
        lines.append("\n## Skills library")
        for st in ("learning", "staged", "published"):
            names = by.get(st, [])
            if names:
                lines.append(f"- {st} ({len(names)}): " + ", ".join(names[:20]))

    if RESEARCH.exists():
        docs = sorted(RESEARCH.glob("*.md"))
        if docs:
            lines.append("\n## Research docs (living)")
            lines += [f"- {d.stem}" for d in docs[:20]]

    recents = chats.list_all()[:12]               # titles only — topic signal without shipping content
    if recents:
        lines.append("\n## Recent conversations (titles only)")
        lines += [f"- {c.get('date')}: {c.get('title')} ({c.get('count')} msgs)" for c in recents]

    return "\n".join(lines) if lines else "(no activity recorded yet)"


_REFLECT_PROMPT = """You are Oceano reflecting on your OWN last day of autonomous activity, to help
yourself improve. Below is a factual digest of your day: scheduled runs (with any failures), how
your skills library is moving, your research docs, and recent conversation topics.

Write a SHORT reflection in markdown (~150-300 words) with exactly these sections:
- **What happened** — the day in 2-3 sentences.
- **What went wrong** — any failed runs or stuck patterns and the likely cause; if nothing failed, say so plainly.
- **Next steps** — 2-4 CONCRETE, actionable proposals (a research topic to add, a skill worth learning,
  a workflow to build, a setting to change). Be specific — no vague aspirations.

After the markdown, output the SAME next-step proposals once more as a single fenced ```json block so
they can be queued for the user to approve:
```json
{{"proposals": [{{"kind": "research|workflow|memory|skill|setting|other", "title": "<short imperative>", "detail": "<specifics>"}}]}}
```
Use kind="research" for a topic to investigate on a schedule, "workflow" for a multi-step recipe to
build, "memory" for a durable fact to remember, and "skill"/"setting"/"other" otherwise.

Output the markdown reflection, then the json block — nothing else.

DIGEST:
{digest}"""


def _extract_proposals(text):
    """Split the reflection into (clean_markdown, [proposal dicts]) by pulling out the fenced json
    block. Tolerant: if there's no block or it won't parse, returns the text unchanged and []."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        return text, []
    try:
        proposals = json.loads(m.group(1)).get("proposals") or []
    except Exception:                                # noqa: BLE001 - malformed block → just skip proposals
        return text, []
    clean = (text[:m.start()] + text[m.end():]).strip()
    return clean, [p for p in proposals if isinstance(p, dict) and (p.get("title") or "").strip()]


def reflect():
    """Run one nightly reflection. Writes workspace/journal/<date>.md and returns the full
    reflection plus a pointer to that file (the scheduler notifies it). The reflection is
    ~150-300 words by design, so it reports in full rather than cropped. Blocking; meant to
    run in the background channel."""
    from oceano import delegate, jobs
    digest = _digest()
    with jobs.job("self", "nightly reflection", ref=SOURCE):
        r = delegate.run(_REFLECT_PROMPT.format(digest=digest[:9000]),
                         cwd=config.WORKSPACE, tools="Read", timeout=600, role="improve")
        if not r.get("ok"):
            return f"reflection skipped — delegate unavailable ({r.get('error')})"
        body = (r.get("output") or "").strip()
        if not body:
            return "reflection produced nothing"
        body, proposals = _extract_proposals(body)       # peel the structured proposals off the prose
        day = datetime.now().strftime("%Y-%m-%d")        # local day, matches the chat folders
        JOURNAL.mkdir(parents=True, exist_ok=True)
        path = JOURNAL / f"{day}.md"
        prior = path.read_text(encoding="utf-8") if path.exists() else ""
        head = prior + "\n\n---\n\n" if prior else f"# Reflection — {day}\n\n"
        atomicio.write_text(path, (head + body).strip() + "\n")
        queued = 0                                       # file the proposals as approvable suggestions
        if proposals:
            from oceano import suggestions
            for p in proposals[:8]:
                if suggestions.add(p.get("kind", "other"), p.get("title", ""), p.get("detail", ""), source=SOURCE):
                    queued += 1
        tail = (f"\n\n💡 {queued} suggestion(s) queued — review with list_suggestions, then "
                f"accept_suggestion / dismiss_suggestion.") if queued else ""
        return f"📓 Reflection journaled → workspace/journal/{day}.md{tail}\n\n{body}"


def ensure_task():
    """Make sure the locked '[ SELF ] reflection' schedule exists (visible in the Scheduler, not
    editable/removable there). Nightly at 23:30 — after the day's work, so it has something to chew on."""
    from oceano import scheduler
    if any(t.get("source") == SOURCE for t in scheduler.all_tasks()):
        return
    scheduler.add_task(CRON, PREFIX + "Nightly reflection — review the day, surface failures, propose next steps",
                       source=SOURCE)
