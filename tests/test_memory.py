"""Memory: remember() skips near-identical duplicates (semantic, high bar) so explicit
saves don't pile up copies between the weekly maintenance runs."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import embeddings, memory  # noqa: E402 - after the sys.path bootstrap


def test_remember_rejects_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "mem.db")
    assert "nothing to remember" in memory.remember("   ")


def test_remember_skips_near_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "mem.db")
    # deterministic stand-ins for the embed server: every item embeds the same and
    # cosine reports a perfect match, so the second save is treated as a duplicate.
    monkeypatch.setattr(memory, "_embed", lambda text, kind="document": [1.0, 0.0])
    monkeypatch.setattr(memory, "_cosine", lambda a, b: 1.0)
    monkeypatch.setattr(embeddings, "loads_vec", lambda s: [1.0, 0.0])

    assert "remembered" in memory.remember("the sky is blue")
    assert "already remembered" in memory.remember("the sky is blue, basically")
    assert memory.count() == 1                                   # the duplicate was not stored


def test_remember_keyword_mode_still_saves(tmp_path, monkeypatch):
    # with the embed server down (_embed -> None) we do NOT dedupe — keep the old always-save behaviour
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "mem.db")
    monkeypatch.setattr(memory, "_embed", lambda text, kind="document": None)
    assert "keyword" in memory.remember("a one-off note")
    assert memory.count() == 1


def _kw_embed(text, kind="document"):
    """Deterministic keyword embedding so tests need no embed server: one dim per concept."""
    t = (text or "").lower()
    return [1.0 if "apple" in t else 0.0, 1.0 if "ocean" in t else 0.0, 0.1]


def test_vector_cache_results_and_invalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "mem.db")
    monkeypatch.setattr(memory, "_embed", _kw_embed)         # real loads_vec/cosine; only the embed is faked
    memory._invalidate()                                     # module-global cache — start clean

    memory.remember("apples are red and crisp", category="fact")
    memory.remember("the ocean is deep blue water", category="fact")
    assert memory.count() == 2

    bm = memory.best_match("fresh apples")                   # populates the cache, returns the apple memory
    assert bm and "apple" in bm["text"]
    assert memory._VEC_CACHE, "best_match should have cached a parsed vector"

    mid = bm["id"]
    memory.forget(mid)
    assert mid not in memory._VEC_CACHE                       # forget pops it (so a reused id can't go stale)

    memory.search("ocean")                                   # repopulate from the remaining row
    assert memory._VEC_CACHE
    memory.reindex(force=True)                                # re-embeds → must clear the cache
    assert memory._VEC_CACHE == {}
