import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from oceano import tools
from oceano.tools import selfimprove
from oceano.web import state


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        ("read", "Read,Glob,Grep"),
        ("write", "Read,Glob,Grep,Write,Edit"),
        ("shell", "Read,Glob,Grep,Write,Edit,Bash"),
    ],
)
def test_chat_spawn_uses_configured_access(monkeypatch, tmp_path, access, expected):
    seen = {}

    def fake_spawn(task, **kwargs):
        seen.update(kwargs)
        return {"id": 7, "provider": "codex", "label": "worker"}

    monkeypatch.setattr("oceano.agentjobs.spawn", fake_spawn)
    monkeypatch.setattr("oceano.mindbridge.active_session", lambda: "chat-123")
    monkeypatch.setattr(state, "load", lambda: {"prefs": {"chat_agent_access": access}})

    with tools.background_workspace(tmp_path):
        result = selfimprove.spawn_agent("inspect the workspace")

    assert "started agent #7" in result
    assert "does not complete the parent turn" in result
    assert "proper progress response" in result
    assert seen["tools"] == expected
    assert seen["sid"] == "chat-123"
    assert seen["cwd"] == tmp_path.resolve()


def test_delegate_tool_uses_the_current_turn_workspace(monkeypatch, tmp_path):
    observed = {}

    def fake_run(instructions, **kwargs):
        observed.update(kwargs)
        return {"ok": True, "output": "done"}

    monkeypatch.setattr("oceano.delegate.run", fake_run)
    with tools.background_workspace(tmp_path):
        assert selfimprove.delegate_tool("inspect") == "done"
    assert observed["cwd"] == tmp_path.resolve()


def test_web_preferences_seed_and_repair_chat_agent_access(tmp_path, monkeypatch):
    store = tmp_path / "web.json"
    monkeypatch.setattr(state, "STORE", store)

    assert state.load()["prefs"]["chat_agent_access"] == "read"

    data = json.loads(store.read_text())
    data["prefs"]["chat_agent_access"] = "anything"
    store.write_text(json.dumps(data))

    assert state.load()["prefs"]["chat_agent_access"] == "read"
    assert json.loads(store.read_text())["prefs"]["chat_agent_access"] == "read"
