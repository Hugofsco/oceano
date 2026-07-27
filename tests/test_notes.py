"""oceano.notes: the Kanban board — card fields (title/body/tags), column CRUD, and
migration from the old fixed-3-column {col: [cards]} shape (pre title/body/tags/columns)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import notes  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "STORE", tmp_path / "notes.json")


def test_empty_board_has_the_default_columns():
    b = notes.board()
    assert b["columns"] == ["todo", "doing", "done"]
    assert b["cards"] == {"todo": [], "doing": [], "done": []}


def test_add_defaults_to_the_first_column_and_carries_title_body_tags():
    card = notes.add("write the report", body="due Friday", tags=["work", " urgent "])
    assert card["title"] == "write the report"
    assert card["body"] == "due Friday"
    assert card["tags"] == ["work", "urgent"]
    assert card["ts"] == card["updated"]
    assert notes.board()["cards"]["todo"] == [card]


def test_update_edits_fields_and_bumps_updated_without_touching_ts():
    card = notes.add("draft")
    ok = notes.update(card["id"], title="final draft", tags=["done"])
    assert ok
    got = notes.board()["cards"]["todo"][0]
    assert got["title"] == "final draft"
    assert got["tags"] == ["done"]
    assert got["ts"] == card["ts"]
    assert got["updated"] != card["updated"]


def test_update_can_move_a_card_to_another_column():
    card = notes.add("ship it")
    assert notes.update(card["id"], col="doing")
    b = notes.board()
    assert b["cards"]["todo"] == []
    assert b["cards"]["doing"][0]["id"] == card["id"]


def test_remove_deletes_and_is_idempotent():
    card = notes.add("temp")
    assert notes.remove(card["id"]) is True
    assert notes.remove(card["id"]) is True
    assert notes.board()["cards"]["todo"] == []


def test_tags_are_capped_and_blank_entries_dropped():
    card = notes.add("t", tags=[" ", "a"] + [f"tag{i}" for i in range(20)])
    assert card["tags"][0] == "a"
    assert len(card["tags"]) == notes._MAX_TAGS


def test_migrates_the_old_flat_col_to_cards_list_shape(tmp_path):
    old = {"todo": [{"id": 5, "text": "legacy card", "ts": "2020-01-01T00:00:00+00:00"}],
           "doing": [], "done": []}
    notes.STORE.write_text(json.dumps(old))
    b = notes.board()
    assert b["columns"] == ["todo", "doing", "done"]
    card = b["cards"]["todo"][0]
    assert card["title"] == "legacy card"   # "text" -> "title"
    assert card["body"] == "" and card["tags"] == []
    assert card["updated"] == card["ts"]    # backfilled from ts when absent


def test_add_column_appends_and_rejects_duplicates_and_blanks():
    b = notes.add_column("blocked")
    assert b["columns"] == ["todo", "doing", "done", "blocked"]
    assert notes.add_column("blocked") is None
    assert notes.add_column("  ") is None


def test_add_column_after_a_given_column_inserts_at_that_position():
    b = notes.add_column("triage", after="todo")
    assert b["columns"] == ["todo", "triage", "doing", "done"]


def test_rename_column_keeps_its_cards_under_the_new_key():
    notes.add("a card", col="doing")
    assert notes.rename_column("doing", "in progress")
    b = notes.board()
    assert "doing" not in b["columns"] and "in progress" in b["columns"]
    assert b["cards"]["in progress"][0]["title"] == "a card"


def test_rename_column_refuses_a_name_already_in_use():
    assert notes.rename_column("doing", "done") is False


def test_remove_column_requires_move_to_when_it_still_has_cards():
    notes.add("stuck", col="doing")
    assert notes.remove_column("doing") is False          # no move_to, cards would vanish
    assert notes.remove_column("doing", move_to="done") is True
    b = notes.board()
    assert "doing" not in b["columns"]
    assert b["cards"]["done"][0]["title"] == "stuck"


def test_remove_column_refuses_to_drop_the_last_column():
    notes.remove_column("doing", move_to="done")
    notes.remove_column("todo", move_to="done")
    assert notes.board()["columns"] == ["done"]
    assert notes.remove_column("done") is False


def test_move_column_shifts_order_and_refuses_past_the_edge():
    assert notes.move_column("doing", -1)
    assert notes.board()["columns"] == ["doing", "todo", "done"]
    assert notes.move_column("doing", -1) is False   # already first
