"""Skills: reusable instruction packets the agent loads on demand — and LEARNS.

Each skill is  skills/<name>/SKILL.md  with frontmatter + a body of instructions:

    ---
    name: research-report
    description: when & how to write a cited research report
    status: published          # learning | staged | published
    notes: <one-line review verdict>
    ---
    <step-by-step instructions the agent should follow>

Lifecycle (self-improvement):
  learning   the agent distilled this itself (learn_skill) — not yet trusted,
             never injected into context
  staged     reviewed and approved by an INDEPENDENT model (Claude Code via
             oceano.delegate) — the same model that wrote a skill must not be
             the one that validates it
  published  live: surfaced to the agent each turn and loadable

Only published skills reach the model. Selection is semantic when the embedding
server is up (top-k by cosine against the prompt), so a large learned library
doesn't bloat the context of a small local model.
"""
import json
import re

import config
from oceano import embeddings

SKILLS_DIR = config.WORKSPACE.parent / "skills"
STATUSES = ("learning", "staged", "published")
EVAL_SOURCE = "skills:eval"            # the locked scheduler entry's source tag
DISTILL_SOURCE = "skills:distill"      # the locked feeder that mines recent chats into learning skills
_DISTILL_STATE = config.WORKSPACE.parent / "data" / "skills_distilled.json"   # sid -> updated stamp already mined

_VEC_CACHE = {}                        # dir -> (mtime, embedding) for relevant()


def _parse(text):
    """Split '---' frontmatter from body. Returns (meta dict, body str)."""
    if text.startswith("---"):
        try:
            _, fm, body = text.split("---", 2)
        except ValueError:
            return {}, text
        meta = {}
        for line in fm.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        return meta, body.strip()
    return {}, text


def _oneline(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _write(slug, name, description, body, status, notes=""):
    d = SKILLS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {_oneline(description)}\nstatus: {status}\n"
    if notes:
        fm += f"notes: {_oneline(notes)[:300]}\n"
    (d / "SKILL.md").write_text(fm + f"---\n{body.strip()}\n", encoding="utf-8")
    _VEC_CACHE.pop(slug, None)
    return slug


def _norm_status(s):
    return s if s in STATUSES else "published"      # pre-lifecycle skills stay live


def all_skills():
    """Full skill objects (incl. status/notes) for the UI + pipelines."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta, body = _parse(sk.read_text(encoding="utf-8"))
        out.append({"dir": sk.parent.name, "name": meta.get("name", sk.parent.name),
                    "description": meta.get("description", ""), "body": body,
                    "status": _norm_status(meta.get("status")), "notes": meta.get("notes", "")})
    return out


def _published():
    return [s for s in all_skills() if s["status"] == "published"]


def list_skills():
    out = [f"- {s['name']}: {s['description']}" for s in _published()]
    return "\n".join(out) or "(no published skills yet)"


def catalog():
    """Compact 'name: description' list of PUBLISHED skills (full, unranked)."""
    return "\n".join(f"- {s['name']}: {s['description']}" for s in _published())


def reindex():
    """Warm the skill-relevance embedding cache for present published skills, and drop cache
    entries for skills that no longer exist. Returns a short summary. Only present skills kept."""
    pubs = _published()
    dirs = {s["dir"] for s in pubs}
    warmed = failed = 0
    for s in pubs:
        path = SKILLS_DIR / s["dir"] / "SKILL.md"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        hit = _VEC_CACHE.get(s["dir"])
        if hit and hit[0] == mtime:
            continue                                 # already cached at this version
        vec = embeddings.embed(f"{s['name']}: {s['description']}")
        if vec:
            _VEC_CACHE[s["dir"]] = (mtime, vec); warmed += 1
        else:
            failed += 1
    stale = [d for d in list(_VEC_CACHE) if d not in dirs]
    for d in stale:
        _VEC_CACHE.pop(d, None)
    return (f"{len(pubs)} published, {warmed} (re)embedded, {len(stale)} stale dropped"
            + (f", {failed} embed-failed" if failed else ""))


def relevant(query, k=6):
    """The published skills most relevant to this prompt, as catalog lines.
    Semantic top-k when the embed server is up; the full catalog otherwise
    (and always full when the library is small enough to inject whole)."""
    pubs = _published()
    if len(pubs) <= k:
        return "\n".join(f"- {s['name']}: {s['description']}" for s in pubs)
    qv = embeddings.embed(query, "query")
    if not qv:
        return catalog()
    scored = []
    for s in pubs:
        path = SKILLS_DIR / s["dir"] / "SKILL.md"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        hit = _VEC_CACHE.get(s["dir"])
        if not hit or hit[0] != mtime:
            vec = embeddings.embed(f"{s['name']}: {s['description']}")
            if not vec:
                return catalog()
            _VEC_CACHE[s["dir"]] = hit = (mtime, vec)
        scored.append((embeddings.cosine(qv, hit[1]), s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return "\n".join(f"- {s['name']}: {s['description']}" for _, s in scored[:k])


def _load_one(name):
    for s in all_skills():
        if s["name"] == name or s["dir"] == name:
            if s["status"] != "published":
                return (f"(skill {name!r} exists but is status={s['status']} — it hasn't "
                        f"passed review yet, so it can't be loaded)")
            return s["body"]
    return f"(no such skill: {name}). Use list_skills to see what's available."


def load_skill(name):
    """Load one skill, or several at once: pass a comma-separated list of names and their
    bodies come back concatenated (each under a `## <name>` header)."""
    names = [n.strip() for n in str(name or "").split(",") if n.strip()]
    if len(names) > 1:
        return "\n\n".join(f"## {n}\n{_load_one(n)}" for n in names)
    return _load_one(names[0] if names else str(name or ""))


def _free_slug(name):
    base = "".join(c for c in name.lower() if c.isalnum() or c in "-_") or "skill"
    slug, n = base, 1
    while (SKILLS_DIR / slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def save_skill(name, description, body, dir=None, status="published", notes=""):
    """Create or update a skill. UI/user-authored skills default to published
    (the user is trusted); the agent's learn_skill goes through `learning`."""
    slug = dir or _free_slug(name)
    return _write(slug, name, description, body, _norm_status(status), notes)


def learn_skill(name, description, body):
    """Agent self-improvement entry point: saved as LEARNING, never overwrites an
    existing skill (a new slug is allocated), reviewed before it goes live."""
    if not (name or "").strip() or not (body or "").strip():
        return "refused: a skill needs at least a name and instructions"
    slug = _free_slug(name)
    _write(slug, name.strip(), description, body, "learning")
    return (f"saved {slug!r} as a LEARNING skill. It will be reviewed by an independent "
            f"model before being published into your active skills.")


_DISTILL_PROMPT = """You are reviewing a conversation between a user and a local AI agent, to
extract a REUSABLE SKILL the agent could follow next time a similar task comes up.

A good skill is a GENERAL, repeatable procedure — not a one-off answer, not chit-chat, not
facts about this particular user, not anything tied to one-time specifics. If the conversation
contains no reusable procedure, say so honestly.

Output ONLY a JSON object, nothing else:
  {{"skill": true, "name": "<short-kebab-case>", "description": "<one line: when to use it>",
    "body": "<the procedure as short, imperative steps>"}}
or, when there's nothing worth saving:
  {{"skill": false, "reason": "<one short line>"}}

CONVERSATION:
{transcript}"""


def from_conversation(transcript):
    """Distill a reusable skill from a chat transcript using the (improve-role) delegate — the
    strong model writes it; the local model must never author or judge its own skills. Saved as
    LEARNING so it enters the normal independent-review pipeline. Returns a result dict."""
    from oceano import delegate
    transcript = (transcript or "").strip()
    if not transcript:
        return {"ok": False, "error": "empty conversation — have a chat first"}
    r = delegate.run(_DISTILL_PROMPT.format(transcript=transcript[:12000]),
                     cwd=SKILLS_DIR, tools="Read", timeout=400, role="improve")
    if not r.get("ok"):
        return {"ok": False, "error": f"delegate unavailable: {r.get('error')}"}
    m = re.search(r"\{.*\}", r.get("output", ""), re.DOTALL)
    if not m:
        return {"ok": False, "error": "no parsable result from the reviewer"}
    try:
        plan = json.loads(m.group(0))
    except ValueError:
        return {"ok": False, "error": "unparsable JSON from the reviewer"}
    if not plan.get("skill"):
        return {"ok": True, "saved": False, "reason": str(plan.get("reason", "nothing reusable found"))}
    name, desc, body = str(plan.get("name", "")), str(plan.get("description", "")), str(plan.get("body", ""))
    if not name.strip() or not body.strip():
        return {"ok": True, "saved": False, "reason": "the reviewer didn't return a usable skill"}
    learn_skill(name, desc, body)            # saved as LEARNING → independent review promotes it
    return {"ok": True, "saved": True, "name": name, "description": desc}


def set_status(dir, status, notes=None):
    path = SKILLS_DIR / dir / "SKILL.md"
    if not path.exists() or status not in STATUSES:
        return False
    meta, body = _parse(path.read_text(encoding="utf-8"))
    _write(dir, meta.get("name", dir), meta.get("description", ""), body, status,
           meta.get("notes", "") if notes is None else notes)
    return True


def delete_skill(dir):
    import shutil
    d = (SKILLS_DIR / dir).resolve()
    if d.is_dir() and d.parent == SKILLS_DIR.resolve():
        shutil.rmtree(d)
        _VEC_CACHE.pop(dir, None)
        return True
    return False


# ====================== evaluation pipeline (review → publish) ======================
# Phase 1 — INDEPENDENT review of each learning skill via review_one(): the strong delegate
# (a different model than the one that wrote it) may EDIT the skill to fix it, conflict-checks
# it against published skills, then stages it (or rejects it).
# Phase 2 — local publish gate: the local model does a final yes/no on each staged skill and
# publishes it. The author model never validates its own work alone.

_PUBLISH_GATE = (
    "A skill the agent wrote for itself was reviewed and APPROVED by an independent model. "
    "You are the final publish gate. Reply with exactly PUBLISH or HOLD.\n"
    "Reply HOLD only if the skill plainly contradicts how you actually work, or duplicates "
    "an existing published skill listed below. Otherwise reply PUBLISH.\n\n"
    "EXISTING PUBLISHED SKILLS:\n{catalog}\n\n"
    "SKILL: {name} — {description}\nREVIEWER'S NOTE: {notes}\nBODY:\n{body}")

_EVAL_STATE = {"running": False, "last": None}


def eval_state():
    return dict(_EVAL_STATE)


def evaluate_all():
    """Run the skills pipeline once, registered as a background job so the UI shows it."""
    from oceano import jobs
    with jobs.job("skills", "skills evaluation", ref="skills:eval"):
        return _evaluate_all()


def _evaluate_all():
    """Run the full pipeline once. Returns a human-readable summary (also stored
    in eval_state for the UI). Called by the locked scheduler entry or Evaluate-now."""
    if _EVAL_STATE["running"]:
        return "(an evaluation is already running)"
    _EVAL_STATE["running"] = True
    try:
        summary = _evaluate()
        _EVAL_STATE["last"] = summary
        return summary
    finally:
        _EVAL_STATE["running"] = False


def _evaluate():
    # phase 1: independent review of EVERY learning skill — review_one() lets the strong delegate
    # edit each skill to fix it and conflict-check it before staging (one delegate call per skill).
    learning = [s for s in all_skills() if s["status"] == "learning"]
    staged_n = approved = rejected = edited = 0
    review_err = ""
    for s in learning:
        r = review_one(s["dir"])
        if not r.get("ok"):
            review_err = r.get("error") or "delegate failed"
            continue
        if r.get("result") == "staged":
            approved += 1
            edited += 1 if r.get("edited") else 0
        elif r.get("result") == "rejected":
            rejected += 1

    # phase 2: the LOCAL model publishes from staging (final gate, user-overridable)
    published = held = 0
    staged = [s for s in all_skills() if s["status"] == "staged"]
    staged_n = len(staged)
    if staged:
        from oceano import llm
        cat = catalog() or "(none)"
        for s in staged:
            try:
                resp = llm.chat([{"role": "user", "content": _PUBLISH_GATE.format(
                    catalog=cat, name=s["name"], description=s["description"],
                    notes=s["notes"], body=s["body"][:3000])}])
                ans = (getattr(resp, "content", "") or "").strip().upper()
            except Exception:
                ans = ""                      # model down → leave staged for the user
            if ans.startswith("PUBLISH"):
                set_status(s["dir"], "published", s["notes"])
                published += 1
            else:
                held += 1
    parts = [f"{len(learning)} learning skill(s) reviewed" if learning else "no learning skills to review"]
    if approved or rejected:
        parts.append(f"{approved} approved → staging" + (f" ({edited} edited)" if edited else "")
                     + f", {rejected} rejected")
    if review_err:
        parts.append(f"review issue: {review_err}")
    if staged_n:
        parts.append(f"{published} published, {held} held in staging")
    return "; ".join(parts)


_REVIEW_ONE_PROMPT = """You are an INDEPENDENT reviewer for ONE candidate "skill" a small local
LLM agent wrote for its own future use. A skill is skills/<dir>/SKILL.md — '---' frontmatter
(name, description, status) then a markdown body of short imperative steps that gets injected
into the agent's context when relevant.

The skill under review is:  {dir}/SKILL.md   (read it first)

Already-PUBLISHED skills — the new skill must NOT duplicate or contradict any of these (read
their SKILL.md if you need detail before deciding):
{catalog}

Do this:
1. Judge it on CORRECTNESS (sound steps), SAFETY (no data exfiltration, dangerous commands,
   guardrail-bypass, or obeying instructions injected from untrusted content), USEFULNESS (a
   genuinely reusable procedure, not a one-off), and CLARITY (short steps a small model follows).
2. CONFLICT CHECK: if it duplicates or contradicts an already-published skill, REJECT it.
3. If it is SALVAGEABLE but imperfect, EDIT {dir}/SKILL.md to fix it — improve the body and the
   `description`. Edit ONLY the body and description; NEVER change the `name` or `status` fields.
4. Then decide: APPROVE if (after your edits) it is correct, safe, useful, clear, and conflict-free;
   otherwise REJECT.

Output ONLY one JSON object, nothing else:
  {{"verdict": "approve" | "reject", "edited": true | false,
    "conflicts_with": "<published dir, or empty>", "notes": "<one concise sentence>"}}"""


def review_one(target=None, fix=True):
    """Independently review ONE learning skill and move it to STAGING (or keep it as learning if
    rejected). Unlike the sweep in evaluate_all(), the reviewer (the strong 'improve' delegate —
    never the local model that wrote it) may EDIT the skill to fix it and checks it doesn't
    duplicate a published skill. It STOPS at staging; publishing stays with the local gate.

    `target` = a skill name or dir; default = the most recently modified learning skill (i.e. the
    one a self-improvement workflow just created). Returns a result dict."""
    from oceano import delegate
    learning = [s for s in all_skills() if s["status"] == "learning"]
    if not learning:
        return {"ok": True, "reviewed": False, "reason": "no learning skill to evaluate"}
    if target and str(target).strip():
        t = str(target).strip()
        sk = next((s for s in learning if s["dir"] == t or s["name"] == t), None)
        if not sk:
            return {"ok": True, "reviewed": False, "reason": f"no learning skill matching {target!r}"}
    else:
        sk = max(learning, key=lambda s: (SKILLS_DIR / s["dir"] / "SKILL.md").stat().st_mtime)

    tools = "Read,Glob,Grep" + (",Edit,Write" if fix else "")
    r = delegate.run(_REVIEW_ONE_PROMPT.format(dir=sk["dir"], catalog=catalog() or "(none published yet)"),
                     cwd=SKILLS_DIR, tools=tools, timeout=900, role="improve")
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "delegate failed", "dir": sk["dir"]}
    m = re.search(r"\{.*\}", r.get("output", ""), re.DOTALL)
    try:
        v = json.loads(m.group(0)) if m else {}
    except ValueError:
        v = {}
    notes = _oneline(str(v.get("notes", "")))[:300]
    conflict = _oneline(str(v.get("conflicts_with", "")))
    edited = bool(v.get("edited"))
    approve = str(v.get("verdict", "")).lower() == "approve" and not conflict
    # Force the status ourselves (preserving any body/description edits the delegate made) — the
    # reviewer can fix content but must never publish itself; the workflow path stops at staging.
    if approve:
        set_status(sk["dir"], "staged", "✓ reviewed" + (" + edited" if edited else "") + ": " + (notes or "approved"))
        result = "staged"
    else:
        why = f"conflicts with {conflict}" if conflict else (notes or "needs work")
        set_status(sk["dir"], "learning", "✗ rejected: " + why)
        result = "rejected"
    return {"ok": True, "reviewed": True, "dir": sk["dir"], "name": sk["name"],
            "result": result, "edited": edited, "conflicts_with": conflict, "notes": notes}


def ensure_eval_task():
    """Make sure the locked '[ SKILLS ] evaluate' schedule exists (visible in the
    Scheduler, not editable/removable there). Daily at 05:00."""
    from oceano import scheduler
    if any(t.get("source") == EVAL_SOURCE for t in scheduler.all_tasks()):
        return
    scheduler.add_task("0 5 * * *",
                       "[ SKILLS ] Evaluate learning skills (independent review → staging → publish)",
                       source=EVAL_SOURCE)


def distill_recent(max_chats=6, min_msgs=4):
    """Feed the skills pipeline: mine recently-active conversations into LEARNING skills.

    Each conversation is distilled by the strong improve-delegate (from_conversation) — the local
    model never authors its own skills — so this only PROPOSES; the 05:00 skills:eval review then
    independently judges/edits and the local model does the final publish gate. A small state file
    remembers which chats were already mined at their current `updated` stamp, so each run only
    chews on new or changed conversations (and re-mines a chat that gained new turns). Bounded to
    `max_chats` per run to cap delegate calls. Returns a human-readable summary."""
    from oceano import chats, jobs, atomicio
    try:
        seen = json.loads(_DISTILL_STATE.read_text()) if _DISTILL_STATE.exists() else {}
    except (OSError, ValueError):
        seen = {}
    todo = [c for c in chats.list_all()
            if (c.get("count") or 0) >= min_msgs and seen.get(c["id"]) != c.get("updated")][:max_chats]
    if not todo:
        return "no new conversations to distill"
    saved = nothing = failed = 0
    names = []
    with jobs.job("skills", "skill distillation", ref=DISTILL_SOURCE):
        for c in todo:
            tr = chats.transcript(c["id"])
            if not tr.strip():
                seen[c["id"]] = c.get("updated")        # empty/unreadable → don't revisit
                continue
            r = from_conversation(tr)
            if not r.get("ok"):
                failed += 1
                continue                                # delegate down → leave unseen, retry next run
            if r.get("saved"):
                saved += 1
                names.append(r.get("name"))
            else:
                nothing += 1
            seen[c["id"]] = c.get("updated")            # mined at this stamp → skip until it changes
    try:
        atomicio.write_text(_DISTILL_STATE, json.dumps(seen))
    except OSError:
        pass
    parts = [f"{len(todo)} conversation(s) examined"]
    if saved:
        parts.append(f"{saved} new learning skill(s): " + ", ".join(n for n in names if n))
    if nothing:
        parts.append(f"{nothing} had nothing reusable")
    if failed:
        parts.append(f"{failed} failed (delegate unavailable)")
    return "; ".join(parts)


def ensure_distill_task():
    """Make sure the locked '[ SKILLS ] distill' feeder schedule exists (visible in the Scheduler,
    not editable/removable there). Daily at 03:00 — ahead of the 05:00 review, so a skill distilled
    overnight is reviewed and (maybe) published the same night."""
    from oceano import scheduler
    if any(t.get("source") == DISTILL_SOURCE for t in scheduler.all_tasks()):
        return
    scheduler.add_task("0 3 * * *",
                       "[ SKILLS ] Distill reusable skills from recent conversations (→ learning)",
                       source=DISTILL_SOURCE)
