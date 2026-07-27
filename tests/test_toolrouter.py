import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import toolrouter, tools  # noqa: E402


def _route(query, model="small-local"):
    return toolrouter.route(tools.schemas(), query, model=model, force=True)


def _names(query):
    return set(_route(query).names)


def test_calendar_query_gets_a_small_calendar_focused_catalog():
    names = _names("What meetings are on my calendar next week?")
    assert "calendar_events" in names
    assert "mail_send" not in names
    assert len(names) <= toolrouter.MAX_TOOLS


def test_code_task_keeps_execution_and_verification_tools():
    names = _names("Implement and test the Python project in the repository")
    assert {"read_file", "write_file", "run_shell", "run_tests", "delegate"} <= names


def test_ambiguous_request_falls_back_to_all_tools():
    schemas = tools.schemas()
    assert toolrouter.select(schemas, "do it", force=True) == schemas


def test_routing_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OCEANO_DYNAMIC_TOOLS", raising=False)
    route = toolrouter.route(tools.schemas(), "check my calendar", model="small-local")
    assert route.enabled is False
    assert route.schemas == tools.schemas()


def test_model_allow_and_exclude_patterns(monkeypatch):
    monkeypatch.setenv("OCEANO_DYNAMIC_TOOLS", "1")
    monkeypatch.setenv("OCEANO_DYNAMIC_TOOL_MODELS", "qwen*,local-*")
    monkeypatch.setenv("OCEANO_DYNAMIC_TOOL_EXCLUDE_MODELS", "*-large")
    assert toolrouter.enabled_for("qwen-7b") is True
    assert toolrouter.enabled_for("local-small") is True
    assert toolrouter.enabled_for("qwen-large") is False
    assert toolrouter.enabled_for("gpt-5") is False


def test_invalid_limit_falls_back_without_crashing(monkeypatch):
    monkeypatch.setenv("OCEANO_DYNAMIC_TOOL_LIMIT", "not-a-number")
    route = toolrouter.route(tools.schemas(), "check my calendar", force=True)
    assert route.selected <= toolrouter.DEFAULT_LIMIT


def test_multi_domain_query_keeps_core_tools_from_each_domain():
    names = _names("Read tomorrow's calendar and reply to the newest email")
    assert {"calendar_events", "mail_list", "mail_read", "mail_reply"} <= names


def test_conservative_baseline_is_present_when_routed():
    route = _route("Research the latest sources online")
    assert route.routed is True
    assert {"delegate", "list_skills", "load_skill", "list_files", "read_file", "code_search"} <= set(route.names)


def test_full_catalog_recovery_signals_are_conservative():
    route = _route("Implement and test the repository")
    assert toolrouter.should_expand(route, "I cannot access the required tool") is True
    assert toolrouter.should_expand(route, issues=["no action tool was used"]) is True
    assert toolrouter.should_expand(route, tool_events=[("made_up", "ERROR: tool is not available in this conversation")]) is True
    assert toolrouter.should_expand(route, "Done", [], [("run_tests", "12 passed")]) is False
    expanded = toolrouter.expanded(route, tools.schemas())
    assert expanded.fallback is True and expanded.schemas == tools.schemas()


def test_routing_telemetry_never_accepts_prompt_or_result_fields(tmp_path, monkeypatch):
    from oceano import traces
    monkeypatch.setattr(traces, "TRACE_PATH", tmp_path / "traces.jsonl")
    monkeypatch.setenv("OCEANO_DYNAMIC_TOOL_TELEMETRY", "1")
    route = _route("check my calendar")
    toolrouter.telemetry(route, "completed", used_tools=["calendar_events"], errors=0)
    payload = traces.query(limit=1)[0]
    assert payload["event"] == "tool_routing"
    assert payload["used_tools"] == ["calendar_events"]
    assert "prompt" not in payload and "result" not in payload and "answer" not in payload
