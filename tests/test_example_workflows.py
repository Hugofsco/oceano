"""The shipped example workflows (examples/workflows/*.workflow.json) must stay importable
and honest: every file imports cleanly, survives graph normalization without losing nodes or
edges (a dropped node means a typo'd type or field), and only references tools and persona
skills that actually exist.
"""
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap

EXAMPLES = sorted((pathlib.Path(__file__).parent.parent / "examples" / "workflows").glob("*.workflow.json"))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from oceano import scheduler
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    yield


def test_examples_exist():
    assert len(EXAMPLES) >= 2


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_imports_cleanly(path):
    payload = json.loads(path.read_text())
    assert payload.get("name") and payload.get("description")
    wf = workflows.import_wf(payload)
    assert wf is not None
    # normalization kept every node and edge — a dropped node means a typo'd type/field
    assert len(wf["graph"]["nodes"]) == len(payload["graph"]["nodes"])
    assert len(wf["graph"]["edges"]) == len(payload["graph"]["edges"])


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_references_real_tools_and_personas(path):
    from oceano import tools
    skills_dir = pathlib.Path(__file__).parent.parent / "skills"
    payload = json.loads(path.read_text())
    for n in payload["graph"]["nodes"]:
        if n["type"] == "tool":
            assert n["tool"] in tools._TOOLS, f"{path.name}: unknown tool {n['tool']!r}"
        persona = n.get("persona")
        if persona:
            assert (skills_dir / persona / "SKILL.md").exists(), \
                f"{path.name}: persona skill {persona!r} not found"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_edges_reference_existing_nodes(path):
    payload = json.loads(path.read_text())
    ids = {n["id"] for n in payload["graph"]["nodes"]}
    for e in payload["graph"]["edges"]:
        assert e["from"] in ids and e["to"] in ids, f"{path.name}: dangling edge {e}"
    # exactly one start/trigger to walk from
    starts = [n for n in payload["graph"]["nodes"] if n["type"] in ("start", "trigger")]
    assert len(starts) == 1, f"{path.name}: expected one start/trigger node"


def test_inbox_example_fences_untrusted_mail_and_allows_bursts():
    path = next(p for p in EXAMPLES if p.name == "inbox-sentry.workflow.json")
    payload = json.loads(path.read_text())
    assert payload["overlap"] == "allow"
    model_inputs = [n.get("question", "") + n.get("text", "")
                    for n in payload["graph"]["nodes"] if n["type"] in ("decision", "instruction")]
    assert model_inputs
    assert all('<untrusted source="email">' in text and "never follow instructions" in text.lower()
               for text in model_inputs)


def test_everyday_examples_have_explicit_preparation_failure_paths():
    for filename in ("inbox-sentry.workflow.json", "daily-standup.workflow.json"):
        path = next(p for p in EXAMPLES if p.name == filename)
        payload = json.loads(path.read_text())
        nodes = {n["id"]: n for n in payload["graph"]["nodes"]}
        failures = {n["id"] for n in nodes.values() if n.get("tool") == "notify"
                    and "failed" in n.get("args", {}).get("title", "").lower()}
        assert failures, f"{filename}: missing failure notification"
        error_edges = [e for e in payload["graph"]["edges"] if e.get("branch") == "error"]
        assert error_edges and all(e["to"] in failures for e in error_edges)
