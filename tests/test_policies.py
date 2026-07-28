"""Runtime policy enforcement for risky workflow/tool actions."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import policies, tools, workflows  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(policies, "STORE", tmp_path / "policies.json")
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(workflows, "CHECKPOINT_STORE", tmp_path / "workflow_checkpoints.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


def _wf(nodes, edges):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "policy", "graph": graph, "input": {}}


def test_policy_store_defaults_and_updates():
    d = policies.get()
    assert d["mail_send"] == "confirm"
    assert policies.set_policy("mail_send", "block") is True
    assert policies.get()["mail_send"] == "block"


def test_blocked_policy_refuses_workflow_tool_node():
    policies.set_policy("workspace_write", "block")
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "tool", "tool": "write_file", "args": {"path": "x.txt", "content": "hi"}},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["ok"] is False
    assert "blocked by policy" in steps[2]["output"]


def test_confirm_policy_pauses_workflow_until_approved(monkeypatch):
    policies.set_policy("workspace_write", "confirm")
    decisions = []
    token_box = {"token": None}
    orig = workflows._await_approval

    def fake_await(wf_id, prompt, timeout_min, beat):
        pending = workflows.pending_approvals(wf_id)
        if pending:
            token_box["token"] = pending[0]["token"]
        ok, detail = orig(wf_id, prompt, timeout_min, beat)
        decisions.append((ok, detail))
        return ok, detail

    monkeypatch.setattr(workflows, "_await_approval", fake_await)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "tool", "tool": "write_file", "args": {"path": "x.txt", "content": "hi"}},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])

    import threading
    box = {}

    def work():
        box["rec"] = workflows.run(wf, trigger="manual", nested=True)

    th = threading.Thread(target=work, daemon=True)
    th.start()
    import time
    deadline = time.time() + 5
    while time.time() < deadline and not workflows.pending_approvals(wf["id"]):
        time.sleep(0.05)
    pending = workflows.pending_approvals(wf["id"])
    assert pending
    workflows.resolve_approval(pending[0]["token"], True)
    th.join(5)
    assert box["rec"]["status"] == "ok"
    assert decisions and decisions[0][0] is True


def test_side_effecting_tools_have_auditable_capabilities():
    assert policies.capability_for_tool("add_calendar_event") == "calendar_write"
    assert policies.capability_for_tool("browser_eval") == "browser_control"
    assert policies.capability_for_tool("update_note") == "notes_write"
    assert policies.DEFAULTS["calendar_write"] == "allow"  # backward-compatible rollout
    assert tools.tool_spec("add_calendar_event").side_effecting is True
