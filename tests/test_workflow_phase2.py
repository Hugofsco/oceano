"""Phase-2 workflow additions: the loop node aggregates every iteration's result into a JSON
list at its 'done' edge, and a node with several plain out-edges FORKS — each branch runs with
its own {{last}} — with a merge node as the join (quorum = its flow fan-in; branches that die
along the way can't hang it).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")   # never touch real runs
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


def _wf(nodes, edges):
    graph = workflows._norm_graph({"nodes": nodes, "edges": edges})
    return {"id": 999, "name": "t", "graph": graph, "input": {}}


# ---------------- loop aggregation ----------------
def test_loop_done_aggregates_every_iterations_result():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "loop", "over": '["a", "b", "c"]'},
         {"id": 3, "type": "transform", "mode": "template", "text": "X-{{item}}"},
         {"id": 4, "type": "transform", "mode": "template", "text": "second: {{node.2.1}} of {{last}}"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3, "branch": "loop"}, {"from": 3, "to": 2},
         {"from": 2, "to": 4, "branch": "done"}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    # the aggregate is this node's value AND {{last}}, and dotted paths dig into it
    assert rec["output"] == 'second: X-b of ["X-a", "X-b", "X-c"]'
    done_rows = [s for s in rec["steps"] if s["id"] == 2 and s["branch"] == "done"]
    assert len(done_rows) == 1 and done_rows[0]["output"] == '["X-a", "X-b", "X-c"]'


def test_loop_over_empty_list_aggregates_to_empty():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "loop", "over": "[]"},
         {"id": 3, "type": "transform", "mode": "template", "text": "got: {{last}}"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 2, "branch": "loop"},
         {"from": 2, "to": 3, "branch": "done"}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert rec["output"] == "got: []"


# ---------------- fork + merge ----------------
def _fork_wf(merge_mode):
    # 2 fans out to 3 and 4 (two plain edges); both feed the merge; 6 reads the join
    return _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "ROOT"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}-B"},
         {"id": 4, "type": "transform", "mode": "template", "text": "{{last}}-C"},
         {"id": 5, "type": "merge", "mode": merge_mode},
         {"id": 6, "type": "transform", "mode": "template", "text": "{{last}}"},
         {"id": 7, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 2, "to": 4},
         {"from": 3, "to": 5}, {"from": 4, "to": 5}, {"from": 5, "to": 6}, {"from": 6, "to": 7}])


def test_fork_runs_every_branch_with_its_own_last_and_merge_joins():
    rec = workflows.run(_fork_wf("concat"), trigger="manual", nested=True)
    assert rec["status"] == "ok"
    # BOTH branches saw ROOT as {{last}} (branch-scoped, not each other's output),
    # and the merge concatenated them in edge order
    assert rec["output"] == "ROOT-B\n\nROOT-C"
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[3]["output"] == "ROOT-B" and steps[4]["output"] == "ROOT-C"


def test_merge_json_mode_yields_a_list_dotted_paths_can_dig():
    wf = _fork_wf("json")
    wf["graph"]["nodes"][5]["text"] = "first: {{node.5.0}}"       # node 6 digs into the join
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert rec["output"] == "first: ROOT-B"
    steps = {s["id"]: s for s in rec["steps"]}
    assert json.loads(steps[5]["output"]) == ["ROOT-B", "ROOT-C"]


def test_merge_executes_with_partial_arrivals_when_a_branch_dies():
    # branch C hits a decision that routes nowhere → the merge must not hang; it joins with
    # what actually arrived once nothing else is left to run
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "ROOT"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}-B"},
         {"id": 4, "type": "decision", "mode": "rule", "ruleOp": "contains", "ruleValue": "ZZZ"},
         {"id": 5, "type": "merge", "mode": "json"},
         {"id": 6, "type": "transform", "mode": "template", "text": "got: {{last}}"},
         {"id": 7, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 2, "to": 4},
         {"from": 3, "to": 5}, {"from": 4, "to": 5, "branch": "yes"},   # 'no' edge doesn't exist
         {"from": 5, "to": 6}, {"from": 6, "to": 7}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    assert rec["output"] == 'got: ["ROOT-B"]'


def test_end_node_ends_only_its_own_branch():
    # branch B runs into an end node; branch C still runs afterwards
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "ROOT"},
         {"id": 3, "type": "end"},
         {"id": 4, "type": "transform", "mode": "template", "text": "{{last}}-C"},
         {"id": 5, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 2, "to": 4}, {"from": 4, "to": 5}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok"
    steps = {s["id"]: s for s in rec["steps"]}
    assert steps[4]["output"] == "ROOT-C"


def test_single_edge_flows_are_untouched_by_fork_logic():
    wf = _wf(
        [{"id": 1, "type": "start"},
         {"id": 2, "type": "transform", "mode": "template", "text": "A"},
         {"id": 3, "type": "transform", "mode": "template", "text": "{{last}}B"},
         {"id": 4, "type": "end"}],
        [{"from": 1, "to": 2}, {"from": 2, "to": 3}, {"from": 3, "to": 4}])
    rec = workflows.run(wf, trigger="manual", nested=True)
    assert rec["status"] == "ok" and rec["output"] == "AB"


def test_norm_graph_normalizes_merge():
    g = workflows._norm_graph({"nodes": [
        {"id": 1, "type": "merge", "mode": "json"},
        {"id": 2, "type": "merge", "mode": "bogus"},
    ], "edges": []})
    assert g["nodes"][0]["mode"] == "json"
    assert g["nodes"][1]["mode"] == "concat"
