"""RAG: overlapping chunks, and the keyword fallback that keeps documents searchable
when the embedding server is down (instead of erroring out)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import rag  # noqa: E402 - after the sys.path bootstrap


def test_chunks_overlap_and_cover():
    words = [f"w{i}" for i in range(600)]
    chunks = [c.split() for c in rag._chunks(" ".join(words))]
    assert chunks
    assert all(len(c) <= rag.CHUNK_WORDS for c in chunks)          # no chunk exceeds the window
    assert chunks[0][-rag.CHUNK_OVERLAP:] == chunks[1][:rag.CHUNK_OVERLAP]   # consecutive chunks overlap
    assert set().union(*chunks) == set(words)                     # every word is covered


def test_chunks_short_text_is_one_chunk():
    assert list(rag._chunks("just a few words")) == ["just a few words"]
    assert list(rag._chunks("   ")) == []


def test_search_docs_keyword_fallback_when_embed_down(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "DB_PATH", tmp_path / "rag.db")
    from oceano import embeddings
    monkeypatch.setattr(embeddings, "embed", lambda *a, **k: None)   # simulate embed server down
    con = rag._db()
    con.execute("INSERT INTO chunks (path, chunk, embedding) VALUES (?,?,?)",
                ("notes/a.md", "the capital of france is paris", None))
    con.execute("INSERT INTO chunks (path, chunk, embedding) VALUES (?,?,?)",
                ("notes/b.md", "bananas are a yellow fruit", None))
    con.commit()
    con.close()
    out = rag.search_docs("what is the capital of france", k=1)
    assert "paris" in out.lower()                                 # found by keyword, not an ERROR string
    assert "error" not in out.lower()
