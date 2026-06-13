"""RAG over the user's own documents: index a folder, search it by meaning.
Reuses the shared embedding server (:8082) + SQLite — same machinery as memory."""
import hashlib
import json
import sqlite3
from pathlib import Path

import config
from oceano import embeddings

DB_PATH = config.WORKSPACE.parent / "data" / "rag.db"
CHUNK_WORDS = 250
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".rst"}
RESEARCH_DIR = config.WORKSPACE / "research"     # where the Researcher writes its living docs


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")    # wait (don't error) when another writer holds the db
    con.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer: web+telegram+scheduler+calendar
    con.execute("CREATE TABLE IF NOT EXISTS chunks ("
                "id INTEGER PRIMARY KEY, path TEXT, chunk TEXT, embedding TEXT)")
    # per-file content hash so re-indexing skips unchanged docs (incremental)
    con.execute("CREATE TABLE IF NOT EXISTS docmeta (path TEXT PRIMARY KEY, hash TEXT)")
    return con


def _read(path: Path):
    if path.suffix.lower() in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join(pg.extract_text() or "" for pg in PdfReader(str(path)).pages)
        except Exception:
            return ""
    return ""


def _chunks(text):
    words = text.split()
    for i in range(0, len(words), CHUNK_WORDS):
        yield " ".join(words[i:i + CHUNK_WORDS])


def index_docs(folder, only=None):
    """Embed readable files under `folder` (recursively) into the RAG store.

    Incremental: a file whose content hash matches its last index is skipped, so a
    re-run only re-embeds what actually changed. Files that vanished from disk (under
    `folder`) are pruned. Pass `only=<path>` to index a single file (e.g. the one doc
    a Researcher run just rewrote) without walking the whole tree."""
    base = Path(folder).expanduser()
    if not base.is_absolute():
        base = config.WORKSPACE / folder
    base = base.resolve()
    if not base.exists():
        return f"(no such folder: {folder})"

    if only is not None:
        op = Path(only).expanduser()
        if not op.is_absolute():
            op = config.WORKSPACE / only
        paths = [op.resolve()]
        prune = False                     # single-file mode never prunes siblings
    else:
        paths = [p for p in base.rglob("*") if p.is_file()]
        prune = True

    con = _db()
    seen = set()
    n_files = n_chunks = skipped = 0
    for path in paths:
        if not path.is_file():
            continue
        text = _read(path)
        if not text.strip():
            continue
        sp = str(path)
        seen.add(sp)
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        row = con.execute("SELECT hash FROM docmeta WHERE path=?", (sp,)).fetchone()
        if row and row[0] == h:
            skipped += 1
            continue                      # unchanged since last index — keep its chunks
        con.execute("DELETE FROM chunks WHERE path=?", (sp,))  # re-index cleanly
        added = False
        for ch in _chunks(text):
            vec = embeddings.embed(ch)
            if not vec:
                con.close()               # uncommitted → only THIS file's DELETE rolls back;
                                          # files committed earlier this run stay indexed, and a
                                          # retry skips them (hash match) and resumes here
                return "ERROR: embed server down — start scripts/serve-embeddings.sh"
            con.execute("INSERT INTO chunks (path, chunk, embedding) VALUES (?,?,?)",
                        (sp, ch, json.dumps(vec)))
            n_chunks += 1
            added = True
        con.execute("INSERT OR REPLACE INTO docmeta (path, hash) VALUES (?,?)", (sp, h))
        con.commit()                      # per file: a later file's embed failure can't undo this one
        n_files += added
    if prune:                             # drop chunks for files removed from disk
        bp = str(base)
        for (sp,) in con.execute("SELECT path FROM docmeta").fetchall():
            if sp.startswith(bp) and sp not in seen and not Path(sp).is_file():
                con.execute("DELETE FROM chunks WHERE path=?", (sp,))
                con.execute("DELETE FROM docmeta WHERE path=?", (sp,))
    con.commit()
    con.close()
    return f"indexed {n_chunks} chunks from {n_files} changed file(s); {skipped} unchanged"


def search_docs(query, k=4):
    """Return the k most relevant document chunks for a question."""
    con = _db()
    rows = con.execute("SELECT path, chunk, embedding FROM chunks").fetchall()
    con.close()
    if not rows:
        return "(no documents indexed yet — run index_docs first)"
    qvec = embeddings.embed(query)
    if not qvec:
        return "ERROR: embed server down"
    scored = [(embeddings.cosine(qvec, json.loads(emb)), path, chunk)
              for path, chunk, emb in rows]
    scored.sort(reverse=True)
    return "\n\n".join(f"[{Path(p).name}]\n{c}" for _, p, c in scored[:k])


def wipe():
    """Delete ALL indexed document chunks (Settings → Wipe). Returns count removed."""
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.execute("DELETE FROM chunks")
    con.execute("DELETE FROM docmeta")   # clear hashes too, else a re-index skips wiped files
    con.commit()
    con.close()
    return n


def research_context(query, k=3, threshold=0.55):
    """Top research-doc chunks relevant to `query`, for auto-injection into the agent's
    context (like memory). Scoped to the Researcher's living docs under research/ only —
    user-indexed documents stay on-demand via search_docs. Returns [(score, topic, chunk)]
    above `threshold`, best first; [] if nothing clears the bar or the embed server is down."""
    prefix = str(RESEARCH_DIR.resolve())
    con = _db()
    rows = con.execute("SELECT path, chunk, embedding FROM chunks WHERE path LIKE ?",
                       (prefix + "%",)).fetchall()
    con.close()
    if not rows:
        return []
    qvec = embeddings.embed(query)
    if not qvec:
        return []
    scored = [(embeddings.cosine(qvec, json.loads(emb)), path, chunk)
              for path, chunk, emb in rows if emb]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(round(s, 3), Path(p).stem, c) for s, p, c in scored[:k] if s >= threshold]


def stats():
    """{files, chunks, dims} for the Brain knowledge panel. dims is read from a
    stored embedding (free — no call to the embed server)."""
    con = _db()
    files = con.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    row = con.execute("SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone()
    con.close()
    return {"files": files, "chunks": chunks, "dims": len(json.loads(row[0])) if row else None}


def search(query, k=6):
    """Structured semantic search for the UI: [{name, path, chunk, score}], best first."""
    con = _db()
    rows = con.execute("SELECT path, chunk, embedding FROM chunks").fetchall()
    con.close()
    if not rows:
        return []
    qvec = embeddings.embed(query)
    if not qvec:
        return []
    scored = [(embeddings.cosine(qvec, json.loads(emb)), path, chunk)
              for path, chunk, emb in rows if emb]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"name": Path(p).name, "path": p, "chunk": c, "score": round(s, 3)}
            for s, p, c in scored[:k]]


def prune_orphans():
    """Drop every chunk whose source file no longer exists on disk — iterating the CHUNKS
    table directly, so it also catches chunks with no docmeta row (a pre-docmeta DB or a
    partially-written index), not just docmeta-tracked files. Stale docmeta rows are cleared
    too. Returns the number of source files pruned. Cheap: one stat per distinct path."""
    con = _db()
    paths = {r[0] for r in con.execute("SELECT DISTINCT path FROM chunks").fetchall()}
    paths |= {r[0] for r in con.execute("SELECT path FROM docmeta").fetchall()}
    pruned = 0
    for p in paths:
        if not Path(p).is_file():
            con.execute("DELETE FROM chunks WHERE path=?", (p,))
            con.execute("DELETE FROM docmeta WHERE path=?", (p,))
            pruned += 1
    con.commit()
    con.close()
    return pruned


def reindex():
    """Re-sync the doc index to disk: prune chunks for files that no longer exist (incl.
    orphan chunks with no docmeta row, via prune_orphans), then re-embed files whose content
    changed since they were indexed. (Brand-new files are added via index_docs.) Returns a
    short summary. Only what's present is kept."""
    pruned = prune_orphans()                         # chunks-driven sweep (catches the docmeta-empty case)
    con = _db()
    rows = con.execute("SELECT path, hash FROM docmeta").fetchall()
    present = refreshed = 0
    for path, oldh in rows:
        p = Path(path)
        if not p.is_file():                          # already handled by prune_orphans(); skip defensively
            continue
        present += 1
        text = _read(p)
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        if h == oldh:
            continue                                 # unchanged
        con.execute("DELETE FROM chunks WHERE path=?", (path,))
        ok = True
        for ch in _chunks(text):
            vec = embeddings.embed(ch)
            if not vec:                              # embed server down → leave this file for next run
                ok = False
                break
            con.execute("INSERT INTO chunks (path, chunk, embedding) VALUES (?,?,?)", (path, ch, json.dumps(vec)))
        if ok:
            con.execute("INSERT OR REPLACE INTO docmeta (path, hash) VALUES (?,?)", (path, h))
            refreshed += 1
        con.commit()
    con.close()
    return f"{present} present, {refreshed} refreshed, {pruned} pruned"
