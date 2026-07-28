from oceano.agent_runtime import (
    ContextCheckpoint, ResidentEventAdapter, TaskSpec, TurnBudget, TurnState,
)
from oceano.tools import ToolResult


def test_turn_state_uses_structured_results_for_evidence_and_metrics():
    state = TurnState("do it", object(), {"write_file"}, TaskSpec(True, True), TurnBudget.create(3))
    assert state.budget.begin_step() is True
    assert state.budget.consume_tool() is True
    state.record("write_file", ToolResult(True, summary="saved", side_effects=("file:a.py",)))
    state.record("run_tests", ToolResult(False, error="tests failed", code="failed"))
    assert state.legacy_events == [("write_file", "saved"), ("run_tests", "ERROR: tests failed")]
    assert state.error_count == 1
    assert state.side_effects == ["file:a.py"]
    assert state.metrics()["model_steps"] == 1
    assert state.completion_issues() == ["at least one tool returned an error"]


def test_turn_budget_bounds_multiple_tool_calls_per_model_step(monkeypatch):
    monkeypatch.setenv("OCEANO_MAX_TOOL_CALLS", "2")
    budget = TurnBudget.create(10)
    assert budget.consume_tool() is True
    assert budget.consume_tool() is True
    assert budget.consume_tool() is False
    assert budget.exhausted is True


def test_context_checkpoint_parses_structured_state_and_falls_back_losslessly():
    checkpoint = ContextCheckpoint.parse(
        '{"decisions":["use SQLite"],"constraints":["offline"],'
        '"artifacts":["app.py"],"evidence":["12 tests passed"],'
        '"unresolved":["deploy"],"notes":[]}'
    )
    rendered = checkpoint.render()
    assert checkpoint.structured is True
    assert "use SQLite" in rendered and "12 tests passed" in rendered and "deploy" in rendered
    fallback = ContextCheckpoint.parse("LEGACY SUMMARY")
    assert fallback.structured is False and "LEGACY SUMMARY" in fallback.render()


def test_successful_retry_resolves_a_retryable_error():
    state = TurnState("read it", object(), {"read_file"}, TaskSpec(), TurnBudget.create(3))
    state.record("read_file", ToolResult(False, error="missing", retryable=True, code="not_found"))
    state.record("read_file", ToolResult(True, summary="contents"))
    assert state.error_count == 0
    assert state.historical_error_count == 1
    assert state.events[0].resolved is True


def test_verification_resolves_transient_setup_error_after_a_mutation():
    state = TurnState("build it", object(), set(), TaskSpec(True, True), TurnBudget.create(5))
    state.record("read_file", ToolResult(False, error="missing", retryable=True, code="not_found"))
    state.record("write_file", ToolResult(True, summary="saved", side_effects=("file:app.py",)))
    state.record("run_tests", ToolResult(True, summary="(exit 0) pytest"))
    assert state.completion_issues() == []
    assert state.metrics()["historical_errors"] == 1


def test_non_retryable_policy_error_remains_unresolved():
    state = TurnState("do it", object(), set(), TaskSpec(True, False), TurnBudget.create(3))
    state.record("write_file", ToolResult(False, error="blocked", code="policy_blocked"))
    state.record("run_tests", ToolResult(True, summary="(exit 0) pytest"))
    assert state.error_count == 1


def test_resident_adapter_normalizes_codex_shell_and_enforces_budget(monkeypatch):
    monkeypatch.setenv("OCEANO_MAX_TOOL_CALLS", "1")
    state = TurnState("run it", object(), set(), TaskSpec(True, True), TurnBudget.create(3))
    adapter = ResidentEventAdapter(state)
    assert adapter.tool_call("shell", "echo ok") is True
    result = adapter.tool_result("shell", "ok")
    assert result.ok is True
    assert state.used_tools == ["run_shell"]
    assert state.completion_issues() == []
    assert adapter.tool_call("shell", "echo no") is False
    assert state.events[-1].result.code == "budget_exhausted"


def test_resident_adapter_types_nonzero_test_results():
    state = TurnState("test it", object(), set(), TaskSpec(), TurnBudget.create(3))
    adapter = ResidentEventAdapter(state)
    assert adapter.tool_call("mcp__oceano__run_tests", '{"path":"."}') is True
    result = adapter.tool_result("mcp__oceano__run_tests", "(exit 2) pytest\nfailed")
    assert result.ok is False and result.code == "tests_failed" and result.retryable is True


def test_resident_adapter_normalizes_claude_native_tools_and_exact_artifacts():
    state = TurnState("build it", object(), set(), TaskSpec(True, True), TurnBudget.create(3))
    adapter = ResidentEventAdapter(state)
    assert adapter.tool_call("Write", {"file_path": "src/app.py", "content": "print(1)"}) is True
    written = adapter.tool_result("Write", "File created")
    assert written.side_effects == ("file:src/app.py",)
    assert adapter.tool_call("Bash", {"command": "python src/app.py"}) is True
    adapter.tool_result("Bash", "1")
    assert state.used_tools == ["write_file", "run_shell"]
    assert state.completion_issues() == []


def test_resident_adapter_respects_claude_structured_error_flag():
    state = TurnState("run it", object(), set(), TaskSpec(True, True), TurnBudget.create(3))
    adapter = ResidentEventAdapter(state)
    adapter.tool_call("Bash", {"command": "false"})
    result = adapter.tool_result("Bash", "permission denied", is_error=True)
    assert result.ok is False and result.code == "command_failed" and result.retryable is True
    assert state.error_count == 1
    adapter.tool_call("Bash", {"command": "echo recovered"})
    adapter.tool_result("Bash", "recovered")
    assert state.error_count == 0 and state.historical_error_count == 1


def test_resident_adapter_marks_missing_results_incomplete():
    state = TurnState("write it", object(), set(), TaskSpec(True, False), TurnBudget.create(3))
    adapter = ResidentEventAdapter(state)
    adapter.tool_call("Write", {"file_path": "out.txt", "content": "x"})
    result = adapter.missing_result("Write")
    assert result.code == "missing_result" and result.retryable is True
    assert state.completion_issues() == ["at least one tool returned an error"]


def test_resident_adapter_never_resolves_claude_policy_failures():
    state = TurnState("send it", object(), set(), TaskSpec(True, False), TurnBudget.create(3))
    adapter = ResidentEventAdapter(state)
    adapter.tool_call("mcp__oceano__mail_send", {"to": "safe@example.test"})
    result = adapter.tool_result(
        "mcp__oceano__mail_send", "tool is blocked by policy", is_error=True)
    assert result.code == "policy_blocked" and result.retryable is False
    adapter.tool_call("mcp__oceano__mail_send", {"to": "safe@example.test"})
    adapter.tool_result("mcp__oceano__mail_send", "sent")
    assert state.error_count == 1
