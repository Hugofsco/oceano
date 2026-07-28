from oceano import delegate, mindbridge, tools
from oceano.agent import Agent, _resident_body_note


def _agent(monkeypatch, tmp_path):
    from oceano import toolrouter
    config_path = tmp_path / "resident-tools.toml"
    config_path.write_text(
        '[surfaces.resident]\nmode = "hybrid"\nschema_budget = 1000\n'
        'max_schema_budget = 4000\ndiscovery = true\n')
    monkeypatch.setenv("OCEANO_TOOL_CONFIG", str(config_path))
    toolrouter._CACHE.update({"path": None, "mtime": None, "data": {}})
    instance = Agent(model="fixture", learn=False)
    monkeypatch.setattr(instance, "_learn", lambda *args: None)

    def prepare(_message, voice=False):
        instance._turn_plan = {"requires_action": True, "verify_code": True}

    monkeypatch.setattr(instance, "_prepare_turn", prepare)
    monkeypatch.setattr(mindbridge, "mcp_config_path", lambda *args, **kwargs: "/tmp/test-mcp.json")
    return instance


def test_claude_parallel_events_feed_turn_state_by_tool_use_id(tmp_path, monkeypatch):
    agent = _agent(monkeypatch, tmp_path)
    observed = {}
    recorded = []
    monkeypatch.setattr("oceano.agent.traces.record_global", lambda event, **data: recorded.append((event, data)))

    def fake_run(prompt, cwd=None, cancel=None, on_progress=None, **kwargs):
        observed["cwd"] = cwd
        observed["tools"] = kwargs.get("tools", "")
        observed["disallow"] = kwargs.get("disallow", "")
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
    assert "mcp__oceano__write_file" in observed["tools"]
    assert "mcp__oceano__*" in observed["tools"]
    assert "Write" in observed["disallow"] and "Bash" in observed["disallow"]
    assert agent.last_mind_error is None
    results = [event["name"] for event in events if event.get("type") == "tool_result"]
    assert results == ["Bash", "Write"]
    resident = next(data for event, data in recorded if event == "resident_turn")
    assert resident["mind"] == "claude"
    assert resident["used_tools"] == ["run_shell", "write_file"]
    assert resident["errors"] == 0 and resident["side_effect_count"] == 2


def test_claude_structured_tool_failure_marks_turn_incomplete(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)

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


def test_claude_missing_tool_result_is_not_treated_as_success(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)

    def fake_run(prompt, on_progress=None, **kwargs):
        on_progress({"kind": "tool", "tool": "Write", "detail": "app.py",
                     "args": {"file_path": "app.py", "content": "x"},
                     "tool_use_id": "write-1"})
        on_progress({"kind": "text", "text": "Done."})
        return {"ok": True, "output": "Done."}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    list(agent._claude_mind_stream("Implement and test a small Python project"))
    assert "at least one tool returned an error" in agent.last_mind_error


def test_resident_body_note_contains_only_the_active_catalog():
    note = _resident_body_note({"discover_tools", "calendar_events"}, "claude")
    assert "calendar_events" in note and "discover_tools" in note
    assert "mail_send" not in note and "ssh_run" not in note
