from oceano import tools


def test_run_result_is_structured_while_run_stays_text(monkeypatch):
    monkeypatch.setitem(tools._TOOLS, "contract_fixture", lambda value: {"value": value})
    monkeypatch.setitem(
        tools._TOOL_SPECS,
        "contract_fixture",
        tools.ToolSpec("contract_fixture"),
    )
    result = tools.run_result("contract_fixture", '{"value": 7}')
    assert result.ok is True and result.data == {"value": 7}
    assert result.text() == "{'value': 7}"
    assert tools.run("contract_fixture", '{"value": 7}') == "{'value': 7}"


def test_structured_tool_error_keeps_legacy_error_prefix(monkeypatch):
    monkeypatch.setitem(
        tools._TOOLS,
        "contract_failure",
        lambda: tools.ToolResult(False, error="temporary failure", retryable=True, code="temporary"),
    )
    monkeypatch.setitem(tools._TOOL_SPECS, "contract_failure", tools.ToolSpec("contract_failure"))
    result = tools.run_result("contract_failure", "{}")
    assert result.ok is False and result.retryable is True and result.code == "temporary"
    assert tools.run("contract_failure", "{}") == "ERROR: temporary failure"


def test_tool_metadata_is_registered_and_can_come_from_schema(monkeypatch):
    schema = {
        "type": "function",
        "function": {"name": "contract_write", "parameters": {"type": "object"}},
        "x-oceano": {"capability": "workspace_write", "side_effecting": True,
                     "idempotent": False, "risk": "medium"},
    }
    tools.register("contract_write", schema, lambda: "saved")
    spec = tools.tool_spec("contract_write")
    assert spec.capability == "workspace_write"
    assert spec.side_effecting is True and spec.idempotent is False


def test_native_missing_file_has_a_retryable_not_found_code(tmp_path):
    with tools.background_workspace(tmp_path):
        result = tools.run_result("read_file", '{"path":"missing.txt"}')
    assert result.ok is False and result.code == "not_found" and result.retryable is True


def test_native_write_reports_the_exact_artifact_and_keeps_direct_api(tmp_path):
    with tools.background_workspace(tmp_path):
        result = tools.run_result("write_file", '{"path":"nested/out.txt","content":"ok"}')
        direct = tools.write_file("second.txt", "two")
    assert result.ok is True and result.side_effects == ("file:nested/out.txt",)
    assert direct.startswith("wrote 3 chars")


def test_native_test_failure_is_typed(monkeypatch):
    monkeypatch.setitem(tools._TOOLS, "run_tests", lambda path=".": "(exit 1) pytest\nfailed")
    result = tools.run_result("run_tests", '{}')
    assert result.ok is False and result.code == "tests_failed" and result.retryable is True
