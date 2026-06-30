"""The anti-exfiltration gate: run_shell / python_exec must refuse once a turn has
ingested untrusted content (web page / email / document), so an injected instruction
can't run a command to read secrets and exfiltrate them. The clean-turn case must
still work — the gate must not over-block normal shell use.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import safety, tools  # noqa: E402 - after the sys.path bootstrap


def teardown_function(_):
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()


def test_clean_turn_allows_shell():
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()
    assert tools._shell_blocked() is None
    assert "hello-oceano" in tools.run_shell("echo hello-oceano")


def test_shell_blocked_after_untrusted_read():
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()
    safety.wrap_untrusted("web", "...some fetched page text...")   # marks this turn tainted
    assert tools._shell_blocked() == tools._SHELL_TAINTED
    # both execution tools refuse — and refuse BEFORE running anything
    assert tools.run_shell("echo should-not-run") == tools._SHELL_TAINTED
    assert tools.python_exec("print('nope')") == tools._SHELL_TAINTED


def test_bridge_taint_also_blocks():
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()
    safety.mark_bridge_untrusted()                                 # the mind-bridge taint path
    assert tools.run_shell("echo x") == tools._SHELL_TAINTED
    safety.reset_bridge_untrusted()
    assert tools._shell_blocked() is None
