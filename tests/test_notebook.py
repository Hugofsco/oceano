"""oceano.notebook: Markdown notes — CRUD, tag/search filtering, and pin-first sort order."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import notebook  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(notebook, "STORE", tmp_path / "notebook.json")


def test_empty_notebook_is_empty():
    assert notebook.list_all() == []
    assert notebook.all_tags() == []


def test_create_round_trips_title_body_tags():
    n = notebook.create("Recipe idea", "flour, water, salt", ["cooking", " draft "])
    assert n["title"] == "Recipe idea"
    assert n["body"] == "flour, water, salt"
    assert n["tags"] == ["cooking", "draft"]
    assert n["pinned"] is False
    assert n["ts"] == n["updated"]
    assert notebook.get(n["id"]) == n


def test_update_edits_fields_and_bumps_updated():
    n = notebook.create("draft")
    ok = notebook.update(n["id"], title="final", body="the real content", tags=["v2"])
    assert ok
    got = notebook.get(n["id"])
    assert got["title"] == "final" and got["body"] == "the real content" and got["tags"] == ["v2"]
    assert got["ts"] == n["ts"]
    assert got["updated"] != n["updated"]


def test_pin_toggle_alone_does_not_bump_updated():
    n = notebook.create("x")
    assert notebook.update(n["id"], pinned=True)
    got = notebook.get(n["id"])
    assert got["pinned"] is True
    assert got["updated"] == n["updated"]


def test_update_unknown_note_returns_false():
    assert notebook.update(999, title="x") is False


def test_remove_deletes_and_is_idempotent():
    n = notebook.create("temp")
    assert notebook.remove(n["id"]) is True
    assert notebook.remove(n["id"]) is True
    assert notebook.get(n["id"]) is None


def test_list_all_sorts_pinned_first_then_most_recently_updated():
    a = notebook.create("a")
    b = notebook.create("b")
    c = notebook.create("c")
    notebook.update(a["id"], title="a")           # bumps a's updated to the newest
    notebook.update(c["id"], pinned=True)          # c is pinned, should lead regardless of recency
    order = [n["id"] for n in notebook.list_all()]
    assert order[0] == c["id"]
    assert order[1:] == [a["id"], b["id"]]


def test_search_matches_title_or_body_case_insensitively():
    notebook.create("Grocery list", "eggs and milk")
    notebook.create("Trip plan", "flights and hotels")
    assert [n["title"] for n in notebook.list_all(q="MILK")] == ["Grocery list"]
    assert [n["title"] for n in notebook.list_all(q="flights")] == ["Trip plan"]
    assert notebook.list_all(q="nonexistent") == []


def test_filter_by_tag_is_an_exact_match():
    notebook.create("a", tags=["work"])
    notebook.create("b", tags=["worker"])
    assert [n["title"] for n in notebook.list_all(tag="work")] == ["a"]


def test_all_tags_is_sorted_and_deduped():
    notebook.create("a", tags=["zeta", "alpha"])
    notebook.create("b", tags=["alpha", "beta"])
    assert notebook.all_tags() == ["alpha", "beta", "zeta"]


def test_tags_are_capped_and_blank_entries_dropped():
    n = notebook.create("t", tags=[" ", "a"] + [f"tag{i}" for i in range(20)])
    assert n["tags"][0] == "a"
    assert len(n["tags"]) == notebook._MAX_TAGS
