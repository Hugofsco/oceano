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


def test_reindex_discovers_new_files_in_indexed_roots(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(rag, "DB_PATH", tmp_path / "rag.db")
    monkeypatch.setattr(config, "CONFINE_TO_WORKSPACE", False)    # tmp_path lives outside the workspace
    from oceano import embeddings
    monkeypatch.setattr(embeddings, "embed", lambda *a, **k: [0.1, 0.2, 0.3])   # fake "up" embed server
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("first document about apples")
    rag.index_docs(str(docs))                                    # indexes a.md, records the root
    (docs / "b.md").write_text("second document about bananas")  # drop a NEW file in afterwards
    summary = rag.reindex()                                      # nightly job → should DISCOVER b.md
    assert "1 new" in summary
    con = rag._db()
    paths = {os.path.basename(p) for (p,) in con.execute("SELECT DISTINCT path FROM chunks")}
    con.close()
    assert {"a.md", "b.md"} <= paths                             # both now indexed
