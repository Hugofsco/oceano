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
