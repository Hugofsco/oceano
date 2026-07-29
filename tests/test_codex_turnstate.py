from oceano import codex_mind, tools
from oceano.agent import Agent


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
        instance._turn_plan = {
            "requires_action": True,
            "verify_code": True,
        }

    monkeypatch.setattr(instance, "_prepare_turn", prepare)
    return instance


def test_codex_resident_events_feed_turn_state_and_use_active_workspace(tmp_path, monkeypatch):
    agent = _agent(monkeypatch, tmp_path)
    observed = {}

    def fake_run(prompt, cwd=None, cancel=None, model="", on_event=None, **kwargs):
        observed["cwd"] = cwd
        observed["catalog_id"] = kwargs.get("catalog_id")
        on_event({"type": "tool_call", "name": "write_file", "source": "mcp",
                  "args": '{"path":"app.py","content":"print(1)"}'})
        on_event({"type": "tool_result", "name": "write_file", "source": "mcp",
                  "result": "wrote app.py"})
        on_event({"type": "tool_call", "name": "run_shell", "source": "mcp",
                  "args": '{"command":"python app.py"}'})
        on_event({"type": "tool_result", "name": "run_shell", "source": "mcp",
                  "result": "1"})
        on_event({"type": "token", "text": "Implemented and verified."})
        return {"ok": True, "output": "Implemented and verified."}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    with tools.background_workspace(tmp_path):
        events = list(agent._codex_mind_stream("Implement and test a small Python app"))
    assert observed["cwd"] == tmp_path.resolve()
    assert observed["catalog_id"]
    assert agent.last_mind_error is None
    assert events[-1] == {"type": "answer_done"}


def test_codex_resident_marks_an_unacted_build_incomplete(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)

    def fake_run(prompt, on_event=None, **kwargs):
        on_event({"type": "token", "text": "Done."})
        return {"ok": True, "output": "Done."}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    list(agent._codex_mind_stream("Implement and test a small Python app"))
    assert "no action tool was used" in agent.last_mind_error


def test_codex_native_mutation_event_fails_closed(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)

    def fake_run(prompt, on_event=None, **kwargs):
        on_event({"type": "tool_call", "name": "shell", "source": "native",
                  "args": "touch blocked.txt"})
        on_event({"type": "tool_result", "name": "shell", "source": "native",
                  "result": "blocked by PreToolUse"})
        on_event({"type": "token", "text": "Could not use the native tool."})
        return {"ok": True, "output": "Could not use the native tool."}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    list(agent._codex_mind_stream("Implement a small Python app"))
    assert "at least one tool returned an error" in agent.last_mind_error


def test_codex_native_collaboration_event_is_structured_and_fails_closed(
        monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    recorded = []
    monkeypatch.setattr(
        "oceano.agent.traces.record_global",
        lambda event, **data: recorded.append((event, data)))

    def fake_run(prompt, on_event=None, **kwargs):
        on_event({"type": "tool_call", "name": "spawn_agent", "source": "native",
                  "args": '{"prompt":"inspect"}'})
        on_event({"type": "tool_result", "name": "spawn_agent", "source": "native",
                  "result": "blocked by resident boundary"})
        return {"ok": True, "output": ""}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    list(agent._codex_mind_stream("Delegate this inspection"))
    assert "at least one tool returned an error" in agent.last_mind_error
    resident = next(data for event, data in recorded if event == "resident_turn")
    assert resident["failed_tools"] == ["spawn_agent"]
    assert resident["error_codes"] == ["native_agent_blocked"]


def test_codex_tool_only_spawn_gets_one_bounded_continuation(monkeypatch, tmp_path):
    from oceano.tools.core import ToolResult
    agent = _agent(monkeypatch, tmp_path)
    agent.resident_tool_mode = False
    monkeypatch.setattr(agent, "_prepare_turn", lambda message, voice=False:
                        setattr(agent, "_turn_plan", None))
    calls = []

    def fake_run(prompt, on_event=None, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            on_event({"type": "tool_call", "name": "spawn_agent", "source": "mcp",
                      "args": '{"task":"inspect"}'})
            on_event({"type": "tool_result", "name": "spawn_agent", "source": "mcp",
                      "result": ToolResult(
                          True, summary="started agent #81",
                          side_effects=("capability:agent_spawn",)).to_wire()})
            return {"ok": True, "output": ""}
        assert "POST-SPAWN CONTINUATION" in prompt
        on_event({"type": "token", "text": "Agent #81 is running; parent continued."})
        return {"ok": True, "output": "Agent #81 is running; parent continued."}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    events = list(agent._codex_mind_stream("Coordinate this with a background agent"))
    assert len(calls) == 2
    assert sum(event.get("name") == "spawn_agent" for event in events
               if event.get("type") == "tool_call") == 1
    assert any("parent continued" in event.get("text", "") for event in events)
    assert agent.last_mind_error is None
