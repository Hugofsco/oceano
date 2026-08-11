"""Shared test fixtures.

The taint reset below is a correctness guard, not convenience. Injection taint lives in a ContextVar
(turnctx) and a module-level set (safety._bridge_seen); neither is torn down by pytest. Any test that
exercises a real content-reading tool leaves the flag set for every test that follows in the same
process — which is how tests/test_webcontrol.py::test_web_search_reuses_short_cache silently armed
the gates for the whole rest of the run.

It went unnoticed because Agent.run() used to reset taint unconditionally in its `finally`, so the
next agent turn scrubbed the leak. Making that reset conditional on trusted_origin (so a derived
workflow/sub-agent turn can't launder its parent's taint) removed the accidental cleanup and exposed
the pre-existing gap. Resetting per test makes the isolation explicit instead of incidental.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolate_injection_taint():
    from oceano import safety
    safety.reset_untrusted()
    safety._bridge_seen.clear()
    yield
    safety.reset_untrusted()
    safety._bridge_seen.clear()


@pytest.fixture(autouse=True)
def _isolate_security_settings(monkeypatch, tmp_path):
    """Point the Settings → Security store at a per-test temp file, so the guard/gate tests always
    run against the shipped defaults (all protective) instead of whatever the developer toggled in
    their live data/security.json — and so tests exercising set_security never write into data/."""
    from oceano import safety
    monkeypatch.setattr(safety, "_security_path", lambda: tmp_path / "security.json")
    safety._sec_cache = None
    yield
    safety._sec_cache = None
