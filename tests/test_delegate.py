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


def _fake_claude_two_calls(tmp_path, first_lines, second_lines):
    """A stateful shim: emits `first_lines` (stream-json) on its first invocation,
    `second_lines` on any later one, and appends each invocation's argv to argv.txt."""
    import json
    calls = tmp_path / "calls.txt"
    argv = tmp_path / "argv.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys, os\n"
        f"calls, argv = {str(calls)!r}, {str(argv)!r}\n"
        "n = int(open(calls).read()) if os.path.exists(calls) else 0\n"
        "open(calls, 'w').write(str(n + 1))\n"
        "open(argv, 'a').write(' '.join(sys.argv) + '\\n')\n"
        f"first, second = {first_lines!r}, {second_lines!r}\n"
        "for ev in (first if n == 0 else second):\n"
        "    print(json.dumps(ev))\n")
    shim = tmp_path / "claude"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    return shim, argv, calls


def test_rate_limited_run_waits_then_resumes_the_same_session(monkeypatch, tmp_path):
    """A usage-limit failure (routine on a subscription) must not kill an unattended job: the
    stream retries after the reset and RESUMES the session so completed work isn't redone."""
    shim, argv, calls = _fake_claude_two_calls(
        tmp_path,
        first_lines=[
            {"type": "system", "subtype": "init", "session_id": "sess-123"},
            {"type": "result", "result": "Claude AI usage limit reached|1751000000",
             "is_error": True, "num_turns": 2, "total_cost_usd": 0.1}],
        second_lines=[
            {"type": "result", "result": "all done", "is_error": False,
             "num_turns": 3, "total_cost_usd": 0.2}])
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    monkeypatch.setattr("oceano.delegate._RL_MIN_WAIT", 0.01)   # the |epoch is in the past → floor applies
    events = []
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path), on_progress=events.append)
    assert r["ok"] is True and r["output"] == "all done"
    assert calls.read_text() == "2"
    assert "--resume sess-123" in argv.read_text().splitlines()[1]
    assert r["turns"] == 5 and r["cost"] == 0.3                 # both attempts accounted
    assert any("usage/rate limit" in e.get("text", "") for e in events if e["kind"] == "text")


def test_rate_limit_event_rejection_triggers_the_retry_even_without_error_text(monkeypatch, tmp_path):
    """The structured rate_limit_event (status rejected + a relative reset) must classify the
    failure as rate-limited even when the CLI's own error text doesn't say so."""
    shim, argv, calls = _fake_claude_two_calls(
        tmp_path,
        first_lines=[
            {"type": "system", "subtype": "init", "session_id": "sess-9"},
            {"type": "rate_limit_event", "rate_limit": {"status": "rejected", "resetsInSeconds": 1}},
            {"type": "result", "result": "", "is_error": True, "num_turns": 1}],
        second_lines=[{"type": "result", "result": "recovered", "is_error": False, "num_turns": 1}])
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    monkeypatch.setattr("oceano.delegate._RL_MIN_WAIT", 0.01)
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert r["ok"] is True and r["output"] == "recovered"
    assert calls.read_text() == "2"


def test_a_plain_error_is_not_retried(monkeypatch, tmp_path):
    """Only rate/usage-limit failures re-run — an ordinary error (bad task, max-turns) must
    surface immediately, exactly once."""
    shim, argv, calls = _fake_claude_two_calls(
        tmp_path,
        first_lines=[{"type": "result", "result": "Reached maximum number of turns (60)",
                      "is_error": True, "num_turns": 60}],
        second_lines=[{"type": "result", "result": "should never run", "is_error": False, "num_turns": 1}])
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    monkeypatch.setattr("oceano.delegate._RL_MIN_WAIT", 0.01)
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert r["ok"] is False and calls.read_text() == "1"
    assert "Reached maximum number of turns" in r["error"]


def test_a_reset_beyond_the_wait_cap_fails_fast_with_the_reset_in_the_error(monkeypatch, tmp_path):
    """A window that only resets hours out must NOT block the thread — fail fast, and tell the
    caller when it lifts so a scheduler can requeue."""
    import time as _time
    far = int(_time.time()) + 7200
    shim, argv, calls = _fake_claude_two_calls(
        tmp_path,
        first_lines=[{"type": "result", "result": f"Claude AI usage limit reached|{far}",
                      "is_error": True, "num_turns": 1}],
        second_lines=[{"type": "result", "result": "should never run", "is_error": False, "num_turns": 1}])
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    monkeypatch.setenv("OCEANO_DELEGATE_RL_WAIT", "60")
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert r["ok"] is False and calls.read_text() == "1"
    assert "not retrying" in r["error"] and "OCEANO_DELEGATE_RL_WAIT" in r["error"]


def test_retries_are_bounded_and_partial_work_from_an_earlier_attempt_survives(monkeypatch, tmp_path):
    """Every attempt rate-limited → give up after OCEANO_DELEGATE_RL_RETRIES, keeping the best
    partial output instead of returning empty-handed."""
    shim, argv, calls = _fake_claude_two_calls(
        tmp_path,
        first_lines=[{"type": "result", "result": "half the report… usage limit reached|1751000000",
                      "is_error": True, "num_turns": 1}],
        second_lines=[{"type": "result", "result": "", "is_error": True, "num_turns": 0},
                      {"type": "rate_limit_event", "status": "rejected"}])
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    monkeypatch.setattr("oceano.delegate._RL_MIN_WAIT", 0.01)
    monkeypatch.setenv("OCEANO_DELEGATE_RL_RETRIES", "1")
    r = delegate.to_claude_stream("do the thing", cwd=str(tmp_path))
    assert r["ok"] is False and calls.read_text() == "2"        # 1 original + 1 retry, then stop
    assert r["partial"] is True and "half the report" in r["output"]


def test_set_config_preserves_every_non_role_key(monkeypatch, tmp_path):
    """Saving a role's provider config rewrites delegation.json — it used to keep only a
    whitelist of keys, silently wiping any stored key the list had fallen behind on (the mind
    reset to local, the claude/codex effort pins vanished). Now every non-role key survives."""
    monkeypatch.setattr(delegate, "_CONFIG_PATH", tmp_path / "delegation.json")
    delegate.set_claude_effort("high")
    delegate.set_mind("claude")
    delegate.set_route_by_evals(True)
    delegate.set_config({"provider": "codex_cli"}, role="improve")
    assert delegate.get_claude_effort() == "high"
    assert delegate.get_mind() == "claude"
    assert delegate.get_route_by_evals() is True
    assert delegate.get_config("improve")["provider"] == "codex_cli"


def test_resolve_primary_routes_to_the_eval_winner_when_enabled(monkeypatch, tmp_path):
    """Route-by-evals closes the eval loop: with no pinned primary, the leaderboard's top
    scorer among SERVED models wins over llama-swap file order — and degrades to file order
    when there's no usable signal or the toggle is off."""
    import config
    monkeypatch.setattr(delegate, "_CONFIG_PATH", tmp_path / "delegation.json")
    monkeypatch.setattr(config, "MODEL", "")
    monkeypatch.setattr(delegate, "served_models", lambda: ["first-served", "eval-champ"])
    monkeypatch.setattr("oceano.evals.best_model",
                        lambda among=None, category=None, max_age_days=45: "eval-champ")
    assert delegate.resolve_primary()["source"] == "served"       # toggle off → file order
    delegate.set_route_by_evals(True)
    r = delegate.resolve_primary()
    assert (r["model"], r["source"]) == ("eval-champ", "evals")
    monkeypatch.setattr("oceano.evals.best_model",
                        lambda among=None, category=None, max_age_days=45: None)
    r = delegate.resolve_primary()                                # no signal → fall back, never break
    assert (r["model"], r["source"]) == ("first-served", "served")
    delegate.set_primary("pinned-model")                          # an explicit pin always wins
    assert delegate.resolve_primary()["source"] == "primary"


def test_api_tool_map_grants_code_search_but_keeps_run_tests_git_behind_bash():
    """The api/local providers had no path to code_search/run_tests/git at all — folding them
    into the existing Read/Grep/Bash CLI-style buckets closes that gap without adding a new
    tier. run_tests/git ride on Bash, not Write: the CLI providers (claude/codex) never grant
    execution at the plain "write" tier either (no Bash in that spec), so Write staying
    file-edit-only keeps every provider's "write" tier meaning the same thing."""
    assert "code_search" in delegate._API_TOOL_MAP["Read"]
    assert "code_search" in delegate._API_TOOL_MAP["Grep"]
    assert "run_tests" not in delegate._API_TOOL_MAP["Write"]
    assert "git" not in delegate._API_TOOL_MAP["Write"]
    assert "run_tests" in delegate._API_TOOL_MAP["Bash"]
    assert "git" in delegate._API_TOOL_MAP["Bash"]


def test_api_only_tools_translates_write_to_file_edit_only():
    names = delegate._api_only_tools("Write")
    assert names == {"write_file", "make_folder"}
    assert "run_tests" not in names and "git" not in names


def test_api_only_tools_translates_bash_to_execution_including_tests_and_git():
    names = delegate._api_only_tools("Bash")
    assert names == {"run_shell", "python_exec", "run_tests", "git"}


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
    assert "--disallowedTools Skill,Agent,Workflow,SendMessage" in argv
    assert "Never invoke Claude's native Skill" in argv
    assert "Never use native Agent/Workflow/Task tools" in argv


def test_to_claude_stream_isolated_resident_disables_inherited_surfaces(
        monkeypatch, tmp_path):
    argv_file = tmp_path / "argv.txt"
    env_file = tmp_path / "env.txt"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, os, sys\n"
        f"open({str(argv_file)!r}, 'w').write(repr(sys.argv))\n"
        f"open({str(env_file)!r}, 'w').write(os.environ.get('CLAUDE_CODE_DISABLE_BACKGROUND_TASKS', ''))\n"
        "print(json.dumps({'type':'result','result':'ok','is_error':False,'num_turns':1}))\n")
    shim = tmp_path / "claude"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    result = delegate.to_claude_stream(
        "do it", cwd=str(tmp_path), isolated_resident=True)
    assert result["ok"] is True
    argv = argv_file.read_text()
    assert "--setting-sources" in argv and "--disable-slash-commands" in argv
    assert "--permission-mode" in argv and "dontAsk" in argv
    assert env_file.read_text() == "1"


def test_to_codex_skills_true_uses_a_private_subagent_home_and_keeps_the_scoped_config(monkeypatch, tmp_path):
    """skills=True must load a PRIVATE, one-off CODEX_HOME (never the old single shared one — two
    concurrent codex processes sharing a home corrupt each other's session state, which is what
    made parallel orchestrate-node agent spawns fail) with the scoped bridge's own config.toml —
    and must NOT pass --ignore-user-config (which would block loading it), unlike the plain
    contained delegate path. The private home must be discarded once codex exits."""
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
    onehome = tmp_path / "codex-home-subagent-deadbeef"
    monkeypatch.setattr(codex_mind, "new_subagent_home", lambda: onehome)
    monkeypatch.setattr(codex_mind, "ensure_subagent_home", lambda home: {"ok": True, "home": str(home)})
    discarded = []
    monkeypatch.setattr(codex_mind, "discard_subagent_home", lambda home: discarded.append(home))
    r = delegate.to_codex("do it", cwd=str(tmp_path), skills=True)
    assert r["ok"] is True
    out = argv_file.read_text()
    argv_line, env_home = out.split("\n", 1)
    assert "--ignore-user-config" not in argv_line
    assert env_home == str(onehome)
    assert discarded == [onehome]              # cleaned up after the process exited


def test_to_codex_skills_true_discards_the_private_home_even_on_failure(monkeypatch, tmp_path):
    from oceano import codex_mind
    monkeypatch.setattr("oceano.delegate.find_codex", lambda: str(tmp_path / "nonexistent-codex"))
    onehome = tmp_path / "codex-home-subagent-cafef00d"
    monkeypatch.setattr(codex_mind, "new_subagent_home", lambda: onehome)
    monkeypatch.setattr(codex_mind, "ensure_subagent_home", lambda home: {"ok": False, "error": "no auth"})
    discarded = []
    monkeypatch.setattr(codex_mind, "discard_subagent_home", lambda home: discarded.append(home))
    r = delegate.to_codex("do it", cwd=str(tmp_path), skills=True)
    assert r["ok"] is False and "no auth" in r["error"]
    assert discarded == [onehome]


def test_to_codex_error_surfaces_the_error_line_not_the_echoed_prompt(monkeypatch, tmp_path):
    """codex's own stderr on failure opens with its startup banner and echoes the whole prompt
    back before the real reason — a naive head-truncation buried the actual error (e.g. a usage
    limit) under that noise for any non-trivial prompt. The real "ERROR:" line must survive."""
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import sys\n"
        "banner = 'Reading prompt from stdin...\\n' + ('x' * 500) + '\\n'\n"
        "sys.stderr.write(banner + 'ERROR: You have hit your usage limit. Try again at 5:15 PM.\\n')\n"
        "sys.exit(1)\n")
    shim = tmp_path / "codex"
    shim.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setattr("oceano.delegate.find_codex", lambda: str(shim))
    monkeypatch.setattr("oceano.codex_mind.ensure_auth", lambda: (True, ""))
    r = delegate.to_codex("do it", cwd=str(tmp_path))
    assert r["ok"] is False
    assert "usage limit" in r["error"]
    assert "xxxx" not in r["error"]


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


def test_claude_progress_preserves_tool_ids_arguments_and_error_flag(monkeypatch, tmp_path):
    shim, _argv, _calls = _fake_claude_two_calls(
        tmp_path,
        first_lines=[
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "tool-7", "name": "Write",
                "input": {"file_path": "app.py", "content": "print(1)"}}]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-7",
                "content": "permission denied", "is_error": True}]}},
            {"type": "result", "result": "recovered", "is_error": False, "num_turns": 1},
        ],
        second_lines=[])
    monkeypatch.setattr("oceano.delegate.find_claude", lambda: str(shim))
    events = []
    result = delegate.to_claude_stream("do it", cwd=str(tmp_path), on_progress=events.append)
    assert result["ok"] is True
    call = next(event for event in events if event["kind"] == "tool")
    outcome = next(event for event in events if event["kind"] == "tool_result")
    assert call["tool_use_id"] == "tool-7"
    assert call["args"] == {"file_path": "app.py", "content": "print(1)"}
    assert outcome["tool_use_id"] == "tool-7" and outcome["is_error"] is True
