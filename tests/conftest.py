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
