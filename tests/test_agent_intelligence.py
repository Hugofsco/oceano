import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano.agent import Agent, _outcome_issues, _parse_facts, _task_plan  # noqa: E402


def test_planning_is_adaptive_not_universal():
    assert _task_plan("What is 2 + 2?") is None
    assert _task_plan("Create one short note file") is None
    plan = _task_plan("Implement these changes across the codebase in sequence and run the test suite")
    assert plan["requires_action"] is True
    assert plan["verify_code"] is True


def test_complex_plan_is_injected_for_only_the_current_turn(monkeypatch):
    ag = Agent(model="m", learn=False, inject_context=False)
    ag._prepare_turn("Implement these changes across the codebase and test the project")
    assert "TASK EXECUTION PLAN" in ag.messages[0]["content"]
    ag._prepare_turn("What is 2 + 2?")
    assert "TASK EXECUTION PLAN" not in ag.messages[0]["content"]


def test_outcome_gate_requires_action_and_code_verification():
    plan = _task_plan("Implement all these changes across the codebase")
    assert "no action tool was used" in _outcome_issues(plan, [])
    assert "the changed code was not exercised" in _outcome_issues(
        plan, [("write_file", "saved")])
    assert _outcome_issues(plan, [("write_file", "saved"), ("run_tests", "10 passed")]) == []
    assert "at least one tool returned an error" in _outcome_issues(
        plan, [("delegate", "ERROR: delegate failed")])


def test_memory_extraction_requires_structured_grounded_evidence():
    user = "I'm vegetarian and I prefer early meetings."
    good = """[{"text":"My user is vegetarian","category":"preference",
                "confidence":0.98,"evidence":"I'm vegetarian"}]"""
    parsed = _parse_facts(good, user)
    assert parsed[0]["text"] == "My user is vegetarian"
    assert _parse_facts("- My user is vegetarian", user) == []       # prose fallback removed
    ungrounded = ('[{"text":"My user owns a yacht","category":"fact",'
                  '"confidence":0.99,"evidence":"owns a yacht"}]')
    assert _parse_facts(ungrounded, user) == []


def test_dynamic_tool_failure_retries_once_with_full_catalog(monkeypatch):
    from oceano import tools
    seen = []

    class Msg:
        tool_calls = None
        def __init__(self, content):
            self.content = content

    replies = iter([Msg("I cannot access the required tool."), Msg("Recovered.")])

    def fake_chat(*args, **kwargs):
        seen.append(len(kwargs.get("tools") or []))
        return next(replies)

    monkeypatch.setattr("oceano.llm.chat", fake_chat)
    monkeypatch.setattr("oceano.toolrouter.telemetry", lambda *a, **k: None)
    ag = Agent(model="small-local", learn=False, inject_context=False, dynamic_tools=True)
    assert ag.run("Research the latest sources online") == "Recovered."
    assert len(seen) == 2
    assert seen[0] < seen[1] == len(tools.schemas())
