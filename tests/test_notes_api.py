"""The Notes/Kanban web surface: /api/notes* must round-trip the notes module's card and
column CRUD, including the new title/body/tags fields and column management routes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import quote

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano import notes  # noqa: E402
from oceano.web import routes_content  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "STORE", tmp_path / "notes.json")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes_content.router)
    return TestClient(app)


def test_get_board_returns_default_columns(client):
    r = client.get("/api/notes")
    assert r.status_code == 200
    assert r.json()["columns"] == ["todo", "doing", "done"]


def test_add_card_with_title_body_tags_round_trips(client):
    r = client.post("/api/notes", json={"title": "write docs", "body": "for the release", "tags": ["docs"]})
    assert r.status_code == 200 and r.json()["ok"]
    card = r.json()["card"]
    assert card["title"] == "write docs" and card["body"] == "for the release" and card["tags"] == ["docs"]
    assert client.get("/api/notes").json()["cards"]["todo"][0]["id"] == card["id"]


def test_patch_card_edits_and_moves(client):
    card = client.post("/api/notes", json={"title": "x"}).json()["card"]
    r = client.patch(f"/api/notes/{card['id']}", json={"title": "y", "col": "doing"})
    assert r.status_code == 200 and r.json()["ok"]
    b = client.get("/api/notes").json()
    assert b["cards"]["todo"] == []
    assert b["cards"]["doing"][0]["title"] == "y"


def test_patch_unknown_card_404s(client):
    assert client.patch("/api/notes/999", json={"title": "y"}).status_code == 404


def test_delete_card(client):
    card = client.post("/api/notes", json={"title": "x"}).json()["card"]
    assert client.delete(f"/api/notes/{card['id']}").json()["ok"]
    assert client.get("/api/notes").json()["cards"]["todo"] == []


def test_column_add_rename_move_delete_round_trip(client):
    r = client.post("/api/notes/columns", json={"name": "blocked"})
    assert r.status_code == 200 and r.json()["columns"][-1] == "blocked"

    r = client.patch("/api/notes/columns/blocked", json={"name": "on hold"})
    assert r.status_code == 200 and "on hold" in r.json()["columns"]

    r = client.post(f"/api/notes/columns/{quote('on hold')}/move", json={"direction": -1})
    assert r.status_code == 200
    assert r.json()["columns"].index("on hold") < r.json()["columns"].index("done")

    card = client.post("/api/notes", json={"title": "stuck", "col": "on hold"}).json()["card"]
    assert client.delete(f"/api/notes/columns/{quote('on hold')}").status_code == 400   # cards would vanish
    r = client.delete(f"/api/notes/columns/{quote('on hold')}", params={"move_to": "done"})
    assert r.status_code == 200
    b = r.json()
    assert "on hold" not in b["columns"]
    assert any(c["id"] == card["id"] for c in b["cards"]["done"])


def test_duplicate_column_name_rejected(client):
    assert client.post("/api/notes/columns", json={"name": "todo"}).status_code == 400
