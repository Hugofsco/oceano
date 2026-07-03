"""The consolidated per-turn context (oceano.turnctx): channel, workspace override, chat
session, and injection taint in ONE ContextVar. These tests pin the two properties the old
scattered thread-locals could not give us:

  1. one bracketing discipline — tools.channel()/background_workspace(), safety taint, and
     mindbridge.session() all ride the same context and restore correctly;
  2. carry() — a worker thread can INHERIT the calling turn's whole context instead of
     silently reverting to defaults (the _run_tool_streamed regression), while its own
     mutations stay in its copy (no leak back into the caller's turn).
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import mindbridge, safety, tools, turnctx  # noqa: E402 - after the sys.path bootstrap


def teardown_function(_):
    safety.reset_untrusted()
    safety.reset_bridge_untrusted()


def test_defaults_and_push_restore():
    assert turnctx.get() == turnctx.TurnContext()          # interactive web turn by default
    with turnctx.push(channel="background", session="chatA"):
        assert turnctx.get().channel == "background"
        assert turnctx.get().session == "chatA"
        with turnctx.push(channel="telegram"):             # nested override, session kept
            assert turnctx.get().channel == "telegram"
            assert turnctx.get().session == "chatA"
        assert turnctx.get().channel == "background"       # inner restored, not cleared
    assert turnctx.get() == turnctx.TurnContext()


def test_public_apis_share_one_context():
    """channel + workspace + taint + session are four views of the SAME TurnContext."""
    with tools.background_workspace("/tmp/oceano-turnctx-test") as root:
        with mindbridge.session("chatB"):
            safety.wrap_untrusted("web", "page text")
            ctx = turnctx.get()
            assert ctx.channel == "background"
            assert ctx.workspace == root
            assert ctx.session == "chatB"
            assert ctx.tainted
            assert tools.is_background() and safety.untrusted_seen()
            assert mindbridge.active_session() == "chatB"
    assert turnctx.get() == turnctx.TurnContext()          # everything restored together


def test_fresh_thread_starts_at_defaults():
    """A raw worker thread does NOT inherit the caller's context (same as the thread-local
    era) — inheritance is opt-in via carry()."""
    seen = {}
    with tools.channel("background"):
        t = threading.Thread(target=lambda: seen.update(ctx=turnctx.get()))
        t.start(); t.join()
    assert seen["ctx"] == turnctx.TurnContext()


def test_carry_hands_the_whole_context_to_a_worker():
    """The _run_tool_streamed fix: a carried worker sees the calling turn's channel,
    workspace, session, and taint — no silent revert to an interactive web turn."""
    seen = {}

    def worker():
        seen["channel"] = tools.current_channel()
        seen["session"] = mindbridge.active_session()
        seen["tainted"] = safety.untrusted_seen()

    with tools.background(), mindbridge.session("chatC"):
        safety.wrap_untrusted("mail", "message body")
        t = threading.Thread(target=turnctx.carry(worker))
        t.start(); t.join()
    assert seen == {"channel": "background", "session": "chatC", "tainted": True}


def test_carried_worker_mutations_do_not_leak_back():
    """Taint picked up INSIDE the worker stays in its context copy — the caller's turn is
    not retroactively tainted by a worker's reads (same as the thread-local era)."""
    def worker():
        safety.wrap_untrusted("web", "fetched inside the worker")
        assert safety.untrusted_seen()

    t = threading.Thread(target=turnctx.carry(worker))
    t.start(); t.join()
    assert not safety.untrusted_seen()


def test_client_field_defaults_and_carries():
    """client (which app made the request — plain browser vs OceanoDesktop) defaults to "web" and
    rides the same context/carry discipline as channel — set in routes_chat.py from the
    X-Oceano-Client header, read via tools.current_client()/is_desktop_client()."""
    assert turnctx.get().client == "web"
    assert not tools.is_desktop_client()
    seen = {}

    def worker():
        seen["client"] = tools.current_client()
        seen["is_desktop"] = tools.is_desktop_client()

    with turnctx.push(client="desktop"):
        assert tools.current_client() == "desktop"
        assert tools.is_desktop_client()
        t = threading.Thread(target=turnctx.carry(worker))
        t.start(); t.join()
    assert seen == {"client": "desktop", "is_desktop": True}
    assert turnctx.get().client == "web"                   # restored outside the push


def test_concurrent_turns_stay_isolated():
    """Two overlapping turns on different threads each keep their own full context."""
    seen, barrier = {}, threading.Barrier(2)

    def turn(sid, chan):
        with tools.channel(chan), mindbridge.session(sid):
            if sid == "one":
                safety.wrap_untrusted("web", "page")       # taint ONLY turn one
            barrier.wait()
            seen[sid] = (tools.current_channel(), mindbridge.active_session(), safety.untrusted_seen())

    t1 = threading.Thread(target=turn, args=("one", "background"))
    t2 = threading.Thread(target=turn, args=("two", "web"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert seen == {"one": ("background", "one", True), "two": ("web", "two", False)}
