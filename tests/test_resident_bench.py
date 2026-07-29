import json
import stat

from oceano import mindbridge, resident_bench, traces


def test_routing_benchmark_compares_full_and_hybrid_without_leaking_catalogs(tmp_path, monkeypatch):
    monkeypatch.setattr(traces, "TRACE_PATH", tmp_path / "traces.jsonl")
    before = mindbridge.catalog_inventory()["active"]
    rows = resident_bench.routing_benchmark(
        providers=("codex",), modes=("full", "hybrid"),
        cases=resident_bench.CASES[:1])
    assert len(rows) == 2
    assert {row["mode"] for row in rows} == {"full", "hybrid"}
    full = next(row for row in rows if row["mode"] == "full")
    hybrid = next(row for row in rows if row["mode"] == "hybrid")
    from oceano import tools
    assert full["advertised_tools"] == len(tools.schemas())
    assert full["catalog_tools"] == len(tools.schemas())
    assert hybrid["schema_tokens_saved"] >= 0
    assert hybrid["expected_bundles"] == ["code-execution", "files-write"]
    assert hybrid["expected_within_budget"] is True
    assert 0 <= hybrid["bundle_recall"] <= 1
    assert "prompt" not in hybrid
    assert mindbridge.catalog_inventory()["active"] == before


def test_routing_benchmark_has_ground_truth_across_major_domains():
    rows = resident_bench.routing_benchmark(
        providers=("claude",), modes=("hybrid",), cases=resident_bench.ROUTING_CASES)
    assert len(rows) == len(resident_bench.ROUTING_CASES)
    by_case = {row["case"]: row for row in rows}
    assert by_case["calendar-read"]["expected_bundles"] == ["calendar-read"]
    assert by_case["email-reply"]["expected_bundles"] == ["email-read", "email-write"]
    assert by_case["browser-form"]["expected_bundles"] == ["browser"]
    assert all(row["expected_within_budget"] for row in rows)
    for case in ("concept-memory", "concept-email", "concept-scheduler", "concept-agents"):
        assert by_case[case]["expected_bundles"] == []
        assert 0 <= by_case[case]["bundle_precision"] <= 1


def test_extended_hidden_validators_accept_reference_implementations(tmp_path):
    implementations = {
        "log-parser": (
            "logstats.py",
            "def summarize(lines):\n"
            "    counts = {}\n    first = None\n"
            "    for line in lines:\n"
            "        if not line.startswith('[') or '] ' not in line:\n            continue\n"
            "        level, message = line[1:].split('] ', 1)\n        level = level.upper()\n"
            "        counts[level] = counts.get(level, 0) + 1\n"
            "        if level == 'ERROR' and first is None:\n            first = message\n"
            "    return {'counts': counts, 'first_error': first}\n"),
        "retry-contract": (
            "retry.py",
            "def retry_call(fn, attempts, retry_on=(Exception,)):\n"
            "    if attempts < 1:\n        raise ValueError('attempts')\n"
            "    for index in range(attempts):\n"
            "        try:\n            return fn()\n"
            "        except retry_on:\n"
            "            if index + 1 == attempts:\n                raise\n"),
        "seeded-repair": (
            "inventory.py",
            "def reserve(stock, quantity):\n"
            "    if quantity <= 0 or quantity > stock['available']:\n        return False\n"
            "    stock['available'] -= quantity\n    return True\n"),
    }
    for case_id, (filename, source) in implementations.items():
        folder = tmp_path / case_id
        folder.mkdir()
        (folder / filename).write_text(source)
        case = next(case for case in resident_bench.EXTENDED_CASES if case.id == case_id)
        assert resident_bench._functional_check(case, folder) is True


def test_holdout_and_long_hidden_validators_accept_reference_implementations(tmp_path):
    implementations = {
        "chunking-contract": {
            "chunks.py": (
                "def chunked(items, size):\n"
                "    if size <= 0:\n        raise ValueError('size')\n"
                "    return [list(items[i:i + size]) for i in range(0, len(items), size)]\n")},
        "event-fold": {
            "eventfold.py": (
                "def fold(events):\n    result = {}\n"
                "    for event in events:\n"
                "        if 'kind' not in event or 'amount' not in event:\n            continue\n"
                "        amount = event['amount']\n"
                "        if not isinstance(amount, (int, float)):\n            raise TypeError('amount')\n"
                "        kind = event['kind'].lower()\n"
                "        result[kind] = result.get(kind, 0) + amount\n"
                "    return result\n")},
        "seeded-quota-repair": {
            "quota.py": (
                "def consume(state, quantity):\n"
                "    if isinstance(quantity, bool) or quantity <= 0 or quantity > state['remaining']:\n"
                "        return False\n"
                "    state['remaining'] -= quantity\n    return True\n")},
        "stable-dedupe": {
            "dedupe.py": (
                "def unique_by(items, key):\n    result, seen = [], []\n"
                "    for item in items:\n        value = key(item)\n"
                "        if not any(value == prior for prior in seen):\n"
                "            seen.append(value)\n            result.append(item)\n"
                "    return result\n")},
        "order-workflow-repair": {
            "orders.py": (
                "def cancel_order(order):\n"
                "    if str(order.get('status', '')).lower() != 'pending':\n        return False\n"
                "    order['status'] = 'cancelled'\n    return True\n"),
            "reports.py": (
                "def summarize_orders(orders):\n    result = {}\n"
                "    for order in orders:\n"
                "        if not isinstance(order, dict) or not isinstance(order.get('status'), str):\n"
                "            continue\n        status = order['status'].lower()\n"
                "        result[status] = result.get(status, 0) + 1\n"
                "    return result\n")},
        "ttl-cache-repair": {
            "cache.py": (
                "class TTLCache:\n"
                "    def __init__(self, clock):\n        self.clock = clock\n        self.data = {}\n"
                "    def set(self, key, value, ttl):\n"
                "        if ttl <= 0:\n            raise ValueError('ttl')\n"
                "        self.data[key] = (value, self.clock() + ttl)\n"
                "    def get(self, key, default=None):\n"
                "        value, expires = self.data.get(key, (default, self.clock()))\n"
                "        return value if self.clock() < expires else default\n")},
    }
    cases = {case.id: case for case in resident_bench.HOLDOUT_CASES + resident_bench.LONG_CASES}
    for case_id, files in implementations.items():
        folder = tmp_path / case_id
        folder.mkdir()
        for filename, source in files.items():
            (folder / filename).write_text(source)
        assert resident_bench._functional_check(cases[case_id], folder) is True


def test_live_matrix_accepts_a_safe_runner_and_report_is_content_free(tmp_path, monkeypatch):
    monkeypatch.setattr(traces, "TRACE_PATH", tmp_path / "traces.jsonl")
    def runner(provider, mode, case):
        return {"provider": provider, "mode": mode, "case": case.id,
                "score": 90, "passed": True, "tool_calls": 2}

    rows = resident_bench.live_benchmark(
        providers=("claude", "codex"), modes=("hybrid",),
        cases=resident_bench.CASES[:1], runner=runner)
    output = tmp_path / "report.json"
    report = resident_bench.write_report(rows, output, live=True)
    raw = output.read_text()
    assert len(rows) == 2 and all(row["passed"] for row in rows)
    assert report["summary"][0]["avg_score"] == 90.0
    assert report["summary"][0]["avg_tool_calls"] == 2.0
    assert all(word not in raw.lower() for word in ("prompt", "answer", "arguments", "result_text"))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    json.loads(raw)

    repeated = resident_bench.live_benchmark(
        providers=("claude",), modes=("hybrid",), cases=resident_bench.CASES[:1],
        runner=runner, repetitions=2)
    assert [row["run"] for row in repeated] == [1, 2]


def test_live_summary_reports_efficiency_and_functional_results():
    summary = resident_bench.summarize([
        {"provider": "claude", "mode": "hybrid", "case": "one",
         "score": 100, "passed": True, "functional": True,
         "tool_calls": 3, "elapsed_ms": 1000, "direct": True},
        {"provider": "claude", "mode": "hybrid", "case": "two",
         "score": 100, "passed": True, "functional": True,
         "tool_calls": 5, "elapsed_ms": 2000, "direct": True},
    ])[0]
    assert summary["functional"] == 2
    assert summary["avg_elapsed_ms"] == 1500.0
    assert summary["avg_tool_calls"] == 4.0
