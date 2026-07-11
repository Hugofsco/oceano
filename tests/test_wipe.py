"""Settings → Wipe for connected surfaces: mail accounts (credentials + arm windows must go)
and MCP servers (config cleared, live tools unregistered). The scheduler wipe lives in
test_scheduler.py alongside the rest of the task behavior.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import mail, mcp_client  # noqa: E402 - after the sys.path bootstrap


def test_mail_wipe_removes_all_accounts_and_their_arm_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STORE", tmp_path / "mail.json")
    monkeypatch.setattr(mail, "_ARM", {})
    a = mail.create("personal", "a@x.com", "imap.x.com", "smtp.x.com", password="pw")
    mail.create("work", "b@y.com", "imap.y.com", "smtp.y.com", password="pw2")
    mail.arm(a["id"])
    assert len(mail.list_all()) == 2 and mail.is_armed(a["id"])

    assert mail.wipe() == 2
    assert mail.list_all() == [] and not mail.is_armed(a["id"])
    assert "a@x.com" not in (tmp_path / "mail.json").read_text()   # credentials gone from disk
    assert mail.wipe() == 0                                        # idempotent


def test_mcp_wipe_clears_config_and_unregisters_live_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "CONFIG", tmp_path / "mcp.json")
    mcp_client._write_config([{"name": "linear", "url": "https://mcp.linear.app/sse", "enabled": True},
                              {"name": "local", "command": "some-mcp", "enabled": False}])
    unregistered = []
    monkeypatch.setattr("oceano.tools.unregister_prefix", unregistered.append)

    assert mcp_client.wipe() == 2
    assert mcp_client._read_config() == []
    assert json.loads((tmp_path / "mcp.json").read_text()) == {"servers": []}
    assert unregistered == ["mcp__linear__", "mcp__local__"]       # tools pulled from the agent
    assert mcp_client.wipe() == 0                                  # idempotent


def test_wipe_route_wires_the_new_targets(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from oceano import scheduler
    from oceano.web import routes_system
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    monkeypatch.setattr(mail, "STORE", tmp_path / "mail.json")
    monkeypatch.setattr(mcp_client, "CONFIG", tmp_path / "mcp.json")
    scheduler.add_task("0 8 * * *", "plain task")
    app = FastAPI()                                    # no middleware: exercise the handlers
    app.include_router(routes_system.router)
    c = TestClient(app)
    assert c.post("/api/wipe/tasks").json() == {"ok": True, "removed": 1, "what": "scheduled tasks"}
    assert c.post("/api/wipe/mcp").json()["what"] == "MCP servers"
    assert c.post("/api/wipe/mail").json()["what"] == "mail accounts"
    assert c.post("/api/wipe/nonsense").status_code == 400


def test_research_wipe_removes_topics_and_their_schedules_but_keeps_docs(tmp_path, monkeypatch):
    from oceano import researcher, scheduler
    monkeypatch.setattr(researcher, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    researcher.add_topic("solar sail propulsion", cron="0 8 * * *")
    researcher.add_topic("prediction markets", cron="0 9 * * *")
    doc = tmp_path / "workspace" / "research" / "solar-sail-propulsion.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# accumulated knowledge")
    assert len(researcher.all_topics()) == 2 and len(scheduler.all_tasks()) == 2

    assert researcher.wipe() == 2
    assert researcher.all_topics() == []
    assert scheduler.all_tasks() == []                 # the mirrored [ RESEARCH ] entries went too
    assert doc.read_text() == "# accumulated knowledge"          # the living doc is kept
    assert researcher.wipe() == 0                                # idempotent


def test_workflows_wipe_removes_definitions_runs_and_schedules(tmp_path, monkeypatch):
    from oceano import scheduler, workflows
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    a = workflows.create("digest", description="daily digest")
    workflows.create("triage", description="mail triage")
    workflows.set_schedule(a["id"], "0 7 * * *")       # one of them also runs on a cron
    assert len(workflows.list_all()) == 2 and len(scheduler.all_tasks()) == 1

    assert workflows.wipe() == 2
    assert workflows.list_all() == [] and workflows.runs() == []
    assert scheduler.all_tasks() == []                 # the mirrored schedule went too
    assert workflows.wipe() == 0                       # idempotent
