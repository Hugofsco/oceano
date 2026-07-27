"""POST /api/workflows/{id}/pause and GET /api/workflows/checkpoints: thin routes over
jobs.cancel_by_ref and workflows.resumable_info, exercised through the real router so a wiring
mistake (wrong ref format, wrong HTTP method) shows up here rather than only in the browser."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano import jobs, workflows  # noqa: E402
from oceano.web import routes_content  # noqa: E402
from oceano.web.server import _require_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_log(monkeypatch):
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


@pytest.fixture(autouse=True)
def _isolate_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "CHECKPOINT_STORE", tmp_path / "workflow_checkpoints.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")   # resumable_info() reads runs()
    yield


def _client():
    app = FastAPI()
    app.include_router(routes_content.router)
    return TestClient(app)


def test_pause_route_requires_auth():
    app = FastAPI()
    app.middleware("http")(_require_auth)
    app.include_router(routes_content.router)
    assert TestClient(app).post("/api/workflows/1/pause").status_code == 401


def test_pause_route_is_false_when_nothing_is_running():
    assert _client().post("/api/workflows/1234/pause").json() == {"ok": False}


def test_pause_route_signals_the_matching_workflow_job_only():
    client = _client()
    with jobs.job("workflow", "other one", ref="workflow:2") as other_jid:
        with jobs.job("workflow", "the target", ref="workflow:1") as jid:
            assert client.post("/api/workflows/1/pause").json() == {"ok": True}
            assert jobs.cancel_event(jid).is_set()
            assert not jobs.cancel_event(other_jid).is_set()   # only the addressed workflow stops


def test_checkpoints_route_requires_auth():
    app = FastAPI()
    app.middleware("http")(_require_auth)
    app.include_router(routes_content.router)
    assert TestClient(app).get("/api/workflows/checkpoints").status_code == 401


def test_checkpoints_route_lists_resumable_workflow_ids_with_status():
    client = _client()
    assert client.get("/api/workflows/checkpoints").json() == {"ids": [], "info": {}}
    workflows._save_checkpoint(3, {"next_node_id": 2, "ts": "2026-07-14T00:00:00"})
    d = client.get("/api/workflows/checkpoints").json()
    assert d["ids"] == [3]
    assert d["info"]["3"]["status"] == "unknown"     # no run record for it in this isolated store
    assert d["info"]["3"]["ts"] == "2026-07-14T00:00:00"
