"""run_shell streams its output live to oceano.shellfeed (the per-chat read-only spectator
panel), not to the chat itself — this covers the shell-feed contract: the command echo, live
chunks, and completion footer it pushes (tagged with the current turn's session, so it lands in
the right chat's panel), plus that the returned string (for the model) is unchanged in shape
(exit code + output, or a timeout)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
from oceano import shellfeed, turnctx  # noqa: E402
from oceano.tools import shell  # noqa: E402


@pytest.fixture
def pushed(monkeypatch):
    got = []
    monkeypatch.setattr(shellfeed, "push", lambda text, session=None: got.append(text))
    return got


def test_run_shell_pushes_a_command_echo_then_output_then_exit_footer(pushed):
    out = shell.run_shell("echo hi")
    assert out == "(exit 0)\nhi"
    assert pushed[0] == "\x1b[2m$ echo hi\x1b[0m\r\n"
    assert "".join(pushed[1:-1]) == "hi\n"
    assert pushed[-1] == "\x1b[2m(exit 0)\x1b[0m\r\n\r\n"


def test_run_shell_preserves_carriage_returns_raw(pushed):
    # \r-driven progress bars must reach the feed as raw bytes (readline() would instead
    # swallow everything up to the next '\n', so a progress bar would appear frozen until done).
    shell.run_shell(r'printf "a"; sleep 0.05; printf "\rb"; sleep 0.05; printf "\rc\n"')
    body = pushed[1:-1]                # strip the command echo and the exit footer
    assert "".join(body) == "a\rb\rc\n"
    assert len(body) >= 2              # the sleeps force at least two separate OS-level reads


def test_run_shell_with_nobody_watching_still_works():
    # shellfeed.push is real here (not monkeypatched) — must be a safe no-op with 0 listeners.
    assert shell.run_shell("echo ok") == "(exit 0)\nok"


def test_run_shell_no_output_and_nonzero_exit_still_reported_correctly(pushed):
    assert shell.run_shell("true") == "(exit 0, no output)"
    assert shell.run_shell("exit 7") == "(exit 7, no output)"
    assert pushed[-1] == "\x1b[2m(exit 7)\x1b[0m\r\n\r\n"


def test_run_shell_times_out_kills_the_process_and_pushes_a_timeout_footer(monkeypatch, pushed):
    monkeypatch.setattr(config, "SHELL_TIMEOUT", 1)
    out = shell.run_shell("sleep 5; echo should-not-appear")
    assert out.startswith("(timed out after 1s)")
    assert "should-not-appear" not in out
    assert pushed[-1] == "\x1b[2m(timed out after 1s)\x1b[0m\r\n\r\n"


def test_run_shell_tags_every_push_with_the_current_turns_session(monkeypatch):
    seen = []
    monkeypatch.setattr(shellfeed, "push", lambda text, session=None: seen.append(session))
    with turnctx.push(session="chat-7"):
        shell.run_shell("echo hi")
    assert seen and all(s == "chat-7" for s in seen)


def test_run_shell_outside_any_turn_context_tags_none(monkeypatch):
    seen = []
    monkeypatch.setattr(shellfeed, "push", lambda text, session=None: seen.append(session))
    shell.run_shell("echo hi")
    assert seen and all(s is None for s in seen)


def test_run_shell_keeps_the_TAIL_of_long_output(pushed):
    """Regression: the old capture kept the first 8000 chars and dropped the rest — but a build/test
    failure prints LAST, so exactly the useful part was truncated away. Now the end is preserved."""
    # ~40k chars of noise, then the line that actually matters at the very end.
    out = shell.run_shell(r'for i in $(seq 1 4000); do echo "noise line number $i padding padding"; done; echo "FATAL: build failed"')
    assert "FATAL: build failed" in out, "the end of long output (where errors land) must survive"
    assert "elided" in out, "the middle should be marked as elided, not silently dropped"
    assert len(out) < shell._OUT_HEAD + shell._OUT_TAIL + 200


def test_run_shell_short_output_is_returned_verbatim(pushed):
    out = shell.run_shell(r'printf "alpha\nbeta\ngamma\n"')
    assert out == "(exit 0)\nalpha\nbeta\ngamma"
