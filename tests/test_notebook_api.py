"""The Notebook web surface: /api/notebook* must round-trip the notebook module's CRUD
and the q/tag list filters."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano import notebook  # noqa: E402
from oceano.web import routes_content  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(notebook, "STORE", tmp_path / "notebook.json")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes_content.router)
    return TestClient(app)


def test_list_empty_notebook(client):
    r = client.get("/api/notebook")
    assert r.status_code == 200
    assert r.json() == {"notes": [], "tags": []}


def test_create_and_list_round_trip(client):
    r = client.post("/api/notebook", json={"title": "Idea", "body": "a longer thought", "tags": ["draft"]})
    assert r.status_code == 200 and r.json()["ok"]
    note = r.json()["note"]
    assert note["title"] == "Idea" and note["tags"] == ["draft"]
    listed = client.get("/api/notebook").json()
    assert listed["notes"][0]["id"] == note["id"]
    assert listed["tags"] == ["draft"]


def test_patch_updates_fields_and_pin(client):
    note = client.post("/api/notebook", json={"title": "x"}).json()["note"]
    r = client.patch(f"/api/notebook/{note['id']}", json={"title": "y", "pinned": True})
    assert r.status_code == 200
    assert r.json()["note"]["title"] == "y"
    assert r.json()["note"]["pinned"] is True


def test_patch_unknown_note_404s(client):
    assert client.patch("/api/notebook/999", json={"title": "y"}).status_code == 404


def test_delete_note(client):
    note = client.post("/api/notebook", json={"title": "x"}).json()["note"]
    assert client.delete(f"/api/notebook/{note['id']}").json()["ok"]
    assert client.get("/api/notebook").json()["notes"] == []


def test_search_and_tag_query_params(client):
    client.post("/api/notebook", json={"title": "Grocery list", "body": "eggs", "tags": ["home"]})
    client.post("/api/notebook", json={"title": "Trip plan", "body": "flights", "tags": ["travel"]})
    r = client.get("/api/notebook", params={"q": "eggs"})
    assert [n["title"] for n in r.json()["notes"]] == ["Grocery list"]
    r = client.get("/api/notebook", params={"tag": "travel"})
    assert [n["title"] for n in r.json()["notes"]] == ["Trip plan"]
