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

_embed = embeddings.embed     # shared with RAG — see oceano/embeddings.py
_cosine = embeddings.cosine


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "id INTEGER PRIMARY KEY, ts TEXT, text TEXT, tags TEXT, embedding TEXT)"
    )
    return con


def remember(text, tags=""):
    """Store a durable fact/preference/note the agent should keep."""
    vec = _embed(text)
    con = _db()
    con.execute(
        "INSERT INTO memories (ts, text, tags, embedding) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), text, tags,
         json.dumps(vec) if vec else None),
    )
    con.commit()
    con.close()
    return f"remembered ({'semantic' if vec else 'keyword'}): {text!r}"


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
    rows = con.execute("SELECT id, ts, text, tags FROM memories ORDER BY id DESC LIMIT ?",
                       (limit,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "text": r[2], "tags": r[3]} for r in rows]


def forget(mid):
    con = _db()
    con.execute("DELETE FROM memories WHERE id=?", (mid,))
    con.commit()
    con.close()
    return True


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
    rows = con.execute("SELECT id, ts, text, tags, embedding FROM memories").fetchall()
    con.close()
    if not rows:
        return []
    qvec = _embed(query)
    if qvec:
        scored = [(_cosine(qvec, json.loads(emb)) if emb else -1.0, rid, ts, text, tags)
                  for rid, ts, text, tags, emb in rows]
    else:
        words = set(query.lower().split())
        scored = [(float(sum(w in text.lower() for w in words)), rid, ts, text, tags)
                  for rid, ts, text, tags, _ in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"id": rid, "ts": ts, "text": text, "tags": tags, "score": round(max(s, 0.0), 3)}
            for s, rid, ts, text, tags in scored[:k]]


def add_if_new(text, tags="", threshold=0.86):
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
    con.execute("INSERT INTO memories (ts, text, tags, embedding) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), text, tags, json.dumps(vec) if vec else None))
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
