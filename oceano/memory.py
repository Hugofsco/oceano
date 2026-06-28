"""Persistent long-term memory for the agent.

Storage: SQLite (durable, local-first). Recall is semantic (cosine over the
shared embedding server) when it's up, and falls back to keyword matching when
it isn't — so memory always works.
"""
import json
import sqlite3
from datetime import datetime, timezone

import config
from oceano import embeddings, atomicio

DB_PATH = config.WORKSPACE.parent / "data" / "memory.db"
POLICY_PATH = config.WORKSPACE.parent / "data" / "memory_policy.json"

_EMBED_CLIP = 5000            # backstop: keep 'search_document: '+text within the embed server's
                              # 2048-token batch (see scripts/serve-embeddings.sh) so a very long
                              # memory still embeds (truncated) instead of silently failing.


def _embed(text, kind="document"):
    """Embed a memory's text (as a document) or a query, clipped to a safe length and None-safe.
    `kind` is 'document' (stored, the default) or 'query' — picks the nomic prefix."""
    return embeddings.embed((text or "")[:_EMBED_CLIP], kind)


_cosine = embeddings.cosine

# Memory types + how each is injected into the agent's context:
#   always   = inject every turn, regardless of the prompt (identity-type facts)
#   relevant = inject only when semantically related to the prompt (default)
#   off      = never inject
# Pinned memories are ALWAYS injected, whatever their category's policy says.
# 'identity' is the agent's OWN sense of self — who I am, my continuity, my
# responsibilities, and the core facts about my user and our relationship. It is written
# in the FIRST PERSON ("I…" / "my user…"), injected every turn, and read by the agent as
# itself — so a fact about the human is phrased "my user", never a bare "User does X"
# (which the agent would misread as something IT does).
# 'knowledge' is the agent's OWN learned facts (not about the user) — e.g. things it
# worked out from research or reading. These usually carry a `source` (the URL/file they
# came from) so the agent can reopen it to dig deeper. Injected 'relevant' so they surface
# only when on-topic — never as always-on truth.
CATEGORIES = ["identity", "preference", "project", "fact", "task", "knowledge"]
_DEFAULT_POLICY = {"identity": "always", "preference": "always",
                   "project": "relevant", "fact": "relevant", "task": "relevant",
                   "knowledge": "relevant"}


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")    # wait (don't error) when another writer holds the db
    con.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer: web+telegram+scheduler+calendar
    con.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "id INTEGER PRIMARY KEY, ts TEXT, text TEXT, tags TEXT, embedding TEXT, "
        "category TEXT DEFAULT 'fact', pinned INTEGER DEFAULT 0, source TEXT)"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(memories)").fetchall()}
    if "category" not in cols:                       # migrate an older DB in place
        con.execute("ALTER TABLE memories ADD COLUMN category TEXT")
        con.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER DEFAULT 0")
        # light inference from existing free-text tags so the feature works on day one
        con.execute("UPDATE memories SET category='preference' WHERE category IS NULL AND lower(tags) LIKE '%pref%'")
        con.execute("UPDATE memories SET category='identity'   WHERE category IS NULL AND (lower(tags) LIKE '%ident%' OR lower(tags) LIKE '%name%')")
        con.execute("UPDATE memories SET category='project'    WHERE category IS NULL AND (lower(tags) LIKE '%project%' OR lower(tags) LIKE '%goal%')")
        con.execute("UPDATE memories SET category='fact'       WHERE category IS NULL")
        con.commit()
    if "source" not in cols:                         # 'knowledge' memories point back to a URL/file
        con.execute("ALTER TABLE memories ADD COLUMN source TEXT")
        con.commit()
    return con


def _norm_cat(category):
    return category if category in CATEGORIES else "fact"


def remember(text, tags="", category="fact", pinned=False, source=""):
    """Store a durable fact/preference/note the agent should keep. `source` is an optional
    URL or workspace file path the fact came from — mainly for 'knowledge' memories, so the
    agent can reopen it later (fetch_url / read_file) to investigate further."""
    vec = _embed(text)
    con = _db()
    con.execute(
        "INSERT INTO memories (ts, text, tags, embedding, category, pinned, source) VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), text, tags,
         json.dumps(vec) if vec else None, _norm_cat(category), 1 if pinned else 0, (source or "").strip()),
    )
    con.commit()
    con.close()
    return f"remembered ({'semantic' if vec else 'keyword'}): {text!r}"


# ---------------- injection policy (configurable in Settings) ----------------
def get_policy():
    try:
        p = json.loads(POLICY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        p = {}
    return {c: (p.get(c) if p.get(c) in ("always", "relevant", "off") else _DEFAULT_POLICY[c])
            for c in CATEGORIES}


def set_policy(policy):
    full = {c: (policy.get(c) if (policy or {}).get(c) in ("always", "relevant", "off")
                else _DEFAULT_POLICY[c]) for c in CATEGORIES}
    atomicio.write_text(POLICY_PATH, json.dumps(full, indent=2))
    return full


def set_pinned(mid, pinned):
    con = _db(); con.execute("UPDATE memories SET pinned=? WHERE id=?", (1 if pinned else 0, mid))
    con.commit(); con.close(); return True


def set_category(mid, category):
    con = _db(); con.execute("UPDATE memories SET category=? WHERE id=?", (_norm_cat(category), mid))
    con.commit(); con.close(); return True


def for_prompt(query, k=5, max_always=20, threshold=0.28):
    """The memories to inject this turn, applying the policy: all pinned, all from
    'always' categories, plus the top semantically-relevant from 'relevant' ones.
    Returns [{id, text, tags, category, pinned, source, ts}], deduped. `ts` lets the
    caller show each memory's age so the model can spot ones that may have gone stale."""
    policy = get_policy()
    con = _db()
    rows = con.execute("SELECT id, text, tags, category, pinned, embedding, source, ts FROM memories").fetchall()
    con.close()
    if not rows:
        return []
    chosen = {}

    def take(r):
        chosen[r[0]] = {"id": r[0], "text": r[1], "tags": r[2], "category": r[3] or "fact",
                        "pinned": bool(r[4]), "source": r[6] or "", "ts": r[7]}

    for r in rows:                                   # 1. pinned — always
        if r[4]:
            take(r)
    always = {c for c, p in policy.items() if p == "always"}
    n = 0
    for r in rows:                                   # 2. whole 'always' categories
        if r[0] not in chosen and (r[3] or "fact") in always and n < max_always:
            take(r); n += 1
    relevant = {c for c, p in policy.items() if p == "relevant"}
    pool = [r for r in rows if r[0] not in chosen and (r[3] or "fact") in relevant]
    if pool:                                         # 3. semantic top-k from 'relevant'
        qv = _embed(query, "query")
        if qv:
            scored = []
            for r in pool:                           # skip rows with a missing/corrupt vector
                v = embeddings.loads_vec(r[5])
                if v:
                    scored.append((_cosine(qv, v), r))
        else:
            words = set(query.lower().split())
            scored = [(float(sum(w in r[1].lower() for w in words)), r) for r in pool]
        scored.sort(key=lambda x: x[0], reverse=True)
        for s, r in [sr for sr in scored if sr[0] >= threshold][:k]:   # threshold FIRST, then top-k
            take(r)
    return list(chosen.values())


def reindex(force=False):
    """Backfill embeddings for memories stored before the embed server existed. Safe to run
    repeatedly — only touches rows still missing an embedding, unless force=True re-embeds EVERY
    row (used after an embedding model/convention change — see reindex.rebuild_embeddings())."""
    con = _db()
    q = "SELECT id, text FROM memories" + ("" if force else " WHERE embedding IS NULL")
    rows = con.execute(q).fetchall()
    done = 0
    for mid, text in rows:
        vec = _embed(text)
        if vec:
            con.execute("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(vec), mid))
            done += 1
    con.commit()
    con.close()
    return f"reindexed {done}/{len(rows)} memories"


def recall(query, k=5):
    """Return the k most relevant stored memories for a query."""
    con = _db()
    rows = con.execute("SELECT text, tags, embedding, source FROM memories").fetchall()
    con.close()
    if not rows:
        return "(no memories yet)"

    qvec = _embed(query)
    if qvec:  # semantic path
        scored = []
        for text, tags, emb, src in rows:
            v = embeddings.loads_vec(emb)             # skip a missing/corrupt embedding row
            if v:
                scored.append((_cosine(qvec, v), text, tags, src))
        scored.sort(reverse=True)
        top = scored[:k]
    else:     # keyword fallback
        words = set(query.lower().split())
        scored = [(sum(w in text.lower() for w in words), text, tags, src)
                  for text, tags, _, src in rows]
        scored.sort(reverse=True)
        top = [s for s in scored[:k] if s[0] > 0] or scored[:k]

    return "\n".join(f"- {text}" + (f"  [{tags}]" if tags else "")
                     + (f"  ↪ source: {src}" if src else "") for _, text, tags, src in top)


def list_all(limit=300):
    """All stored memories, newest first (for the UI)."""
    con = _db()
    rows = con.execute("SELECT id, ts, text, tags, category, pinned, source FROM memories "
                       "ORDER BY pinned DESC, id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "text": r[2], "tags": r[3],
             "category": r[4] or "fact", "pinned": bool(r[5]), "source": r[6] or ""} for r in rows]


def forget(mid):
    con = _db()
    con.execute("DELETE FROM memories WHERE id=?", (mid,))
    con.commit()
    con.close()
    return True


def wipe():
    """Delete ALL memories (Settings → Wipe). Returns the number removed."""
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    con.execute("DELETE FROM memories")
    con.commit()
    con.close()
    return n


def count():
    """Number of stored memories (for the Brain stats panel)."""
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    con.close()
    return n


def search(query, k=8):
    """Structured semantic search for the UI: [{id, ts, text, tags, score}], best first.
    Semantic when the embed server is up; keyword fallback otherwise."""
    con = _db()
    rows = con.execute("SELECT id, ts, text, tags, category, pinned, embedding FROM memories").fetchall()
    con.close()
    if not rows:
        return []
    qvec = _embed(query)
    if qvec:
        scored = []
        for r in rows:                               # corrupt/missing vector → -1.0 (sorts last)
            v = embeddings.loads_vec(r[6])
            scored.append((_cosine(qvec, v) if v else -1.0, r))
    else:
        words = set(query.lower().split())
        scored = [(float(sum(w in r[2].lower() for w in words)), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"id": r[0], "ts": r[1], "text": r[2], "tags": r[3], "category": r[4] or "fact",
             "pinned": bool(r[5]), "score": round(max(s, 0.0), 3)} for s, r in scored[:k]]


def _split_tags(tags):
    """Free-text tag string -> a set of normalized tag tokens (comma/space separated)."""
    if not tags:
        return set()
    raw = tags.replace(",", " ").split()
    return {t.strip().lower() for t in raw if t.strip()}


def graph(threshold=0.62, max_nodes=140, max_edges=500):
    """The memory store as a graph, for the Memory Graph window. Nodes are memories
    (colored by category in the UI); edges connect memories that are either strongly
    semantically similar (cosine >= threshold, using the stored embeddings) or share a
    tag. Returns {nodes, edges, threshold}. Edges are capped, strongest first."""
    con = _db()
    rows = con.execute("SELECT id, text, tags, category, pinned, embedding FROM memories "
                       "ORDER BY pinned DESC, id DESC LIMIT ?", (max_nodes,)).fetchall()
    con.close()
    nodes, vecs, tagmap = [], {}, {}
    for r in rows:
        mid = r[0]
        nodes.append({"id": mid, "text": r[1], "tags": r[2] or "",
                      "category": r[3] or "fact", "pinned": bool(r[4])})
        if r[5]:
            try:
                vecs[mid] = json.loads(r[5])
            except ValueError:
                pass
        for t in _split_tags(r[2]):
            tagmap.setdefault(t, []).append(mid)

    ids = [n["id"] for n in nodes]
    edges, seen = [], set()
    for i in range(len(ids)):                         # semantic edges (strongest signal)
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if a in vecs and b in vecs:
                s = _cosine(vecs[a], vecs[b])
                if s >= threshold:
                    edges.append({"a": a, "b": b, "w": round(s, 3), "kind": "semantic"})
                    seen.add((a, b))
    # Tag edges, but ONLY for *rare* tags. A tag shared by many memories is a hub: it
    # links everything to everything (O(n^2) edges) and says nothing — skip it, or the
    # graph becomes a hairball. Co-tagging is only a meaningful link when the tag is rare.
    tag_cap = max(3, int(len(nodes) * 0.25))
    for tag, members in tagmap.items():
        if len(members) > tag_cap:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = sorted((members[i], members[j]))
                if a != b and (a, b) not in seen:
                    edges.append({"a": a, "b": b, "w": 0.5, "kind": "tag", "tag": tag})
                    seen.add((a, b))
    edges.sort(key=lambda e: e["w"], reverse=True)
    return {"nodes": nodes, "edges": edges[:max_edges],
            "threshold": threshold, "categories": CATEGORIES}


def add_if_new(text, tags="", category="fact", threshold=0.86, source=""):
    """Save a memory only if it isn't a near-duplicate of an existing one (semantic
    when the embed server is up, exact-text otherwise). Used by auto-learning so
    repeated facts don't pile up. Returns True if saved."""
    text = (text or "").strip()
    if not text:
        return False
    vec = _embed(text)
    con = _db()
    rows = con.execute("SELECT text, embedding FROM memories").fetchall()
    if vec:
        for t, emb in rows:
            v = embeddings.loads_vec(emb)             # ignore a corrupt row rather than crash
            if v and _cosine(vec, v) >= threshold:
                con.close(); return False
    else:
        low = text.lower()
        if any(t.strip().lower() == low for t, _ in rows):
            con.close(); return False
    con.execute("INSERT INTO memories (ts, text, tags, embedding, category, pinned, source) VALUES (?,?,?,?,?,0,?)",
                (datetime.now(timezone.utc).isoformat(), text, tags,
                 json.dumps(vec) if vec else None, _norm_cat(category), (source or "").strip()))
    con.commit(); con.close()
    return True


def best_match(query):
    """The single closest memory to `query`: {id, text, score}, or None if empty."""
    con = _db()
    rows = con.execute("SELECT id, text, embedding FROM memories").fetchall()
    con.close()
    if not rows:
        return None
    qv = _embed(query, "query")
    if qv:
        scored = []
        for i, t, e in rows:                          # corrupt/missing vector → -1.0 (sorts last)
            v = embeddings.loads_vec(e)
            scored.append((_cosine(qv, v) if v else -1.0, i, t))
    else:
        ql = set(query.lower().split())
        scored = [(float(sum(w in t.lower() for w in ql)), i, t) for i, t, e in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    s, i, t = scored[0]
    return {"id": i, "text": t, "score": round(max(s, 0.0), 3)}


def update(mid, text):
    """Replace a memory's text (re-embeds it). For agent self-correction."""
    vec = _embed(text)
    con = _db()
    con.execute("UPDATE memories SET text=?, embedding=? WHERE id=?",
                (text, json.dumps(vec) if vec else None, mid))
    con.commit(); con.close()
    return True


# ============================ scheduled maintenance (locked, independently reviewed) ============================
# A locked scheduler job that delegates memory hygiene to the configured 'improve' delegate
# (Settings → Delegation; Claude Code by default — no API key needed — or a cloud model),
# mirroring the locked eval + skills-evaluation jobs. The local model never judges its own
# memory; the delegate reviews the whole store and returns a plan, which we apply.
MAINT_SOURCE = "memory:maintain"
MAINT_PREFIX = "[ MEMORY ] "

_MAINT_PROMPT = """You are doing maintenance on an AI agent's long-term memory about its user.
Below are the {count} stored memories, one per line as:  #<id> [<category>]<📌 if pinned>: <text>

YOUR JOB: keep the store clean, non-redundant, and accurate. Look for:
- exact or near duplicates (keep ONE; delete the rest);
- facts fully subsumed by a more complete memory (delete the weaker one);
- stale facts contradicted by a newer memory (delete the outdated one);
- memories filed under the wrong category (categories: identity, preference, project, fact, task, knowledge).

'knowledge' = facts the agent learned for itself (from research/reading), not facts about the user;
they often cite a source — keep distinct knowledge entries even if their topics are related.

'identity' is the agent's OWN sense of self, written in the FIRST PERSON ("I…"). An identity
memory may mention the human, but always as "my user" — rewrite any bare "User does X"
in an identity memory into that voice (e.g. "User is a trader" -> "My user is a trader"), since the agent
reads its identity block as itself and would otherwise take "User does X" to mean it does X.

RULES:
- NEVER delete a memory marked 📌 pinned — the user chose to keep it. You MAY rewrite a pinned one for clarity.
- Be conservative: when unsure, leave it. Don't merge distinct facts into one vague memory.
- To merge duplicates, rewrite the survivor (in "update") to be complete, then "delete" the others.

Output ONLY a JSON object, nothing else:
{{"delete": [<ids>],
  "update": [{{"id": <id>, "text": "<rewritten text>"}}],
  "recategorize": [{{"id": <id>, "category": "<one of the categories>"}}],
  "notes": "<one line: what you changed and why>"}}

MEMORIES:
{listing}"""


def _parse_plan(output):
    import re
    m = re.search(r"\{.*\}", output or "", re.DOTALL)
    if not m:
        return None
    try:
        plan = json.loads(m.group(0))
    except ValueError:
        return None
    return plan if isinstance(plan, dict) else None


def maintain():
    """Memory-hygiene run, registered as a background job so the UI can show it running."""
    from oceano import jobs
    with jobs.job("memory", "memory maintenance", ref="memory:maintain"):
        return _maintain()


def _maintain():
    """One maintenance run: hand the whole memory store to the configured delegate, apply its
    plan. Pinned memories are never deleted; a plan that would wipe most of the store is refused
    as a safety net. Returns a short summary. Called by the scheduler's locked job."""
    from oceano import delegate
    items = list_all(limit=1000)
    if not items:
        return "memory is empty — nothing to maintain"
    listing = "\n".join(
        f'#{m["id"]} [{m["category"]}]{" 📌" if m["pinned"] else ""}: {m["text"]}' for m in items)
    prompt = _MAINT_PROMPT.format(count=len(items), listing=listing[:14000])
    # role="improve": memory maintenance is a self-improvement job with its own configurable delegate.
    r = delegate.run(prompt, cwd=config.WORKSPACE, tools="Read", timeout=600, role="improve")
    if not r["ok"]:
        return f"maintenance skipped — delegate unavailable: {r['error']}"
    plan = _parse_plan(r["output"])
    if plan is None:
        return "maintenance skipped — no parsable plan from the reviewer"

    ids = {m["id"] for m in items}
    pinned = {m["id"] for m in items if m["pinned"]}
    to_delete = [i for i in plan.get("delete", []) if isinstance(i, int) and i in ids and i not in pinned]
    if len(to_delete) > max(2, len(items) // 2):        # safety net: never let one run gut the store
        return (f"maintenance aborted — reviewer proposed deleting {len(to_delete)}/{len(items)} "
                "memories (over half); skipped to be safe")

    deleted = edited = recat = 0
    for mid in to_delete:
        forget(mid); deleted += 1
    for upd in plan.get("update", []) if isinstance(plan.get("update"), list) else []:
        mid = upd.get("id") if isinstance(upd, dict) else None
        if isinstance(mid, int) and mid in ids and upd.get("text"):
            update(mid, str(upd["text"])); edited += 1
    for rc in plan.get("recategorize", []) if isinstance(plan.get("recategorize"), list) else []:
        mid = rc.get("id") if isinstance(rc, dict) else None
        if isinstance(mid, int) and mid in ids and rc.get("category"):
            set_category(mid, str(rc["category"])); recat += 1
    note = str(plan.get("notes", "")).strip()
    return (f"reviewed {len(items)} memories · removed {deleted} · rewrote {edited} · "
            f"recategorized {recat}" + (f" — {note}" if note else ""))


def ensure_maintenance_task():
    """Make sure the locked '[ MEMORY ]' schedule exists (visible + retimable + toggleable
    in the Scheduler, but not deletable). Weekly, Mondays 06:00."""
    from oceano import scheduler
    label = MAINT_PREFIX + "Evaluate & maintain long-term memory (dedupe, optimize) — independently reviewed via the configured delegate"
    existing = next((t for t in scheduler.all_tasks() if t.get("source") == MAINT_SOURCE), None)
    if existing:
        if existing.get("instruction") != label:      # refresh stale wording on an existing entry
            scheduler.update_task(existing["id"], instruction=label, allow_managed=True)
        return
    scheduler.add_task("0 6 * * 1", label, source=MAINT_SOURCE)
