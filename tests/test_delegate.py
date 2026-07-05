"""to_claude_stream's error surfacing: when Claude Code's own "result" event reports is_error,
its actual explanation (e.g. hitting --max-turns on a large build) must survive into the
returned error string — this used to be discarded for a canned, undiagnostic phrase, which made
a workflow agent node's failure (its ONLY trace of what happened) untraceable.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import delegate  # noqa: E402 - after the sys.path bootstrap


def _fake_claude_binary(tmp_path, result_event):
    """A shim standing in for the real `claude` CLI: emits one stream-json 'result' line,
    ignoring whatever argv it's called with, then exits."""
    import json
    script = tmp_path / "fake_claude.py"
    script.write_text("import json, sys\n"
                      f"print(json.dumps({result_event!r}))\n")
    shim = tmp_path / "claude"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    return str(shim)


def test_is_error_surfaces_the_clis_own_explanation_and_turn_count(monkeypatch, tmp_path):
    binary = _fake_claude_binary(tmp_path, {
        "type": "result", "result": "Reached maximum number of turns (60)",
        "is_error": True, "num_turns": 60, "total_cost_usd": 0.5})
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: binary)
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert r["ok"] is False
    assert "60 turn" in r["error"]
    assert "Reached maximum number of turns" in r["error"]


def test_is_error_falls_back_to_stderr_when_clis_result_text_is_empty(monkeypatch, tmp_path):
    import json
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys\n"
        "print('boom: something went wrong', file=sys.stderr)\n"
        f"print(json.dumps({{'type': 'result', 'result': '', 'is_error': True, 'num_turns': 3}}))\n")
    shim = tmp_path / "claude"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert r["ok"] is False
    assert "3 turn" in r["error"]
    assert "boom: something went wrong" in r["error"]


def test_api_tool_map_grants_code_search_and_run_tests_git():
    """The api/local providers had no path to code_search/run_tests/git at all — folding them
    into the existing Read/Grep/Write CLI-style buckets closes that gap without adding a new
    tier (native Bash already covers this for the claude/codex CLI providers)."""
    assert "code_search" in delegate._API_TOOL_MAP["Read"]
    assert "code_search" in delegate._API_TOOL_MAP["Grep"]
    assert "run_tests" in delegate._API_TOOL_MAP["Write"]
    assert "git" in delegate._API_TOOL_MAP["Write"]


def test_api_only_tools_translates_write_to_the_new_tool_names():
    names = delegate._api_only_tools("Write")
    assert names == {"write_file", "make_folder", "run_tests", "git"}


def test_api_only_tools_skills_true_grants_list_and_load_skill_at_any_tier():
    """skills=True (workflow Delegate/Agent-spawn nodes) must reach list_skills/load_skill even at
    the read-only default tier — skill-reuse is orthogonal to file-access, and read-only-safe."""
    assert delegate._api_only_tools("Read,Glob,Grep", skills=True) == {
        "read_file", "list_files", "code_search", "list_skills", "load_skill"}
    # never granted unless the caller opts in
    assert "list_skills" not in delegate._api_only_tools("Read,Glob,Grep", skills=False)
    # and it never smuggles in memory or learn_skill — those stay behind the full body bridge
    granted = delegate._api_only_tools("Read,Glob,Grep,Write,Edit,Bash", skills=True)
    assert "learn_skill" not in granted and "remember" not in granted and "recall" not in granted


def test_to_claude_stream_skills_true_wires_the_scoped_bridge_not_memory(monkeypatch, tmp_path):
    """skills=True must load mindbridge's narrow "skills" scope (list_skills/load_skill only) via
    its own --mcp-config, and widen --allowedTools with the matching mcp__oceano__ names — but
    never reach memory, which stays exclusive to the full-body bridge (an Instructions node)."""
    import config
    import oceano.tools  # noqa: F401 - ensure list_skills/load_skill are registered before we filter
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    argv_file = tmp_path / "argv.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(' '.join(sys.argv))\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'is_error': False, 'num_turns': 1}))\n")
    shim = tmp_path / "claude"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path), tools="Read,Glob,Grep", skills=True)
    assert r["ok"] is True
    argv = argv_file.read_text()
    assert "--mcp-config" in argv and "--strict-mcp-config" in argv
    assert "mcp__oceano__list_skills" in argv and "mcp__oceano__load_skill" in argv
    assert "mcp__oceano__remember" not in argv and "mcp__oceano__recall" not in argv


def test_to_codex_skills_true_uses_the_subagent_home_and_keeps_the_scoped_config(monkeypatch, tmp_path):
    """skills=True must load a SEPARATE CODEX_HOME (codex_mind.SUBAGENT_HOME) with the scoped
    bridge's own config.toml — and must NOT pass --ignore-user-config (which would block loading
    it), unlike the plain contained delegate path."""
    from oceano import codex_mind
    argv_file = tmp_path / "argv.txt"
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import sys, os\n"
        f"open({str(argv_file)!r}, 'w').write(' '.join(sys.argv) + '\\n' + os.environ.get('CODEX_HOME', ''))\n"
        "args = sys.argv[1:]\n"
        "if '-o' in args:\n"
        "    open(args[args.index('-o') + 1], 'w').write('ok')\n")
    shim = tmp_path / "codex"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setattr("oceano.delegate.find_codex", lambda: str(shim))
    subhome = tmp_path / "subagent-home"
    monkeypatch.setattr(codex_mind, "SUBAGENT_HOME", subhome)
    monkeypatch.setattr(codex_mind, "ensure_subagent_home", lambda: {"ok": True, "home": str(subhome)})
    r = delegate.to_codex("do it", cwd=str(tmp_path), skills=True)
    assert r["ok"] is True
    out = argv_file.read_text()
    argv_line, env_home = out.split("\n", 1)
    assert "--ignore-user-config" not in argv_line
    assert env_home == str(subhome)


def test_to_claude_stream_max_turns_honors_the_user_override(monkeypatch, tmp_path):
    """A user-raised max_delegate_turns override (Settings → Tools) must reach the CLI's own
    --max-turns flag — this is the actual fix for a workflow agent hitting the turn cap."""
    from oceano.tools import core   # patch core's OWN globals, not the oceano.tools facade's
    # re-exported names — _save_state/get_max_delegate_turns always read core.py's module
    # globals, so redirecting the facade's copy wouldn't actually affect them.
    argv_file = tmp_path / "argv.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(' '.join(sys.argv))\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'is_error': False, 'num_turns': 1}))\n")
    shim = tmp_path / "claude"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    monkeypatch.setattr(core, "_STATE_PATH", tmp_path / "tools.json")   # never touch the real store
    monkeypatch.setattr(core, "_MAX_DELEGATE_TURNS", 0)
    core.set_max_delegate_turns(123)
    delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert "--max-turns 123" in argv_file.read_text()
