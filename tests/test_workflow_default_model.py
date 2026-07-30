"""A scheduled task's model pin (tasks.model on a workflow: source row) rides into the run as
its default mind: un-pinned instruction nodes, decision gates and agent nodes follow it instead
of the global PRIMARY INTELLIGENCE. Per-node pins still win. Before this, the Scheduler showed a
model selector on workflow tasks but the choice was silently ignored at dispatch."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")   # never touch real data
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


def _wf(nodes, edges):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "t", "graph": graph, "input": {}}


class FakeAgent:
    """Records which entry point took each turn — the whole point under test."""
    calls = []

    def __init__(self, model=None, on_event=None, base_url=None, api_key=None, learn=True,
                 exclude_tools=None, only_tools=None, inject_context=True,
                              trusted_origin=True, **kw):
        self.model, self.base_url, self.api_key = model, base_url, api_key
        self.trusted_origin = trusted_origin
        self.messages = []
        self.on_event = on_event or (lambda k, d: None)

    def run(self, text, **kw):
        FakeAgent.calls.append(("local", self.model))
        return f"local[{self.model}]: {text}"

    def run_claude(self, text, **kw):
        FakeAgent.calls.append(("claude", None))
        return f"claude: {text}"

    def run_codex(self, text, **kw):
        FakeAgent.calls.append(("codex", None))
        return f"codex: {text}"


@pytest.fixture()
def fake_agent(monkeypatch):
    FakeAgent.calls = []
    monkeypatch.setattr("oceano.agent.Agent", FakeAgent)
    monkeypatch.setattr("oceano.delegate.available", lambda: True)
    monkeypatch.setattr("oceano.delegate.codex_available", lambda: True)
    return FakeAgent


def _instruction_wf():
    return _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "instruction", "text": "do {{input}}"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])


def test_task_pin_beats_global_mind_on_unpinned_instruction(monkeypatch, fake_agent):
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")     # global says Claude…
    rec = workflows.run(_instruction_wf(), nested=True, default_model="codex")
    assert rec["status"] == "ok"
    assert rec["output"] == "codex: do "                                  # …but the task pin wins
    assert ("claude", None) not in fake_agent.calls


def test_without_pin_unpinned_instruction_follows_global_mind(monkeypatch, fake_agent):
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "codex")
    rec = workflows.run(_instruction_wf(), nested=True)
    assert rec["output"] == "codex: do "


def test_node_provider_still_wins_over_task_pin(monkeypatch, fake_agent):
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "local")
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "instruction", "text": "do it", "provider": "claude"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, nested=True, default_model="codex")
    assert rec["output"] == "claude: do it"


def test_endpoint_pin_runs_shared_loop_on_that_endpoint(monkeypatch, fake_agent):
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")     # must NOT boot the CLI mind
    stub = types.SimpleNamespace(endpoint_key=lambda burl: "sk-test")
    monkeypatch.setitem(sys.modules, "oceano.web.server", stub)
    if "oceano.web" in sys.modules:               # from-import prefers the package attribute
        monkeypatch.setattr(sys.modules["oceano.web"], "server", stub, raising=False)
    rec = workflows.run(_instruction_wf(), nested=True,
                        default_model="qwen3.5-9b", default_base_url="http://127.0.0.1:8081/v1")
    assert rec["output"] == "local[qwen3.5-9b]: do "
    assert fake_agent.calls == [("local", "qwen3.5-9b")]


def test_unpinned_agent_node_spawns_on_task_mind(monkeypatch, fake_agent):
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "local")
    spawned = []

    def spawn(task, provider="", label="", tools=None, timeout=0, cwd=None, sid=None,
              model="", base_url="", skills=False):
        spawned.append({"provider": provider, "model": model})
        return {"id": 1, "label": label or task, "provider": provider or "api", "state": "running"}

    monkeypatch.setattr("oceano.agentjobs.spawn", spawn)
    monkeypatch.setattr("oceano.agentjobs.status",
                        lambda aid: {"state": "done", "output": "OK", "error": ""})
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "research", "label": "r1"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, nested=True, default_model="codex")
    assert rec["status"] == "ok"
    assert spawned[0]["provider"] == "codex"      # inherited from the task pin


def test_agent_node_endpoint_pin_survives_task_mind(monkeypatch, fake_agent):
    """A node-level endpoint model must not be clobbered by the run's CLI-mind pin —
    CLI providers ignore model pins, so injecting the provider would drop the model."""
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "local")
    spawned = []

    def spawn(task, provider="", label="", tools=None, timeout=0, cwd=None, sid=None,
              model="", base_url="", skills=False):
        spawned.append({"provider": provider, "model": model})
        return {"id": 1, "label": label or task, "provider": provider or "api", "state": "running"}

    monkeypatch.setattr("oceano.agentjobs.spawn", spawn)
    monkeypatch.setattr("oceano.agentjobs.status",
                        lambda aid: {"state": "done", "output": "OK", "error": ""})
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "research", "label": "r1", "model": "qwen3.5-9b"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    workflows.run(wf, nested=True, default_model="claude")
    assert spawned[0]["provider"] == ""           # spawn resolves it to an api-style endpoint run
    assert spawned[0]["model"] == "qwen3.5-9b"


def test_decision_model_gate_follows_task_pin(monkeypatch, fake_agent):
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "local")
    asked = []

    def to_codex(prompt, **kw):
        asked.append(prompt)
        return {"ok": True, "output": "YES"}

    monkeypatch.setattr("oceano.delegate.to_codex", to_codex)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "decision", "mode": "model", "question": "proceed?"},
         {"id": 3, "type": "transform", "mode": "template", "text": "went yes"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3, "branch": "yes"}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, nested=True, default_model="codex")
    assert rec["status"] == "ok"
    assert asked and "proceed?" in asked[0]       # the gate ran on the pinned mind
    assert rec["output"] == "went yes"


class TruncatingMindAgent(FakeAgent):
    """A Claude mind whose build gets killed mid-run (idle/wall-clock cap): run_claude returns the
    partial text but sets last_mind_error, exactly like the real mind streams now do."""
    def run_claude(self, text, **kw):
        FakeAgent.calls.append(("claude", None))
        self.last_mind_error = "the delegate produced no output for 900s and was stopped (looked stalled)"
        return "…partial build output before it was killed…"


def test_truncated_mind_build_is_recorded_as_a_failed_step(monkeypatch):
    """The core fix: a stalled/capped mind turn must NOT be logged as a clean node. The workflow
    reads ag.last_mind_error and fails the step, so a cut-off build surfaces instead of masquerading
    as progress."""
    FakeAgent.calls = []
    monkeypatch.setattr("oceano.agent.Agent", TruncatingMindAgent)
    monkeypatch.setattr("oceano.delegate.available", lambda: True)
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")
    rec = workflows.run(_instruction_wf(), nested=True)
    assert ("claude", None) in FakeAgent.calls          # the mind did run
    assert rec["status"] != "ok"                        # …but the truncated build is NOT a clean run
    assert "did not complete" in (rec.get("output") or rec.get("error") or "")


def test_clean_mind_build_still_succeeds(monkeypatch, fake_agent):
    """Guard the happy path: a mind turn that finishes cleanly (last_mind_error stays None) is ok."""
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")
    rec = workflows.run(_instruction_wf(), nested=True)
    assert rec["status"] == "ok" and rec["output"] == "claude: do "
