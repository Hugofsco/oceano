"""The self-improvement loop: reflection emits structured proposals, they queue as
approvable suggestions, and accepting one creates the real artifact."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import reflect, suggestions  # noqa: E402 - after the sys.path bootstrap


def test_extract_proposals_pulls_and_strips_the_json_block():
    text = (
        "**What happened** — a quiet day.\n\n"
        "**Next steps** — investigate X.\n\n"
        '```json\n{"proposals": [{"kind": "research", "title": "study X", "detail": "focus on Y"}]}\n```'
    )
    clean, props = reflect._extract_proposals(text)
    assert "```json" not in clean and "study X" not in clean       # block peeled off the journaled prose
    assert props == [{"kind": "research", "title": "study X", "detail": "focus on Y"}]


def test_extract_proposals_tolerates_no_block():
    clean, props = reflect._extract_proposals("just prose, no proposals block")
    assert props == []
    assert clean == "just prose, no proposals block"


def test_add_lists_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(suggestions, "DB_PATH", tmp_path / "s.db")
    a = suggestions.add("research", "study X", "detail")
    b = suggestions.add("research", "study X", "detail")           # identical pending → same row
    assert a == b
    pending = suggestions.all_suggestions()
    assert len(pending) == 1 and pending[0]["kind"] == "research"


def test_accept_memory_creates_and_marks_done(tmp_path, monkeypatch):
    monkeypatch.setattr(suggestions, "DB_PATH", tmp_path / "s.db")
    from oceano import memory
    captured = {}
    monkeypatch.setattr(memory, "remember", lambda text, **k: captured.update(text=text) or "ok")
    sid = suggestions.add("memory", "the sky is blue", "the sky is blue, file it")
    r = suggestions.accept(sid)
    assert r["ok"] and r["action"] == "memory"
    assert "blue" in captured.get("text", "")
    assert suggestions.get(sid)["status"] == "done"


def test_accept_research_routes_to_add_topic(tmp_path, monkeypatch):
    monkeypatch.setattr(suggestions, "DB_PATH", tmp_path / "s.db")
    from oceano import researcher
    monkeypatch.setattr(researcher, "add_topic", lambda title, focus="": 42)
    sid = suggestions.add("research", "study quantum widgets", "focus on cost")
    r = suggestions.accept(sid)
    assert r["ok"] and "42" in r["result"]
    assert suggestions.get(sid)["status"] == "done"


def test_accept_unknown_and_dismiss(tmp_path, monkeypatch):
    monkeypatch.setattr(suggestions, "DB_PATH", tmp_path / "s.db")
    assert suggestions.accept(999)["ok"] is False
    sid = suggestions.add("other", "ponder something")
    assert suggestions.dismiss(sid)["ok"]
    assert suggestions.get(sid)["status"] == "dismissed"
    assert suggestions.accept(sid)["ok"] is False                  # can't accept a dismissed one
