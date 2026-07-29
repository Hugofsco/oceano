from oceano import delegate, mindbridge, tools
from oceano.agent import Agent, _resident_body_note


def _agent(monkeypatch, tmp_path):
    from oceano import toolrouter
    config_path = tmp_path / "resident-tools.toml"
    config_path.write_text(
        '[surfaces.resident]\nmode = "hybrid"\nschema_budget = 4000\n'
        'max_schema_budget = 6000\ndiscovery = true\n')
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
    assert "Skill" in observed["disallow"]
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
    continuation = _resident_body_note({"spawn_agent"}, "claude")
    assert "not completion of the parent turn" in continuation
    assert "proper progress response" in continuation
    assert "Never use Claude Agent/Workflow/Task tools" in continuation
    skills = _resident_body_note({"list_skills", "load_skill"}, "claude")
    assert "Oceano MCP list_skills and load_skill only" in skills
    assert "Never invoke Claude's native Skill" in skills
    assert "Do not list or load skills for routine self-contained coding" in skills


def test_claude_native_skill_is_denied_in_full_mode_and_recorded(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    agent.resident_tool_mode = False
    monkeypatch.setattr(agent, "_prepare_turn", lambda message, voice=False:
                        setattr(agent, "_turn_plan", None))
    observed = {}
    recorded = []
    monkeypatch.setattr(
        "oceano.agent.traces.record_global",
        lambda event, **data: recorded.append((event, data)))

    def fake_run(prompt, on_progress=None, **kwargs):
        observed["disallow"] = kwargs.get("disallow", "")
        on_progress({"kind": "tool", "tool": "Skill", "detail": "security-review",
                     "args": {"skill": "security-review"}, "tool_use_id": "skill-1"})
        on_progress({"kind": "tool_result", "text": "Unknown tool: Skill",
                     "tool_use_id": "skill-1", "is_error": True})
        return {"ok": True, "output": ""}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    events = list(agent._claude_mind_stream("Use the security review skill"))

    assert "Skill" in observed["disallow"]
    assert any(event.get("name") == "Skill" for event in events)
    assert "disabled native Skill" in agent.last_mind_error
    resident = next(data for event, data in recorded if event == "resident_turn")
    assert resident["failed_tools"] == ["Skill"]
    assert resident["error_codes"] == ["native_skill_blocked"]


def test_claude_native_agent_is_denied_and_recorded_as_orchestration_collision(
        monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    agent.resident_tool_mode = False
    monkeypatch.setattr(agent, "_prepare_turn", lambda message, voice=False:
                        setattr(agent, "_turn_plan", None))
    recorded = []
    monkeypatch.setattr(
        "oceano.agent.traces.record_global",
        lambda event, **data: recorded.append((event, data)))

    def fake_run(prompt, on_progress=None, **kwargs):
        assert "Agent" in kwargs["disallow"]
        assert kwargs["isolated_resident"] is True
        on_progress({"kind": "tool", "tool": "Agent", "detail": "inspect",
                     "args": {"prompt": "inspect"}, "tool_use_id": "agent-1"})
        on_progress({"kind": "tool_result", "text": "blocked",
                     "tool_use_id": "agent-1", "is_error": True})
        return {"ok": True, "output": ""}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    list(agent._claude_mind_stream("Delegate this inspection"))
    assert "disabled native Agent" in agent.last_mind_error
    resident = next(data for event, data in recorded if event == "resident_turn")
    assert resident["failed_tools"] == ["Agent"]
    assert resident["error_codes"] == ["native_agent_blocked"]


def test_claude_tool_only_spawn_gets_one_bounded_continuation(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    agent.resident_tool_mode = False
    monkeypatch.setattr(agent, "_prepare_turn", lambda message, voice=False:
                        setattr(agent, "_turn_plan", None))
    calls = []

    def fake_run(prompt, on_progress=None, tools="", **kwargs):
        calls.append({"prompt": prompt, "tools": tools,
                      "disallow": kwargs.get("disallow", "")})
        if len(calls) == 1:
            on_progress({"kind": "tool", "tool": "mcp__oceano__spawn_agent",
                         "args": {"task": "inspect"}, "tool_use_id": "spawn-1"})
            on_progress({"kind": "tool_result",
                         "text": ToolResult(
                             True, summary="started agent #73",
                             side_effects=("capability:agent_spawn",)).to_wire(),
                         "tool_use_id": "spawn-1", "is_error": False})
            return {"ok": True, "output": ""}
        assert "POST-SPAWN CONTINUATION" in prompt
        assert "mcp__oceano__spawn_agent" not in tools
        on_progress({"kind": "text", "text": "Agent #73 is running; I continued normally."})
        return {"ok": True, "output": "Agent #73 is running; I continued normally."}

    from oceano.tools.core import ToolResult
    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    events = list(agent._claude_mind_stream("Coordinate this with a background agent"))
    assert len(calls) == 2
    assert all("Skill" in call["disallow"] for call in calls)
    assert sum(event.get("name") == "spawn_agent" for event in events
               if event.get("type") == "tool_call") == 1
    assert any("continued normally" in event.get("text", "") for event in events)
    assert agent.last_mind_error is None


def test_claude_spawn_continuation_is_cancel_safe(monkeypatch, tmp_path):
    import threading
    from oceano.tools.core import ToolResult
    agent = _agent(monkeypatch, tmp_path)
    agent.resident_tool_mode = False
    monkeypatch.setattr(agent, "_prepare_turn", lambda message, voice=False:
                        setattr(agent, "_turn_plan", None))
    cancelled = threading.Event()
    calls = []

    def fake_run(prompt, on_progress=None, **kwargs):
        calls.append(prompt)
        on_progress({"kind": "tool", "tool": "mcp__oceano__spawn_agent",
                     "args": {"task": "inspect"}, "tool_use_id": "spawn-cancel"})
        on_progress({"kind": "tool_result",
                     "text": ToolResult(
                         True, summary="started agent #92",
                         side_effects=("capability:agent_spawn",)).to_wire(),
                     "tool_use_id": "spawn-cancel", "is_error": False})
        cancelled.set()
        return {"ok": True, "output": ""}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    list(agent._claude_mind_stream(
        "Coordinate this with a background agent", cancel=cancelled))
    assert len(calls) == 1


def test_claude_failed_correction_emits_honest_fallback(monkeypatch, tmp_path):
    from oceano.tools.core import ToolResult
    agent = _agent(monkeypatch, tmp_path)
    agent.resident_tool_mode = False
    monkeypatch.setattr(agent, "_prepare_turn", lambda message, voice=False:
                        setattr(agent, "_turn_plan", None))
    calls = []

    def fake_run(prompt, on_progress=None, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            on_progress({"kind": "tool", "tool": "mcp__oceano__spawn_agent",
                         "args": {"task": "inspect"}, "tool_use_id": "spawn-fallback"})
            on_progress({"kind": "tool_result",
                         "text": ToolResult(
                             True, summary="started agent #93",
                             side_effects=("capability:agent_spawn",)).to_wire(),
                         "tool_use_id": "spawn-fallback", "is_error": False})
            return {"ok": True, "output": ""}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(delegate, "to_claude_stream", fake_run)
    events = list(agent._claude_mind_stream("Coordinate this with a background agent"))
    assert len(calls) == 2
    assert any("will still be delivered" in event.get("text", "") for event in events)
    assert agent.last_mind_error == "post-spawn continuation failed"
