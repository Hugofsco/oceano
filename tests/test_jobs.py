"""oceano/jobs.py: the live job registry and its cancel-by-ref lookup (used by the workflow
pause endpoint, which knows a workflow id but not the job slot it landed in)."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import jobs  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_log(monkeypatch):
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)   # never touch data/logs.db
    yield


def test_cancel_by_ref_signals_the_matching_job():
    entered = threading.Event()
    with jobs.job("workflow", "t", ref="workflow:42") as jid:
        entered.set()
        ev = jobs.cancel_event(jid)
        assert not ev.is_set()
        assert jobs.cancel_by_ref("workflow:42") is True
        assert ev.is_set()


def test_cancel_by_ref_is_false_when_nothing_live_matches():
    assert jobs.cancel_by_ref("workflow:does-not-exist") is False
    with jobs.job("workflow", "t", ref="workflow:1"):
        assert jobs.cancel_by_ref("workflow:2") is False


def test_cancel_by_ref_only_matches_the_current_ref_not_a_stale_one():
    with jobs.job("workflow", "t", ref="workflow:7"):
        pass  # job already finished — its ref is no longer live
    assert jobs.cancel_by_ref("workflow:7") is False
