"""oceano.desktopbridge — the request/response sibling of uibridge.py: a tool's call() must
block until the connected desktop app answers (or time out fast if nothing is connected), and
resolve() must be a safe no-op for an id that already timed out or never existed."""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import desktopbridge  # noqa: E402 - after the sys.path bootstrap


def test_call_with_no_listener_fails_fast():
    ok, result = desktopbridge.call("notify", timeout=1)
    assert not ok
    assert "isn't connected" in result


def test_resolve_unknown_id_is_a_harmless_no_op():
    assert desktopbridge.resolve(999999, True, "whatever") is False


def test_call_times_out_if_nobody_answers():
    async def scenario():
        loop = asyncio.get_running_loop()
        q = desktopbridge.subscribe(loop)
        assert desktopbridge.listener_count() == 1
        try:
            ok, result = await loop.run_in_executor(None, lambda: desktopbridge.call("pick-file", timeout=0.3))
            assert not ok
            assert "didn't respond in time" in result
            cmd = q.get_nowait()                            # the command WAS pushed, just never answered
            assert cmd["action"] == "pick-file"
        finally:
            desktopbridge.unsubscribe(q)
        assert desktopbridge.listener_count() == 0

    asyncio.run(scenario())


def test_call_returns_the_resolved_result():
    # call() blocks the calling thread, so drive the "desktop app" side from a second thread
    # concurrently with the blocking call — mirrors how the real HTTP handler resolves it mid-wait.
    async def scenario():
        loop = asyncio.get_running_loop()
        q = desktopbridge.subscribe(loop)
        result_holder = {}

        def caller():
            result_holder["out"] = desktopbridge.call("pick-file", timeout=3, title="Import a file")

        t = threading.Thread(target=caller)
        t.start()
        cmd = await q.get()
        assert cmd["action"] == "pick-file"
        assert cmd["title"] == "Import a file"
        desktopbridge.resolve(cmd["id"], True, "/home/user/workspace/data.csv")
        t.join(timeout=3)
        desktopbridge.unsubscribe(q)
        return result_holder["out"]

    ok, result = asyncio.run(scenario())
    assert ok is True
    assert result == "/home/user/workspace/data.csv"


def test_a_late_resolve_after_timeout_does_not_crash_or_affect_a_later_call():
    async def scenario():
        loop = asyncio.get_running_loop()
        q = desktopbridge.subscribe(loop)
        try:
            ok, _ = await loop.run_in_executor(None, lambda: desktopbridge.call("notify", timeout=0.2))
            assert not ok
            cmd = q.get_nowait()
            # the id is already gone from _pending (call() popped it on timeout) — must not raise
            assert desktopbridge.resolve(cmd["id"], True, "too-late") is False
        finally:
            desktopbridge.unsubscribe(q)

    asyncio.run(scenario())
