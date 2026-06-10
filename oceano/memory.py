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
