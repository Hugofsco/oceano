import json
import stat

from oceano import turncheckpoints
from oceano.agent_runtime import TaskSpec, TurnBudget, TurnState
from oceano.tools.core import ToolResult


def test_checkpoint_recovers_effects_without_conversation_content(tmp_path, monkeypatch):
    store = tmp_path / "checkpoints.json"
    monkeypatch.setattr(turncheckpoints, "STORE", store)
    state = TurnState("PRIVATE PROMPT", None, set(), TaskSpec(True, True), TurnBudget.create(3))
    key = turncheckpoints.begin("private-session", "codex", state.task)
    state.on_change = lambda current: turncheckpoints.update(key, current)
    state.record("write_file", ToolResult(
        True, summary="PRIVATE RESULT", side_effects=("file:app.py",)),
        {"path": "app.py", "content": "PRIVATE ARGUMENT"})
    state.record("run_tests", ToolResult(
        False, error="PRIVATE ERROR", retryable=True, code="tests_failed"),
        {"path": "."})

    raw = store.read_text()
    note = turncheckpoints.recovery_note("private-session", "codex")
    assert "write_file" in note and "run_tests:tests_failed" in note
    assert "file:app.py" in note
    for private in ("PRIVATE PROMPT", "PRIVATE RESULT", "PRIVATE ARGUMENT", "PRIVATE ERROR", "private-session"):
        assert private not in raw
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert turncheckpoints.status()["recoverable"] == 1
    assert turncheckpoints.clear(key) is True
    assert turncheckpoints.status()["recoverable"] == 0


def test_checkpoint_data_is_structured_and_content_free():
    state = TurnState("secret", None, set(), TaskSpec(), TurnBudget.create(2))
    state.record("run_shell", ToolResult(True, summary="output", verification=("exit:0",)),
                 {"command": "secret command"})
    data = state.checkpoint_data()
    serialized = json.dumps(data)
    assert "secret" not in serialized and "output" not in serialized
    assert data["verification"] == ["exit:0"]
