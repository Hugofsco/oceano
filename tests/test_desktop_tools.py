"""Gate tests for oceano/tools/desktop.py — same shape as ssh_run's gate stack in
test_destructive_gates.py: client → taint, then the actual RPC round trip. These run REAL native
actions on the user's computer (a notification, a file dialog, the clipboard, a screenshot), so an
injected instruction reaching them from a plain browser tab or a tainted turn must never work.

desktop_clipboard_read is the one exception: it's a pure read (not blocked by pre-existing taint,
same reasoning as mail_read/fetch_url), but its OWN result taints the turn going forward."""
import asyncio
import base64
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from oceano import desktopbridge, safety, tools, turnctx  # noqa: E402 - after the sys.path bootstrap

ACTION_TOOLS = {
    "desktop_notify": lambda: tools.desktop_notify("t", "b"),
    "desktop_pick_file": lambda: tools.desktop_pick_file(),
    "desktop_save_file": lambda: tools.desktop_save_file(),
    "desktop_reveal_path": lambda: tools.desktop_reveal_path("/tmp/x"),
    "desktop_open_path": lambda: tools.desktop_open_path("/tmp/x"),
    "desktop_clipboard_write": lambda: tools.desktop_clipboard_write("hi"),
    "desktop_screenshot": lambda: tools.desktop_screenshot(),
}


def teardown_function(_):
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()


def _round_trip(fn, resolve_ok=True, resolve_result=None):
    """Run `fn` (a zero-arg callable wrapping a tools.desktop_* call) on a worker thread as a
    client="desktop" turn, while a fake "desktop app" answers the first command it pushes.
    Returns (pushed_cmd, tool_return_value, tainted_after). tainted_after is read INSIDE the same
    thread/context the call ran in — a bare threading.Thread doesn't share contextvars with the
    caller (see test_turnctx.py's test_fresh_thread_starts_at_defaults), so checking taint from out
    here afterward would always read the outer (untainted) context, not the worker's."""
    async def scenario():
        loop = asyncio.get_running_loop()
        q = desktopbridge.subscribe(loop)
        result_holder = {}

        def caller():
            with turnctx.push(client="desktop"):
                result_holder["out"] = fn()
                result_holder["tainted"] = safety.untrusted_seen()

        t = threading.Thread(target=caller)
        t.start()
        cmd = await q.get()
        desktopbridge.resolve(cmd["id"], resolve_ok, resolve_result)
        t.join(timeout=3)
        desktopbridge.unsubscribe(q)
        return cmd, result_holder["out"], result_holder["tainted"]
    return asyncio.run(scenario())


def test_all_action_tools_refuse_on_a_plain_browser_tab():
    for name, fn in ACTION_TOOLS.items():
        assert "only available when chatting through the OceanoDesktop app" in fn(), name


def test_all_action_tools_blocked_after_untrusted_content():
    with turnctx.push(client="desktop"):
        safety.wrap_untrusted("web", "page text")
        for name, fn in ACTION_TOOLS.items():
            assert "Blocked for safety" in fn(), name


def test_all_action_tools_blocked_by_bridge_taint_too():
    with turnctx.push(client="desktop"):
        safety.mark_bridge_untrusted()
        for name, fn in ACTION_TOOLS.items():
            assert "Blocked for safety" in fn(), name


def test_desktop_notify_reports_no_desktop_app_connected():
    with turnctx.push(client="desktop"):
        assert "couldn't show the notification" in tools.desktop_notify("t", "b")


def test_desktop_notify_round_trip():
    cmd, out, _ = _round_trip(lambda: tools.desktop_notify("Job done", "your report is ready"), True, True)
    assert cmd["action"] == "notify" and cmd["title"] == "Job done"
    assert out == "notification shown"


def test_desktop_pick_file_round_trip_and_cancel():
    cmd, out, _ = _round_trip(lambda: tools.desktop_pick_file(title="Import", kind="file"),
                               True, "/home/user/workspace/report.csv")
    assert cmd["action"] == "pick-file" and cmd["kind"] == "file"
    assert out == "chosen path: /home/user/workspace/report.csv"
    _, out2, _ = _round_trip(lambda: tools.desktop_pick_file(), True, None)
    assert out2 == "the user cancelled the picker"


def test_desktop_save_file_round_trip_and_cancel():
    cmd, out, _ = _round_trip(lambda: tools.desktop_save_file(title="Save report", default_name="r.csv"),
                               True, "/home/user/workspace/r.csv")
    assert cmd["action"] == "save-file" and cmd["default_name"] == "r.csv"
    assert out == "save path: /home/user/workspace/r.csv"
    _, out2, _ = _round_trip(lambda: tools.desktop_save_file(), True, None)
    assert out2 == "the user cancelled the save dialog"


def test_desktop_reveal_path_round_trip():
    cmd, out, _ = _round_trip(lambda: tools.desktop_reveal_path("/home/user/workspace/x.txt"), True, True)
    assert cmd["action"] == "reveal-path" and cmd["path"] == "/home/user/workspace/x.txt"
    assert "revealed" in out


def test_desktop_open_path_round_trip_and_failure():
    cmd, out, _ = _round_trip(lambda: tools.desktop_open_path("/home/user/workspace/x.pdf"), True, True)
    assert cmd["action"] == "open-path"
    assert "opened" in out
    _, out2, _ = _round_trip(lambda: tools.desktop_open_path("/no/such/file"), False, "no application registered")
    assert "couldn't open" in out2 and "no application registered" in out2


def test_desktop_clipboard_read_is_not_blocked_by_pre_existing_taint():
    """Unlike the action tools, a read is allowed even after the turn already saw untrusted content."""
    with turnctx.push(client="desktop"):
        safety.wrap_untrusted("web", "page text")
        cmd, out, _ = _round_trip(lambda: tools.desktop_clipboard_read(), True, "secret-token-123")
    assert cmd["action"] == "clipboard-read"
    assert "secret-token-123" in out
    assert out.startswith('<untrusted source="clipboard">')


def test_desktop_clipboard_read_refuses_on_a_plain_browser_tab():
    assert "only available when chatting through the OceanoDesktop app" in tools.desktop_clipboard_read()


def test_desktop_clipboard_read_taints_the_turn_going_forward():
    _, _, tainted = _round_trip(lambda: tools.desktop_clipboard_read(), True, "whatever was copied")
    assert tainted


def test_desktop_clipboard_read_empty():
    cmd, out, _ = _round_trip(lambda: tools.desktop_clipboard_read(), True, "")
    assert cmd["action"] == "clipboard-read"
    assert "empty" in out


def test_desktop_clipboard_write_round_trip():
    cmd, out, _ = _round_trip(lambda: tools.desktop_clipboard_write("paste me"), True, True)
    assert cmd["action"] == "clipboard-write" and cmd["text"] == "paste me"
    assert out == "copied to clipboard"


def test_desktop_screenshot_round_trip_saves_into_the_workspace():
    png_bytes = b"\x89PNG\r\n\x1a\nfake-but-good-enough-for-a-round-trip-test"
    b64 = base64.b64encode(png_bytes).decode()
    cmd, out, tainted = _round_trip(lambda: tools.desktop_screenshot(name="my-screen.png"), True, b64)
    assert cmd["action"] == "screenshot"
    saved = config.WORKSPACE / "my-screen.png"
    try:
        assert saved.read_bytes() == png_bytes
        assert "![screenshot](my-screen.png)" in out
        assert out.startswith('<untrusted source="desktop-screenshot">')   # screen content is fenced + taints
        assert tainted
    finally:
        saved.unlink(missing_ok=True)


def test_desktop_screenshot_sanitizes_a_path_traversal_filename():
    png_bytes = b"fake-png"
    b64 = base64.b64encode(png_bytes).decode()
    cmd, out, _ = _round_trip(lambda: tools.desktop_screenshot(name="../../etc/evil.png"), True, b64)
    assert cmd["action"] == "screenshot"
    escaped = (config.WORKSPACE / ".." / ".." / "etc" / "evil.png").resolve()
    assert not escaped.exists(), "must never write outside the workspace"
    saved = config.WORKSPACE / "evil.png"
    try:
        assert saved.exists()
        assert "evil.png" in out
    finally:
        saved.unlink(missing_ok=True)


def test_desktop_screenshot_capture_failure_passthrough():
    _, out, _ = _round_trip(lambda: tools.desktop_screenshot(), False,
                             "the screen capture came back empty — grant Screen Recording permission")
    assert "couldn't capture the screen" in out
    assert "Screen Recording" in out
