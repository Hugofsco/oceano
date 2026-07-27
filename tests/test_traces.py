"""Structured tracing: workflows, tools, and model calls write trace events."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import traces, workflows  # noqa: E402
from oceano.agent import Agent  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(traces, "TRACE_PATH", tmp_path / "traces.jsonl")
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(workflows, "CHECKPOINT_STORE", tmp_path / "workflow_checkpoints.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    traces.clear()
    yield


def _wf(nodes, edges):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "trace", "graph": graph, "input": {}}


def test_workflow_run_writes_trace_events():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    evs = traces.query(workflow_id=wf["id"])
    assert rec["status"] == "ok"
    assert any(e["event"] == "workflow_start" for e in evs)
    assert any(e["event"] == "workflow_node_end" and e["node_id"] == 2 for e in evs)
    assert any(e["event"] == "workflow_done" for e in evs)


def test_tool_run_inside_trace_scope_is_recorded():
    with traces.scope(run_id="r1", workflow_id=7):
        from oceano import tools
        out = tools.run("make_folder", '{"path":"tmp"}')
    evs = traces.query(run_id="r1")
    assert "created folder" in out
    assert [e["event"] for e in evs] == ["tool_call", "tool_result"]


def test_agent_model_calls_are_traced(monkeypatch):
    class Msg:
        tool_calls = None
        content = "hello"

    def fake_chat(*a, **k):
        return Msg()

    monkeypatch.setattr("oceano.llm.chat", fake_chat)
    # inject_context=False: this test is about tracing, not context assembly — without it the
    # Agent reads the developer's real memory/research store, which both slows the test and (via
    # the passive research-note injection) taints the shared TurnContext, leaking into later tests.
    ag = Agent(model="fake-model", learn=False, inject_context=False)
    with traces.scope(run_id="r2", session_id="s1"):
        out = ag.run("hi")
    evs = traces.query(run_id="r2")
    assert out == "hello"
    assert any(e["event"] == "model_call_start" for e in evs)
    assert any(e["event"] == "model_call_end" for e in evs)
