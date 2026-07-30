"""Repeatable resident-agent benchmark matrix.

Routing benchmarks are deterministic and credential-free. Live runs are opt-in, execute safe
standard-library tasks in isolated temporary workspaces, and persist metrics without prompts,
answers, tool arguments, or tool results.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from collections import Counter
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

import config
from oceano import atomicio, mindbridge, traces


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    artifacts: tuple[str, ...]
    requires_verification: bool = True
    expected_bundles: tuple[str, ...] = ()
    required_tool_groups: tuple[tuple[str, ...], ...] = ()
    fixtures: tuple[tuple[str, str], ...] = ()
    call_budget: int = 8


CASES = (
    Case("python-smoke",
         "Create hello.py that prints OCEANO_BENCH_OK, then run it to verify the output.",
         ("hello.py",), expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"),
                               ("run_shell", "python_exec", "run_tests"))),
    Case("python-tests",
         "Create calc.py with add(a, b), create test_calc.py using unittest, and run the tests.",
         ("calc.py", "test_calc.py"), expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
    Case("repair-loop",
         "Create app.py, detect and repair one intentional failing assertion, then rerun verification.",
         ("app.py",), expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"),
                               ("run_shell", "python_exec", "run_tests"))),
    Case("interval-edge-cases",
         "Create intervals.py with normalize_and_merge(intervals). Normalize reversed integer "
         "endpoints, sort without mutating the input, and merge overlapping or directly touching "
         "integer intervals. Return a list of tuples. Create test_intervals.py using unittest with "
         "meaningful edge cases, then run the tests.",
         ("intervals.py", "test_intervals.py"),
         expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
)
EXTENDED_CASES = (
    Case("log-parser",
         "Create logstats.py with summarize(lines). Accept lines formatted '[LEVEL] message', "
         "ignore malformed lines, count levels case-insensitively under uppercase keys, and "
         "return {'counts': {...}, "
         "'first_error': message-or-None}. Do not mutate the input. Create and run unittest tests.",
         ("logstats.py", "test_logstats.py"),
         expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
    Case("retry-contract",
         "Create retry.py with retry_call(fn, attempts, retry_on=(Exception,)). Reject attempts "
         "below one, return immediately on success, retry only matching exceptions, and re-raise "
         "the final matching exception without sleeping. Create and run unittest tests.",
         ("retry.py", "test_retry.py"),
         expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
    Case("seeded-repair",
         "Inspect inventory.py and its tests. Fix the reservation logic so failed reservations "
         "never mutate stock, successful reservations subtract exactly once, and invalid quantities "
         "are rejected. Run the tests and preserve the public function signature.",
         ("inventory.py", "test_inventory.py"),
         expected_bundles=("files-read", "files-write", "code-execution"),
         required_tool_groups=(("read_file",), ("write_file", "edit_file"),
                               ("run_tests", "run_shell")),
         fixtures=(("inventory.py", "def reserve(stock, quantity):\n"
                    "    stock['available'] -= quantity\n"
                    "    if quantity <= 0 or stock['available'] < 0:\n"
                    "        return False\n"
                    "    return True\n"),
                   ("test_inventory.py", "import unittest\nfrom inventory import reserve\n\n"
                    "class InventoryTests(unittest.TestCase):\n"
                    "    def test_success(self):\n"
                    "        stock = {'available': 5}\n"
                    "        self.assertTrue(reserve(stock, 2))\n"
                    "        self.assertEqual(stock['available'], 3)\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n"))),
)
HOLDOUT_CASES = (
    Case("chunking-contract",
         "Create chunks.py with chunked(items, size). Return a new list of lists, preserve order, "
         "leave the input unchanged, retain a final partial chunk, and raise ValueError when size "
         "is zero or negative. Create focused unittest coverage and run it.",
         ("chunks.py", "test_chunks.py"),
         expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
    Case("event-fold",
         "Create eventfold.py with fold(events). Each event is a mapping with kind and amount. "
         "Sum amounts by case-insensitive kind, skip entries missing either field, reject nonnumeric "
         "amounts with TypeError, and do not mutate input. Add unittest tests and run them.",
         ("eventfold.py", "test_eventfold.py"),
         expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
    Case("seeded-quota-repair",
         "Inspect quota.py and its tests. Repair consume so rejected requests never mutate state, "
         "accepted requests decrement remaining exactly once, and boolean, zero, negative, or "
         "over-limit quantities are rejected. Preserve its signature and run the tests.",
         ("quota.py", "test_quota.py"),
         expected_bundles=("files-read", "files-write", "code-execution"),
         required_tool_groups=(("read_file",), ("write_file", "edit_file"),
                               ("run_tests", "run_shell")),
         fixtures=(("quota.py", "def consume(state, quantity):\n"
                    "    state['remaining'] -= quantity\n"
                    "    return state['remaining'] >= 0\n"),
                   ("test_quota.py", "import unittest\nfrom quota import consume\n\n"
                    "class QuotaTests(unittest.TestCase):\n"
                    "    def test_accepts(self):\n"
                    "        state = {'remaining': 4}\n"
                    "        self.assertTrue(consume(state, 2))\n"
                    "        self.assertEqual(state['remaining'], 2)\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n"))),
    Case("stable-dedupe",
         "Create dedupe.py with unique_by(items, key). Return the first item for each distinct key "
         "in original order, call key exactly once per input item, support unhashable key values, "
         "and do not mutate input. Create and run unittest tests.",
         ("dedupe.py", "test_dedupe.py"),
         expected_bundles=("files-write", "code-execution"),
         required_tool_groups=(("write_file", "edit_file"), ("run_tests", "run_shell"))),
)
LONG_CASES = (
    Case("order-workflow-repair",
         "Inspect the small order package and its tests. Implement cancel_order and repair "
         "summarize_orders. Cancellation must match pending case-insensitively, return True only "
         "when it changes a pending order to cancelled, return False otherwise, and never mutate "
         "rejected orders. The summary must count statuses case-insensitively, "
         "ignore malformed entries, and not mutate input. Preserve public signatures, add missing "
         "edge-case tests, and run the full test suite.",
         ("orders.py", "reports.py", "test_orders.py"),
         expected_bundles=("files-read", "files-write", "code-execution"),
         required_tool_groups=(("read_file",), ("write_file", "edit_file"),
                               ("run_tests", "run_shell")), call_budget=12,
         fixtures=(("orders.py", "def cancel_order(order):\n    raise NotImplementedError\n"),
                   ("reports.py", "def summarize_orders(orders):\n"
                    "    counts = {}\n"
                    "    for order in orders:\n"
                    "        status = order.get('status')\n"
                    "        counts[status] = counts.get(status, 0) + 1\n"
                    "    return counts\n"),
                   ("test_orders.py", "import unittest\n"
                    "from orders import cancel_order\n"
                    "from reports import summarize_orders\n\n"
                    "class OrderTests(unittest.TestCase):\n"
                    "    def test_cancel_pending(self):\n"
                    "        order = {'status': 'pending'}\n"
                    "        self.assertTrue(cancel_order(order))\n"
                    "        self.assertEqual(order['status'], 'cancelled')\n\n"
                    "    def test_summary(self):\n"
                    "        self.assertEqual(summarize_orders([{'status': 'PAID'}]), {'paid': 1})\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n"))),
    Case("ttl-cache-repair",
         "Inspect cache.py and its tests. Repair TTLCache without changing its public API. Values "
         "must remain available strictly before expiry, disappear at expiry, support overwrite "
         "with a fresh TTL, use the injected clock exclusively, and reject nonpositive TTLs "
         "without changing existing entries. Add edge-case tests and run the full suite.",
         ("cache.py", "test_cache.py"),
         expected_bundles=("files-read", "files-write", "code-execution"),
         required_tool_groups=(("read_file",), ("write_file", "edit_file"),
                               ("run_tests", "run_shell")), call_budget=12,
         fixtures=(("cache.py", "class TTLCache:\n"
                    "    def __init__(self, clock):\n"
                    "        self.clock = clock\n        self.data = {}\n\n"
                    "    def set(self, key, value, ttl):\n"
                    "        self.data[key] = (value, self.clock() + ttl)\n\n"
                    "    def get(self, key, default=None):\n"
                    "        value, expires = self.data.get(key, (default, 0))\n"
                    "        return value if self.clock() <= expires else default\n"),
                   ("test_cache.py", "import unittest\nfrom cache import TTLCache\n\n"
                    "class CacheTests(unittest.TestCase):\n"
                    "    def test_before_expiry(self):\n"
                    "        now = [10]\n        cache = TTLCache(lambda: now[0])\n"
                    "        cache.set('a', 1, 2)\n        now[0] = 11\n"
                    "        self.assertEqual(cache.get('a'), 1)\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n"))),
)
ROUTING_CASES = CASES + EXTENDED_CASES + (
    Case("repo-inspection", "Inspect this repository and explain where authentication is handled.",
         (), False, ("files-read",)),
    Case("documentation-edit", "Update the README installation section and verify the change.",
         (), False, ("files-read", "files-write")),
    Case("calendar-read", "Show my calendar availability tomorrow afternoon.",
         (), False, ("calendar-read",)),
    Case("calendar-write", "Schedule a project planning meeting tomorrow afternoon.",
         (), False, ("calendar-write",)),
    Case("email-read", "Read the newest message in my inbox.",
         (), False, ("email-read",)),
    Case("email-reply", "Reply to the newest email confirming receipt.",
         (), False, ("email-read", "email-write")),
    Case("web-sources", "Research the latest Python release using public web sources.",
         (), False, ("web-research",)),
    Case("browser-form", "Open the website and fill in its contact form.",
         (), False, ("browser",)),
    Case("memory-recall", "Recall what I previously asked you to remember about my preferences.",
         (), False, ("memory",)),
    Case("data-analysis", "Analyze the CSV dataset with Python and summarize the totals.",
         (), False, ("files-read", "data")),
    Case("agent-parallel", "Delegate two independent inspections to background agents.",
         (), False, ("agents",)),
    Case("implicit-python-read", "What does src/auth.py do?", (), False, ("files-read",)),
    Case("implicit-python-edit", "Add retry handling to client.py.",
         (), False, ("files-read", "files-write")),
    Case("test-intent", "Make sure the Python test suite passes.",
         (), False, ("files-read", "code-execution")),
    Case("implicit-availability", "Am I free tomorrow afternoon?",
         (), False, ("calendar-read",)),
    Case("web-not-browser", "Find current release notes online and cite the sources.",
         (), False, ("web-research",)),
    Case("browser-not-web", "Click the sign-in button on the open page.",
         (), False, ("browser",)),
    Case("concept-memory", "Explain how virtual memory works in an operating system.",
         (), False, ()),
    Case("concept-email", "Explain the difference between IMAP and SMTP email.",
         (), False, ()),
    Case("concept-scheduler", "Explain how cron scheduling expressions work.",
         (), False, ()),
    Case("concept-agents", "Explain the benefits of multi-agent architectures.",
         (), False, ()),
)
PROVIDERS = ("claude", "codex", "api")
MODES = ("full", "hybrid")
DEFAULT_REPORT = config.WORKSPACE.parent / "data" / "resident-benchmarks.json"


def _model(provider):
    if provider == "claude":
        return "claude:benchmark"
    if provider == "codex":
        return "codex:benchmark"
    return "api:configured"


def routing_benchmark(providers=PROVIDERS, modes=MODES, cases=ROUTING_CASES):
    from oceano import toolrouter, tools
    available = {schema["function"]["name"]: schema for schema in tools.schemas()}
    bundle_map = toolrouter.bundles()
    rows = []
    for provider in providers:
        for mode in modes:
            for case in cases:
                catalog_id, route = mindbridge.create_catalog(
                    case.prompt, _model(provider), max_calls=100,
                    force=(mode == "hybrid"))
                declared_expected = set(case.expected_bundles)
                core_tools = {
                    name for bundle in route.policy.core_bundles
                    for name in bundle_map.get(bundle, toolrouter.Bundle(bundle, "", ())).tools
                }
                covered_by_core = {
                    bundle for bundle in declared_expected
                    if set(bundle_map[bundle].tools) <= core_tools
                }
                expected = declared_expected - covered_by_core
                selected = (expected if not route.enabled else {
                    bundle for bundle in set(route.loaded_bundles) - set(route.policy.core_bundles)
                    if not set(bundle_map[bundle].tools) <= core_tools
                })
                expected_tools = {
                    name for bundle in expected for name in bundle_map[bundle].tools
                    if name in available
                }
                initial_expected = set(route.names) & expected_tools
                expected_tokens = route.schema_tokens + sum(
                    toolrouter.schema_cost(available[name])
                    for name in expected_tools - set(route.names))
                recall = (len(expected & selected) / len(expected) if expected
                          else float(not selected))
                precision = (len(expected & selected) / len(selected) if selected
                             else float(not expected))
                rows.append({
                    "provider": provider, "mode": mode, "case": case.id,
                    "advertised_tools": route.selected, "catalog_tools": route.total,
                    "schema_tokens": route.schema_tokens,
                    "catalog_schema_tokens": route.catalog_schema_tokens,
                    "schema_tokens_saved": max(
                        0, route.catalog_schema_tokens - route.schema_tokens),
                    "discovery": "discover_tools" in route.names,
                    "expected_bundles": sorted(declared_expected),
                    "expected_routed_bundles": sorted(expected),
                    "covered_by_core": sorted(covered_by_core),
                    "selected_bundles": sorted(selected),
                    "missing_bundles": sorted(expected - selected),
                    "unexpected_bundles": sorted(selected - expected),
                    "bundle_recall": round(recall, 3),
                    "bundle_precision": round(precision, 3),
                    "expected_tools": sorted(expected_tools),
                    "missing_expected_tools": sorted(expected_tools - initial_expected),
                    "expected_schema_tokens": expected_tokens,
                    "expected_within_budget": expected_tokens <= route.policy.schema_budget,
                    "schema_budget": route.policy.schema_budget,
                    "count_limit": route.policy.count_limit,
                })
                mindbridge.close_catalog(catalog_id)
    return rows


def _live_case(provider, mode, case, api_target=None):
    from oceano.agent import Agent
    from oceano import tools
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="oceano-resident-bench-") as folder:
        workspace = Path(folder)
        for relative, content in case.fixtures:
            destination = workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomicio.write_text(destination, content)
        target = api_target or {}
        agent = Agent(
            model=target.get("model") if provider == "api" else None,
            base_url=target.get("base_url") if provider == "api" else None,
            api_key=target.get("api_key") if provider == "api" else None,
            learn=False,
            dynamic_tools=(mode == "hybrid") if provider == "api" else None,
            resident_tool_mode=(mode == "hybrid"),
            trusted_origin=False)
        events = []
        first_action_ms = None
        stream_failed = False
        try:
            with tools.background_workspace(workspace):
                if provider == "claude":
                    stream = agent._claude_mind_stream(case.prompt)
                elif provider == "codex":
                    stream = agent._codex_mind_stream(case.prompt)
                else:
                    stream = agent.run_stream(case.prompt)
                for event in stream:
                    events.append(event)
                    if event.get("type") == "tool_call" and first_action_ms is None:
                        first_action_ms = round((time.monotonic() - started) * 1000)
        except Exception:  # A failed model run is a benchmark result, not a matrix abort.
            stream_failed = True
        artifacts = sum((workspace / name).exists() for name in case.artifacts)
        used = [event.get("name") for event in events if event.get("type") == "tool_call"]
        counts = Counter(used)
        verified = any(name in {"run_tests", "run_shell", "python_exec", "Bash", "shell"}
                       for name in used)
        complete = not agent.last_mind_error and not stream_failed
        functional = _functional_check(case, workspace)
        group_hits = [bool(set(group) & set(used)) for group in case.required_tool_groups]
        delegated = "delegate" in used
        direct = functional and all(group_hits) and not delegated
        write_tools = {"write_file", "edit_file"}
        verify_tools = {"run_tests", "run_shell", "python_exec", "Bash", "shell"}
        write_indexes = [index for index, name in enumerate(used) if name in write_tools]
        verification_after_write = (not write_indexes or any(
            index > write_indexes[-1] and name in verify_tools
            for index, name in enumerate(used)))
        allowed_tools = {
            "read_file", "write_file", "edit_file", "list_files", "search_files", "code_search",
            "run_tests", "run_shell", "python_exec", "discover_tools",
        }
        unrelated = [name for name in used if name not in allowed_tools]
        expected_paths = {Path(name).as_posix() for name in case.artifacts}
        expected_paths.update(Path(name).as_posix() for name, _ in case.fixtures)
        actual_paths = {
            path.relative_to(workspace).as_posix() for path in workspace.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
            and ".pytest_cache" not in path.parts and path.suffix != ".pyc"
        }
        unexpected_files = sorted(actual_paths - expected_paths)
        call_overage = max(0, len(used) - case.call_budget)
        deviation_score = max(0, 100 - 15 * len(unrelated) - 10 * len(unexpected_files)
                              - 25 * int(delegated) - 5 * counts.get("discover_tools", 0)
                              - 15 * int(case.requires_verification
                                         and not verification_after_write)
                              - 5 * call_overage)
        scope_adherent = not unrelated and not unexpected_files and not delegated
        score = round(100 * (
            0.25 * (artifacts / max(1, len(case.artifacts)))
            + 0.40 * float(functional)
            + 0.20 * float(complete)
            + 0.15 * float(verified or not case.requires_verification)))
        return {
            "provider": target.get("label", provider) if provider == "api" else provider,
            "mode": mode, "case": case.id,
            "score": score, "passed": score >= 80, "complete": complete,
            "artifacts": artifacts, "expected_artifacts": len(case.artifacts),
            "verified": verified, "functional": functional, "tool_calls": len(used),
            "direct": direct, "delegated": delegated,
            "discovery_calls": counts.get("discover_tools", 0),
            "repeated_tools": sorted(name for name, count in counts.items() if count > 1),
            "repeated_call_count": sum(max(0, count - 1) for count in counts.values()),
            "tool_counts_by_name": dict(sorted(counts.items())),
            "call_budget": case.call_budget, "call_overage": call_overage,
            "required_tool_groups_hit": group_hits,
            "verification_after_write": verification_after_write,
            "unrelated_tool_calls": len(unrelated),
            "unexpected_files": len(unexpected_files),
            "scope_adherent": scope_adherent, "deviation_score": deviation_score,
            "time_to_first_action_ms": first_action_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": bool(agent.last_mind_error or stream_failed),
        }


def _functional_check(case, workspace):
    checks = {
        "python-smoke": "import hello",
        "python-tests": "import unittest; r=unittest.TextTestRunner(verbosity=0).run("
                        "unittest.defaultTestLoader.discover('.')); assert r.wasSuccessful()",
        "repair-loop": "import app",
        "interval-edge-cases": (
            "from intervals import normalize_and_merge as f; "
            "x=[(5,1),(2,4),(10,12),(9,9),(-1,-3)]; before=list(x); "
            "assert f(x)==[(-3,-1),(1,5),(9,12)]; assert x==before; "
            "assert f([])==[]; assert f([(3,3),(4,4)])==[(3,4)]"
        ),
        "log-parser": (
            "from logstats import summarize as f; "
            "x=['[INFO] ready','bad','[error] broken','[ERROR] later','[WARN] careful']; "
            "before=list(x); r=f(x); assert x==before; "
            "assert r=={'counts':{'INFO':1,'ERROR':2,'WARN':1},'first_error':'broken'}"
        ),
        "retry-contract": (
            "from retry import retry_call as r\n"
            "calls=[]\n"
            "def flaky():\n calls.append(1)\n if len(calls)<3: raise KeyError('x')\n return 7\n"
            "assert r(flaky,3,(KeyError,))==7 and len(calls)==3\n"
            "try:\n r(lambda:1,0)\nexcept ValueError:\n pass\nelse:\n raise AssertionError('attempts')\n"
            "once=[]\n"
            "def wrong():\n once.append(1)\n raise ValueError('stop')\n"
            "try:\n r(wrong,4,(KeyError,))\nexcept ValueError:\n pass\nelse:\n raise AssertionError('type')\n"
            "assert len(once)==1\n"
        ),
        "seeded-repair": (
            "from inventory import reserve; "
            "s={'available':5}; assert reserve(s,2) is True and s['available']==3; "
            "s={'available':2}; assert reserve(s,3) is False and s['available']==2; "
            "s={'available':2}; assert reserve(s,0) is False and s['available']==2; "
            "s={'available':2}; assert reserve(s,-1) is False and s['available']==2"
        ),
        "chunking-contract": (
            "from chunks import chunked as f; "
            "x=[1,2,3,4,5]; assert f(x,2)==[[1,2],[3,4],[5]] and x==[1,2,3,4,5]; "
            "assert f([],3)==[]; "
            "\nfor bad in (0,-1):\n"
            " try: f([1],bad)\n"
            " except ValueError: pass\n"
            " else: raise AssertionError('size')"
        ),
        "event-fold": (
            "from eventfold import fold; "
            "x=[{'kind':'Sale','amount':2},{'kind':'sale','amount':3},{'kind':'x'},{}]; "
            "before=[dict(v) for v in x]; assert fold(x)=={'sale':5}; assert x==before; "
            "\ntry: fold([{'kind':'x','amount':'bad'}])\n"
            "except TypeError: pass\nelse: raise AssertionError('amount')"
        ),
        "seeded-quota-repair": (
            "from quota import consume; "
            "s={'remaining':4}; assert consume(s,2) is True and s['remaining']==2; "
            "\nfor bad in (True,0,-1,5):\n"
            " s={'remaining':4}; assert consume(s,bad) is False and s['remaining']==4"
        ),
        "stable-dedupe": (
            "from dedupe import unique_by; "
            "x=[{'v':[1],'n':'a'},{'v':[1],'n':'b'},{'v':[2],'n':'c'}]; before=[dict(i) for i in x]; "
            "calls=[]; r=unique_by(x,lambda i:(calls.append(1) or i['v'])); "
            "assert r==[x[0],x[2]] and len(calls)==3 and x==before"
        ),
        "order-workflow-repair": (
            "from orders import cancel_order; from reports import summarize_orders; "
            "p={'status':'PENDING','id':1}; assert cancel_order(p) is True; "
            "assert p=={'status':'cancelled','id':1}; before=dict(p); "
            "assert cancel_order(p) is False and p==before; "
            "paid={'status':'paid'}; before=dict(paid); "
            "assert cancel_order(paid) is False and paid==before; "
            "x=[{'status':'PAID'},{'status':'paid'},{'status':'Pending'},{},'bad']; "
            "before=[dict(v) if isinstance(v,dict) else v for v in x]; "
            "assert summarize_orders(x)=={'paid':2,'pending':1} and x==before"
        ),
        "ttl-cache-repair": (
            "from cache import TTLCache; now=[10]; c=TTLCache(lambda:now[0]); "
            "c.set('a',1,2); now[0]=11; assert c.get('a')==1; "
            "now[0]=12; assert c.get('a') is None; "
            "now[0]=20; c.set('x','old',5); now[0]=21; c.set('x','new',2); "
            "now[0]=22; assert c.get('x')=='new'; "
            "\nfor bad in (0,-1):\n"
            " try: c.set('x','bad',bad)\n"
            " except ValueError: pass\n"
            " else: raise AssertionError('ttl')\n"
            " assert c.get('x')=='new'"
        ),
    }
    code = checks.get(case.id)
    if not code:
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=workspace,
            capture_output=True, text=True, timeout=15, check=False)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@contextmanager
def _live_daemon():
    """Run the real MCP HTTP routes in-process so ephemeral catalogs are actually shared.

    Pointing a standalone benchmark at a separately running Oceano daemon is invalid: catalogs
    are deliberately process-local, so that daemon cannot recognize the benchmark's catalog id.
    """
    import uvicorn
    from oceano.web.server import app
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    old_host = os.environ.get("OCEANO_WEB_HOST")
    old_port = os.environ.get("OCEANO_WEB_PORT")
    os.environ["OCEANO_WEB_HOST"] = "127.0.0.1"
    os.environ["OCEANO_WEB_PORT"] = str(port)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("benchmark MCP daemon did not start")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if old_host is None:
            os.environ.pop("OCEANO_WEB_HOST", None)
        else:
            os.environ["OCEANO_WEB_HOST"] = old_host
        if old_port is None:
            os.environ.pop("OCEANO_WEB_PORT", None)
        else:
            os.environ["OCEANO_WEB_PORT"] = old_port


def live_benchmark(providers=PROVIDERS, modes=MODES, cases=CASES, runner=None,
                   api_target=None, repetitions=1):
    def execute(run, provider, mode, case):
        row = (runner(provider, mode, case) if runner is not None
               else _live_case(provider, mode, case, api_target=api_target))
        return {**row, "run": run}

    if runner is not None:
        return [execute(run, provider, mode, case)
                for run in range(1, repetitions + 1)
                for provider in providers for mode in modes for case in cases]
    with _live_daemon():
        return [execute(run, provider, mode, case)
                for run in range(1, repetitions + 1)
                for provider in providers for mode in modes for case in cases]


def summarize(rows):
    groups = {}
    for row in rows:
        key = (row["provider"], row["mode"])
        bucket = groups.setdefault(key, {
            "scores": [], "passed": 0, "rows": 0, "schema_saved": [],
            "advertised": [], "recall": [], "precision": [], "direct": 0,
            "delegated": 0, "discovery_calls": 0, "functional": 0,
            "elapsed": [], "tool_counts": [], "deviation": [],
            "first_action": [], "scope_adherent": 0, "call_overage": 0,
            "case_ids": set(), "run_ids": set(),
        })
        bucket["rows"] += 1
        bucket["case_ids"].add(row["case"])
        bucket["run_ids"].add(row.get("run", 1))
        if "score" in row:
            bucket["scores"].append(row["score"])
            bucket["passed"] += int(bool(row.get("passed")))
            bucket["functional"] += int(bool(row.get("functional")))
            if "elapsed_ms" in row:
                bucket["elapsed"].append(row["elapsed_ms"])
            if "tool_calls" in row:
                bucket["tool_counts"].append(row["tool_calls"])
            if "deviation_score" in row:
                bucket["deviation"].append(row["deviation_score"])
                bucket["scope_adherent"] += int(bool(row.get("scope_adherent")))
                bucket["call_overage"] += int(row.get("call_overage") or 0)
            if row.get("time_to_first_action_ms") is not None:
                bucket["first_action"].append(row["time_to_first_action_ms"])
        if "schema_tokens_saved" in row:
            bucket["schema_saved"].append(row["schema_tokens_saved"])
            bucket["advertised"].append(row.get("advertised_tools", 0))
        if "bundle_recall" in row:
            bucket["recall"].append(row["bundle_recall"])
            bucket["precision"].append(row["bundle_precision"])
        if "direct" in row:
            bucket["direct"] += int(bool(row.get("direct")))
            bucket["delegated"] += int(bool(row.get("delegated")))
            bucket["discovery_calls"] += int(row.get("discovery_calls") or 0)
    return [{
        "provider": provider, "mode": mode, "cases": values["rows"],
        "unique_cases": len(values["case_ids"]), "repetitions": len(values["run_ids"]),
        "avg_score": (round(sum(values["scores"]) / len(values["scores"]), 1)
                      if values["scores"] else None),
        "passed": values["passed"] if values["scores"] else None,
        "avg_schema_tokens_saved": (round(sum(values["schema_saved"]) / len(values["schema_saved"]), 1)
                                    if values["schema_saved"] else None),
        "avg_advertised_tools": (round(sum(values["advertised"]) / len(values["advertised"]), 1)
                                 if values["advertised"] else None),
        "avg_bundle_recall": (round(sum(values["recall"]) / len(values["recall"]), 3)
                              if values["recall"] else None),
        "avg_bundle_precision": (round(sum(values["precision"]) / len(values["precision"]), 3)
                                 if values["precision"] else None),
        "direct": values["direct"] if values["scores"] else None,
        "delegated": values["delegated"] if values["scores"] else None,
        "discovery_calls": values["discovery_calls"] if values["scores"] else None,
        "functional": values["functional"] if values["scores"] else None,
        "avg_elapsed_ms": (round(sum(values["elapsed"]) / len(values["elapsed"]), 1)
                           if values["elapsed"] else None),
        "avg_tool_calls": (round(sum(values["tool_counts"]) / len(values["tool_counts"]), 2)
                           if values["tool_counts"] else None),
        "score_stddev": (round(statistics.pstdev(values["scores"]), 2)
                         if values["scores"] else None),
        "elapsed_stddev_ms": (round(statistics.pstdev(values["elapsed"]), 1)
                              if values["elapsed"] else None),
        "tool_calls_stddev": (round(statistics.pstdev(values["tool_counts"]), 2)
                              if values["tool_counts"] else None),
        "avg_time_to_first_action_ms": (
            round(sum(values["first_action"]) / len(values["first_action"]), 1)
            if values["first_action"] else None),
        "avg_deviation_score": (
            round(sum(values["deviation"]) / len(values["deviation"]), 1)
            if values["deviation"] else None),
        "scope_adherent": values["scope_adherent"] if values["deviation"] else None,
        "call_overage": values["call_overage"] if values["deviation"] else None,
    } for (provider, mode), values in sorted(groups.items())]


def write_report(rows, output=DEFAULT_REPORT, *, live=False):
    report = {
        "version": 3, "created": time.time(), "live": bool(live),
        "summary": summarize(rows), "results": rows,
    }
    atomicio.write_text(output, json.dumps(report, indent=2, sort_keys=True))
    traces.record_global(
        "resident_benchmark", live=bool(live), cases=len(rows),
        providers=sorted({row["provider"] for row in rows}),
        modes=sorted({row["mode"] for row in rows}))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark resident agent tool loading")
    parser.add_argument("--live", action="store_true",
                        help="run real providers in isolated temporary workspaces")
    parser.add_argument("--providers", default=",".join(PROVIDERS))
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    parser.add_argument("--api-model", default="",
                        help="explicit API model for benchmark runs (never persisted)")
    parser.add_argument("--api-base-url", default="",
                        help="saved endpoint URL whose configured key should be used")
    parser.add_argument("--api-label", default="",
                        help="content-free provider label written to the report")
    parser.add_argument(
        "--suite", choices=("basic", "extended", "holdout", "long", "all", "routing"),
        default=None,
        help="case suite (default: basic for live runs, routing otherwise)")
    parser.add_argument("--cases", default="",
                        help="comma-separated case ids selected from the chosen suite")
    parser.add_argument("--repetitions", type=int, default=1,
                        help="number of live repetitions per provider, mode, and case")
    args = parser.parse_args(argv)
    providers = tuple(item.strip() for item in args.providers.split(",") if item.strip())
    modes = tuple(item.strip() for item in args.modes.split(",") if item.strip())
    unknown = set(providers) - set(PROVIDERS)
    if unknown:
        parser.error("unknown providers: " + ", ".join(sorted(unknown)))
    if set(modes) - set(MODES):
        parser.error("modes must be full and/or hybrid")
    if args.repetitions < 1:
        parser.error("--repetitions must be at least one")
    api_target = None
    if args.api_model or args.api_base_url:
        if not (args.api_model and args.api_base_url):
            parser.error("--api-model and --api-base-url must be supplied together")
        from oceano.web import server
        api_target = {"model": args.api_model, "base_url": args.api_base_url,
                      "api_key": server.endpoint_key(args.api_base_url),
                      "label": args.api_label or args.api_model}
    suite = args.suite or ("basic" if args.live else "routing")
    if args.live and suite == "routing":
        parser.error("the routing suite is deterministic only; omit --live")
    if not args.live and suite in {"basic", "extended", "holdout", "long", "all"}:
        parser.error("basic, extended, holdout, and long suites require --live")
    if not args.live and args.repetitions != 1:
        parser.error("--repetitions applies to live suites only")
    suites = {
        "basic": CASES,
        "extended": EXTENDED_CASES,
        "holdout": HOLDOUT_CASES,
        "long": LONG_CASES,
        "all": CASES + EXTENDED_CASES + HOLDOUT_CASES + LONG_CASES,
        "routing": ROUTING_CASES,
    }
    cases = suites[suite]
    if args.cases:
        requested = {item.strip() for item in args.cases.split(",") if item.strip()}
        known = {case.id for case in cases}
        if requested - known:
            parser.error("unknown cases for suite: " + ", ".join(sorted(requested - known)))
        cases = tuple(case for case in cases if case.id in requested)
    rows = (live_benchmark(providers, modes, cases=cases, api_target=api_target,
                           repetitions=args.repetitions)
            if args.live else routing_benchmark(providers, modes, cases=cases))
    report = write_report(rows, Path(args.output), live=args.live)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
