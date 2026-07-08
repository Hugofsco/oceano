"""Regression test for the MCP anti-exfiltration taint gate: every other risky tool
(ssh_run, mail_send/reply, run_shell/spawn_job/python_exec) refuses to act once a turn has
read untrusted content, so an injected instruction in a fetched page/email can't drive an
outbound action. mcp_client._call_sync() used to skip this check entirely, so a connected
write-capable MCP tool (Slack, GitHub, ...) could be triggered by the same injected content
with no gating at all. This pins the fix.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import mcp_client, safety  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_turn():
    """Every test starts (and leaves) an untainted turn."""
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()
    yield
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


class _FakeSession:
    """Stands in for the real MCP ClientSession — call_tool must never be reached once
    the turn is tainted (mirrors _boom in test_destructive_gates.py)."""
    def __init__(self):
        self.called = False

    async def call_tool(self, tool_name, kwargs):
        self.called = True
        return _FakeResult("ok")


@pytest.fixture
def mcp_session(monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(mcp_client, "_sessions", {"srv": sess})
    monkeypatch.setattr(mcp_client, "_loop", _real_loop())
    yield sess


def _real_loop():
    """_call_sync hops onto a running event loop via run_coroutine_threadsafe — give it
    the current thread's loop, running in a background thread for the duration of the test."""
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop


def test_mcp_call_works_on_a_clean_turn(mcp_session):
    out = mcp_client._call_sync("srv", "some_tool", {})
    assert out == "ok"
    assert mcp_session.called


def test_mcp_call_blocked_after_untrusted_content(mcp_session):
    safety.wrap_untrusted("web", "...a booby-trapped page...")
    out = mcp_client._call_sync("srv", "some_tool", {})
    assert "Blocked for safety" in out
    assert not mcp_session.called


def test_mcp_call_blocked_by_bridge_taint(mcp_session):
    safety.mark_bridge_untrusted()   # the Claude/Codex-mind taint path
    out = mcp_client._call_sync("srv", "some_tool", {})
    assert "Blocked for safety" in out
    assert not mcp_session.called
