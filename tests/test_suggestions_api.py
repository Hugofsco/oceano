"""The Suggestions web surface: /api/suggestions* must sit behind the normal auth middleware
(the queue can create research topics/workflows/memories on accept), and the handlers must
round-trip the suggestions module. Minimal app (real middleware + real router, no lifespan),
so nothing touches real data/.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano import scheduler, suggestions  # noqa: E402
from oceano.web import routes_content, state  # noqa: E402
from oceano.web.server import _require_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STORE", tmp_path / "web.json")
    monkeypatch.setattr(suggestions, "DB_PATH", tmp_path / "suggestions.db")
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")


def test_suggestions_routes_require_a_session_cookie(tmp_path):
    app = FastAPI()
    app.middleware("http")(_require_auth)
    app.include_router(routes_content.router)
    client = TestClient(app)
    assert client.get("/api/suggestions").status_code == 401
    assert client.post("/api/suggestions/1/accept").status_code == 401
    assert client.post("/api/suggestions/1/dismiss").status_code == 401


def test_suggestions_list_accept_dismiss_round_trip(tmp_path, monkeypatch):
    app = FastAPI()                                    # no middleware: exercise the handlers
    app.include_router(routes_content.router)
    client = TestClient(app)
    sid = suggestions.add("memory", "note the ocean is deep", detail="the ocean is deep", source="reflection")
    s2 = suggestions.add("other", "reorganize the workspace")

    d = client.get("/api/suggestions").json()
    assert d["pending"] == 2 and {s["id"] for s in d["suggestions"]} == {sid, s2}

    saved = {}
    monkeypatch.setattr("oceano.memory.remember",
                        lambda text, **kw: saved.setdefault("text", text) and None or "remembered")
    r = client.post(f"/api/suggestions/{sid}/accept").json()
    assert r["ok"] is True and saved["text"] == "the ocean is deep"
    r = client.post(f"/api/suggestions/{s2}/dismiss").json()
    assert r["ok"] is True

    d = client.get("/api/suggestions").json()
    assert d["pending"] == 0 and d["suggestions"] == []
    hist = client.get("/api/suggestions", params={"status": "all"}).json()["suggestions"]
    assert {s["status"] for s in hist} == {"done", "dismissed"}
    assert client.post("/api/suggestions/99999/accept").json()["ok"] is False


def test_suggestions_response_reports_the_producers_health(tmp_path):
    """The [ SELF ] reflection is the queue's only producer — the panel warns when it's off or
    missing, so the API must report its state plus a staleness stamp (a starved queue must be
    loud, not indistinguishable from 'no ideas')."""
    app = FastAPI()
    app.include_router(routes_content.router)
    client = TestClient(app)

    r = client.get("/api/suggestions").json()["reflection"]
    assert r == {"exists": False, "enabled": False, "last_filed": None}    # task missing entirely

    tid = scheduler.add_task("30 23 * * *", "[ SELF ] Nightly reflection", source="self:reflect")
    suggestions.add("memory", "a fresh idea", source="self:reflect")
    r = client.get("/api/suggestions").json()["reflection"]
    assert r["exists"] is True and r["enabled"] is True and r["last_filed"]

    scheduler.update_task(tid, enabled=False)                              # switched OFF → warn state
    r = client.get("/api/suggestions").json()["reflection"]
    assert r["exists"] is True and r["enabled"] is False
