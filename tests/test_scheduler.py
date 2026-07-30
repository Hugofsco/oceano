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


def test_db_file_is_not_world_or_group_readable(tmp_path, monkeypatch):
    db_path = tmp_path / "tasks.db"
    monkeypatch.setattr(scheduler, "DB_PATH", db_path)
    scheduler._db()
    assert oct(db_path.stat().st_mode)[-3:] == "600"


def test_heartbeat_file_is_not_world_or_group_readable(tmp_path, monkeypatch):
    hb_path = tmp_path / "heartbeat"
    monkeypatch.setattr(scheduler, "HEARTBEAT", hb_path)
    scheduler.beat()
    assert oct(hb_path.stat().st_mode)[-3:] == "600"


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


def test_agent_tools_refuse_to_touch_managed_tasks(tmp_path, monkeypatch):
    """The agent's update_task/cancel_task must not reach source-tagged (managed) entries:
    pausing e.g. the nightly [ SELF ] reflection PERSISTS across restarts (the bootstrap only
    recreates missing tasks), so a bad turn — or injected text — could silently switch off the
    self-improvement loop. Only plain agent tasks are the agent's to manage."""
    from oceano.tools import sched as tools_sched
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    self_id = scheduler.add_task("30 23 * * *", "[ SELF ] Nightly reflection", source="self:reflect")
    plain_id = scheduler.add_task("0 8 * * *", "check the news")

    assert "refused" in tools_sched.update_task(self_id, enabled=False)
    assert "refused" in tools_sched.cancel_task(self_id)
    kept = next(t for t in scheduler.all_tasks() if t["id"] == self_id)
    assert kept["enabled"] is True                     # untouched: still there, still on

    assert "updated" in tools_sched.update_task(plain_id, enabled=False)
    assert "cancelled" in tools_sched.cancel_task(plain_id)

    # the user's own paths: toggling/retiming a managed task from the Scheduler UI stays fine
    assert scheduler.update_task(self_id, enabled=False) is True


def test_self_reflection_is_delete_protected_for_everyone(tmp_path, monkeypatch):
    """[ SELF ] is the SOLE producer of the suggestions queue — deleting it starves the queue
    silently. No path may delete it (UI, agent, owner modules); toggling OFF is the sanctioned
    way to stop it, and the Suggestions panel warns about that state."""
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    self_id = scheduler.add_task("30 23 * * *", "[ SELF ] Nightly reflection", source="self:reflect")
    other_id = scheduler.add_task("0 5 * * *", "[ SKILLS ] Evaluate", source="skills:eval")

    assert scheduler.delete_task(self_id) is False
    assert scheduler.delete_task(self_id, allow_managed=True) is False    # no bypass
    assert any(t["id"] == self_id for t in scheduler.all_tasks())
    assert scheduler.update_task(self_id, enabled=False) is True          # OFF stays allowed

    assert scheduler.delete_task(other_id) is True     # other built-ins: deletable, self-healing
                                                       # (recreated by ensure_*() on restart)


def test_dispatch_follows_primary_intelligence_when_task_has_no_model_override(monkeypatch):
    """A scheduled task left on "default" (no per-task model override) must follow whatever
    mind is configured as the PRIMARY INTELLIGENCE — same as an un-pinned workflow step — not
    silently boot the local resident model. Regression test: _dispatch's else-branch used to
    call plain Agent().run() unconditionally, ignoring delegate.get_mind() entirely, so a task
    left on "default" ran locally even with Claude/Codex set as primary."""
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)

    from oceano import delegate
    monkeypatch.setattr(delegate, "get_mind", lambda: "claude")
    monkeypatch.setattr(delegate, "available", lambda: True)

    calls = []

    class FakeAgent:
        def __init__(self, **kw):
            self.kw = kw

        def run_claude(self, instruction, cancel=None):
            calls.append("claude")
            return "ran via claude mind"

        def run_codex(self, instruction, cancel=None):
            calls.append("codex")
            return "ran via codex mind"

        def run(self, instruction, cancel=None):
            calls.append("local")
            return "ran via local model"

    monkeypatch.setattr("oceano.agent.Agent", FakeAgent)

    answer = scheduler._dispatch(None, "check the news", model=None)

    assert calls == ["claude"]
    assert answer == "ran via claude mind"


def test_wipe_removes_plain_tasks_and_keeps_managed(tmp_path, monkeypatch):
    """Settings → Wipe → Scheduled tasks clears what you/the agent scheduled, but never the
    source-tagged entries — maintenance built-ins and researcher/workflow schedules belong to
    their owner features (and [ SELF ] is delete-protected besides)."""
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    scheduler.add_task("0 8 * * *", "plain repeating task")
    scheduler.add_task("", "one-shot reminder", run_once_at="2026-07-12 09:00")
    scheduler.add_task("30 23 * * *", "[ SELF ] reflection", source="self:reflect")
    scheduler.add_task("0 5 * * *", "[ SKILLS ] evaluate", source="skills:eval")
    scheduler.add_task("0 9 * * *", "research topic", source="research:3")
    assert scheduler.wipe() == 2
    assert {t["source"] for t in scheduler.all_tasks()} == {"self:reflect", "skills:eval", "research:3"}
    assert scheduler.wipe() == 0                       # idempotent
