"""agent._feed_shell_event: the resident mind (Claude's native "Bash", Codex's native "shell")
never calls Oceano's own run_shell, so its tool_call/tool_result SSE events are the only place
that command + output is ever visible — this hooks them into oceano.shellfeed so THIS chat's
spectator panel shows them too, not just run_shell's own live chunks, tagged with the current
turn's session so it never bleeds into a different chat's panel."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import agent, shellfeed, turnctx  # noqa: E402


@pytest.fixture
def pushed(monkeypatch):
    got = []
    monkeypatch.setattr(shellfeed, "push", lambda text, session=None: got.append(text))
    return got


def test_bash_tool_call_pushes_a_command_echo(pushed):
    ev = {"type": "tool_call", "name": "Bash", "args": "ls -la"}
    assert agent._feed_shell_event(ev) is ev              # returns the event unchanged
    assert pushed == ["\x1b[2m$ ls -la\x1b[0m\r\n"]


def test_codex_shell_tool_result_pushes_the_output(pushed):
    ev = {"type": "tool_result", "name": "shell", "result": "line1\nline2\n"}
    agent._feed_shell_event(ev)
    assert pushed == ["line1\r\nline2\r\n\r\n"]


def test_empty_result_pushes_a_no_output_marker(pushed):
    agent._feed_shell_event({"type": "tool_result", "name": "Bash", "result": ""})
    assert pushed == ["\x1b[2m(no output)\x1b[0m\r\n\r\n"]


def test_non_shell_tools_are_left_alone(pushed):
    agent._feed_shell_event({"type": "tool_call", "name": "Read", "args": "notes.md"})
    agent._feed_shell_event({"type": "tool_result", "name": "web_search", "result": "some results"})
    assert pushed == []


def test_pushes_are_tagged_with_the_current_turns_session(monkeypatch):
    seen = []
    monkeypatch.setattr(shellfeed, "push", lambda text, session=None: seen.append(session))
    with turnctx.push(session="chat-42"):
        agent._feed_shell_event({"type": "tool_call", "name": "Bash", "args": "ls"})
    assert seen == ["chat-42"]


def test_a_call_outside_any_turn_context_tags_none(monkeypatch):
    seen = []
    monkeypatch.setattr(shellfeed, "push", lambda text, session=None: seen.append(session))
    agent._feed_shell_event({"type": "tool_call", "name": "Bash", "args": "ls"})
    assert seen == [None]
