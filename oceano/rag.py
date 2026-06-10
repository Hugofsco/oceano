"""RAG over the user's own documents: index a folder, search it by meaning.
Reuses the shared embedding server (:8082) + SQLite — same machinery as memory."""
import json
import sqlite3
from pathlib import Path

import config
from oceano import embeddings

DB_PATH = config.WORKSPACE.parent / "data" / "rag.db"
CHUNK_WORDS = 250
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".rst"}


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS chunks ("
                "id INTEGER PRIMARY KEY, path TEXT, chunk TEXT, embedding TEXT)")
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


def index_docs(folder):
    """Embed every readable file under `folder` (recursively) into the RAG store."""
    base = Path(folder).expanduser()
    if not base.is_absolute():
        base = config.WORKSPACE / folder
    base = base.resolve()
    if not base.exists():
        return f"(no such folder: {folder})"

    con = _db()
    n_files = n_chunks = 0
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        text = _read(path)
        if not text.strip():
            continue
        con.execute("DELETE FROM chunks WHERE path=?", (str(path),))  # re-index cleanly
        added = False
        for ch in _chunks(text):
            vec = embeddings.embed(ch)
            if not vec:
                con.close()
                return "ERROR: embed server down — start scripts/serve-embeddings.sh"
            con.execute("INSERT INTO chunks (path, chunk, embedding) VALUES (?,?,?)",
                        (str(path), ch, json.dumps(vec)))
            n_chunks += 1
            added = True
        n_files += added
    con.commit()
    con.close()
    return f"indexed {n_chunks} chunks from {n_files} files under {base}"


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
