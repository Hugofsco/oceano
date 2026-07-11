"""Memory: remember() skips near-identical duplicates (semantic, high bar) so explicit
saves don't pile up copies between the weekly maintenance runs."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import embeddings, memory  # noqa: E402 - after the sys.path bootstrap


def test_remember_rejects_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "mem.db")
    assert "nothing to remember" in memory.remember("   ")


def test_db_file_is_not_world_or_group_readable(tmp_path, monkeypatch):
    db_path = tmp_path / "mem.db"
    monkeypatch.setattr(memory, "DB_PATH", db_path)
    memory._db()
    assert oct(db_path.stat().st_mode)[-3:] == "600"


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


# --- the weekly maintenance run: applies a DELEGATE-authored plan, so parse + apply defensively ---
def _memories(monkeypatch, tmp_path, n=10, pinned=()):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "mem.db")
    monkeypatch.setattr(memory, "_embed", lambda text, kind="document": None)   # keyword mode: always saves
    memory._invalidate()
    for i in range(1, n + 1):
        memory.remember(f"fact number {i}", pinned=(i in pinned))
    return {m["text"]: m["id"] for m in memory.list_all()}


def _plan(monkeypatch, plan, ok=True):
    out = "Here is the plan:\n" + json.dumps(plan) if isinstance(plan, dict) else plan
    monkeypatch.setattr("oceano.delegate.run",
                        lambda prompt, **kw: {"ok": ok, "output": out, "error": "" if ok else out})


def test_parse_plan_rejects_garbage_and_non_objects():
    assert memory._parse_plan("no json here") is None
    assert memory._parse_plan(None) is None
    assert memory._parse_plan('{"delete": [1], "notes": "x"') is None      # truncated
    assert memory._parse_plan("prose then {\"delete\": []} more prose") == {"delete": []}


def test_maintain_refuses_a_plan_that_guts_the_store(monkeypatch, tmp_path):
    ids = _memories(monkeypatch, tmp_path, n=10)
    _plan(monkeypatch, {"delete": list(ids.values())[:8], "notes": "everything is redundant"})
    summary = memory._maintain()
    assert "aborted" in summary and "over half" in summary
    assert memory.count() == 10                                            # nothing was deleted


def test_maintain_never_deletes_pinned_memories(monkeypatch, tmp_path):
    ids = _memories(monkeypatch, tmp_path, n=6, pinned=(1, 2))
    pinned_ids = [ids["fact number 1"], ids["fact number 2"]]
    _plan(monkeypatch, {"delete": pinned_ids + [ids["fact number 3"]], "notes": "cleanup"})
    summary = memory._maintain()
    assert "removed 1" in summary                                          # only the unpinned one
    left = {m["text"] for m in memory.list_all()}
    assert "fact number 1" in left and "fact number 2" in left and "fact number 3" not in left


def test_maintain_ignores_malformed_plan_entries(monkeypatch, tmp_path):
    ids = _memories(monkeypatch, tmp_path, n=4)
    _plan(monkeypatch, {
        "delete": ["not-an-int", 999999, None],                 # wrong type / unknown id
        "update": [{"id": 999999, "text": "x"}, {"text": "no id"}, "garbage",
                   {"id": ids["fact number 1"], "text": "fact number 1 (clarified)"}],
        "recategorize": [{"id": ids["fact number 2"], "category": "project"}, {"id": "x"}],
        "notes": "mixed-quality plan"})
    summary = memory._maintain()
    assert "removed 0" in summary and "rewrote 1" in summary and "recategorized 1" in summary
    assert memory.count() == 4
    texts = {m["id"]: m for m in memory.list_all()}
    assert texts[ids["fact number 1"]]["text"] == "fact number 1 (clarified)"
    assert texts[ids["fact number 2"]]["category"] == "project"


def test_maintain_skips_cleanly_when_the_delegate_is_down(monkeypatch, tmp_path):
    _memories(monkeypatch, tmp_path, n=3)
    _plan(monkeypatch, "usage limit reached", ok=False)
    assert "delegate unavailable" in memory._maintain()
    assert memory.count() == 3
    _plan(monkeypatch, "I think these all look fine!")          # no parsable JSON plan
    assert "no parsable plan" in memory._maintain()
