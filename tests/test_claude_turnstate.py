from oceano import delegate, mindbridge, tools
from oceano.agent import Agent


def _agent(monkeypatch):
    instance = Agent(model="fixture", learn=False)
    monkeypatch.setattr(instance, "_learn", lambda *args: None)

    def prepare(_message, voice=False):
        instance._turn_plan = {"requires_action": True, "verify_code": True}

    monkeypatch.setattr(instance, "_prepare_turn", prepare)
    monkeypatch.setattr(mindbridge, "mcp_config_path", lambda *args, **kwargs: None)
    return instance


def test_claude_parallel_events_feed_turn_state_by_tool_use_id(tmp_path, monkeypatch):
    agent = _agent(monkeypatch)
    observed = {}
    recorded = []
    monkeypatch.setattr("oceano.agent.traces.record", lambda event, **data: recorded.append((event, data)))

    def fake_run(prompt, cwd=None, cancel=None, on_progress=None, **kwargs):
        observed["cwd"] = cwd
        on_progress({"kind": "tool", "tool": "Write", "detail": "app.py",
                     "args": {"file_path": "app.py", "content": "print(1)"},
                     "tool_use_id": "write-1"})
        on_progress({"kind": "tool", "tool": "Bash", "detail": "python app.py",
                     "args": {"command": "python app.py"}, "tool_use_id": "bash-1"})
        # Reverse result order to prove correlation does not depend on adjacency/FIFO.
        on_progress({"kind": "tool_result", "text": "1", "tool_use_id": "bash-1",
                     "is_error": False})
        on_progress({"kind": "tool_result", "text": "File created", "tool_use_id": "write-1",
                     "is_error": False})
        on_progress({"kind": "text", "text": "Implemented and verified."})
        return {"ok": True, "output": "Implemented and verified."}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    with tools.background_workspace(tmp_path):
        events = list(agent._claude_mind_stream("Implement and test a small Python project"))
    assert observed["cwd"] == tmp_path.resolve()
    assert agent.last_mind_error is None
    results = [event["name"] for event in events if event.get("type") == "tool_result"]
    assert results == ["Bash", "Write"]
    resident = next(data for event, data in recorded if event == "resident_turn")
    assert resident["mind"] == "claude"
    assert resident["used_tools"] == ["run_shell", "write_file"]
    assert resident["errors"] == 0 and resident["side_effect_count"] == 2


def test_claude_structured_tool_failure_marks_turn_incomplete(monkeypatch):
    agent = _agent(monkeypatch)

    def fake_run(prompt, on_progress=None, **kwargs):
        on_progress({"kind": "tool", "tool": "Bash", "detail": "false",
                     "args": {"command": "false"}, "tool_use_id": "bash-1"})
        on_progress({"kind": "tool_result", "text": "permission denied",
                     "tool_use_id": "bash-1", "is_error": True})
        on_progress({"kind": "text", "text": "Could not complete it."})
        return {"ok": True, "output": "Could not complete it."}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    list(agent._claude_mind_stream("Implement and test a small Python project"))
    assert "at least one tool returned an error" in agent.last_mind_error


def test_claude_missing_tool_result_is_not_treated_as_success(monkeypatch):
    agent = _agent(monkeypatch)

    def fake_run(prompt, on_progress=None, **kwargs):
        on_progress({"kind": "tool", "tool": "Write", "detail": "app.py",
                     "args": {"file_path": "app.py", "content": "x"},
                     "tool_use_id": "write-1"})
        on_progress({"kind": "text", "text": "Done."})
        return {"ok": True, "output": "Done."}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    list(agent._claude_mind_stream("Implement and test a small Python project"))
    assert "at least one tool returned an error" in agent.last_mind_error
