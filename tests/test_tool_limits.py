"""User-configurable tool-call budgets (Settings → Tools → "Tool-call budgets"):
- tools.get_max_steps()/set_max_steps() — the agent loop's turn cap (chat + background api/local
  agents), falling back to config.MAX_STEPS when unset.
- tools.get_max_delegate_turns()/set_max_delegate_turns() — Claude/Codex CLI delegation's own
  --max-turns, falling back to delegate._DELEGATE_TURNS when unset (0).
Both persist to data/tools.json alongside the existing enabled/chat_off state.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from oceano.tools import core  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_STATE_PATH", tmp_path / "tools.json")
    core._DISABLED, core._CHAT_OFF, core._MAX_STEPS, core._MAX_DELEGATE_TURNS = set(), set(), 0, 0


def test_max_steps_defaults_to_config_when_unset(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert core.get_max_steps() == config.MAX_STEPS
    assert core.get_max_steps_override() == 0


def test_set_max_steps_overrides_and_persists(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    core.set_max_steps(80)
    assert core.get_max_steps() == 80
    assert core.get_max_steps_override() == 80
    saved = json.loads(core._STATE_PATH.read_text())
    assert saved["max_steps"] == 80
    # a fresh load (e.g. process restart) picks the persisted override back up
    core._MAX_STEPS = 0
    core._load_state()
    assert core.get_max_steps() == 80


def test_set_max_steps_zero_clears_the_override(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    core.set_max_steps(80)
    core.set_max_steps(0)
    assert core.get_max_steps() == config.MAX_STEPS
    assert core.get_max_steps_override() == 0


def test_set_max_steps_clamps_to_1_500(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    core.set_max_steps(-5)
    assert core.get_max_steps_override() == 0          # non-positive → treated as unset
    core.set_max_steps(99999)
    assert core.get_max_steps_override() == 500


def test_max_delegate_turns_unset_returns_zero_not_a_default(tmp_path, monkeypatch):
    """Unlike get_max_steps (which bakes config.MAX_STEPS into itself), get_max_delegate_turns
    stays a bare int — delegate.py resolves the OCEANO_DELEGATE_MAXTURNS fallback itself, so
    oceano.tools never needs to import oceano.delegate."""
    _isolate(tmp_path, monkeypatch)
    assert core.get_max_delegate_turns() == 0


def test_set_max_delegate_turns_persists_and_clamps(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    core.set_max_delegate_turns(120)
    assert core.get_max_delegate_turns() == 120
    saved = json.loads(core._STATE_PATH.read_text())
    assert saved["max_delegate_turns"] == 120
    core.set_max_delegate_turns(99999)
    assert core.get_max_delegate_turns() == 500
    core.set_max_delegate_turns(0)
    assert core.get_max_delegate_turns() == 0


def test_limits_persist_independently_of_disabled_chat_off_state(tmp_path, monkeypatch):
    """A round trip through _save_state/_load_state must not disturb the OTHER settings already
    living in tools.json (regression guard for the shared read/write of the same file)."""
    _isolate(tmp_path, monkeypatch)
    core.set_enabled("web_search", False)
    core.set_max_steps(40)
    core.set_chat_tool("recall", False)
    core._DISABLED, core._CHAT_OFF, core._MAX_STEPS, core._MAX_DELEGATE_TURNS = set(), set(), 0, 0
    core._load_state()
    assert "web_search" in core._DISABLED
    assert "recall" in core._CHAT_OFF
    assert core.get_max_steps_override() == 40


def test_agent_run_loop_uses_the_configured_max_steps(tmp_path, monkeypatch):
    """agent.py's run() must consult tools.get_max_steps(), not the static config.MAX_STEPS —
    otherwise a user-raised budget would silently have no effect on the actual loop."""
    _isolate(tmp_path, monkeypatch)
    core.set_max_steps(2)
    from oceano.agent import Agent
    calls = {"n": 0}

    class FakeMsg:
        tool_calls = None
        content = "done"

        def model_dump(self, exclude_none=True):
            return {"role": "assistant", "content": "done"}

    ag = Agent(model="m", base_url="http://x", api_key="k", learn=False, inject_context=False)
    monkeypatch.setattr(ag, "_prepare_turn", lambda *a, **k: None)

    def fake_chat(with_tools=True):
        calls["n"] += 1
        return FakeMsg()
    monkeypatch.setattr(ag, "_chat", fake_chat)
    monkeypatch.setattr(ag, "_tool_schemas", lambda *a, **k: [])
    monkeypatch.setattr(ag, "_learn", lambda *a, **k: None)
    ag.run("hi")
    assert calls["n"] <= 2   # bounded by our tiny override, not the (larger) real default
