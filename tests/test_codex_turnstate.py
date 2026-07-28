from oceano import codex_mind, tools
from oceano.agent import Agent


def _agent(monkeypatch):
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
    agent = _agent(monkeypatch)
    observed = {}

    def fake_run(prompt, cwd=None, cancel=None, model="", on_event=None, **kwargs):
        observed["cwd"] = cwd
        on_event({"type": "tool_call", "name": "write_file",
                  "args": '{"path":"app.py","content":"print(1)"}'})
        on_event({"type": "tool_result", "name": "write_file", "result": "wrote app.py"})
        on_event({"type": "tool_call", "name": "shell", "args": "python app.py"})
        on_event({"type": "tool_result", "name": "shell", "result": "1"})
        on_event({"type": "token", "text": "Implemented and verified."})
        return {"ok": True, "output": "Implemented and verified."}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    with tools.background_workspace(tmp_path):
        events = list(agent._codex_mind_stream("Implement and test a small Python app"))
    assert observed["cwd"] == tmp_path.resolve()
    assert agent.last_mind_error is None
    assert events[-1] == {"type": "answer_done"}


def test_codex_resident_marks_an_unacted_build_incomplete(monkeypatch):
    agent = _agent(monkeypatch)

    def fake_run(prompt, on_event=None, **kwargs):
        on_event({"type": "token", "text": "Done."})
        return {"ok": True, "output": "Done."}

    monkeypatch.setattr(codex_mind, "run_stream", fake_run)
    list(agent._codex_mind_stream("Implement and test a small Python app"))
    assert "no action tool was used" in agent.last_mind_error
