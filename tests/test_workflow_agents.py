"""The workflow agent/await nodes: an agent node spawns a background sub-agent WITHOUT
blocking the walk, and an await node joins — collecting results into ctx so downstream
{{node.ID}} templating sees them, and routing the error edge on timeout/failure. agentjobs
is stubbed: these tests exercise the workflow executor, not the providers.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")   # never touch real runs
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


def _wf(nodes, edges):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "t", "graph": graph, "input": {}}


class FakeAgentJobs:
    """A registry stub: spawn() records the call; status() follows a scripted state table."""

    def __init__(self, states):
        self.states = states          # agent_id -> record dict returned by status()
        self.spawned = []
        self._ids = iter(range(1, 100))

    def spawn(self, task, provider="", label="", timeout=0, cwd=None, sid=None):
        aid = next(self._ids)
        self.spawned.append({"id": aid, "task": task, "provider": provider, "timeout": timeout})
        return {"id": aid, "label": label or task, "provider": provider or "api", "state": "running"}

    def status(self, aid=None):
        return dict(self.states[aid]) if aid in self.states else None


def test_agent_node_spawns_without_blocking_and_await_collects(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "AGENT-RESULT", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "research {{input}}", "provider": "api", "label": "r1"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "transform", "mode": "template", "text": "got: {{node.2}}"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", inp="tides", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["task"] == "research tides"        # {{input}} templated into the task
    steps = {s["id"]: s for s in rec["steps"]}
    assert "AGENT-RESULT" in steps[3]["output"]               # await surfaced the result
    assert rec["output"] == "got: AGENT-RESULT"               # ctx["nodes"][2] replaced by the real result


def test_await_timeout_routes_error_edge(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "running", "output": "", "error": ""}})   # never finishes
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    monkeypatch.setattr(time, "sleep", lambda s: None)        # don't actually wait out the poll loop
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "never ends", "provider": "api"},
         {"id": 3, "type": "await", "timeout": 1},
         {"id": 4, "type": "transform", "mode": "template", "text": "HAPPY"},
         {"id": 5, "type": "transform", "mode": "template", "text": "RESCUED"},
         {"id": 6, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4},
         {"from": 3, "to": 5, "branch": "error"}, {"from": 4, "to": 6}, {"from": 5, "to": 6}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[3]["ok"] is False
    assert "TIMED OUT" in steps[3]["output"]
    assert 5 in steps and 4 not in steps                      # error edge taken, happy path skipped


def test_await_with_nothing_spawned_fails_cleanly(monkeypatch):
    fake = FakeAgentJobs({})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"}, {"id": 2, "type": "await", "timeout": 1}, {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["ok"] is False and "no agents" in steps[2]["output"]


def test_spawn_refusal_takes_error_edge(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("agent limit reached (3 running)")
    monkeypatch.setattr("oceano.agentjobs.spawn", refuse)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "t", "provider": "api"},
         {"id": 3, "type": "transform", "mode": "template", "text": "HAPPY"},
         {"id": 4, "type": "transform", "mode": "template", "text": "RESCUED"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3},
         {"from": 2, "to": 4, "branch": "error"}, {"from": 3, "to": 5}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["ok"] is False and "agent limit" in steps[2]["output"]
    assert 4 in steps and 3 not in steps


def test_norm_graph_normalizes_agent_and_await():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "agent", "task": "t", "provider": "bogus", "timeout": 99999},
        {"id": 2, "type": "await", "timeout": -5},
    ], "edges": []})
    a, w = g["nodes"]
    assert a["provider"] == "" and a["timeout"] == 3600       # bogus provider → default; clamped
    assert w["timeout"] == 1
