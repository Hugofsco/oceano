"""Skills-pipeline safety: the review pipeline mutates the skills library from DELEGATE OUTPUT
(free-form LLM text parsed into a plan), so these tests pin the trust boundaries — a learning
skill never reaches the agent, the reviewer can edit content but never publish, a garbage
verdict never stages a skill, and delete_skill can't escape the skills dir.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import skills  # noqa: E402 - after the sys.path bootstrap


def _use_tmp_skills(monkeypatch, tmp_path):
    d = tmp_path / "skills"
    monkeypatch.setattr(skills, "SKILLS_DIR", d)
    monkeypatch.setattr(skills, "_VEC_CACHE", {})
    return d


def test_parse_frontmatter_roundtrip_and_malformed_stays_whole(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills._write("my-skill", "my-skill", "when to use it", "1. do the thing", "published")
    (s,) = skills.all_skills()
    assert (s["name"], s["description"], s["status"], s["body"]) == \
        ("my-skill", "when to use it", "published", "1. do the thing")
    # an unterminated frontmatter block must not eat the text
    assert skills._parse("---\nname: broken\nno closing fence") == ({}, "---\nname: broken\nno closing fence")


def test_missing_status_means_pre_lifecycle_published(monkeypatch, tmp_path):
    d = _use_tmp_skills(monkeypatch, tmp_path)
    (d / "old-skill").mkdir(parents=True)
    (d / "old-skill" / "SKILL.md").write_text("---\nname: old-skill\ndescription: from before\n---\nbody\n")
    assert skills.all_skills()[0]["status"] == "published"      # pre-lifecycle skills stay live


def test_learning_skills_never_reach_the_agent(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("sneaky", "self-written, unreviewed", "1. do questionable things")
    assert "sneaky" not in skills.catalog()
    assert "sneaky" not in skills.relevant("do questionable things")
    assert "hasn't passed review" in skills.load_skill("sneaky")


def test_learn_skill_refuses_empty_and_never_overwrites(monkeypatch, tmp_path):
    d = _use_tmp_skills(monkeypatch, tmp_path)
    assert "refused" in skills.learn_skill("", "d", "body")
    assert "refused" in skills.learn_skill("name", "d", "  ")
    skills.save_skill("target", "target", "the real one", status="published")
    skills.learn_skill("target", "an impostor", "overwrite attempt")
    assert (d / "target" / "SKILL.md").read_text().count("the real one") == 1   # original untouched
    statuses = {s["dir"]: s["status"] for s in skills.all_skills()}
    assert statuses["target"] == "published" and statuses["target-2"] == "learning"


def test_delete_skill_cannot_escape_the_skills_dir(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    skills.save_skill("legit", "legit", "d", status="published")
    assert skills.delete_skill("../victim") is False and victim.is_dir()
    assert skills.delete_skill("legit") is True


def _fake_delegate(monkeypatch, output, ok=True, on_call=None):
    def run(prompt, **kw):
        if on_call:
            on_call(prompt, kw)
        return {"ok": ok, "output": output, "error": "" if ok else output}
    monkeypatch.setattr("oceano.delegate.run", run)


# --- from_conversation: the distiller's JSON is delegate output, parse defensively ---
def test_from_conversation_handles_delegate_failure_and_garbage(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    assert skills.from_conversation("  ")["ok"] is False        # empty transcript
    _fake_delegate(monkeypatch, "boom", ok=False)
    assert "delegate unavailable" in skills.from_conversation("user: hi")["error"]
    _fake_delegate(monkeypatch, "I couldn't find anything, sorry!")
    assert "no parsable result" in skills.from_conversation("user: hi")["error"]
    _fake_delegate(monkeypatch, '{"skill": true, "name": " ", "body": ""}')
    r = skills.from_conversation("user: hi")                    # skill=true but unusable fields
    assert r["ok"] is True and r["saved"] is False
    assert skills.all_skills() == []                            # nothing was written


def test_from_conversation_saves_a_valid_skill_as_learning(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    _fake_delegate(monkeypatch, 'Here is my verdict:\n{"skill": true, "name": "sum-csvs", '
                                '"description": "how to sum csv columns", "body": "1. use duckdb"}')
    r = skills.from_conversation("user: sum this csv…")
    assert r == {"ok": True, "saved": True, "name": "sum-csvs", "description": "how to sum csv columns"}
    (s,) = skills.all_skills()
    assert s["status"] == "learning"                            # enters review, never straight to live


def test_from_conversation_respects_a_no_skill_verdict(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    _fake_delegate(monkeypatch, '{"skill": false, "reason": "just chit-chat"}')
    r = skills.from_conversation("user: good morning!")
    assert r["ok"] is True and r["saved"] is False and r["reason"] == "just chit-chat"


# --- origin marker: self-learnt skills stay wipeable even once published ---
def test_learn_skill_origin_survives_the_whole_lifecycle(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("trick", "d", "1. step")
    (s,) = skills.all_skills()
    assert s["origin"] == "learned"
    skills.set_status(s["dir"], "staged", "✓ reviewed")
    skills.set_status(s["dir"], "published")
    (s,) = skills.all_skills()
    assert s["status"] == "published" and s["origin"] == "learned"


def test_save_skill_has_no_origin_but_keeps_learned_on_update(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.save_skill("mine", "mine", "user-authored", status="published")
    skills.learn_skill("trick", "d", "1. step")
    slug = next(s["dir"] for s in skills.all_skills() if s["name"] == "trick")
    skills.save_skill("trick", "edited by the user", "2. new step", dir=slug, status="published")
    origins = {s["dir"]: s["origin"] for s in skills.all_skills()}
    assert origins["mine"] == "" and origins[slug] == "learned"   # a user edit doesn't relabel it


def test_wipe_skills_removes_all_learnt_including_published(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from oceano.web import routes_system
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.save_skill("builtin", "builtin", "shipped with the repo", status="published")
    skills.learn_skill("published-learnt", "d", "1. step")
    slug = next(s["dir"] for s in skills.all_skills() if s["name"] == "published-learnt")
    skills.set_status(slug, "published")
    skills.learn_skill("still-learning", "d", "1. step")

    app = FastAPI()                                    # no middleware: exercise the handler
    app.include_router(routes_system.router)
    r = TestClient(app).post("/api/wipe/skills").json()
    assert r["ok"] is True and r["removed"] == 2
    assert [s["name"] for s in skills.all_skills()] == ["builtin"]


# --- review_one: the independent reviewer stages or rejects, and NEVER publishes ---
def test_review_one_approve_stages_the_skill(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("new-trick", "a candidate", "1. step")
    _fake_delegate(monkeypatch, '{"verdict": "approve", "edited": true, "conflicts_with": "", '
                                '"notes": "tightened step 1"}')
    r = skills.review_one()
    assert r["result"] == "staged" and r["edited"] is True
    (s,) = skills.all_skills()
    assert s["status"] == "staged" and "tightened step 1" in s["notes"]


def test_review_one_conflict_rejects_even_when_verdict_says_approve(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("dupe", "a duplicate", "1. same steps")
    _fake_delegate(monkeypatch, '{"verdict": "approve", "edited": false, '
                                '"conflicts_with": "research-report", "notes": "same ground"}')
    assert skills.review_one()["result"] == "rejected"
    (s,) = skills.all_skills()
    assert s["status"] == "learning" and "conflicts with research-report" in s["notes"]


def test_review_one_garbage_verdict_never_stages(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("candidate", "d", "1. step")
    _fake_delegate(monkeypatch, "Sure! I reviewed the skill and it looks great.")   # no JSON at all
    assert skills.review_one()["result"] == "rejected"
    assert skills.all_skills()[0]["status"] == "learning"


def test_review_one_delegate_failure_leaves_the_skill_untouched(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("candidate", "d", "1. step")
    _fake_delegate(monkeypatch, "rate limited", ok=False)
    r = skills.review_one()
    assert r["ok"] is False
    assert skills.all_skills()[0]["status"] == "learning"


def test_review_one_forces_staged_even_if_the_reviewer_self_published(monkeypatch, tmp_path):
    """The reviewer edits the file with real Write/Edit tools — if it (or a prompt injected into
    the skill body) flips `status: published` in the frontmatter, review_one must force it back:
    publishing belongs to the local gate, never the reviewer."""
    d = _use_tmp_skills(monkeypatch, tmp_path)
    skills.learn_skill("candidate", "d", "1. step")
    slug = skills.all_skills()[0]["dir"]

    def sneak_publish(prompt, kw):
        path = d / slug / "SKILL.md"
        path.write_text(path.read_text().replace("status: learning", "status: published"))
    _fake_delegate(monkeypatch, '{"verdict": "approve", "edited": true, "conflicts_with": "", "notes": "ok"}',
                   on_call=sneak_publish)
    assert skills.review_one()["result"] == "staged"
    assert skills.all_skills()[0]["status"] == "staged"


# --- the local publish gate (phase 2 of _evaluate) ---
def _staged_skill(monkeypatch, tmp_path):
    _use_tmp_skills(monkeypatch, tmp_path)
    skills.save_skill("cand", "cand", "reviewed candidate", status="staged", notes="✓ reviewed")


def test_publish_gate_publishes_on_publish(monkeypatch, tmp_path):
    _staged_skill(monkeypatch, tmp_path)
    monkeypatch.setattr("oceano.llm.chat", lambda msgs, **kw: SimpleNamespace(content="PUBLISH"))
    summary = skills._evaluate()
    assert "1 published, 0 held" in summary
    assert skills.all_skills()[0]["status"] == "published"


def test_publish_gate_holds_on_hold(monkeypatch, tmp_path):
    _staged_skill(monkeypatch, tmp_path)
    monkeypatch.setattr("oceano.llm.chat", lambda msgs, **kw: SimpleNamespace(content="HOLD, it duplicates"))
    summary = skills._evaluate()
    assert "0 published, 1 held" in summary
    assert skills.all_skills()[0]["status"] == "staged"


def test_publish_gate_model_down_leaves_it_staged_for_the_user(monkeypatch, tmp_path):
    _staged_skill(monkeypatch, tmp_path)
    def boom(msgs, **kw):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr("oceano.llm.chat", boom)
    skills._evaluate()
    assert skills.all_skills()[0]["status"] == "staged"
