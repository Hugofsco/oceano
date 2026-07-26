"""Static contract checks for the shipped app-builder example: deterministic project isolation,
human approval before mutation, a real test gate, least-privilege reviews, and a bounded
fix/reverify loop. The generic
"imports cleanly, real tools/personas, no dangling edges" checks live in
test_example_workflows.py; this file checks THIS workflow's specific design intent: every
fallible node has error-edge recovery, no meeting/review panel exceeds the concurrency cap, the
production-readiness loop actually loops back, and — per the explicit brief this was built to —
there's no financial/business-analysis persona anywhere in it.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap

EXAMPLE = pathlib.Path(__file__).parent.parent / "examples" / "workflows" / "app-builder-idea-to-production.workflow.json"


def _payload():
    return json.loads(EXAMPLE.read_text())


def test_example_file_exists_and_imports_cleanly():
    payload = _payload()
    assert payload.get("name") and payload.get("description")
    wf = workflows.import_wf(payload)
    assert len(wf["graph"]["nodes"]) == len(payload["graph"]["nodes"])
    assert len(wf["graph"]["edges"]) == len(payload["graph"]["edges"])


def test_takes_an_app_idea_as_input():
    payload = _payload()
    inp = payload.get("input") or {}
    assert inp.get("enabled") is True
    assert "idea" in (inp.get("label") or "").lower()


def test_no_financial_or_business_analysis_persona_anywhere():
    """The brief this was built to is explicit: 'no financial analysis — we are building an app,
    not a business.' persona-finance-lead (and its business-flavored sibling
    persona-growth-strategist) must never appear on any node."""
    payload = _payload()
    personas = {n.get("persona") for n in payload["graph"]["nodes"] if n.get("persona")}
    assert "persona-finance-lead" not in personas
    assert "persona-growth-strategist" not in personas


def test_every_fallible_node_has_an_error_edge_to_the_failure_notice():
    payload = _payload()
    nodes = {n["id"]: n for n in payload["graph"]["nodes"]}
    edges = payload["graph"]["edges"]
    error_targets = {e["from"]: e["to"] for e in edges if e.get("branch") == "error"}
    attached = {e["from"] for e in edges
                if nodes[e["from"]]["type"] == "agent" and nodes[e["to"]]["type"] == "orchestrate"
                and e.get("branch") in (None, "next")}
    # the two terminal single-purpose notify nodes (success, failure) are the end of their
    # branch — nothing left to fall back to if THEY fail, same convention as the app-builder
    # workflow this replaced
    terminal_notices = {n["id"] for n in payload["graph"]["nodes"]
                         if n["type"] == "tool" and n.get("tool") == "notify"}
    fallible = {n["id"] for n in payload["graph"]["nodes"]
                if n["type"] not in ("start", "trigger", "end", "approval")} - attached - terminal_notices
    missing = fallible - set(error_targets)
    assert not missing, f"nodes with no error recovery: {sorted(missing)}"
    assert len(terminal_notices) == 2                          # the success and failure notices
    failure_notice = next(nid for nid in terminal_notices
                           if "failed" in nodes[nid]["args"]["title"].lower())
    assert set(error_targets.values()) == {failure_notice}


def test_no_meeting_or_review_panel_exceeds_two_concurrent_agents():
    payload = _payload()
    for n in payload["graph"]["nodes"]:
        if n["type"] != "orchestrate":
            continue
        plan = n.get("plan") or {}
        by_step = {}
        for nid, step in plan.items():
            by_step.setdefault(step, []).append(nid)
        widest = max((len(v) for v in by_step.values()), default=0)
        assert widest <= 2, f"orchestrate node {n['id']} runs {widest} agents concurrently in one step"


def test_the_development_loop_actually_loops_back_to_retest():
    payload = _payload()
    nodes = {n["id"]: n for n in payload["graph"]["nodes"]}
    edges = payload["graph"]["edges"]

    run_tests_id = next(n["id"] for n in payload["graph"]["nodes"]
                         if n["type"] == "tool" and n.get("tool") == "run_tests")
    decision_id = next(n["id"] for n in payload["graph"]["nodes"]
                       if n["type"] == "decision" and n.get("mode") == "model")

    # the "no" branch must route to a fix step, and that fix step must route back to run_tests
    no_edge = next(e for e in edges if e["from"] == decision_id and e.get("branch") == "no")
    fix_id = no_edge["to"]
    assert nodes[fix_id]["type"] == "delegate"
    back_edge = next(e for e in edges if e["from"] == fix_id and e.get("branch") is None)
    assert back_edge["to"] == run_tests_id

    # the "yes" branch must NOT loop — it heads toward an end node
    yes_edge = next(e for e in edges if e["from"] == decision_id and e.get("branch") == "yes")
    seen, cur = set(), yes_edge["to"]
    while cur is not None and cur not in seen:
        seen.add(cur)
        if nodes[cur]["type"] == "end":
            break
        nxt = [e["to"] for e in edges if e["from"] == cur and e.get("branch") in (None, "next")]
        cur = nxt[0] if nxt else None
    assert cur is not None and nodes[cur]["type"] == "end"


def test_build_steps_have_shell_access_and_reviews_are_execute_only():
    """'Each agent must work on their own task' with shell access to run tests — the BUILD
    delegates need the shell tier; production-readiness reviewers can run checks but must not
    receive file-edit tools while judging the app."""
    payload = _payload()
    build_labels = {"BUILD — backend", "BUILD — frontend", "BUILD — operability"}
    for n in payload["graph"]["nodes"]:
        text = n.get("text") or n.get("task") or ""
        if any(text.startswith(b) for b in build_labels):
            assert n.get("write") == "shell", f"node {n['id']} should build and verify with shell access"
        if "review" in (n.get("label") or "").lower():
            assert n.get("write") == "execute", f"node {n['id']} should execute checks without edit tools"


def test_project_path_is_deterministic_and_used_for_artifacts_and_tests():
    payload = _payload()
    nodes = {n["id"]: n for n in payload["graph"]["nodes"]}
    path_node = next(n for n in nodes.values() if n["type"] == "transform" and "projects/" in n.get("text", ""))
    token = "{{node.%s}}" % path_node["id"]
    assert path_node.get("mode") == "python"
    run_tests = next(n for n in nodes.values() if n.get("tool") == "run_tests")
    assert run_tests["args"]["path"] == token
    reports = [n["args"]["path"] for n in nodes.values() if n.get("tool") == "write_file"]
    assert reports and all(path.startswith(token + "/docs/") for path in reports)
    builders = [n for n in nodes.values() if (n.get("text") or "").startswith("BUILD —")]
    assert builders and all(token in n["text"] for n in builders)


def test_human_approval_precedes_first_mutating_delegate():
    payload = _payload()
    nodes = {n["id"]: n for n in payload["graph"]["nodes"]}
    edges = payload["graph"]["edges"]
    approval = next(n for n in nodes.values() if n["type"] == "approval")
    first_builder = next(n for n in nodes.values() if (n.get("text") or "").startswith("BUILD — backend"))
    assert any(e["from"] == approval["id"] and e["to"] == first_builder["id"]
               and e.get("branch") == "approved" for e in edges)
    assert any(e["from"] == approval["id"] and e.get("branch") == "rejected"
               and nodes[e["to"]]["type"] == "end" for e in edges)


def test_zero_exit_test_gate_precedes_model_readiness_review():
    payload = _payload()
    nodes = {n["id"]: n for n in payload["graph"]["nodes"]}
    edges = payload["graph"]["edges"]
    run_tests = next(n for n in nodes.values() if n.get("tool") == "run_tests")
    gate = next(n for n in nodes.values() if n["type"] == "decision" and n.get("mode") == "rule")
    assert gate.get("ruleOp") == "contains" and gate.get("ruleValue") == "(exit 0)"
    assert any(e["from"] == run_tests["id"] and e["to"] == gate["id"] for e in edges)
    no_target = next(e["to"] for e in edges if e["from"] == gate["id"] and e.get("branch") == "no")
    assert nodes[no_target]["type"] == "delegate"
