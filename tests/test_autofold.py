"""The rolling context fold (Agent._autofold): the always-on safety net that keeps a
long-running conversation from growing the per-turn prompt without bound. Properties pinned:
fires only past the char threshold; folds the OLDEST ~half while the newest _FOLD_KEEP
messages stay verbatim; the summary note rolls forward on the next overflow; a failed
summarize leaves the history untouched (the turn must still run).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import agent as agent_mod  # noqa: E402 - after the sys.path bootstrap
from oceano.agent import Agent  # noqa: E402


def _agent(monkeypatch, n_messages=40, msg_chars=1000, summary="SUMMARY-NOTE"):
    """An offline Agent with a synthetic conversation and a stubbed summarizer."""
    ag = Agent(model="test-model", base_url="http://localhost:1", api_key="k", learn=False)
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        ag.messages.append({"role": role, "content": f"m{i:03d} " + "x" * msg_chars})
    monkeypatch.setattr(Agent, "_summarize_convo", lambda self, text: summary)
    return ag


def test_no_fold_below_threshold(monkeypatch):
    monkeypatch.setattr(agent_mod, "_FOLD_CHARS", 10_000_000)
    ag = _agent(monkeypatch)
    before = list(ag.messages)
    assert ag._autofold() == 0
    assert ag.messages == before


def test_fold_keeps_recent_messages_verbatim(monkeypatch):
    monkeypatch.setattr(agent_mod, "_FOLD_CHARS", 10_000)      # 40 msgs × ~1KB ≫ threshold
    ag = _agent(monkeypatch)
    tail_before = ag.messages[-agent_mod._FOLD_KEEP:]
    folded = ag._autofold()
    assert folded > 0
    assert ag.messages[0]["role"] == "system"                  # system untouched
    assert "SUMMARY-NOTE" in ag.messages[1]["content"]         # the fold note sits up front
    assert "search_chats" in ag.messages[1]["content"]         # points at recall for the rest
    assert ag.messages[-agent_mod._FOLD_KEEP:] == tail_before  # newest messages verbatim
    # roughly half the content went into the fold, and the conversation actually shrank
    assert len(ag.messages) < 2 + 40


def test_fold_is_rolling(monkeypatch):
    """After one fold, another overflow folds the previous note too — summaries never stack."""
    monkeypatch.setattr(agent_mod, "_FOLD_CHARS", 10_000)
    ag = _agent(monkeypatch)
    assert ag._autofold() > 0
    for i in range(40):                                        # grow past the threshold again
        ag.messages.append({"role": "user", "content": f"n{i:03d} " + "y" * 1000})
    monkeypatch.setattr(Agent, "_summarize_convo", lambda self, text: "SECOND-SUMMARY")
    assert ag._autofold() > 0
    notes = [m for m in ag.messages if "folded to keep the context small" in str(m.get("content"))]
    assert len(notes) == 1                                     # exactly one fold note, rolled forward
    assert "SECOND-SUMMARY" in notes[0]["content"]


def test_failed_summarize_leaves_history_untouched(monkeypatch):
    monkeypatch.setattr(agent_mod, "_FOLD_CHARS", 10_000)
    ag = _agent(monkeypatch)

    def boom(self, text):
        raise RuntimeError("summarizer offline")
    monkeypatch.setattr(Agent, "_summarize_convo", boom)
    before = list(ag.messages)
    assert ag._autofold() == 0
    assert ag.messages == before


def test_fold_disabled_by_env(monkeypatch):
    monkeypatch.setattr(agent_mod, "_FOLD_CHARS", 0)
    ag = _agent(monkeypatch)
    assert ag._autofold() == 0


def test_short_conversations_never_fold(monkeypatch):
    """Fewer messages than the keep-window can never fold, whatever their size."""
    monkeypatch.setattr(agent_mod, "_FOLD_CHARS", 10)
    ag = _agent(monkeypatch, n_messages=agent_mod._FOLD_KEEP, msg_chars=5000)
    assert ag._autofold() == 0


def test_chunking_preserves_the_entire_transcript():
    text = "alpha\n" + ("x" * 73) + "\nomega"
    chunks = Agent._chunk_text(text, limit=17)
    assert "".join(chunks) == text
    assert all(len(c) <= 17 for c in chunks)


def test_hierarchical_summary_reads_the_tail(monkeypatch):
    ag = Agent(model="test-model", learn=False)
    seen = []

    def summarize_once(self, text):
        seen.append(text)
        # Compact enough that the merge fits in one final call.
        return f"[{text[:8]}…{text[-8:]}]"

    monkeypatch.setattr(Agent, "_summarize_once", summarize_once)
    monkeypatch.setattr(agent_mod, "_FOLD_CHUNK_CHARS", 2000)
    transcript = "START\n" + ("middle\n" * 800) + "TAIL-SENTINEL"
    summary = ag._summarize_convo(transcript)
    assert any("TAIL-SENTINEL" in part for part in seen)
    assert "ENTINEL" in summary


def test_compaction_serialises_tool_calls_without_text():
    msg = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "write_file", "arguments": '{"path":"result.txt"}'},
    }]}
    rendered = Agent._message_for_summary(msg)
    assert "write_file" in rendered
    assert "result.txt" in rendered
