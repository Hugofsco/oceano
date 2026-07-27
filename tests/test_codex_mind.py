"""codex_mind.py's per-call isolated CODEX_HOME for skill-enabled contained Codex runs.

Concurrent orchestrate-node agent spawns used to all share ONE CODEX_HOME (SUBAGENT_HOME) —
`codex exec` writes session/rollout state under that directory, so two processes running against
it at the same time corrupted each other's state (the actual cause of parallel agent-node
failures in workflows like "App builder — idea to launch"). new_subagent_home() hands out a
private, disposable home per call instead.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import codex_mind  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(codex_mind, "_SUBAGENT_HOME", data_dir / "codex-home-subagent")
    monkeypatch.setattr(codex_mind, "_SUBAGENT_CONFIG", data_dir / "codex-home-subagent" / "config.toml")
    auth_home = tmp_path / "fake-codex-auth"
    auth_home.mkdir(parents=True)
    (auth_home / "auth.json").write_text('{"token": "fake"}')
    monkeypatch.setenv("OCEANO_CODEX_AUTH_HOME", str(auth_home))
    monkeypatch.setattr(codex_mind.mindbridge, "daemon_url", lambda: "http://127.0.0.1:0")
    monkeypatch.setattr(codex_mind.mindbridge, "token", lambda: "tok")
    yield


def test_new_subagent_home_is_unique_and_lives_beside_the_shared_one():
    a = codex_mind.new_subagent_home()
    b = codex_mind.new_subagent_home()
    assert a != b
    assert a.parent == codex_mind._SUBAGENT_HOME.parent
    assert a.name.startswith("codex-home-subagent-") and a.name != "codex-home-subagent"


def test_ensure_subagent_home_writes_auth_and_scoped_config_at_the_given_path():
    home = codex_mind.new_subagent_home()
    r = codex_mind.ensure_subagent_home(home)
    assert r == {"ok": True, "home": str(home)}
    assert (home / "auth.json").read_text() == '{"token": "fake"}'
    cfg = (home / "config.toml").read_text()
    assert 'OCEANO_MCP_SCOPE = "skills"' in cfg
    assert 'OCEANO_MCP_BACKGROUND = "1"' in cfg


def test_ensure_subagent_home_defaults_to_the_shared_home_for_backward_compat():
    r = codex_mind.ensure_subagent_home()
    assert r == {"ok": True, "home": str(codex_mind._SUBAGENT_HOME)}
    assert (codex_mind._SUBAGENT_HOME / "config.toml").exists()


def test_ensure_subagent_home_reports_missing_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("OCEANO_CODEX_AUTH_HOME", str(tmp_path / "nowhere"))
    home = codex_mind.new_subagent_home()
    r = codex_mind.ensure_subagent_home(home)
    assert r["ok"] is False and "codex auth not found" in r["error"]


def test_discard_subagent_home_removes_the_directory():
    home = codex_mind.new_subagent_home()
    codex_mind.ensure_subagent_home(home)
    assert home.is_dir()
    codex_mind.discard_subagent_home(home)
    assert not home.exists()


def test_discard_subagent_home_is_a_noop_when_nothing_is_there():
    codex_mind.discard_subagent_home(codex_mind._SUBAGENT_HOME.parent / "codex-home-subagent-ghost")


def test_concurrent_private_homes_never_collide():
    """Two 'concurrent' calls (simulated sequentially — the real guarantee is uniqueness, not
    timing) each get their own home and neither's files leak into the other's."""
    h1, h2 = codex_mind.new_subagent_home(), codex_mind.new_subagent_home()
    codex_mind.ensure_subagent_home(h1)
    codex_mind.ensure_subagent_home(h2)
    (h1 / "session-marker.txt").write_text("run-1")
    assert not (h2 / "session-marker.txt").exists()
    codex_mind.discard_subagent_home(h1)
    assert h2.is_dir()   # discarding one private home never touches the other


def test_new_subagent_home_sweeps_stale_leftovers_but_keeps_fresh_ones(monkeypatch):
    stale = codex_mind._SUBAGENT_HOME.parent / "codex-home-subagent-stale00000000"
    fresh = codex_mind._SUBAGENT_HOME.parent / "codex-home-subagent-fresh0000000"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    old_time = codex_mind.time.time() - codex_mind._SUBAGENT_HOME_STALE_S - 60
    os.utime(stale, (old_time, old_time))
    codex_mind.new_subagent_home()          # triggers the sweep as a side effect
    assert not stale.exists()
    assert fresh.exists()


# --- tool-event parsing (real `codex exec --json` item shapes, codex-cli 0.144.6) ---
# Regression for: shell-command output was invisible in the Codex mind because _tool_result read
# output/text/summary/result but codex puts a command's stdout/stderr in `aggregated_output`.

def test_tool_result_reads_shell_aggregated_output():
    # Exact shape of an item.completed command_execution item from codex-cli 0.144.6.
    item = {"id": "item_1", "type": "command_execution",
            "command": "/bin/bash -lc 'echo OCEANO_OK_123'",
            "aggregated_output": "OCEANO_OK_123\n", "exit_code": 0, "status": "completed"}
    assert codex_mind._tool_result(item) == "OCEANO_OK_123"


def test_tool_result_shell_failure_shows_exit_and_output():
    item = {"type": "command_execution", "command": "false; echo boom 1>&2",
            "aggregated_output": "boom\n", "exit_code": 1, "status": "completed"}
    out = codex_mind._tool_result(item)
    assert "boom" in out and "exit 1" in out


def test_tool_result_shell_no_output_still_surfaces_status():
    item = {"type": "command_execution", "command": "true", "aggregated_output": "", "exit_code": 0}
    assert codex_mind._tool_result(item) == "(no output)"
    item2 = {"type": "command_execution", "command": "false", "aggregated_output": "", "exit_code": 3}
    assert codex_mind._tool_result(item2) == "(exit 3, no output)"


def test_tool_result_still_reads_mcp_content():
    # MCP body-tool results were never broken — guard that the aggregated_output branch didn't regress them.
    item = {"type": "mcp_tool_call", "server": "oceano", "tool": "list_skills",
            "result": {"content": [{"type": "text", "text": "- a11y-audit: ...\n- deep-research: ..."}]},
            "error": None, "status": "completed"}
    out = codex_mind._tool_result(item)
    assert "a11y-audit" in out and "deep-research" in out


def test_tool_call_labels_shell_and_mcp():
    assert codex_mind._tool_call(
        {"type": "command_execution", "command": "/bin/bash -lc 'ls'"}) == ("shell", "/bin/bash -lc 'ls'")
    name, _detail = codex_mind._tool_call(
        {"type": "mcp_tool_call", "server": "oceano", "tool": "recall", "arguments": {"query": "x"}})
    assert name == "recall"
