import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import toolrouter, tools  # noqa: E402


def _route(query, model="small-local"):
    return toolrouter.route(tools.schemas(), query, model=model, force=True)


def _names(query):
    return set(_route(query).names)


def test_calendar_query_gets_a_small_calendar_focused_catalog():
    route = _route("What meetings are on my calendar next week?")
    names = set(route.names)
    assert "calendar_events" in names
    assert "mail_send" not in names
    assert route.schema_tokens <= route.policy.schema_budget


def test_code_task_keeps_execution_and_verification_tools():
    names = _names("Implement and test the Python project in the repository")
    assert {"read_file", "write_file", "run_shell", "run_tests", "delegate"} <= names


def test_ambiguous_request_keeps_core_and_discovery_instead_of_all_tools():
    schemas = tools.schemas()
    route = toolrouter.route(schemas, "do it", force=True)
    assert "discover_tools" in route.names
    assert route.selected < len(schemas)
    assert route.reason == "ambiguous-discovery"


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


def test_discovery_loads_cumulatively_but_never_crosses_allowed_boundary():
    schemas = tools.schemas()
    forbidden = "add_calendar_event"
    allowed = {s["function"]["name"] for s in schemas} - {forbidden}
    route = toolrouter.route([s for s in schemas if s["function"]["name"] in allowed],
                             "do it", force=True)
    before = set(route.names)
    updated, result = toolrouter.discover(
        route, schemas, allowed, {"query": "inspect calendar", "operation": "load"})
    assert before <= set(updated.names)
    assert "calendar_events" in updated.names
    assert forbidden not in updated.names
    assert '"loaded"' in result
    assert updated.schema_tokens <= updated.policy.max_schema_budget


def test_recovery_expands_relevant_tools_before_full_catalog():
    schemas = tools.schemas()
    allowed = {s["function"]["name"] for s in schemas}
    route = toolrouter.route(schemas, "do it", force=True)
    discovered, phase, _ = toolrouter.recover(route, schemas, allowed, "inspect calendar")
    assert phase == "discovery"
    assert discovered.recovery_level == 1
    assert "calendar_events" in discovered.names
    full, phase, _ = toolrouter.recover(discovered, schemas, allowed, "inspect calendar")
    assert phase == "full"
    assert full.fallback is True
    assert full.names == tuple(s["function"]["name"] for s in schemas)


def test_config_precedence_is_surface_then_model_then_global(tmp_path, monkeypatch):
    config_path = tmp_path / "tool-loading.toml"
    config_path.write_text(
        '[default]\nmode = "full"\nschema_budget = 7000\n'
        '[[models]]\npattern = "qwen*"\nmode = "hybrid"\nschema_budget = 4000\n'
        '[surfaces.workflow]\nmode = "full"\nschema_budget = 3000\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OCEANO_TOOL_CONFIG", str(config_path))
    monkeypatch.setenv("OCEANO_TOOL_LOADING_MODE", "hybrid")
    toolrouter._CACHE.update({"path": None, "mtime": None, "data": {}})
    chat = toolrouter.resolve_policy("qwen-small", "chat")
    workflow = toolrouter.resolve_policy("qwen-small", "workflow")
    assert chat.mode == "hybrid" and chat.schema_budget == 4000
    assert workflow.mode == "full" and workflow.schema_budget == 3000


def test_config_can_define_a_custom_capability_bundle(tmp_path, monkeypatch):
    config_path = tmp_path / "tool-loading.toml"
    config_path.write_text(
        '[default]\nschema_budget = 500\n'
        '[bundles.issues]\ndescription = "Project issue tracking"\n'
        'aliases = ["ticket"]\ntools = ["issue_list", "issue_read"]\n'
        'core = ["issue_list"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OCEANO_TOOL_CONFIG", str(config_path))
    toolrouter._CACHE.update({"path": None, "mtime": None, "data": {}})
    schemas = [
        {"type": "function", "function": {"name": name, "description": name,
                                           "parameters": {"type": "object", "properties": {}}}}
        for name in ("issue_list", "issue_read", *(f"unrelated_{i}" for i in range(30)))
    ]
    route = toolrouter.route(schemas, "read the ticket", force=True)
    assert {"issue_list", "issue_read"} <= set(route.names)
    assert "issues" in route.loaded_bundles


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
