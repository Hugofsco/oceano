"""Workflow checkpoints + manual resume."""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import jobs, workflows  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(workflows, "CHECKPOINT_STORE", tmp_path / "workflow_checkpoints.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr(jobs, "_serialize", False)   # a non-nested run must not queue behind the gate
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


def _wf(nodes, edges):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "t", "graph": graph, "input": {}}


def test_checkpoint_saved_after_each_node_and_cleared_on_success():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}B"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert workflows.resume_state(wf["id"]) is None


def test_resume_continues_from_last_checkpoint_after_failure():
    calls = {"n": 0}
    orig = workflows._run_transform

    def flaky(node, ctx):
        if node["id"] == 3:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
        return orig(node, ctx)

    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}B"},
         {"id": 4, "type": "transform", "mode": "template", "text": "{{last}}C"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}, {"from": 4, "to": 5}])
    try:
        workflows._run_transform = flaky
        rec1 = workflows.run(wf, trigger="manual", nested=True)
    finally:
        workflows._run_transform = orig
    assert rec1["status"] == "error"
    st = workflows.resume_state(wf["id"])
    assert st and st["next_node_id"] == 3
    rec2 = workflows.resume(wf["id"])
    assert rec2["status"] == "ok"
    assert rec2["output"] == "ABC"
    assert workflows.resume_state(wf["id"]) is None


def test_resume_replays_branch_queue_and_loop_state():
    calls = {"n": 0}
    orig = workflows._run_transform

    def flaky(node, ctx):
        if node["id"] == 4:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("branch fail")
        return orig(node, ctx)

    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "ROOT"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}-L"},
         {"id": 4, "type": "transform", "mode": "template", "text": "{{last}}-R"},
         {"id": 5, "type": "merge", "mode": "concat"},
         {"id": 6, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 2, "to": 4},
         {"from": 3, "to": 5}, {"from": 4, "to": 5}, {"from": 5, "to": 6}])
    try:
        workflows._run_transform = flaky
        rec1 = workflows.run(wf, trigger="manual", nested=True)
    finally:
        workflows._run_transform = orig
    assert rec1["status"] == "error"
    st = workflows.resume_state(wf["id"])
    assert st and st["branch_q"]
    rec2 = workflows.resume(wf["id"])
    assert rec2["status"] == "ok"
    assert rec2["output"] == "ROOT-L\n\nROOT-R"


def test_resume_returns_none_without_checkpoint():
    assert workflows.resume(12345) is None


def test_resumable_info_lists_workflows_with_a_saved_checkpoint_and_why():
    assert workflows.resumable_info() == {}
    calls = {"n": 0}
    orig = workflows._run_transform

    def flaky(node, ctx):
        if node["id"] == 3 and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("boom")
        return orig(node, ctx)

    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}B"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    try:
        workflows._run_transform = flaky
        workflows.run(wf, trigger="manual", nested=True)
    finally:
        workflows._run_transform = orig
    info = workflows.resumable_info()
    assert list(info.keys()) == [wf["id"]]
    assert info[wf["id"]]["status"] == "error"       # a genuine crash, not a deliberate pause
    assert info[wf["id"]]["ts"]
    workflows.resume(wf["id"])
    assert workflows.resumable_info() == {}


def test_resumable_info_reports_cancelled_status_for_a_deliberate_pause():
    """A pause (⏸ button / jobs-popup ✕) leaves the SAME kind of checkpoint as a crash — the
    'status' field is what lets the UI tell the two apart instead of showing an ambiguous button."""
    started = threading.Event()
    orig = workflows._run_transform

    def slow_at_node_2(node, ctx):
        if node["id"] == 2:
            started.set()
            time.sleep(0.3)
        return orig(node, ctx)

    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])

    def work():
        workflows._run_transform = slow_at_node_2
        try:
            workflows.run(wf, trigger="manual", nested=False)
        finally:
            workflows._run_transform = orig

    th = threading.Thread(target=work, daemon=True)
    th.start()
    assert started.wait(2)
    jobs.cancel_by_ref(f"workflow:{wf['id']}")
    th.join(5)
    info = workflows.resumable_info()
    assert info[wf["id"]]["status"] == "cancelled"


def test_checkpoint_store_is_json_serializable():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": json.dumps({"ok": True})},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    workflows.run(wf, trigger="manual", nested=True)
    try:
        data = json.loads(workflows.CHECKPOINT_STORE.read_text())
    except OSError:
        data = {}
    assert isinstance(data, dict)


def test_pause_via_cancel_by_ref_keeps_the_checkpoint_and_resume_continues():
    """'Pause' (the workflow web UI's ⏸ button) is a jobs.cancel_by_ref("workflow:<id>") call —
    the same signal a jobs-popup ✕ click sends. A run stopped this way ends 'cancelled', which
    (unlike ok/empty/skipped) does NOT clear the checkpoint, so /api/workflows/{id}/resume can
    pick it back up. This must go through a real, non-nested run() — nested=True (used by every
    other test in this file) never registers with jobs.job(), so cancel_by_ref would have
    nothing to find."""
    started = threading.Event()
    orig = workflows._run_transform

    def slow_at_node_2(node, ctx):
        if node["id"] == 2:
            started.set()
            time.sleep(0.3)
        return orig(node, ctx)

    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}B"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])

    box = {}

    def work():
        workflows._run_transform = slow_at_node_2
        try:
            box["rec"] = workflows.run(wf, trigger="manual", nested=False)
        finally:
            workflows._run_transform = orig

    th = threading.Thread(target=work, daemon=True)
    th.start()
    assert started.wait(2), "the slow node never started"
    assert jobs.cancel_by_ref(f"workflow:{wf['id']}") is True
    assert jobs.cancel_by_ref(f"workflow:{wf['id']}") is True   # idempotent while still live
    th.join(5)

    assert box["rec"]["status"] == "cancelled"
    st = workflows.resume_state(wf["id"])
    assert st and st["next_node_id"] == 3

    rec2 = workflows.resume(wf["id"])
    assert rec2["status"] == "ok"
    assert rec2["output"] == "AB"
    assert workflows.resume_state(wf["id"]) is None   # checkpoint cleared on a clean finish
