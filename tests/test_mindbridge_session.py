"""The mind-bridge per-turn routing: which chat a bridged tool call (and so a spawn_job it makes)
belongs to, and which CHANNEL it runs on (web vs background). Both are per-call attributes carried
through the bridge — NOT process-globals — so two mind turns for different sessions running at the
same instant never misattribute each other's jobs, and an unattended (scheduled) turn never drags a
concurrent interactive chat onto the background channel. These tests pin those guarantees — the
whole point of the fix over the earlier single-global approaches (`_tls.session` replaced a global
sid; the `background` arg replaced the global `_bg_mind_turns` counter).
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import mindbridge  # noqa: E402 - after the sys.path bootstrap


def test_session_is_threadlocal_not_global():
    """Two overlapping turns, each in its own thread, must each see ONLY its own session."""
    seen, barrier = {}, threading.Barrier(2)

    def turn(sid):
        with mindbridge.session(sid):
            barrier.wait()                 # guarantee both are inside session() simultaneously
            time.sleep(0.05)
            seen[sid] = mindbridge.active_session()

    t1 = threading.Thread(target=turn, args=("chatA",))
    t2 = threading.Thread(target=turn, args=("chatB",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert seen == {"chatA": "chatA", "chatB": "chatB"}
    assert mindbridge.active_session() is None      # cleared on exit


def test_session_nests_and_restores():
    assert mindbridge.active_session() is None
    with mindbridge.session("outer"):
        assert mindbridge.active_session() == "outer"
        with mindbridge.session("inner"):
            assert mindbridge.active_session() == "inner"
        assert mindbridge.active_session() == "outer"   # restored, not cleared to None
    assert mindbridge.active_session() is None


def test_run_tool_marks_then_restores_the_call_thread():
    from oceano import tools
    tools.register("__probe_sess",
                   {"type": "function", "function": {"name": "__probe_sess",
                    "parameters": {"type": "object", "properties": {}}}},
                   lambda: "S=" + str(mindbridge.active_session()))
    mindbridge._ALLOW.add("__probe_sess")
    try:
        assert mindbridge.run_tool("__probe_sess", {}, session="chatZ") == "S=chatZ"
        assert mindbridge.active_session() is None       # restored after the call
    finally:
        mindbridge._ALLOW.discard("__probe_sess")
        tools.unregister_prefix("__probe_sess")


def test_per_session_mcp_config_carries_the_sid(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    p = mindbridge.mcp_config_path("chatQ")
    cfg = json.loads(Path(p).read_text())
    assert p.endswith("mind-mcp-chatQ.json")
    assert cfg["mcpServers"]["oceano"]["env"]["OCEANO_MCP_SESSION"] == "chatQ"
    # the session-less config (telegram/scheduler) must NOT leak a stale sid
    p0 = mindbridge.mcp_config_path(None)
    cfg0 = json.loads(Path(p0).read_text())
    assert p0.endswith("mind-mcp.json")
    assert "OCEANO_MCP_SESSION" not in cfg0["mcpServers"]["oceano"]["env"]


def test_mcp_config_carries_the_background_flag(tmp_path, monkeypatch):
    """A background turn's config sets OCEANO_MCP_BACKGROUND and gets its OWN file, so a
    session-less scheduler turn can never clobber (or be clobbered by) a concurrent
    session-less interactive turn's config."""
    import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    p = mindbridge.mcp_config_path("chatQ", background=True)
    cfg = json.loads(Path(p).read_text())
    assert p.endswith("mind-mcp-chatQ-bg.json")
    assert cfg["mcpServers"]["oceano"]["env"]["OCEANO_MCP_BACKGROUND"] == "1"
    p0 = mindbridge.mcp_config_path(None, background=True)
    cfg0 = json.loads(Path(p0).read_text())
    assert p0.endswith("mind-mcp-bg.json")
    assert cfg0["mcpServers"]["oceano"]["env"]["OCEANO_MCP_BACKGROUND"] == "1"
    # an interactive config must NOT carry the flag
    pi = mindbridge.mcp_config_path("chatQ")
    assert "OCEANO_MCP_BACKGROUND" not in json.loads(Path(pi).read_text())["mcpServers"]["oceano"]["env"]


def test_mcp_config_carries_the_client_flag(tmp_path, monkeypatch):
    """A turn that started via OceanoDesktop gets OCEANO_MCP_CLIENT=desktop and its own config
    file, so oceano/tools/desktop.py's tools unlock for the mind on that turn — and a plain web
    turn's config must NOT carry the flag (the default, so it doesn't need its own filename)."""
    import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    p = mindbridge.mcp_config_path("chatQ", client="desktop")
    cfg = json.loads(Path(p).read_text())
    assert p.endswith("mind-mcp-chatQ-desktop.json")
    assert cfg["mcpServers"]["oceano"]["env"]["OCEANO_MCP_CLIENT"] == "desktop"
    # the default (web) config must NOT carry the flag or get a suffixed filename
    p0 = mindbridge.mcp_config_path("chatQ")
    cfg0 = json.loads(Path(p0).read_text())
    assert p0.endswith("mind-mcp-chatQ.json")
    assert "OCEANO_MCP_CLIENT" not in cfg0["mcpServers"]["oceano"]["env"]
    # background + desktop together get both suffixes, no clobbering either concurrent config
    pb = mindbridge.mcp_config_path("chatQ", background=True, client="desktop")
    assert pb.endswith("mind-mcp-chatQ-bg-desktop.json")


def test_run_tool_client_is_per_call_not_global():
    """Two OVERLAPPING bridged calls — one from OceanoDesktop, one from a plain browser tab —
    must each see only their own client, same guarantee as channel above."""
    from oceano import tools
    barrier = threading.Barrier(2)

    def probe():
        barrier.wait()                     # guarantee both calls are inside run_tool simultaneously
        time.sleep(0.05)
        return "CLI=" + tools.current_client()

    tools.register("__probe_client",
                   {"type": "function", "function": {"name": "__probe_client",
                    "parameters": {"type": "object", "properties": {}}}},
                   probe)
    mindbridge._ALLOW.add("__probe_client")
    seen = {}
    try:
        def call(key, client):
            seen[key] = mindbridge.run_tool("__probe_client", {}, client=client)

        t1 = threading.Thread(target=call, args=("desktop", "desktop"))
        t2 = threading.Thread(target=call, args=("web", "web"))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert seen == {"desktop": "CLI=desktop", "web": "CLI=web"}
    finally:
        mindbridge._ALLOW.discard("__probe_client")
        tools.unregister_prefix("__probe_client")


def test_desktop_tools_are_allowed_to_the_mind():
    """Regression lock: these must stay in _ALLOW or the Claude/Codex mind can never see them at
    all, no matter how the client/taint gates in oceano/tools/desktop.py are configured."""
    for name in ("desktop_notify", "desktop_pick_file", "desktop_save_file", "desktop_reveal_path",
                 "desktop_open_path", "desktop_clipboard_read", "desktop_clipboard_write", "desktop_screenshot"):
        assert name in mindbridge._ALLOW, name


def test_run_tool_desktop_gate_follows_the_threaded_client():
    """End-to-end through the actual bridge call, not just turnctx: a desktop-client call reaches
    (and is refused only by) the desktopbridge "not connected" gate; a plain web-client call is
    refused earlier, by the client gate itself."""
    out_web = mindbridge.run_tool("desktop_notify", {"title": "t", "body": "b"}, client="web")
    assert "only available when chatting through the OceanoDesktop app" in out_web
    out_desktop = mindbridge.run_tool("desktop_notify", {"title": "t", "body": "b"}, client="desktop")
    assert "couldn't show the notification" in out_desktop and "isn't connected" in out_desktop


def test_run_tool_channel_is_per_call_not_global():
    """Two OVERLAPPING bridged calls — one background, one interactive — must each run on their
    own channel. This is the regression test for the old `_bg_mind_turns` process-global, where
    any live background turn flipped EVERY concurrent session's tools to 'background'."""
    from oceano import tools
    barrier = threading.Barrier(2)

    def probe():
        barrier.wait()                     # guarantee both calls are inside run_tool simultaneously
        time.sleep(0.05)
        return "CH=" + tools.current_channel()

    tools.register("__probe_chan",
                   {"type": "function", "function": {"name": "__probe_chan",
                    "parameters": {"type": "object", "properties": {}}}},
                   probe)
    mindbridge._ALLOW.add("__probe_chan")
    seen = {}
    try:
        def call(key, background):
            seen[key] = mindbridge.run_tool("__probe_chan", {}, background=background)

        t1 = threading.Thread(target=call, args=("bg", True))
        t2 = threading.Thread(target=call, args=("web", False))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert seen == {"bg": "CH=background", "web": "CH=web"}
    finally:
        mindbridge._ALLOW.discard("__probe_chan")
        tools.unregister_prefix("__probe_chan")
