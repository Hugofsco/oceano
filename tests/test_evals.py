"""Eval harness: grading combines code graders with an LLM judge whose output is untrusted
free-form text — these tests pin the verdict parsing (a malformed judge score must grade 0,
never crash a whole run), the deterministic graders, the score composition, the leaderboard
aggregation, and the guards around live-run state.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import evals  # noqa: E402 - after the sys.path bootstrap


def _use_tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(evals, "DB_PATH", tmp_path / "evals.db")


def _case(graders, rubric="be right"):
    return {"id": 1, "name": "c", "category": "qa", "prompt": "p", "rubric": rubric,
            "graders": graders, "seed": {}, "timeout": 60, "weight": 1.0, "enabled": True}


def _run(tmp_path, answer="", tools=(), error=None):
    return {"answer": answer, "tools": list(tools), "tokens": 10, "steps": 1, "ms": 5,
            "error": error, "scratch": str(tmp_path), "files": []}


def test_case_crud_normalises_bad_category_and_graders(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    cid = evals.save_case(None, "c1", "not-a-category", "prompt", "rubric",
                          [{"type": "nonsense"}, "garbage", {"no": "type"}])
    (c,) = evals.all_cases()
    assert c["category"] == "qa"                       # unknown category → qa
    assert c["graders"] == [{"type": "judge"}]         # invalid graders → judge default
    evals.save_case(cid, "c1", "code", "prompt2", "rubric",
                    [{"type": "contains", "value": "x"}], enabled=False)
    (c,) = evals.all_cases()
    assert c["category"] == "code" and c["enabled"] is False and c["prompt"] == "prompt2"
    evals.delete_case(cid)
    assert evals.all_cases() == []


def test_grade_deterministic_contains_files_and_tools(tmp_path):
    case = _case([{"type": "contains", "value": "Tokyo"},
                  {"type": "file_exists", "path": "out.txt", "nonempty": True},
                  {"type": "tool_called", "name": "fetch_url"}])
    run = _run(tmp_path, answer="the capital is tokyo", tools=["fetch_url"])
    (tmp_path / "out.txt").write_text("data")
    ok, notes = evals._grade_deterministic(case, run)
    assert ok is True and len(notes) == 3              # contains is case-insensitive
    (tmp_path / "out.txt").write_text("")              # empty file fails nonempty
    ok, _ = evals._grade_deterministic(case, run)
    assert ok is False


def test_grade_deterministic_invalid_regex_falls_back_to_substring(tmp_path):
    case = _case([{"type": "contains", "value": "a(b", "regex": True}])
    ok, _ = evals._grade_deterministic(case, _run(tmp_path, answer="found A(B here"))
    assert ok is True                                  # bad regex → plain (lowercased) substring


def _fake_judge_delegate(monkeypatch, output, ok=True):
    monkeypatch.setattr("oceano.delegate.run",
                        lambda prompt, **kw: {"ok": ok, "output": output, "error": "" if ok else output})


def test_judge_clamps_and_coerces_llm_scores(monkeypatch, tmp_path):
    case = _case([{"type": "judge"}])
    for raw, want in (("250", 100), ("-5", 0), ('"85.5"', 85), ('"high"', 0), ("null", 0)):
        _fake_judge_delegate(monkeypatch, f'{{"score": {raw}, "pass": true}}')
        v = evals._judge(case, _run(tmp_path, answer="a"))
        assert v["score"] == want, f"score {raw!r} should grade {want}"
        assert not v.get("judge_error")                # coerced, not treated as a judge failure


def test_judge_failure_and_garbage_grade_zero_with_the_error_flag(monkeypatch, tmp_path):
    case = _case([{"type": "judge"}])
    _fake_judge_delegate(monkeypatch, "usage limit reached", ok=False)
    v = evals._judge(case, _run(tmp_path))
    assert v["score"] == 0 and v["pass"] is False and v["judge_error"] is True
    _fake_judge_delegate(monkeypatch, "Looks good to me!")            # no JSON verdict
    assert evals._judge(case, _run(tmp_path))["judge_error"] is True
    _fake_judge_delegate(monkeypatch, '{"score": 90, "pass": true')   # truncated JSON
    assert evals._judge(case, _run(tmp_path))["judge_error"] is True


def test_grade_composition(monkeypatch, tmp_path):
    # a run error grades 0 before any grader runs
    g = evals._grade(_case([{"type": "judge"}]), _run(tmp_path, error="timeout"))
    assert g["score"] == 0.0 and g["passed"] is False and "run error" in g["verdict"]["reasoning"]
    # no judge grader → purely deterministic 100/0
    case = _case([{"type": "contains", "value": "yes"}])
    assert evals._grade(case, _run(tmp_path, answer="yes"))["score"] == 100.0
    assert evals._grade(case, _run(tmp_path, answer="no"))["score"] == 0.0
    # a deterministic failure caps the judge's score at 0
    monkeypatch.setattr(evals, "_judge", lambda c, r: {"score": 90, "pass": True})
    case = _case([{"type": "contains", "value": "yes"}, {"type": "judge"}])
    g = evals._grade(case, _run(tmp_path, answer="no"))
    assert g["score"] == 0.0 and g["passed"] is False
    # det pass + judge verdict → the judge's score and pass
    g = evals._grade(case, _run(tmp_path, answer="yes"))
    assert g["score"] == 90.0 and g["passed"] is True


def _insert_run(status, results):
    """results: [(model, score, passed)]"""
    con = evals._db()
    cur = con.execute("INSERT INTO runs (ts, models, status, summary) VALUES ('t','[]',?, '')", (status,))
    rid = cur.lastrowid
    for model, score, passed in results:
        con.execute("INSERT INTO results (run_id, case_id, case_name, model, score, passed, tokens, "
                    "ms, steps, tools, error, verdict, answer) VALUES (?,1,'c',?,?,?,10,5,1,'[]',NULL,'{}','')",
                    (rid, model, score, 1 if passed else 0))
    con.commit()
    con.close()
    return rid


def test_leaderboard_aggregates_the_latest_finished_run(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    assert evals.leaderboard() == {"run_id": None, "rows": []}
    _insert_run("done", [("old-model", 10, False)])
    rid = _insert_run("done", [("a", 80, True), ("a", 60, False), ("b", 90, True), ("b", 90, True)])
    _insert_run("running", [("newer-but-unfinished", 100, True)])
    board = evals.leaderboard()
    assert board["run_id"] == rid                      # latest DONE run, not the running one
    assert [r["model"] for r in board["rows"]] == ["b", "a"]     # sorted by score desc
    a = next(r for r in board["rows"] if r["model"] == "a")
    assert a["score"] == 70.0 and a["pass_rate"] == 50 and a["cases"] == 2


def test_best_model_picks_the_top_scorer_among_served(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    assert evals.best_model() is None                  # no finished run → no signal
    _insert_run("done", [("weak", 40, False), ("strong", 90, True), ("mid", 70, True)])
    assert evals.best_model() == "strong"
    assert evals.best_model(among=["mid", "weak"]) == "mid"      # winner not served → next best
    assert evals.best_model(among=["unserved"]) is None


def test_best_model_ignores_a_stale_run(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    con = evals._db()
    con.execute("INSERT INTO runs (ts, models, status, summary) VALUES "
                "('2020-01-01T00:00:00+00:00','[]','done','')")
    rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("INSERT INTO results (run_id, case_id, case_name, model, score, passed, tokens, ms, "
                "steps, tools, error, verdict, answer) VALUES (?,1,'c','old-king',99,1,1,1,1,'[]',NULL,'{}','')",
                (rid,))
    con.commit()
    con.close()
    assert evals.best_model() is None                  # years-old verdicts must not steer routing


def test_leaderboard_category_filter_joins_the_case(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    code_id = evals.save_case(None, "write-code", "code", "p", "r", [{"type": "judge"}])
    qa_id = evals.save_case(None, "answer-q", "qa", "p", "r", [{"type": "judge"}])
    con = evals._db()
    cur = con.execute("INSERT INTO runs (ts, models, status, summary) VALUES ('t','[]','done','')")
    rid = cur.lastrowid
    for cid, model, score in ((code_id, "coder", 95), (code_id, "chatter", 40),
                              (qa_id, "coder", 50), (qa_id, "chatter", 90)):
        con.execute("INSERT INTO results (run_id, case_id, case_name, model, score, passed, tokens, ms, "
                    "steps, tools, error, verdict, answer) VALUES (?,?,'c',?,?,1,1,1,1,'[]',NULL,'{}','')",
                    (rid, cid, model, score))
    con.commit()
    con.close()
    assert [r["model"] for r in evals.leaderboard(rid)["rows"]] == ["coder", "chatter"]   # 72.5 vs 65
    assert [r["model"] for r in evals.leaderboard(rid, category="qa")["rows"]] == ["chatter", "coder"]
    assert evals.best_model(category="code") == "coder"
    assert evals.best_model(category="qa") == "chatter"


def test_selected_models_intersects_saved_with_available(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(evals, "available_models", lambda: ["m1", "m2", "m3"])
    assert evals.set_selected_models(["m2", "gone", "m2", 7]) == ["m2", "gone"]   # dedup, non-str dropped
    assert evals.selected_models() == ["m2"]           # saved ∩ available
    evals.set_selected_models(["gone", "also-gone"])   # nothing saved still served
    assert evals.selected_models() == ["m1", "m2", "m3"]          # → run all (never silently empty)
    evals.set_selected_models([])
    assert evals.selected_models() == ["m1", "m2", "m3"]


def test_delete_and_clear_refuse_while_that_run_is_live(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    rid = _insert_run("running", [("m", 50, True)])
    other = _insert_run("done", [("m", 50, True)])
    monkeypatch.setitem(evals._STATE, "running", True)
    monkeypatch.setitem(evals._STATE, "run_id", rid)
    assert evals.delete_run(rid) is False              # its worker is still writing rows
    assert evals.clear_runs() is None
    assert evals.delete_run(other) is True             # a finished run can still be pruned
    monkeypatch.setitem(evals._STATE, "running", False)
    monkeypatch.setitem(evals._STATE, "run_id", None)
    assert evals.delete_run(rid) is True


def test_judge_prompt_includes_rubric_and_truncates_the_answer(monkeypatch, tmp_path):
    """The judge must see the rubric and files, but a runaway answer is capped so a rambling
    model can't blow the judge's context."""
    seen = {}
    def capture(prompt, **kw):
        seen["prompt"] = prompt
        return {"ok": True, "output": '{"score": 50, "pass": true}', "error": ""}
    monkeypatch.setattr("oceano.delegate.run", capture)
    case = _case([{"type": "judge"}], rubric="must cite a source")
    evals._judge(case, _run(tmp_path, answer="x" * 10000))
    assert "must cite a source" in seen["prompt"]
    assert len(seen["prompt"]) < 6000                  # 4000-char answer cap held


def test_eval_tool_setup_never_exposes_unfixtureed_personal_tools():
    case = _case([{"type": "tool_called", "name": "mail_send"}])
    allowed, fixtures = evals._case_tool_setup(case)
    assert "mail_send" not in allowed
    assert fixtures == {}

    calendar = {**case, "name": "tool-choice-calendar",
                "graders": [{"type": "tool_called", "name": "calendar_events"}]}
    allowed, fixtures = evals._case_tool_setup(calendar)
    assert allowed == {"calendar_events"}
    assert "synthetic eval fixture" in fixtures["calendar_events"]


def test_leaderboard_honours_case_weights(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    important = evals.save_case(None, "important", "qa", "p", "r", [{"type": "judge"}], weight=3)
    minor = evals.save_case(None, "minor", "qa", "p", "r", [{"type": "judge"}], weight=1)
    con = evals._db()
    rid = con.execute("INSERT INTO runs (ts, models, status, summary) "
                      "VALUES ('2099-01-01T00:00:00+00:00','[]','done','')").lastrowid
    for cid, score, weight in ((important, 100, 3), (minor, 0, 1)):
        con.execute("INSERT INTO results (run_id,case_id,case_name,model,score,passed,tokens,ms,"
                    "steps,tools,error,verdict,answer,weight) "
                    "VALUES (?,?,?,'m',?,1,1,1,1,'[]',NULL,'{}','',?)",
                    (rid, cid, str(cid), score, weight))
    con.commit(); con.close()
    assert evals.leaderboard(rid)["rows"][0]["score"] == 75.0


def test_best_model_refuses_to_route_on_an_effective_tie(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    _insert_run("done", [("a", 80, True), ("b", 79, True)])
    assert evals.best_model() is None


def test_compare_tool_routing_runs_the_same_cases_in_both_modes(monkeypatch):
    case = {"id": 7, "name": "routing-case", "enabled": True}
    seen = []
    monkeypatch.setattr(evals, "all_cases", lambda: [case])

    def fake_run(c, model, dynamic_tools=None):
        seen.append(dynamic_tools)
        return {"case": c, "model": model, "tokens": 12 if dynamic_tools else 20,
                "steps": 1, "ms": 80 if dynamic_tools else 100, "tools": ["read_file"],
                "error": None, "routing": {"advertised_tools": 8 if dynamic_tools else 20,
                                             "catalog_tools": 20}}

    monkeypatch.setattr(evals, "_run_case", fake_run)
    monkeypatch.setattr(evals, "_grade", lambda c, run: {"score": 100, "passed": True})
    report = evals.compare_tool_routing("model-a")
    assert seen == [False, True]
    assert report["full"]["avg_advertised_tools"] == 20
    assert report["routed"]["avg_advertised_tools"] == 8
    assert report["routed"]["avg_score"] == report["full"]["avg_score"] == 100
