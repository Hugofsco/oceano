"""Tests for agentjobs: the background SUB-AGENT registry (spawn_agent) — bgjobs' sibling,
where the job is a daemon thread running a contained agent instead of an OS process. Providers
are stubbed via _dispatch so nothing real runs; notify/log side effects are neutralized so
these tests never push to the user's actual phone/Telegram.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import agentjobs  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(agentjobs, "STATE_PATH", tmp_path / "agentjobs.json")
    monkeypatch.setattr(agentjobs, "LOG_DIR", tmp_path / "agent-logs")
    monkeypatch.setattr("oceano.scheduler.notify", lambda *a, **k: None)   # never push for real
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    monkeypatch.setattr(agentjobs, "_on_complete", None)
    agentjobs._jobs.clear()
    yield
    agentjobs._jobs.clear()


def _wait_for(aid, states, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = agentjobs.status(aid)
        if rec and rec["state"] in states:
            return rec
        time.sleep(0.05)
    raise AssertionError(f"agent {aid} never reached {states}: {agentjobs.status(aid)}")


def _stub(result=None, delay=0.0, progress=None):
    """A fake provider dispatch: optionally emits progress, sleeps, returns `result`."""
    def dispatch(provider, task, tools, timeout, cwd, on_progress):
        if progress:
            for ev in progress:
                on_progress(ev)
        if delay:
            time.sleep(delay)
        return result or {"ok": True, "output": f"did: {task}", "error": ""}
    return dispatch


def test_lifecycle_done_and_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(agentjobs, "_dispatch",
                        _stub(progress=[{"kind": "tool", "tool": "read_file", "detail": "notes.md"}]))
    rec = agentjobs.spawn("summarize notes", provider="api", label="sum")
    assert rec["state"] == "running" and rec["provider"] == "api"
    done = _wait_for(rec["id"], {"done", "failed"})
    assert done["state"] == "done" and done["ok"] and done["output"] == "did: summarize notes"
    assert "read_file" in done["tail"]                       # progress landed in the log
    assert (tmp_path / "agentjobs.json").exists()            # persisted


def test_failure_is_a_failed_agent_not_a_dead_thread(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(agentjobs, "_dispatch", boom)
    rec = agentjobs.spawn("t", provider="api")
    done = _wait_for(rec["id"], {"done", "failed"})
    assert done["state"] == "failed" and "provider exploded" in done["error"]


def test_concurrency_cap_refuses(monkeypatch):
    monkeypatch.setattr(agentjobs, "MAX_AGENTS", 1)
    monkeypatch.setattr(agentjobs, "_dispatch", _stub(delay=2.0))
    rec = agentjobs.spawn("slow one", provider="api")
    with pytest.raises(RuntimeError, match="agent limit reached"):
        agentjobs.spawn("second", provider="api")
    _wait_for(rec["id"], {"done"})
    agentjobs.spawn("third", provider="api")                 # slot freed → allowed again


def test_local_single_slot_and_warning(monkeypatch):
    monkeypatch.setattr(agentjobs, "_dispatch", _stub(delay=2.0))
    rec = agentjobs.spawn("local one", provider="local")
    assert "resident model" in rec["warning"]
    with pytest.raises(RuntimeError, match="LOCAL agent is already running"):
        agentjobs.spawn("local two", provider="local")
    agentjobs.spawn("cloud is fine", provider="api")         # non-local unaffected by the local slot
    _wait_for(rec["id"], {"done"})


def test_unknown_provider_refused(monkeypatch):
    monkeypatch.setattr(agentjobs, "_dispatch", _stub())
    with pytest.raises(RuntimeError, match="unknown provider"):
        agentjobs.spawn("t", provider="gpt-o-matic")


def test_api_dispatch_excludes_recursion(monkeypatch):
    """A spawned api agent must be built WITHOUT spawn/delegate/workflow tools."""
    seen = {}

    def fake_to_api(task, cwd=None, tools=None, timeout=600, on_progress=None, exclude=None):
        seen["exclude"] = exclude
        return {"ok": True, "output": "x", "error": ""}
    monkeypatch.setattr("oceano.delegate.to_api", fake_to_api)
    rec = agentjobs.spawn("t", provider="api")
    _wait_for(rec["id"], {"done"})
    assert seen["exclude"] >= {"spawn_agent", "delegate", "delegate_to_claude", "run_workflow"}


def test_local_runs_under_the_serialization_gate(monkeypatch):
    """The local provider must acquire jobs.job(gate=True) — the one-resident-model queue."""
    gates = []
    from oceano import jobs as jobs_mod
    real_job = jobs_mod.job

    def spy_job(kind, label="", ref=None, gate=None):
        gates.append((kind, gate))
        return real_job(kind, label=label, ref=ref, gate=gate)
    monkeypatch.setattr("oceano.jobs.job", spy_job)
    monkeypatch.setattr("oceano.delegate.resolve_primary",
                        lambda: {"model": "m", "base_url": "", "api_key": "", "source": "primary"})

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run(self, task, deadline=None):
            return "local says hi"
    monkeypatch.setattr("oceano.agent.Agent", FakeAgent)
    rec = agentjobs.spawn("t", provider="local")
    done = _wait_for(rec["id"], {"done", "failed"})
    assert done["state"] == "done" and done["output"] == "local says hi"
    assert ("agent", True) in gates


def test_on_complete_hook_fires_exactly_once(monkeypatch):
    fired = []
    monkeypatch.setattr(agentjobs, "_dispatch", _stub())
    agentjobs.set_on_complete(lambda rec: fired.append(rec["id"]))
    rec = agentjobs.spawn("t", provider="api", sid="chatX")
    _wait_for(rec["id"], {"done"})
    time.sleep(0.1)
    assert fired == [rec["id"]]


def test_pending_and_mark_delivered(monkeypatch):
    monkeypatch.setattr(agentjobs, "_dispatch", _stub())
    rec = agentjobs.spawn("t", provider="api", sid="chatY")
    _wait_for(rec["id"], {"done"})
    pend = agentjobs.pending_for("chatY")
    assert [p["id"] for p in pend] == [rec["id"]]
    assert agentjobs.mark_delivered(rec["id"]) is True
    assert agentjobs.mark_delivered(rec["id"]) is False      # idempotent
    assert agentjobs.pending_for("chatY") == []


def test_reconcile_marks_running_lost():
    """A record left 'running' by a previous daemon process is honestly lost — threads
    don't survive restarts."""
    agentjobs._jobs[99] = {"id": 99, "label": "orphan", "task": "t", "provider": "api",
                           "state": "running", "started": time.time() - 60, "ended": None,
                           "ok": None, "output": "", "error": "", "log_path": "", "sid": None,
                           "delivered": False, "warning": ""}
    agentjobs._reconcile()
    assert agentjobs.status(99)["state"] == "lost"


def test_concurrent_spawns_thread_safe(monkeypatch):
    """The cap check and insert happen under one lock — hammering spawn from many threads
    must never exceed MAX_AGENTS running at once."""
    monkeypatch.setattr(agentjobs, "MAX_AGENTS", 3)
    monkeypatch.setattr(agentjobs, "_dispatch", _stub(delay=1.0))
    started, refused = [], []

    def go(i):
        try:
            started.append(agentjobs.spawn(f"t{i}", provider="api")["id"])
        except RuntimeError:
            refused.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(started) == 3 and len(refused) == 5
