"""Phase-3 workflow additions: export/import/duplicate (portable JSON, webhook secrets
stripped), run history split into its own store and pruned per workflow, persisted
watch/email trigger baselines, and the synchronous webhook flavour.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import workflows  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "STORE", tmp_path / "workflows.json")   # never touch real data
    monkeypatch.setattr(workflows, "RUNS_STORE", tmp_path / "workflow_runs.json")
    monkeypatch.setattr(workflows, "TRIG_STATE", tmp_path / "trigger_state.json")
    monkeypatch.setattr(workflows, "_LIVE", {})
    monkeypatch.setattr(workflows, "_WATCH_SIG", {})
    monkeypatch.setattr(workflows, "_EMAIL_SEEN", {})
    monkeypatch.setattr(workflows, "_trig_loaded", False)
    monkeypatch.setattr("oceano.logs.log_run", lambda *a, **k: None)
    yield


_GRAPH = {"nodes": [{"id": 1, "type": "start"},
                    {"id": 2, "type": "transform", "mode": "template", "text": "hi {{input}}"},
                    {"id": 3, "type": "end"}],
          "edges": [{"from": 1, "to": 2}, {"from": 2, "to": 3}]}


# ---------------- export / import / duplicate ----------------
def test_export_strips_webhook_tokens_and_import_mints_fresh_ones():
    wf = workflows.create("hooked", graph=_GRAPH, overlap="allow")
    workflows.set_triggers(wf["id"], [{"type": "webhook", "enabled": True}])
    tok = workflows.get_triggers(wf["id"])[0]["token"]
    assert tok
    out = workflows.export_wf(wf["id"])
    assert "token" not in out["triggers"][0]                  # a shared export carries no live URL
    assert "id" not in out and "created" not in out
    imported = workflows.import_wf(out)
    assert imported["name"] == "hooked (2)"                   # de-duped, not clobbered
    assert imported["overlap"] == "allow"
    new_tok = imported["triggers"][0]["token"]
    assert new_tok and new_tok != tok                         # fresh secret
    assert imported["graph"]["nodes"] == wf["graph"]["nodes"]


def test_export_import_carries_the_cron(monkeypatch, tmp_path):
    from oceano import scheduler
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    wf = workflows.create("timed", graph=_GRAPH)
    workflows.set_schedule(wf["id"], "0 7 * * *")
    out = workflows.export_wf(wf["id"])
    assert out["cron"] == "0 7 * * *"
    imported = workflows.import_wf(out)
    assert workflows.schedule_info(imported["id"])["cron"] == "0 7 * * *"


def test_import_rejects_non_workflow_payloads():
    assert workflows.import_wf({"name": "no graph"}) is None
    assert workflows.import_wf("junk") is None


def test_duplicate_copies_definition_not_history():
    wf = workflows.create("orig", graph=_GRAPH)
    workflows._record_run(wf["id"], "manual", "ok", [], "1/1 nodes ok")
    dup = workflows.duplicate(wf["id"])
    assert dup["id"] != wf["id"] and dup["name"] == "orig (2)"
    assert workflows.runs(dup["id"]) == []                    # history stays with the original


def test_import_replace_updates_in_place_keeping_id_and_history(monkeypatch, tmp_path):
    from oceano import scheduler
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "tasks.db")
    wf = workflows.create("digest", description="old", graph=_GRAPH)
    workflows.set_schedule(wf["id"], "0 7 * * *")
    workflows._record_run(wf["id"], "manual", "ok", [], "old run")
    doc = workflows.export_wf(wf["id"])
    doc["description"] = "new"
    doc["cron"] = "0 9 * * *"
    doc["graph"]["nodes"][1]["text"] = "bye {{input}}"
    out = workflows.import_wf(doc, replace=True)
    assert out["id"] == wf["id"] and out["description"] == "new"        # same workflow, updated
    assert out["graph"]["nodes"][1]["text"] == "bye {{input}}"
    assert workflows.schedule_info(wf["id"])["cron"] == "0 9 * * *"     # schedule replaced too
    assert len(workflows.runs(wf["id"])) == 1                           # history survived
    assert len(workflows.list_all()) == 1                               # no "digest (2)" copy


def test_import_without_replace_still_dedupes_the_name():
    workflows.create("digest", graph=_GRAPH)
    out = workflows.import_wf({"name": "digest", "graph": _GRAPH})
    assert out["name"] == "digest (2)"
    assert len(workflows.list_all()) == 2


def test_import_replace_with_no_existing_name_just_creates():
    out = workflows.import_wf({"name": "fresh", "graph": _GRAPH}, replace=True)
    assert out["name"] == "fresh"
    assert len(workflows.list_all()) == 1


# ---------------- run history: own store, per-workflow pruning ----------------
def test_runs_prune_per_workflow_not_globally():
    for i in range(workflows._RUNS_PER_WF + 10):
        workflows._record_run(1, "manual", "ok", [], f"busy {i}")
    workflows._record_run(2, "manual", "ok", [], "quiet")
    assert len(workflows.runs(1, limit=100)) == workflows._RUNS_PER_WF
    assert len(workflows.runs(2, limit=100)) == 1             # the busy flow didn't starve it
    assert workflows.runs(1, limit=100)[0]["summary"] == f"busy {workflows._RUNS_PER_WF + 9}"


def test_legacy_runs_migrate_out_of_the_hot_store():
    workflows.STORE.parent.mkdir(parents=True, exist_ok=True)
    workflows.STORE.write_text(json.dumps({
        "workflows": [], "runs": [{"id": 1, "workflow_id": 7, "ts": "t", "trigger": "manual",
                                   "status": "ok", "summary": "old", "steps": []}]}))
    assert workflows.runs(7)[0]["summary"] == "old"           # readable via the new store
    assert "runs" not in json.loads(workflows.STORE.read_text())   # and gone from the hot one
    assert json.loads(workflows.RUNS_STORE.read_text())["runs"]


def test_remove_purges_only_that_workflows_runs():
    a = workflows.create("a", graph=_GRAPH)
    workflows._record_run(a["id"], "manual", "ok", [], "keep? no")
    workflows._record_run(a["id"] + 1000, "manual", "ok", [], "other")
    workflows.remove(a["id"])
    assert workflows.runs(a["id"]) == []
    assert len(workflows.runs(a["id"] + 1000)) == 1


# ---------------- persisted trigger baselines ----------------
def test_watch_baseline_survives_a_restart(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "inbox").mkdir(parents=True)
    monkeypatch.setattr("config.WORKSPACE", ws)
    fired = []
    monkeypatch.setattr(workflows, "run_async", lambda wf, **kw: fired.append(wf["id"]))
    wf = workflows.create("watcher", graph=_GRAPH)
    workflows.set_triggers(wf["id"], [{"type": "watch", "enabled": True, "folder": "inbox"}])
    assert workflows.poll_watch_triggers() == 0               # first sight: baseline only
    # --- simulate a restart: fresh in-memory state, same persisted file ---
    monkeypatch.setattr(workflows, "_WATCH_SIG", {})
    monkeypatch.setattr(workflows, "_trig_loaded", False)
    assert workflows.poll_watch_triggers() == 0               # nothing changed while "down"
    (ws / "inbox" / "new.txt").write_text("arrived during downtime")
    monkeypatch.setattr(workflows, "_WATCH_SIG", {})
    monkeypatch.setattr(workflows, "_trig_loaded", False)
    assert workflows.poll_watch_triggers() == 1               # the downtime change still fires
    assert fired == [wf["id"]]


def test_folder_sig_is_stable_not_process_salted(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "d").mkdir(parents=True)
    (ws / "d" / "f.txt").write_text("x")
    monkeypatch.setattr("config.WORKSPACE", ws)
    s1 = workflows._folder_sig("d")
    assert isinstance(s1, str) and s1 == workflows._folder_sig("d")


# ---------------- synchronous webhook ----------------
def test_webhook_run_sync_returns_the_run_record():
    wf = workflows.create("api-flow", graph=_GRAPH,
                          input_cfg={"enabled": True})
    workflows.set_triggers(wf["id"], [{"type": "webhook", "enabled": True}])
    tok = workflows.get_triggers(wf["id"])[0]["token"]
    rec = workflows.webhook_run_sync(wf["id"], tok, inp="ocean")
    assert rec["status"] == "ok" and rec["output"] == "hi ocean"
    assert workflows.webhook_run_sync(wf["id"], "wrong-token") is None
