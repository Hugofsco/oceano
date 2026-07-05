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

    def spawn(self, task, provider="", label="", tools=None, timeout=0, cwd=None, sid=None,
              model="", base_url="", skills=False):
        aid = next(self._ids)
        self.spawned.append({"id": aid, "task": task, "provider": provider,
                             "model": model, "base_url": base_url,
                             "tools": tools, "timeout": timeout, "skills": skills})
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
    # gate parity with the sibling delegate node: a background agent in a flow gets the SAME
    # read-only tool scope, never the read-write default
    assert fake.spawned[0]["tools"] == "Read,Glob,Grep"
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


# ---------------- orchestrate: plugged-in agents run in ordered steps ----------------
def _orch_wf(plan, states, extra_orch=None):
    """start → orchestrate (with agents 2,3,4 plugged in) → transform → end.
    The agent→orchestrate edges are ATTACHMENTS — traversal must never walk them."""
    orch = {"id": 5, "type": "orchestrate", "plan": plan, "timeout": 5, **(extra_orch or {})}
    return _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "scan A", "label": "a"},
         {"id": 3, "type": "agent", "task": "scan B", "label": "b"},
         {"id": 4, "type": "agent", "task": "synthesize", "label": "c"},
         orch,
         {"id": 6, "type": "transform", "mode": "template", "text": "got: {{node.4}}"},
         {"id": 7, "type": "end"}],
        [{"from": 1, "to": 5},
         {"from": 2, "to": 5}, {"from": 3, "to": 5}, {"from": 4, "to": 5},
         {"from": 5, "to": 6}, {"from": 6, "to": 7}])


def test_orchestrate_steps_in_order_with_context_passing(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "A1-OUT", "error": ""},
                          2: {"state": "done", "output": "A2-OUT", "error": ""},
                          3: {"state": "done", "output": "A3-OUT", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _orch_wf({"2": 1, "3": 1, "4": 2}, fake.states)
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    tasks = [s["task"] for s in fake.spawned]
    assert len(tasks) == 3
    assert tasks[0] == "scan A" and tasks[1] == "scan B"      # step 1: parallel pair, no context yet
    assert tasks[2].startswith("synthesize")                  # step 2 spawned after step 1 joined…
    assert "A1-OUT" in tasks[2] and "A2-OUT" in tasks[2]      # …and received step 1's results
    steps = {s["id"]: s for s in rec["steps"]}
    assert "A3-OUT" in steps[5]["output"]                     # compile includes the final agent
    assert rec["output"] == "got: A3-OUT"                     # each agent is {{node.ID}}-addressable
    # attachments are not flow EDGES (traversal never walks them) but each still gets its own
    # persisted history row — otherwise per-agent detail is visible live and vanishes from history
    assert steps[2]["ok"] is True and steps[2]["output"] == "A1-OUT"
    assert steps[3]["ok"] is True and steps[3]["output"] == "A2-OUT"
    assert steps[4]["ok"] is True and steps[4]["output"] == "A3-OUT"
    # agent rows precede the orchestrator's own compiled row, in plugged-in order
    order = [s["id"] for s in rec["steps"]]
    assert order.index(2) < order.index(5) and order.index(3) < order.index(5) and order.index(4) < order.index(5)


def test_orchestrate_failure_stops_later_steps_and_takes_error_edge(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "failed", "output": "", "error": "boom"}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    monkeypatch.setattr(workflows, "_SALVAGE_BACKOFF", 0)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "flaky", "label": "a"},
         {"id": 3, "type": "agent", "task": "never spawned", "label": "b"},
         {"id": 4, "type": "orchestrate", "plan": {"2": 1, "3": 2}, "timeout": 5},
         {"id": 5, "type": "transform", "mode": "template", "text": "HAPPY"},
         {"id": 6, "type": "transform", "mode": "template", "text": "RESCUED"},
         {"id": 7, "type": "end"}],
        [{"from": 1, "to": 4}, {"from": 2, "to": 4}, {"from": 3, "to": 4},
         {"from": 4, "to": 5}, {"from": 4, "to": 6, "branch": "error"},
         {"from": 5, "to": 7}, {"from": 6, "to": 7}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[4]["ok"] is False and "boom" in steps[4]["output"]
    # the failed agent got exactly ONE serial salvage retry; step 2 was never triggered
    assert [sp["task"] for sp in fake.spawned] == ["flaky", "flaky"]
    assert 6 in steps and 5 not in steps                      # error edge taken
    # the failed agent still gets its own persisted history row (both attempts' errors kept)…
    assert steps[2]["ok"] is False and "boom" in steps[2]["output"]
    # …but the agent from the never-reached step 2 was never spawned, so it has no row at all
    assert 3 not in steps


def test_orchestrate_without_agents_fails_cleanly(monkeypatch):
    fake = FakeAgentJobs({})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"}, {"id": 2, "type": "orchestrate", "plan": {}, "timeout": 5},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["ok"] is False and "no agent nodes" in steps[2]["output"]


def test_norm_graph_normalizes_orchestrate():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "orchestrate", "plan": {"7": "2", "x": 3, "8": 0, "9": 99},
         "mode": "bogus", "timeout": 99999},
    ], "edges": []})
    n = g["nodes"][0]
    assert n["plan"] == {"7": 2, "8": 1, "9": 20}             # ints, clamped 1..20, junk keys dropped
    assert n["mode"] == "concat" and n["timeout"] == 3600


def test_agent_model_pin_reaches_spawn(monkeypatch):
    """An agent node pinned to a registered endpoint's model passes the pin to agentjobs.spawn
    (which routes it through the api provider with model/base_url overrides)."""
    fake = FakeAgentJobs({1: {"state": "done", "output": "X", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "t", "provider": "api",
          "model": "qwen2.5-72b", "baseUrl": "https://api.example.com/v1"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["model"] == "qwen2.5-72b"
    assert fake.spawned[0]["base_url"] == "https://api.example.com/v1"


def test_instruction_model_pin_runs_on_pinned_agent_with_shared_context(monkeypatch):
    """An instruction node pinned to an endpoint model runs THAT turn on a pinned Agent that
    shares the run's conversation (same messages object), with the endpoint's API key."""
    import sys, types
    made = []

    class FakeAgent:
        def __init__(self, model=None, on_event=None, base_url=None, api_key=None, learn=True,
                     exclude_tools=None, only_tools=None, inject_context=True):
            self.model, self.base_url, self.api_key = model, base_url, api_key
            self.messages = []
            self.on_event = on_event or (lambda k, d: None)
            made.append(self)

        def run(self, text, **kw):
            return f"ran[{self.model}]: {text}"

    monkeypatch.setattr("oceano.agent.Agent", FakeAgent)
    stub = types.SimpleNamespace(endpoint_key=lambda burl: "sk-test")
    monkeypatch.setitem(sys.modules, "oceano.web.server", stub)
    if "oceano.web" in sys.modules:               # from-import prefers the package attribute
        monkeypatch.setattr(sys.modules["oceano.web"], "server", stub, raising=False)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "instruction", "text": "summarize {{input}}",
          "model": "qwen3.5-9b", "baseUrl": "http://127.0.0.1:8081/v1"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", inp="tides", nested=True)
    assert rec["status"] == "ok"
    assert rec["output"] == "ran[qwen3.5-9b]: summarize tides"
    shared, pinned = made[0], made[1]
    assert pinned.model == "qwen3.5-9b" and pinned.base_url == "http://127.0.0.1:8081/v1"
    assert pinned.api_key == "sk-test"
    assert pinned.messages is shared.messages     # same conversation object → context accumulates


def test_orchestrate_salvage_retry_rescues_a_stalled_agent(monkeypatch):
    """A step-1 agent that stalls on its first attempt (e.g. an endpoint holding the request past
    the client timeout) is retried once, ALONE — a successful retry keeps the step (and run) ok."""
    fake = FakeAgentJobs({1: {"state": "done", "output": "R-OK", "error": ""},
                          2: {"state": "failed", "output": "", "error": "APITimeoutError: stalled"},
                          3: {"state": "done", "output": "SALVAGED", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    monkeypatch.setattr(workflows, "_SALVAGE_BACKOFF", 0)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "steady", "label": "a"},
         {"id": 3, "type": "agent", "task": "flaky-then-fine", "label": "b"},
         {"id": 4, "type": "orchestrate", "plan": {"2": 1, "3": 1}, "timeout": 5},
         {"id": 5, "type": "transform", "mode": "template", "text": "got: {{node.3}}"},
         {"id": 6, "type": "end"}],
        [{"from": 1, "to": 4}, {"from": 2, "to": 4}, {"from": 3, "to": 4},
         {"from": 4, "to": 5}, {"from": 5, "to": 6}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    tasks = [sp["task"] for sp in fake.spawned]
    assert tasks == ["steady", "flaky-then-fine", "flaky-then-fine"]   # one serial retry, same task
    assert rec["output"] == "got: SALVAGED"                            # retry result won
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[4]["ok"] is True and "SALVAGED" in steps[4]["output"]
    # the salvaged agent's own history row shows the RETRY's result, not the failed first attempt
    assert steps[2]["ok"] is True and steps[2]["output"] == "R-OK"
    assert steps[3]["ok"] is True and steps[3]["output"] == "SALVAGED"


def test_norm_graph_normalizes_instruction_provider():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "instruction", "text": "hi", "provider": "codex"},
        {"id": 2, "type": "instruction", "text": "hi", "provider": "bogus"},
    ], "edges": []})
    n1, n2 = g["nodes"]
    assert n1["provider"] == "codex"
    assert n2["provider"] == ""                       # unknown value → falls back to "" (follow mind)


class FakeMindAgent:
    """Stands in for the run's shared Agent: records which entry point (run/run_claude/run_codex)
    each turn actually took, so tests can tell a mind-routed turn from a plain OpenAI-loop one."""

    def __init__(self, model=None, on_event=None, base_url=None, api_key=None, learn=True,
                 exclude_tools=None, only_tools=None, inject_context=True):
        self.model, self.base_url, self.api_key = model, base_url, api_key
        self.messages = []
        self.on_event = on_event or (lambda k, d: None)
        self.calls = []

    def run(self, text, **kw):
        self.calls.append(("local", text))
        return f"local: {text}"

    def run_claude(self, text):
        self.calls.append(("claude", text))
        return f"claude: {text}"

    def run_codex(self, text):
        self.calls.append(("codex", text))
        return f"codex: {text}"


def _mind_wf(node_extra):
    return _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "instruction", "text": "do the thing", **node_extra},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])


def test_instruction_default_follows_claude_mind(monkeypatch):
    """An unpinned instruction node (no provider, no model) follows Settings → Primary
    intelligence: mind=claude + the CLI available → routes through run_claude, not the plain loop."""
    made = []
    monkeypatch.setattr("oceano.agent.Agent", lambda **kw: made.append(FakeMindAgent(**kw)) or made[-1])
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")
    monkeypatch.setattr("oceano.delegate.available", lambda: True)
    rec = workflows.run(_mind_wf({}), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert made[0].calls == [("claude", "do the thing")]


def test_instruction_default_follows_codex_mind(monkeypatch):
    made = []
    monkeypatch.setattr("oceano.agent.Agent", lambda **kw: made.append(FakeMindAgent(**kw)) or made[-1])
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "codex")
    monkeypatch.setattr("oceano.delegate.codex_available", lambda: True)
    rec = workflows.run(_mind_wf({}), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert made[0].calls == [("codex", "do the thing")]


def test_instruction_default_stays_local_when_mind_is_local(monkeypatch):
    made = []
    monkeypatch.setattr("oceano.agent.Agent", lambda **kw: made.append(FakeMindAgent(**kw)) or made[-1])
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "local")
    rec = workflows.run(_mind_wf({}), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert made[0].calls == [("local", "do the thing")]


def test_instruction_explicit_provider_overrides_global_mind(monkeypatch):
    """A node pinned to 'codex' runs on Codex even though the global mind is set to Claude —
    a per-node pin always wins over the default."""
    made = []
    monkeypatch.setattr("oceano.agent.Agent", lambda **kw: made.append(FakeMindAgent(**kw)) or made[-1])
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")
    monkeypatch.setattr("oceano.delegate.available", lambda: True)
    monkeypatch.setattr("oceano.delegate.codex_available", lambda: True)
    rec = workflows.run(_mind_wf({"provider": "codex"}), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert made[0].calls == [("codex", "do the thing")]


def test_instruction_explicit_local_pin_ignores_global_mind(monkeypatch):
    made = []
    monkeypatch.setattr("oceano.agent.Agent", lambda **kw: made.append(FakeMindAgent(**kw)) or made[-1])
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")
    monkeypatch.setattr("oceano.delegate.available", lambda: True)
    rec = workflows.run(_mind_wf({"provider": "local"}), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert made[0].calls == [("local", "do the thing")]


def test_instruction_mind_pinned_but_cli_unavailable_fails_cleanly(monkeypatch):
    """Mind is claude but the claude CLI isn't installed on this host — the node fails with a
    clear message instead of crashing or silently falling back to the local model."""
    made = []
    monkeypatch.setattr("oceano.agent.Agent", lambda **kw: made.append(FakeMindAgent(**kw)) or made[-1])
    monkeypatch.setattr("oceano.delegate.get_mind", lambda: "claude")
    monkeypatch.setattr("oceano.delegate.available", lambda: False)
    rec = workflows.run(_mind_wf({}), trigger="manual", nested=True)
    assert rec["status"] == "error"
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["ok"] is False
    assert "claude" in steps[2]["output"].lower() and "isn't available" in steps[2]["output"]
    assert made[0].calls == []                        # never fell back to the local loop


def test_orchestrate_revisited_via_manual_loop_back_gets_fresh_history_each_pass(monkeypatch):
    """A decision node can route back to an EARLIER orchestrate node (reiterate until some
    condition holds) — a manual loop via a plain back-edge, not the built-in foreach `loop` type.
    Regression test: a revisited node's second (and later) pass must get its OWN persisted history
    row, same as the first — this used to silently vanish because the live-view row cache keyed
    on node id was mistakenly reused for revisits too (see the orchestrator card-highlight fix)."""
    fake = FakeAgentJobs({1: {"state": "done", "output": "SCANNING", "error": ""},
                          2: {"state": "done", "output": "READY", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "scan", "label": "scanner"},
         {"id": 3, "type": "orchestrate", "plan": {"2": 1}, "timeout": 5},
         {"id": 4, "type": "decision", "mode": "rule", "ruleOp": "contains", "ruleValue": "READY"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 3}, {"from": 2, "to": 3},
         {"from": 3, "to": 4},
         {"from": 4, "to": 3, "branch": "no"},     # loop back — reiterate the orchestrator
         {"from": 4, "to": 5, "branch": "yes"}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    # the orchestrator ran TWICE — its own node id appears twice in the persisted history…
    orch_rows = [s for s in rec["steps"] if s["id"] == 3]
    assert len(orch_rows) == 2
    assert "SCANNING" in orch_rows[0]["output"] and "READY" in orch_rows[1]["output"]
    # …and so did its attached agent — EACH pass gets its own row, not just the first
    agent_rows = [s for s in rec["steps"] if s["id"] == 2]
    assert len(agent_rows) == 2
    assert agent_rows[0]["output"] == "SCANNING" and agent_rows[1]["output"] == "READY"
    assert [sp["task"] for sp in fake.spawned] == ["scan", "scan"]    # spawned fresh each pass


# ---------------- agent/delegate write-access opt-in (default stays read-only) ----------------
def test_agent_node_defaults_to_read_only_tools(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "X", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "t"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["tools"] == "Read,Glob,Grep"       # no write opt-in → stays read-only


def test_agent_node_write_opt_in_grants_write_and_edit(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "X", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "t", "write": "write"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["tools"] == "Read,Glob,Grep,Write,Edit"


def test_orchestrated_agent_write_opt_in_travels_with_its_own_node(monkeypatch):
    """Write access is per-agent-node, not orchestrator-wide: one plugged-in agent can write
    while its sibling stays read-only."""
    fake = FakeAgentJobs({1: {"state": "done", "output": "R1", "error": ""},
                          2: {"state": "done", "output": "R2", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "reader", "label": "reader"},
         {"id": 3, "type": "agent", "task": "writer", "label": "writer", "write": "write"},
         {"id": 4, "type": "orchestrate", "plan": {"2": 1, "3": 1}, "timeout": 5},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 4}, {"from": 2, "to": 4}, {"from": 3, "to": 4}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    by_task = {sp["task"]: sp["tools"] for sp in fake.spawned}
    assert by_task["reader"] == "Read,Glob,Grep"
    assert by_task["writer"] == "Read,Glob,Grep,Write,Edit"


def test_delegate_node_write_opt_in(monkeypatch):
    calls = []

    def fake_delegate_run(text, cwd=None, tools=None, timeout=None, role="default", skills=False):
        calls.append(tools)
        return {"ok": True, "output": "DONE", "error": ""}
    monkeypatch.setattr("oceano.delegate.run", fake_delegate_run)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "delegate", "text": "fix it", "write": "write"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert calls == ["Read,Glob,Grep,Write,Edit"]


def test_delegate_node_defaults_to_read_only(monkeypatch):
    calls = []

    def fake_delegate_run(text, cwd=None, tools=None, timeout=None, role="default", skills=False):
        calls.append(tools)
        return {"ok": True, "output": "DONE", "error": ""}
    monkeypatch.setattr("oceano.delegate.run", fake_delegate_run)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "delegate", "text": "look around"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert calls == ["Read,Glob,Grep"]


def test_norm_graph_write_field_is_explicit_opt_in_only():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "agent", "task": "t", "write": "write"},
        {"id": 2, "type": "agent", "task": "t"},                     # unset → read-only
        {"id": 3, "type": "agent", "task": "t", "write": "yes please"},   # junk → read-only, not silently on
        {"id": 4, "type": "delegate", "text": "t", "write": "write"},
        {"id": 5, "type": "delegate", "text": "t"},
    ], "edges": []})
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id[1]["write"] == "write"
    assert by_id[2]["write"] == ""
    assert by_id[3]["write"] == ""
    assert by_id[4]["write"] == "write"
    assert by_id[5]["write"] == ""


def test_norm_graph_accepts_the_shell_tier():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "agent", "task": "t", "write": "shell"},
        {"id": 2, "type": "delegate", "text": "t", "write": "shell"},
    ], "edges": []})
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id[1]["write"] == "shell"
    assert by_id[2]["write"] == "shell"


def test_tool_scope_for_the_three_access_tiers():
    assert workflows._tool_scope_for("") == "Read,Glob,Grep"
    assert workflows._tool_scope_for(None) == "Read,Glob,Grep"
    assert workflows._tool_scope_for("write") == "Read,Glob,Grep,Write,Edit"
    assert workflows._tool_scope_for("shell") == "Read,Glob,Grep,Write,Edit,Bash"


def test_access_marker_matches_the_tier():
    assert workflows._access_marker("") == ""
    assert workflows._access_marker("write") == " ✎"
    assert workflows._access_marker("shell") == " ⚠"


def test_agent_node_shell_tier_reaches_spawn_and_marks_the_label(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "X", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "t", "write": "shell", "label": "builder"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["tools"] == "Read,Glob,Grep,Write,Edit,Bash"
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["label"].endswith(" ⚠")


def test_delegate_node_shell_tier_reaches_delegate_run(monkeypatch):
    calls = []

    def fake_delegate_run(text, cwd=None, tools=None, timeout=None, role="default", skills=False):
        calls.append(tools)
        return {"ok": True, "output": "DONE", "error": ""}
    monkeypatch.setattr("oceano.delegate.run", fake_delegate_run)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "delegate", "text": "build and verify it", "write": "shell"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert calls == ["Read,Glob,Grep,Write,Edit,Bash"]


def test_orchestrated_agent_shell_tier_travels_with_its_own_node(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "R1", "error": ""},
                          2: {"state": "done", "output": "R2", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "reader", "label": "reader"},
         {"id": 3, "type": "agent", "task": "builder", "label": "builder", "write": "shell"},
         {"id": 4, "type": "orchestrate", "plan": {"2": 1, "3": 1}, "timeout": 5},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 4}, {"from": 2, "to": 4}, {"from": 3, "to": 4}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    by_task = {sp["task"]: sp["tools"] for sp in fake.spawned}
    assert by_task["reader"] == "Read,Glob,Grep"
    assert by_task["builder"] == "Read,Glob,Grep,Write,Edit,Bash"


# ---------------- regression: a local named `tools` must never shadow the tools MODULE ----------------
def test_tool_node_still_works_alongside_agent_and_delegate_nodes_in_same_run(monkeypatch):
    """Regression test for a real incident: assigning a local variable named `tools` anywhere in
    run() shadows the `oceano.tools` MODULE import for the WHOLE function (Python's per-function,
    not per-branch, scoping), raising UnboundLocalError the moment a 'tool' node tries to use it —
    even in a run that never executes an agent/delegate node. A workflow mixing a 'tool' node with
    'agent'/'delegate' nodes (write-access opt-in lives on the latter two) is exactly the shape that
    would blow up if this regresses."""
    monkeypatch.setattr("oceano.tools.is_enabled", lambda name: True)
    monkeypatch.setattr("oceano.tools.run", lambda name, args_json: "TOOL-OUT")
    fake = FakeAgentJobs({1: {"state": "done", "output": "AGENT-OUT", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    monkeypatch.setattr("oceano.delegate.run", lambda *a, **k: {"ok": True, "output": "DELEGATE-OUT", "error": ""})
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "tool", "tool": "list_files", "args": {}},
         {"id": 3, "type": "agent", "task": "t", "write": "write"},
         {"id": 4, "type": "await", "timeout": 5},
         {"id": 5, "type": "delegate", "text": "t", "write": "write"},
         {"id": 6, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4},
         {"from": 4, "to": 5}, {"from": 5, "to": 6}])
    rec = workflows.run(wf, trigger="manual", nested=True)     # would raise UnboundLocalError if regressed
    assert rec["status"] == "ok"
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[2]["output"] == "TOOL-OUT"
    assert "AGENT-OUT" in steps[4]["output"]
    assert steps[5]["output"] == "DELEGATE-OUT"


# ---------------- skill-reuse (never memory): agent/delegate/orchestrate nodes opt in ----------------
def test_agent_node_reaches_spawn_with_skills_true(monkeypatch):
    """A standalone agent node always asks agentjobs.spawn for skill-reuse (list_skills/load_skill),
    regardless of its file-access tier — see mindbridge._SCOPES / delegate._SKILLS_TOOLS."""
    fake = FakeAgentJobs({1: {"state": "done", "output": "X", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "t"},
         {"id": 3, "type": "await", "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["skills"] is True


def test_delegate_node_reaches_delegate_run_with_skills_true(monkeypatch):
    calls = []

    def fake_delegate_run(text, cwd=None, tools=None, timeout=None, role="default", skills=False):
        calls.append(skills)
        return {"ok": True, "output": "DONE", "error": ""}
    monkeypatch.setattr("oceano.delegate.run", fake_delegate_run)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "delegate", "text": "look around"},
         {"id": 3, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert calls == [True]


def test_orchestrated_agent_reaches_spawn_with_skills_true(monkeypatch):
    fake = FakeAgentJobs({1: {"state": "done", "output": "R1", "error": ""}})
    monkeypatch.setattr("oceano.agentjobs.spawn", fake.spawn)
    monkeypatch.setattr("oceano.agentjobs.status", fake.status)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "agent", "task": "scanner", "label": "scanner"},
         {"id": 3, "type": "orchestrate", "plan": {"2": 1}, "timeout": 5},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 3}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert fake.spawned[0]["skills"] is True
