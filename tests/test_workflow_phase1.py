"""Phase-1 workflow additions: dotted-path {{…}} templating into JSON outputs, the wait
node (duration / until a clock time, cancellable), and the overlap guard that records a
'skipped' run instead of racing an in-flight run of the same workflow.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")   # never touch real runs
    monkeypatch.setattr(workflows, "_LIVE", {})                            # nor the live registry
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


def _wf(nodes, edges, **extra):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "t", "graph": graph, "input": {}, **extra}


# ---------------- dotted-path templating ----------------
def _ctx(**over):
    ctx = {"input": "", "last": "", "nodes": {}, "item": None, "index": None}
    ctx.update(over)
    return ctx


def test_dotted_path_digs_into_json_node_output():
    ctx = _ctx(nodes={7: json.dumps({"items": [{"url": "https://a.example"}, {"url": "https://b.example"}]})})
    assert workflows._tmpl("{{node.7.items.0.url}}", ctx) == "https://a.example"
    assert workflows._tmpl("{{node.7.items[1].url}}", ctx) == "https://b.example"


def test_dotted_path_on_last_input_and_item():
    ctx = _ctx(input=json.dumps({"q": "tides"}), last=json.dumps({"result": {"ok": True}}),
               item=json.dumps({"email": "x@y.z"}))
    assert workflows._tmpl("{{input.q}}", ctx) == "tides"
    assert workflows._tmpl("{{last.result.ok}}", ctx) == "true"       # non-string leaf → compact JSON
    assert workflows._tmpl("{{item.email}}", ctx) == "x@y.z"


def test_dotted_path_keys_stay_case_sensitive():
    ctx = _ctx(nodes={3: json.dumps({"customerName": "Ada"})})
    assert workflows._tmpl("{{node.3.customerName}}", ctx) == "Ada"
    assert workflows._tmpl("{{NODE.3.customerName}}", ctx) == "Ada"   # the base is case-insensitive


def test_dotted_path_missing_or_non_json_renders_empty():
    ctx = _ctx(last="plain text, not json", nodes={5: json.dumps({"a": 1})})
    assert workflows._tmpl("[{{last.result}}]", ctx) == "[]"
    assert workflows._tmpl("[{{node.5.b}}]", ctx) == "[]"
    assert workflows._tmpl("[{{node.5.a.deeper}}]", ctx) == "[]"


def test_whole_value_tokens_unchanged():
    ctx = _ctx(input="IN", last="LAST", nodes={2: "N2"})
    assert workflows._tmpl("{{input}}|{{last}}|{{node.2}}|{{node.2.output}}|{{step.2}}", ctx) \
        == "IN|LAST|N2|N2|N2"
    assert workflows._tmpl("[{{item}}]", ctx) == "[]"                 # None item never renders "None"


def test_node_output_suffix_still_means_whole_value_even_with_output_key():
    ctx = _ctx(nodes={4: json.dumps({"output": {"x": 1}})})
    assert workflows._tmpl("{{node.4.output}}", ctx) == ctx["nodes"][4]      # backward compat
    assert workflows._tmpl("{{node.4.output.x}}", ctx) == "1"                # dig past it


def test_dotted_path_end_to_end_through_a_transform_node():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template",
          "text": json.dumps({"user": {"name": "Ada", "langs": ["py", "js"]}})},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{node.2.user.name}} likes {{last.user.langs.1}}"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert rec["output"] == "Ada likes js"


# ---------------- wait node ----------------
def test_norm_graph_normalizes_wait():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "wait", "minutes": 99999, "until": "9:30"},
        {"id": 2, "type": "wait", "minutes": "junk", "until": "25:99"},
    ], "edges": []})
    n1, n2 = g["nodes"]
    assert n1["minutes"] == 1440 and n1["until"] == "9:30"            # clamped; HH:MM accepted
    assert n2["minutes"] == 1 and n2["until"] == ""                   # junk → defaults


def test_wait_seconds_duration_and_until():
    assert workflows._wait_seconds({"minutes": 3}) == 180
    secs = workflows._wait_seconds({"minutes": 3, "until": "12:00"})  # until beats minutes
    assert 0 < secs <= 24 * 3600


def test_wait_node_waits_then_continues_without_clobbering_last(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    # freeze the deadline race: first call sets the deadline, the rest walk past it
    t = {"now": 1000.0}

    def fake_time():
        t["now"] += 40                   # every check jumps a slice forward
        return t["now"]
    monkeypatch.setattr(time, "time", fake_time)
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "PRE-WAIT"},
         {"id": 3, "type": "wait", "minutes": 1},
         {"id": 4, "type": "transform", "mode": "template", "text": "after: {{last}}"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[3]["ok"] is True and steps[3]["output"].startswith("waited")
    assert rec["output"] == "after: PRE-WAIT"        # the pause didn't overwrite {{last}}


# ---------------- overlap guard ----------------
def _seed_running(wid):
    workflows._LIVE[wid] = {"workflow_id": wid, "name": "t", "trigger": "manual",
                            "started": workflows._now(), "beat": time.time(), "status": "running",
                            "current": None, "steps": [], "summary": "", "finished": None,
                            "run_id": None, "awaiting": None}


def test_second_run_is_skipped_while_one_is_in_flight():
    wf = _wf([{"id": 1, "type": "start"}, {"id": 2, "type": "end"}], [{"from": 1, "to": 2}])
    _seed_running(wf["id"])
    events = []
    rec = workflows.run(wf, trigger="watch", on_step=events.append)
    assert rec["status"] == "skipped"
    assert "already in progress" in rec["summary"]
    assert workflows.runs(wf["id"])[0]["status"] == "skipped"         # persisted for the history view
    assert events and events[-1]["event"] == "done"                   # an SSE listener still completes
    # the in-flight run's live entry was left untouched
    assert workflows._LIVE[wf["id"]]["status"] == "running"


def test_overlap_allow_opts_into_concurrent_runs():
    wf = _wf([{"id": 1, "type": "start"}, {"id": 2, "type": "end"}], [{"from": 1, "to": 2}],
             overlap="allow")
    _seed_running(wf["id"])
    rec = workflows.run(wf, trigger="watch")                          # non-nested, like a real trigger
    assert rec["status"] != "skipped"


def test_nested_subflow_runs_ignore_the_guard():
    wf = _wf([{"id": 1, "type": "start"}, {"id": 2, "type": "end"}], [{"from": 1, "to": 2}])
    _seed_running(wf["id"])
    rec = workflows.run(wf, trigger="subflow", nested=True)
    assert rec["status"] != "skipped"


def test_create_and_update_normalize_overlap():
    wf = workflows.create("a", overlap="allow")
    assert wf["overlap"] == "allow"
    wf2 = workflows.create("b", overlap="bogus")
    assert wf2["overlap"] == "skip"
    workflows.update(wf["id"], overlap="skip")
    assert workflows.get(wf["id"])["overlap"] == "skip"
    workflows.update(wf["id"], overlap="bogus")                       # junk never changes it
    assert workflows.get(wf["id"])["overlap"] == "skip"
