"""Tests for bgjobs: the OS-process registry that lets spawn_job survive past the CLI turn
that started it (unlike the mind's own native background execution, which dies with it).
spawn() -> _reap() runs on a background thread, so most assertions poll with a bounded
timeout rather than sleeping a fixed amount. Real notify/log side effects are neutralized —
these tests must never send a push to the user's actual phone/Telegram.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import bgjobs  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(bgjobs, "STATE_PATH", tmp_path / "bgjobs.json")
    monkeypatch.setattr(bgjobs, "LOG_DIR", tmp_path / "job-logs")
    monkeypatch.setattr("oceano.scheduler.notify", lambda *a, **k: None)   # never push a real notification
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    bgjobs._jobs.clear()
    yield


def _wait_for(jid, states, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = bgjobs.status(jid)
        if rec and rec["state"] in states:
            return rec
        time.sleep(0.05)
    raise AssertionError(f"job {jid} never reached {states}: {bgjobs.status(jid)}")


def test_spawn_successful_command_completes(tmp_path):
    rec = bgjobs.spawn(["bash", "-c", "echo hi"], cwd=str(tmp_path), display="echo hi", label="greet")
    done = _wait_for(rec["id"], {"done", "failed"})
    assert done["state"] == "done"
    assert done["exit_code"] == 0
    assert "hi" in done["tail"]


def test_spawn_failing_command_reports_exit_code(tmp_path):
    rec = bgjobs.spawn(["bash", "-c", "exit 3"], cwd=str(tmp_path), display="exit 3")
    failed = _wait_for(rec["id"], {"done", "failed"})
    assert failed["state"] == "failed"
    assert failed["exit_code"] == 3


def test_status_lists_all_jobs_when_no_id(tmp_path):
    rec = bgjobs.spawn(["bash", "-c", "true"], cwd=str(tmp_path), display="true")
    _wait_for(rec["id"], {"done", "failed"})
    js = bgjobs.status()
    assert any(j["id"] == rec["id"] for j in js)


def test_status_unknown_id_returns_none():
    assert bgjobs.status(999999999) is None


def test_alive_distinguishes_pid_reuse():
    me = os.getpid()
    ticks = bgjobs._proc_start_ticks(me)
    assert ticks is not None
    assert bgjobs._alive(me, ticks) is True
    assert bgjobs._alive(me, ticks + 999999) is False   # wrong start time -> not "our" process
    assert bgjobs._alive(None, None) is False


def test_reconcile_marks_dead_pid_as_lost(tmp_path):
    dead_pid = 999999999      # not a real pid on any normal box
    log_path = tmp_path / "42.log"
    log_path.write_text("partial output\n")
    bgjobs._jobs[42] = {"id": 42, "label": "orphan", "command": "sleep 999", "pid": dead_pid,
                        "start_ticks": 12345, "state": "running", "started": time.time(),
                        "ended": None, "exit_code": None, "log_path": str(log_path)}
    bgjobs._reconcile()
    assert bgjobs._jobs[42]["state"] == "lost"


def test_reconcile_reattaches_watcher_for_a_live_pid():
    me = os.getpid()
    ticks = bgjobs._proc_start_ticks(me)
    bgjobs._jobs[7] = {"id": 7, "label": "still-alive", "command": "n/a", "pid": me,
                      "start_ticks": ticks, "state": "running", "started": time.time(),
                      "ended": None, "exit_code": None, "log_path": "", "sid": None, "delivered": False}
    bgjobs._reconcile()
    time.sleep(0.1)                    # let the spawned watcher thread take its first look
    assert bgjobs._jobs[7]["state"] == "running"   # a real, live pid must NOT be marked lost


def test_completed_job_is_pending_for_its_conversation_then_ackable(tmp_path):
    rec = bgjobs.spawn(["bash", "-c", "echo done-in-chat"], cwd=str(tmp_path),
                       display="echo done-in-chat", label="build", sid="chat-42")
    _wait_for(rec["id"], {"done", "failed"})
    pend = bgjobs.pending_for("chat-42")
    assert [p["id"] for p in pend] == [rec["id"]]
    assert "done-in-chat" in pend[0]["tail"]
    assert bgjobs.pending_for("other-chat") == []          # scoped to the spawning conversation
    assert bgjobs.mark_delivered(rec["id"]) is True
    assert bgjobs.pending_for("chat-42") == []             # delivered → no longer pending
    assert bgjobs.mark_delivered(rec["id"]) is False       # idempotent: never delivered twice


def test_on_complete_hook_fires_with_the_record(tmp_path):
    seen = []
    bgjobs.set_on_complete(lambda r: seen.append((r["id"], r["state"], r.get("sid"))))
    try:
        rec = bgjobs.spawn(["bash", "-c", "exit 0"], cwd=str(tmp_path), display="exit 0", sid="chat-9")
        _wait_for(rec["id"], {"done", "failed"})
        for _ in range(50):
            if seen:
                break
            time.sleep(0.05)
        assert seen and seen[0] == (rec["id"], "done", "chat-9")
    finally:
        bgjobs.set_on_complete(None)


def test_job_without_sid_is_never_pending(tmp_path):
    rec = bgjobs.spawn(["bash", "-c", "true"], cwd=str(tmp_path), display="true")   # no sid
    _wait_for(rec["id"], {"done", "failed"})
    assert bgjobs.pending_for(None) == []
    assert all(p["id"] != rec["id"] for p in bgjobs.pending_for(""))
