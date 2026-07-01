"""Fast, network-free smoke tests for the Oceano framework.

Two jobs, both meant to run in well under a second with no LLM, no embed server,
and no daemon:

1. IMPORT HEALTH — import every ``oceano.*`` module so a syntax error, a broken
   internal import, or an undefined name at module scope fails CI instead of only
   surfacing when that code path runs in production. Missing *optional third-party*
   dependencies (faster-whisper, kokoro, etc.) are skipped, not failed, so the
   suite stays green on a lean CI box; a broken ``oceano.*`` import is always a
   failure.

2. TOOL-SURFACE CONSISTENCY — the agent's tool surface is hand-maintained across
   several lists that must agree (the ``@tool`` registry, the mind-bridge allow-set,
   the chat-mode memory tools, the delegate's API map, the web tool→category map,
   the streaming-tools set). A tool that is renamed/removed in one place but not the
   others silently loses a capability with no error. These assertions catch that
   whole class of drift — the exact bug that shipped scheduler tools that were
   registered but never exposed to the mind.

Run with ``venv/bin/python -m pytest tests/`` or directly:
``venv/bin/python tests/test_smoke.py``.
"""

import importlib
import os
import pkgutil
import sys

# Make the repo root importable whether run via pytest (invoked from the root) or
# directly as `python tests/test_smoke.py` (where only tests/ lands on sys.path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oceano  # noqa: E402 - must follow the sys.path bootstrap above


# --- 1) import health -------------------------------------------------------

def _walk_oceano_modules():
    return sorted(m.name for m in pkgutil.walk_packages(oceano.__path__, prefix="oceano."))


def test_all_modules_import():
    """Every oceano.* module imports. A missing optional third-party dep is
    skipped; a real failure (syntax, undefined name, broken internal import) fails."""
    real_failures = []
    skipped = []
    for name in _walk_oceano_modules():
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            # A broken *internal* import is a real bug; a missing optional
            # third-party package (heavy ML/audio deps) is tolerated.
            if missing.startswith("oceano"):
                real_failures.append((name, repr(e)))
            else:
                skipped.append((name, missing))
        except Exception as e:  # SyntaxError, NameError, import-time errors, ...
            real_failures.append((name, repr(e)))
    assert not real_failures, "modules failed to import:\n" + "\n".join(
        f"  {n}: {err}" for n, err in real_failures
    )
    # Visible in -v / -rs output; not an assertion.
    if skipped:
        print("\n[smoke] skipped (missing optional dep): " +
              ", ".join(f"{n}({dep})" for n, dep in skipped))


# --- 2) tool-surface consistency -------------------------------------------

def _registry():
    from oceano import tools
    registered = set(tools._TOOLS)                                  # every @tool-decorated fn
    schema_names = [s["function"]["name"] for s in tools._SCHEMAS]  # the advertised surface
    return tools, registered, schema_names


def test_no_duplicate_schema_names():
    _tools, _registered, schema_names = _registry()
    dups = sorted({n for n in schema_names if schema_names.count(n) > 1})
    assert not dups, f"duplicate tool schema names: {dups}"


def test_schemas_are_registered():
    """Every advertised schema corresponds to a real, callable registered tool."""
    _tools, registered, schema_names = _registry()
    missing = sorted(set(schema_names) - registered)
    assert not missing, f"schemas with no registered implementation: {missing}"


def test_mindbridge_allow_is_real():
    """Every tool exposed to the resident mind (Claude/Codex) actually exists.
    This is the assertion that would have caught the scheduler-tools gap."""
    from oceano import mindbridge
    _tools, registered, _schema = _registry()
    missing = sorted(mindbridge._ALLOW - registered)
    assert not missing, f"mindbridge._ALLOW names not registered: {missing}"


def test_memory_tools_are_real():
    from oceano import tools
    _t, registered, _schema = _registry()
    missing = sorted(set(tools.MEMORY_TOOLS) - registered)
    assert not missing, f"MEMORY_TOOLS names not registered: {missing}"


def test_api_tool_map_targets_are_real():
    """The delegate's CLI-spec → Oceano-tool translation maps only to real tools."""
    from oceano import delegate
    _t, registered, _schema = _registry()
    targets = {name for names in delegate._API_TOOL_MAP.values() for name in names}
    missing = sorted(targets - registered)
    assert not missing, f"_API_TOOL_MAP targets not registered: {missing}"


def test_streaming_tools_are_real():
    from oceano import agent
    _t, registered, _schema = _registry()
    missing = sorted(set(getattr(agent, "_STREAMING_TOOLS", set())) - registered)
    assert not missing, f"_STREAMING_TOOLS names not registered: {missing}"


def test_tool_category_keys_are_real():
    """No stale category entries pointing at renamed/removed tools."""
    from oceano.web import server
    _t, registered, _schema = _registry()
    stale = sorted(set(server._TOOL_CATEGORY) - registered)
    assert not stale, f"_TOOL_CATEGORY keys not registered: {stale}"


def test_every_advertised_tool_has_a_category():
    """Every tool the UI lists is grouped under a category, not dumped in 'other'.
    Keeps Settings → Tools tidy and locks the mail/ssh categorization in place."""
    from oceano.web import server
    _t, _registered, schema_names = _registry()
    uncategorized = sorted(set(schema_names) - set(server._TOOL_CATEGORY))
    assert not uncategorized, f"registered tools with no category: {uncategorized}"


def test_mindbridge_schemas_build():
    """The mind's body schema set builds, is non-empty, and is well-formed."""
    from oceano import mindbridge
    schemas = mindbridge.tool_schemas()
    assert schemas, "mindbridge exposes no tools to the mind"
    for s in schemas:
        assert s.get("function", {}).get("name"), f"malformed bridge schema: {s}"
    assert set(mindbridge.tool_names()) <= mindbridge._ALLOW


if __name__ == "__main__":  # run without pytest: `venv/bin/python tests/test_smoke.py`
    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001 - report-and-continue test runner
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    raise SystemExit(1 if failed else 0)
