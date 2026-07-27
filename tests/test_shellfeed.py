"""oceano.shellfeed — the per-chat shell-activity pub/sub (uibridge.py's sibling, but with a
small per-session backlog so a spectator panel opened mid-session isn't blank). The whole point
is that a chat's spectator panel never sees another chat's commands, even when both are running
in parallel — that isolation is what most of this file actually tests."""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import shellfeed  # noqa: E402 - after the sys.path bootstrap


def _reset():
    shellfeed._backlogs.clear()
    shellfeed._backlog_lens.clear()
    shellfeed._listeners.clear()


def test_push_with_no_listeners_is_a_harmless_no_op():
    _reset()
    shellfeed.push("hello\n", session="chat1")
    assert shellfeed.listener_count() == 0


def test_subscribe_then_push_delivers_to_the_listener():
    _reset()

    async def scenario():
        loop = asyncio.get_running_loop()
        q = shellfeed.subscribe(loop, "chat1")
        assert shellfeed.listener_count() == 1
        t = threading.Thread(target=lambda: shellfeed.push("hello\n", session="chat1"))
        t.start(); t.join()
        text = await asyncio.wait_for(q.get(), timeout=2)
        assert text == "hello\n"
        shellfeed.unsubscribe(q)
        assert shellfeed.listener_count() == 0

    asyncio.run(scenario())


def test_a_new_subscriber_gets_that_sessions_recent_backlog_immediately():
    _reset()
    shellfeed.push("first\n", session="chat1")
    shellfeed.push("second\n", session="chat1")

    async def scenario():
        loop = asyncio.get_running_loop()
        q = shellfeed.subscribe(loop, "chat1")
        assert q.get_nowait() == "first\n"
        assert q.get_nowait() == "second\n"
        shellfeed.unsubscribe(q)

    asyncio.run(scenario())


def test_backlog_is_trimmed_to_the_char_cap_per_session():
    _reset()
    shellfeed.push("x" * (shellfeed._BACKLOG_CAP - 10), session="chat1")
    shellfeed.push("y" * 100, session="chat1")          # pushes total over the cap
    assert shellfeed._backlog_lens["chat1"] <= shellfeed._BACKLOG_CAP
    assert "".join(shellfeed._backlogs["chat1"]).endswith("y" * 100)   # newest text always survives


def test_unsubscribe_stops_delivery():
    _reset()

    async def scenario():
        loop = asyncio.get_running_loop()
        q = shellfeed.subscribe(loop, "chat1")
        shellfeed.unsubscribe(q)
        shellfeed.push("after unsubscribe\n", session="chat1")
        assert q.empty()

    asyncio.run(scenario())


# ---------------- the actual point of this module: cross-session isolation ----------------
def test_push_to_one_session_is_invisible_to_a_listener_on_another_session():
    _reset()

    async def scenario():
        loop = asyncio.get_running_loop()
        qa = shellfeed.subscribe(loop, "chatA")
        shellfeed.push("this belongs to chatB\n", session="chatB")
        await asyncio.sleep(0)                          # let any (wrongly) queued delivery land
        assert qa.empty()
        shellfeed.unsubscribe(qa)

    asyncio.run(scenario())


def test_backlog_replay_only_returns_that_sessions_own_backlog():
    _reset()
    shellfeed.push("chatA line\n", session="chatA")
    shellfeed.push("chatB line\n", session="chatB")

    async def scenario():
        loop = asyncio.get_running_loop()
        qa = shellfeed.subscribe(loop, "chatA")
        assert qa.get_nowait() == "chatA line\n"
        assert qa.empty()                                # chatB's line never replayed here
        shellfeed.unsubscribe(qa)

    asyncio.run(scenario())


def test_listener_count_is_scoped_per_session():
    _reset()

    async def scenario():
        loop = asyncio.get_running_loop()
        q1 = shellfeed.subscribe(loop, "chat1")
        q2 = shellfeed.subscribe(loop, "chat2")
        assert shellfeed.listener_count("chat1") == 1
        assert shellfeed.listener_count("chat2") == 1
        assert shellfeed.listener_count("chat3") == 0
        assert shellfeed.listener_count() == 2           # no session arg → everyone
        shellfeed.unsubscribe(q1); shellfeed.unsubscribe(q2)

    asyncio.run(scenario())


def test_none_session_is_its_own_bucket():
    """Unattended work (scheduler/workflows/Telegram) never sets a session — push(text) defaults
    to session=None, which must behave as just another (unwatched, in practice) bucket, not
    collide with or leak into any named chat session."""
    _reset()

    async def scenario():
        loop = asyncio.get_running_loop()
        qa = shellfeed.subscribe(loop, "chat1")
        qn = shellfeed.subscribe(loop, None)
        shellfeed.push("unattended work\n")               # session=None by default
        await asyncio.sleep(0)
        assert qa.empty()
        assert qn.get_nowait() == "unattended work\n"
        shellfeed.unsubscribe(qa); shellfeed.unsubscribe(qn)

    asyncio.run(scenario())


# ---------------- session-count eviction bound ----------------
def test_session_eviction_bound():
    _reset()
    for i in range(shellfeed._MAX_SESSIONS + 5):
        shellfeed.push("x\n", session=f"chat{i}")
    assert len(shellfeed._backlogs) <= shellfeed._MAX_SESSIONS
    # the earliest sessions were the ones evicted; the most recent ones survive
    assert f"chat{shellfeed._MAX_SESSIONS + 4}" in shellfeed._backlogs
    assert "chat0" not in shellfeed._backlogs


def test_eviction_never_evicts_the_session_just_written_to():
    _reset()
    for i in range(shellfeed._MAX_SESSIONS + 5):
        shellfeed.push("x\n", session=f"chat{i}")
    # the very last push's session must always survive its own push, regardless of the cap
    assert f"chat{shellfeed._MAX_SESSIONS + 4}" in shellfeed._backlogs
