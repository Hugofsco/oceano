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
    try:
        assert mindbridge.run_tool("__probe_sess", {}, session="chatZ") == "S=chatZ"
        assert mindbridge.active_session() is None       # restored after the call
    finally:
        tools.unregister_prefix("__probe_sess")


def test_catalog_carries_originating_workspace_across_bridge_threads(tmp_path):
    from oceano import tools
    from oceano.tools import core
    tools.register("__probe_workspace",
                   {"type": "function", "function": {"name": "__probe_workspace",
                    "parameters": {"type": "object", "properties": {}}}},
                   lambda: str(core._ws()))
    try:
        with tools.background_workspace(tmp_path):
            catalog_id, _route = mindbridge.create_catalog(
                "workspace probe", "fixture", max_calls=1, force=False)
        result = mindbridge.run_tool("__probe_workspace", {}, catalog_id=catalog_id)
        assert result == str(tmp_path.resolve())
        assert core._ws() != tmp_path.resolve()
    finally:
        tools.unregister_prefix("__probe_workspace")


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
    seen = {}
    try:
        def call(key, client):
            seen[key] = mindbridge.run_tool("__probe_client", {}, client=client)

        t1 = threading.Thread(target=call, args=("desktop", "desktop"))
        t2 = threading.Thread(target=call, args=("web", "web"))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert seen == {"desktop": "CLI=desktop", "web": "CLI=web"}
    finally:
        tools.unregister_prefix("__probe_client")


def test_desktop_tools_are_allowed_to_the_mind():
    """Desktop tools travel in the full registry; their runtime client/taint gates still apply."""
    names = set(mindbridge.tool_names())
    for name in ("desktop_notify", "desktop_pick_file", "desktop_save_file", "desktop_reveal_path",
                 "desktop_open_path", "desktop_clipboard_read", "desktop_clipboard_write", "desktop_screenshot"):
        assert name in names, name


def test_run_tool_desktop_gate_follows_the_threaded_client():
    """End-to-end through the actual bridge call, not just turnctx: a desktop-client call reaches
    (and is refused only by) the desktopbridge "not connected" gate; a plain web-client call is
    refused earlier, by the client gate itself."""
    out_web = mindbridge.run_tool("desktop_notify", {"title": "t", "body": "b"}, client="web")
    assert "only available when chatting through the OceanoDesktop app" in out_web
    out_desktop = mindbridge.run_tool("desktop_notify", {"title": "t", "body": "b"}, client="desktop")
    assert "couldn't show the notification" in out_desktop and "isn't connected" in out_desktop


def test_skills_scope_exposes_only_list_and_load_skill():
    """The contained sub-agent bridge (workflow Delegate/Agent-spawn nodes) must see list_skills/
    load_skill and NOTHING else — no memory, no learn_skill, no web/mail/ssh/the rest of the body."""
    names = set(mindbridge.tool_names(scope="skills"))
    assert names == {"list_skills", "load_skill"}
    for leaked in ("remember", "recall", "update_memory", "forget_memory", "learn_skill",
                   "web_search", "spawn_agent"):
        assert leaked not in names
    # the unscoped (full) bridge still has everything, unaffected
    assert "remember" in mindbridge.tool_names()


def test_unknown_scope_fails_closed():
    assert mindbridge.tool_names(scope="unknown-scope") == []
    result = mindbridge.run_tool_result("remember", {}, scope="unknown-scope")
    assert result.code == "not_allowed"


def test_run_tool_scope_refuses_tools_outside_the_scope():
    out_of_scope = mindbridge.run_tool("remember", {}, scope="skills")
    assert "not available to the mind" in out_of_scope
    # a tool that IS in scope actually runs (list_skills is a plain read, safe to call for real)
    in_scope = mindbridge.run_tool("list_skills", {}, scope="skills")
    assert "not available to the mind" not in in_scope


def test_mcp_config_carries_the_scope(tmp_path, monkeypatch):
    """A scoped config gets its own filename (never clobbers/is clobbered by the unscoped one) and
    forwards OCEANO_MCP_SCOPE so the daemon narrows /api/mcp/tools + /api/mcp/call to that scope."""
    import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    import json as _json
    from pathlib import Path as _Path
    p = mindbridge.mcp_config_path(background=True, scope="skills")
    cfg = _json.loads(_Path(p).read_text())
    assert p.endswith("mind-mcp-bg-skills.json")
    assert cfg["mcpServers"]["oceano"]["env"]["OCEANO_MCP_SCOPE"] == "skills"
    # the plain background config (no scope) must not carry it or collide with the scoped filename
    p0 = mindbridge.mcp_config_path(background=True)
    assert p0.endswith("mind-mcp-bg.json")
    assert "OCEANO_MCP_SCOPE" not in _json.loads(_Path(p0).read_text())["mcpServers"]["oceano"]["env"]


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
    seen = {}
    try:
        def call(key, background):
            seen[key] = mindbridge.run_tool("__probe_chan", {}, background=background)

        t1 = threading.Thread(target=call, args=("bg", True))
        t2 = threading.Thread(target=call, args=("web", False))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert seen == {"bg": "CH=background", "web": "CH=web"}
    finally:
        tools.unregister_prefix("__probe_chan")


def _resident_hybrid(monkeypatch, tmp_path):
    from oceano import toolrouter
    config_path = tmp_path / "resident-tools.toml"
    config_path.write_text(
        '[default]\nmode = "full"\n'
        '[surfaces.resident]\nmode = "hybrid"\nschema_budget = 500\n'
        'max_schema_budget = 3000\ndiscovery = true\n')
    monkeypatch.setenv("OCEANO_TOOL_CONFIG", str(config_path))
    toolrouter._CACHE.update({"path": None, "mtime": None, "data": {}})


def test_resident_catalog_is_routed_discoverable_and_budgeted(monkeypatch, tmp_path):
    _resident_hybrid(monkeypatch, tmp_path)
    catalog_id, route = mindbridge.create_catalog("do it", "claude:test", max_calls=2)
    assert route.enabled is True
    assert "discover_tools" in mindbridge.tool_names(catalog_id=catalog_id)
    result = mindbridge.run_tool(
        "discover_tools", {"query": "inspect calendar", "operation": "load"},
        catalog_id=catalog_id)
    assert '"loaded"' in result
    assert "calendar_events" in mindbridge.tool_names(catalog_id=catalog_id)
    # Discovery consumed one call; reserve the second without touching the real calendar,
    # then prove a third operation is rejected before execution.
    assert mindbridge.consume_catalog_call(catalog_id, "calendar_events")[0] is True
    blocked = mindbridge.run_tool("calendar_events", {}, catalog_id=catalog_id)
    assert "budget exhausted" in blocked
    assert mindbridge.catalog_status(catalog_id)["calls"] == 2


def test_resident_catalog_rejects_unadvertised_and_expired_ids(monkeypatch, tmp_path):
    _resident_hybrid(monkeypatch, tmp_path)
    catalog_id, _route = mindbridge.create_catalog("calendar", "codex:test", max_calls=2)
    assert "not advertised" in mindbridge.run_tool("mail_send", {}, catalog_id=catalog_id)
    assert mindbridge.tool_schemas(catalog_id="missing") == []
    assert "expired or is invalid" in mindbridge.run_tool(
        "calendar_events", {}, catalog_id="missing")


def test_mcp_config_carries_opaque_catalog_id(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    path = mindbridge.mcp_config_path("chatQ", catalog_id="opaque-catalog-123")
    cfg = json.loads(Path(path).read_text())
    env = cfg["mcpServers"]["oceano"]["env"]
    assert env["OCEANO_MCP_CATALOG"] == "opaque-catalog-123"
    assert path.endswith("mind-mcp-chatQ-cat-opaque-c.json")


def test_catalog_replay_does_not_execute_or_consume_budget_twice(monkeypatch, tmp_path):
    from oceano import toolrouter, tools
    _resident_hybrid(monkeypatch, tmp_path)
    config_path = tmp_path / "resident-tools-wide.toml"
    config_path.write_text(
        '[surfaces.resident]\nmode = "hybrid"\nschema_budget = 3000\n'
        'max_schema_budget = 4000\ndiscovery = true\n')
    monkeypatch.setenv("OCEANO_TOOL_CONFIG", str(config_path))
    toolrouter._CACHE.update({"path": None, "mtime": None, "data": {}})
    calls = []
    monkeypatch.setitem(
        tools._TOOLS, "write_file",
        lambda path, content: calls.append((path, content)) or f"wrote {path}")
    catalog_id, route = mindbridge.create_catalog(
        "write a Python file", "codex:test", max_calls=1)
    assert "write_file" in route.names
    args = {"path": "app.py", "content": "print(1)"}
    first = mindbridge.run_tool_result(
        "write_file", args, catalog_id=catalog_id, operation_id="write-1")
    replay = mindbridge.run_tool_result(
        "write_file", args, catalog_id=catalog_id, operation_id="write-1")
    assert first.ok is True and replay.to_wire() == first.to_wire()
    assert calls == [("app.py", "print(1)")]
    assert mindbridge.catalog_status(catalog_id)["calls"] == 1


def test_concurrent_duplicate_side_effect_waits_for_first_result():
    from oceano import tools
    entered = threading.Event()
    release = threading.Event()
    calls = []
    schema = {
        "type": "function",
        "function": {
            "name": "concurrent_mutation_probe",
            "description": "Controlled concurrent mutation",
            "parameters": {"type": "object", "properties": {}},
        },
        "x-oceano": {
            "capability": "concurrent_mutation",
            "side_effecting": True,
            "idempotent": False,
        },
    }

    def mutate():
        calls.append("called")
        entered.set()
        assert release.wait(2)
        return "done"

    tools.register("concurrent_mutation_probe", schema, mutate)
    results = []

    def invoke():
        results.append(mindbridge.run_tool_result(
            "concurrent_mutation_probe", {}, operation_id="concurrent-operation"))

    try:
        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        assert entered.wait(2)
        second.start()
        release.set()
        first.join(2)
        second.join(2)
        assert calls == ["called"]
        assert len(results) == 2
        assert results[0].to_wire() == results[1].to_wire()
    finally:
        release.set()
        tools.unregister_prefix("concurrent_mutation_probe")


def test_catalog_is_bound_to_its_turn_context(monkeypatch, tmp_path):
    _resident_hybrid(monkeypatch, tmp_path)
    catalog_id, _route = mindbridge.create_catalog(
        "calendar", "codex:test", 3, session="chat-a", client="desktop")
    try:
        assert mindbridge.tool_schemas(
            catalog_id=catalog_id, session="chat-b", client="desktop") == []
        rejected = mindbridge.run_tool_result(
            "discover_tools", {}, catalog_id=catalog_id,
            session="chat-a", client="web")
        assert rejected.code == "catalog_context_mismatch"
        assert mindbridge.tool_names(
            catalog_id=catalog_id, session="chat-a", client="desktop")
    finally:
        assert mindbridge.close_catalog(catalog_id) is True
    assert mindbridge.catalog_status(catalog_id) is None


def test_catalog_lru_cap_is_a_crash_fallback(monkeypatch, tmp_path):
    _resident_hybrid(monkeypatch, tmp_path)
    with mindbridge._CATALOG_LOCK:
        prior = dict(mindbridge._CATALOGS)
        mindbridge._CATALOGS.clear()
    monkeypatch.setattr(mindbridge, "_CATALOG_MAX", 2)
    try:
        identifiers = [mindbridge.create_catalog(
            "calendar", "codex:test", 2, session=f"chat-{index}")[0]
            for index in range(3)]
        assert mindbridge.catalog_inventory() == {"active": 2, "limit": 2}
        assert mindbridge.catalog_status(identifiers[0]) is None
        assert all(mindbridge.catalog_status(item) is not None for item in identifiers[1:])
    finally:
        with mindbridge._CATALOG_LOCK:
            mindbridge._CATALOGS.clear()
            mindbridge._CATALOGS.update(prior)


def test_continuation_catalog_hides_and_blocks_spawn_agent():
    catalog_id, _route = mindbridge.create_catalog(
        "coordinate background work", "claude:test", 3, force=False)
    try:
        assert "spawn_agent" in mindbridge.tool_names(catalog_id=catalog_id)
        assert mindbridge.block_catalog_tools(catalog_id, {"spawn_agent"}) is True
        assert "spawn_agent" not in mindbridge.tool_names(catalog_id=catalog_id)
        result = mindbridge.run_tool_result(
            "spawn_agent", {"task": "duplicate"}, catalog_id=catalog_id)
        assert result.code == "continuation_tool_blocked"
    finally:
        mindbridge.close_catalog(catalog_id)
