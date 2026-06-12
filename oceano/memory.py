"""Persistent long-term memory for the agent.

Storage: SQLite (durable, local-first). Recall is semantic (cosine over the
shared embedding server) when it's up, and falls back to keyword matching when
it isn't — so memory always works.
"""
import json
import sqlite3
from datetime import datetime, timezone

import config
from oceano import embeddings

DB_PATH = config.WORKSPACE.parent / "data" / "memory.db"
POLICY_PATH = config.WORKSPACE.parent / "data" / "memory_policy.json"

_embed = embeddings.embed     # shared with RAG — see oceano/embeddings.py
_cosine = embeddings.cosine

# Memory types + how each is injected into the agent's context:
#   always   = inject every turn, regardless of the prompt (identity-type facts)
#   relevant = inject only when semantically related to the prompt (default)
#   off      = never inject
# Pinned memories are ALWAYS injected, whatever their category's policy says.
CATEGORIES = ["identity", "preference", "project", "fact", "task"]
_DEFAULT_POLICY = {"identity": "always", "preference": "always",
                   "project": "relevant", "fact": "relevant", "task": "relevant"}


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "id INTEGER PRIMARY KEY, ts TEXT, text TEXT, tags TEXT, embedding TEXT, "
        "category TEXT DEFAULT 'fact', pinned INTEGER DEFAULT 0)"
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
    return con


def _norm_cat(category):
    return category if category in CATEGORIES else "fact"


def remember(text, tags="", category="fact", pinned=False):
    """Store a durable fact/preference/note the agent should keep."""
    vec = _embed(text)
    con = _db()
    con.execute(
        "INSERT INTO memories (ts, text, tags, embedding, category, pinned) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), text, tags,
         json.dumps(vec) if vec else None, _norm_cat(category), 1 if pinned else 0),
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
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(full, indent=2))
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
    Returns [{id, text, tags, category, pinned}], deduped."""
    policy = get_policy()
    con = _db()
    rows = con.execute("SELECT id, text, tags, category, pinned, embedding FROM memories").fetchall()
    con.close()
    if not rows:
        return []
    chosen = {}

    def take(r):
        chosen[r[0]] = {"id": r[0], "text": r[1], "tags": r[2],
                        "category": r[3] or "fact", "pinned": bool(r[4])}

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
        qv = _embed(query)
        if qv:
            scored = [(_cosine(qv, json.loads(r[5])) if r[5] else -1.0, r) for r in pool]
        else:
            words = set(query.lower().split())
            scored = [(float(sum(w in r[1].lower() for w in words)), r) for r in pool]
        scored.sort(key=lambda x: x[0], reverse=True)
        for s, r in scored[:k]:
            if s >= threshold:
                take(r)
    return list(chosen.values())


def reindex():
    """Backfill embeddings for memories stored before the embed server existed.
    Safe to run repeatedly — only touches rows still missing an embedding."""
    con = _db()
    rows = con.execute("SELECT id, text FROM memories WHERE embedding IS NULL").fetchall()
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
    rows = con.execute("SELECT text, tags, embedding FROM memories").fetchall()
    con.close()
    if not rows:
        return "(no memories yet)"

    qvec = _embed(query)
    if qvec:  # semantic path
        scored = [(_cosine(qvec, json.loads(emb)), text, tags)
                  for text, tags, emb in rows if emb]
        scored.sort(reverse=True)
        top = scored[:k]
    else:     # keyword fallback
        words = set(query.lower().split())
        scored = [(sum(w in text.lower() for w in words), text, tags)
                  for text, tags, _ in rows]
        scored.sort(reverse=True)
        top = [s for s in scored[:k] if s[0] > 0] or scored[:k]

    return "\n".join(f"- {text}" + (f"  [{tags}]" if tags else "") for _, text, tags in top)


def list_all(limit=300):
    """All stored memories, newest first (for the UI)."""
    con = _db()
    rows = con.execute("SELECT id, ts, text, tags, category, pinned FROM memories "
                       "ORDER BY pinned DESC, id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "text": r[2], "tags": r[3],
             "category": r[4] or "fact", "pinned": bool(r[5])} for r in rows]


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
        scored = [(_cosine(qvec, json.loads(r[6])) if r[6] else -1.0, r) for r in rows]
    else:
        words = set(query.lower().split())
        scored = [(float(sum(w in r[2].lower() for w in words)), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"id": r[0], "ts": r[1], "text": r[2], "tags": r[3], "category": r[4] or "fact",
             "pinned": bool(r[5]), "score": round(max(s, 0.0), 3)} for s, r in scored[:k]]


def add_if_new(text, tags="", category="fact", threshold=0.86):
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
            if emb and _cosine(vec, json.loads(emb)) >= threshold:
                con.close(); return False
    else:
        low = text.lower()
        if any(t.strip().lower() == low for t, _ in rows):
            con.close(); return False
    con.execute("INSERT INTO memories (ts, text, tags, embedding, category, pinned) VALUES (?,?,?,?,?,0)",
                (datetime.now(timezone.utc).isoformat(), text, tags,
                 json.dumps(vec) if vec else None, _norm_cat(category)))
    con.commit(); con.close()
    return True


def best_match(query):
    """The single closest memory to `query`: {id, text, score}, or None if empty."""
    con = _db()
    rows = con.execute("SELECT id, text, embedding FROM memories").fetchall()
    con.close()
    if not rows:
        return None
    qv = _embed(query)
    if qv:
        scored = [(_cosine(qv, json.loads(e)) if e else -1.0, i, t) for i, t, e in rows]
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
