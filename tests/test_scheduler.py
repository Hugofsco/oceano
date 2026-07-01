"""Behavioral tests for the scheduler: one-shot tasks, cron due-logic, and the
timezone-aware date parsing. Pure-function tests need no DB; the few that exercise
storage are pinned to a temp DB via monkeypatch so they never touch data/tasks.db.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import scheduler  # noqa: E402 - after the sys.path bootstrap


def test_parse_when_accepts_local_and_iso_rejects_junk():
    assert scheduler._parse_when("2026-07-01 15:00") is not None
    assert scheduler._parse_when("2026-07-01T15:00:00+00:00") is not None
    assert scheduler._parse_when("not a date") is None
    # a bare time gets the schedule timezone attached (so it's an absolute instant)
    assert scheduler._parse_when("2026-07-01 15:00").tzinfo is not None


def test_one_shot_due_only_after_its_time():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    assert scheduler.is_due("", None, now=now, run_once_at=past) is True
    assert scheduler.is_due("", None, now=now, run_once_at=future) is False


def test_cron_due_logic():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    # ran a day ago, fires every minute → due now
    assert scheduler.is_due("* * * * *", (now - timedelta(days=1)).isoformat(), now=now) is True
    # daily 08:00, already ran at 08:00 today → next fire is tomorrow → not due
    last = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc).isoformat()
    assert scheduler.is_due("0 8 * * *", last, now=now) is False


def test_schedule_one_shot_creates_pending_disabling_task(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    msg = scheduler.schedule_task("", "ping once", run_once_at="2030-01-01 09:00")
    assert "one-shot" in msg
    tasks = scheduler.all_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["run_once_at"] and not t["cron"]          # one-shot: time set, cron empty
    assert t["next_run"] == t["run_once_at"]
    # a far-future one-shot is not yet due
    assert scheduler.is_due(t["cron"], t["last_run"], run_once_at=t["run_once_at"]) is False


def test_schedule_cron_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    assert "invalid cron" in scheduler.schedule_task("not a cron", "x")
    assert "scheduled" in scheduler.schedule_task("0 8 * * *", "y")


def test_run_status_is_recorded_and_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    tid = scheduler.add_task("0 8 * * *", "nightly thing")
    scheduler._set_run_status(tid, "error", "BoomError: kaboom")
    listing = scheduler.list_tasks()
    assert "last run FAILED" in listing
    assert scheduler.all_tasks()[0]["last_status"] == "error"
