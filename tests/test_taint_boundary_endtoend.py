"""End-to-end taint boundary: the gaps a review found that 724 passing tests did not cover.

Each of these existed because the earlier tests asserted the FIX rather than the PROPERTY:

  1. Agent._prepare_turn was made conditional on trusted_origin, but run()/run_stream() still reset
     unconditionally in their `finally`. A derived turn inherited taint on the way IN and wiped it on
     the way OUT — so in a multi-node email workflow, one harmless instruction node cleared the taint
     for every node after it. The old test only called _prepare_turn, never run().

  2. delegate_tool()/spawn_agent() were gated, but the workflow delegate/agent NODES call
     delegate.run()/agentjobs.spawn() directly, so a triggered workflow reached autonomous execution
     without passing spawn_blocked().

  3. guarded_request mounted the pinned adapter on the request's own scheme only, leaving the
     session's default unpinned adapter live for the other scheme — so an https→http redirect went
     out unvalidated.

  5. The bridge taint was one process-wide bool, so any concurrent turn's reset cleared it out from
     under a still-tainted resident turn. That UNDER-blocks, contrary to the old comment.

These test behaviour at the boundary, not the shape of the patch.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import safety  # noqa: E402
from oceano.agent import Agent  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    safety.reset_untrusted()
    safety._bridge_seen.clear()
    yield
    safety.reset_untrusted()
    safety._bridge_seen.clear()


# ---------------- 1. the exit path ----------------
def _stub_run(ag):
    """Replace the model call so run() exercises only the turn lifecycle."""
    ag._run = lambda *a, **kw: "done"
    return ag


def test_a_derived_turn_does_not_clear_taint_when_it_FINISHES():
    ag = _stub_run(Agent(learn=False, inject_context=False, trusted_origin=False))
    safety.wrap_untrusted("web", "injected email body")
    ag.run("summarise this")
    assert safety.untrusted_seen() is True, (
        "a derived turn must not wipe its parent chain's taint on exit — the next workflow node "
        "would then run clean")


def test_a_trusted_turn_still_clears_taint_when_it_finishes():
    ag = _stub_run(Agent(learn=False, inject_context=False))          # trusted_origin=True
    safety.wrap_untrusted("web", "page read during the turn")
    ag.run("a real user's message")
    assert safety.untrusted_seen() is False, "a user's turn must still end clean"


def test_consecutive_derived_turns_keep_the_taint_across_the_whole_chain():
    """The multi-node workflow shape: node 1 is harmless, node 2 wants the shell."""
    from oceano.tools import shell
    safety.wrap_untrusted("workflow-trigger:email", "attacker-authored body")
    for _ in range(3):                                                # three instruction nodes
        _stub_run(Agent(learn=False, inject_context=False, trusted_origin=False)).run("a step")
        assert safety.untrusted_seen() is True
    assert shell._shell_blocked() is not None, "the last node must still be refused the shell"


# ---------------- 2. workflow nodes reach the spawn gate ----------------
def test_workflow_delegate_and_agent_nodes_consult_the_spawn_gate():
    """They call delegate.run()/agentjobs.spawn() directly rather than the gated tools, so the check
    has to be at the node. Asserted at the source: a new node that forgets it reopens the hole and no
    unit test would notice."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent.joinpath("oceano/workflows.py").read_text()
    for marker in ('elif ok and t == "delegate":', 'elif ok and t == "agent":'):
        assert marker in src, f"node dispatch moved: {marker!r}"
        block = src[src.index(marker):src.index(marker) + 1200]
        assert "safety.spawn_blocked()" in block, (
            f"the {marker!r} node must consult spawn_blocked() before starting autonomous work")
    # Every real (non-comment) call site must have a spawn_blocked() guard close above it. This is
    # what caught the decision-node and orchestrator sites that the first pass missed.
    lines = src.split("\n")
    for i, ln in enumerate(lines):
        code = ln.split("#", 1)[0]
        if "delegate.run(" not in code and "agentjobs.spawn(" not in code:
            continue
        window = "\n".join(lines[max(0, i - 25):i])
        assert "spawn_blocked()" in window, (
            f"unguarded autonomous-execution call at workflows.py:{i + 1}: {ln.strip()[:70]}")


# ---------------- 3. cross-scheme redirects stay pinned ----------------
def test_the_pinned_adapter_covers_both_schemes():
    import requests
    sess = requests.Session()
    for scheme in ("http", "https"):
        sess.mount(scheme + "://", safety._PinnedAdapter("example.com", "93.184.216.34", scheme=scheme))
    for prefix in ("http://", "https://"):
        assert type(sess.adapters[prefix]).__name__ == "_PinnedAdapter", (
            f"{prefix} left on the default unpinned adapter — an https→http redirect escapes")


def test_guarded_request_mounts_both_schemes(monkeypatch):
    """Exercise the real guarded_request wiring rather than a hand-built session."""
    seen = {}
    monkeypatch.setattr(safety, "_safe_ip", lambda host: "93.184.216.34")

    class _Sess:
        adapters = {}

        def mount(self, prefix, adapter):
            seen[prefix] = type(adapter).__name__

        def request(self, *a, **kw):
            raise RuntimeError("stop before the network")

        def close(self):
            pass

    monkeypatch.setattr(safety.requests, "Session", lambda: _Sess())
    with pytest.raises(RuntimeError):
        safety.guarded_request("GET", "https://example.com/")
    assert seen == {"http://": "_PinnedAdapter", "https://": "_PinnedAdapter"}, seen


def test_a_cross_host_redirect_still_fails_closed():
    adapter = safety._PinnedAdapter("example.com", "93.184.216.34", scheme="http")

    class _Req:
        url = "http://127.0.0.1:8899/"
        headers = {}

    with pytest.raises(safety.Blocked):
        adapter.send(_Req())


# ---------------- 5. the bridge taint is per-session ----------------
def test_one_session_reset_does_not_clear_another_sessions_bridge_taint():
    safety.mark_bridge_untrusted("resident-turn-A")
    safety.reset_bridge_untrusted("user-turn-B")                     # a concurrent, unrelated turn
    assert safety.bridge_untrusted_seen("resident-turn-A") is True, (
        "a concurrent turn's reset must not clear a live resident turn's taint — that under-blocks")
    assert safety.bridge_untrusted_seen("user-turn-B") is False


def test_a_session_clears_its_own_bridge_taint():
    safety.mark_bridge_untrusted("s1")
    assert safety.bridge_untrusted_seen("s1") is True
    safety.reset_bridge_untrusted("s1")
    assert safety.bridge_untrusted_seen("s1") is False


def test_concurrent_turns_do_not_race_each_other_clean():
    """The real shape: a resident turn holds taint while user turns start and finish around it."""
    safety.mark_bridge_untrusted("resident")
    errors = []

    def churn(i):
        try:
            for _ in range(50):
                safety.mark_bridge_untrusted(f"user-{i}")
                safety.reset_bridge_untrusted(f"user-{i}")
        except Exception as e:                                        # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert safety.bridge_untrusted_seen("resident") is True, "the resident turn was raced clean"


def test_agent_turn_boundaries_clear_only_their_own_session():
    ag = _stub_run(Agent(learn=False, inject_context=False))
    ag.session_id = "chat-1"
    safety.mark_bridge_untrusted("chat-2")                            # another conversation, tainted
    ag.run("hello")                                                   # trusted turn: clears chat-1 only
    assert safety.bridge_untrusted_seen("chat-2") is True
    assert safety.bridge_untrusted_seen("chat-1") is False


# ---------------- 4. browser_fill is egress ----------------
def test_browser_fill_is_gated_as_egress(monkeypatch):
    def _tripwire(*a, **kw):
        raise AssertionError("reached the browser — the gate did not fire")

    monkeypatch.setattr("oceano.tools.browsing.live_browser_available", _tripwire)
    safety.wrap_untrusted("web", "injected page asking for the conversation in a form")
    from oceano.tools import browsing
    out = browsing.browser_fill("q", "everything the user said", enter=True)
    assert "Blocked for safety" in out


# ---------------- taint scoping: contained in the run, propagated by the tool ----------------
def _tiny_wf():
    return {"id": 1, "name": "t", "graph": {
        "nodes": [{"id": 1, "type": "start"}, {"id": 2, "type": "end"}],
        "edges": [{"from": 1, "to": 2}]}}


def test_an_untrusted_trigger_run_does_not_leak_taint_into_the_calling_thread(monkeypatch, tmp_path):
    """webhook_run_sync executes inline on a FastAPI threadpool thread, and those threads are
    REUSED — a run's taint must not gate the next unrelated request on that thread."""
    from oceano import workflows
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "runs.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)

    assert safety.untrusted_seen() is False
    rec = workflows.run(_tiny_wf(), trigger="email", inp="attacker body")
    assert rec.get("tainted") is True, "the run itself must have been tainted by the email trigger"
    assert safety.untrusted_seen() is False, "but that taint must not escape into the caller's context"


def test_a_tainted_run_still_taints_a_CALLING_agent_via_the_tool_output(monkeypatch):
    """Containment must not silently drop the propagation: run_workflow fences a tainted run's step
    output, which is the same path every other content-returning tool uses."""
    from oceano import workflows
    from oceano.tools import sched
    monkeypatch.setattr(workflows, "get_by_name", lambda n: _tiny_wf())
    monkeypatch.setattr(workflows, "run",
                        lambda wf, **kw: {"summary": "ok", "steps": [], "tainted": True})
    out = sched._run_one_workflow("t")
    assert "<untrusted" in out, "a tainted run's output must reach the caller fenced"
    assert safety.untrusted_seen() is True, "and must taint the calling turn"


def test_a_clean_run_does_not_fence_or_taint_the_caller(monkeypatch):
    from oceano import workflows
    from oceano.tools import sched
    monkeypatch.setattr(workflows, "get_by_name", lambda n: _tiny_wf())
    monkeypatch.setattr(workflows, "run",
                        lambda wf, **kw: {"summary": "ok", "steps": [], "tainted": False})
    out = sched._run_one_workflow("t")
    assert "<untrusted" not in out
    assert safety.untrusted_seen() is False, "no false positives — a clean run stays clean"


def test_a_nested_subworkflow_hands_its_taint_up_to_the_parent_run(monkeypatch, tmp_path):
    """Containment is per outermost run; a sub-workflow node must not be able to launder by nesting."""
    from oceano import workflows
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "runs.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    workflows.run(_tiny_wf(), trigger="email", inp="x", nested=True)
    assert safety.untrusted_seen() is True, "a nested run must propagate taint to its parent run"
