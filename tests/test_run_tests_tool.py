"""run_tests(path="."): must find a test suite scaffolded into a SUBDIRECTORY (e.g.
workspace/projects/<app-name>/, the shape a build step leaves behind), not just the given path
itself — and must prefer a project-local venv over Oceano's own. Without this, a workflow's
test-then-fix loop can spin forever: run_tests always reports "no test suite detected" (because
it only ever checked the workspace ROOT), the decision node always says "no", and the fix step
can never make a directory that's never actually checked start passing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from oceano.tools import dev  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    yield


def _fake_run(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "1 passed"
        stderr = ""

    def fake(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        calls.append({"cmd": cmd, "cwd": cwd})
        return Result()

    monkeypatch.setattr(dev.subprocess, "run", fake)
    return calls


def test_finds_a_test_suite_at_the_given_path_directly(monkeypatch):
    calls = _fake_run(monkeypatch)
    (config.WORKSPACE / "tests").mkdir()
    out = dev.run_tests(".")
    assert "no test suite" not in out
    assert "using" not in out          # no auto-detect note — it was right there
    assert calls[0]["cwd"] == str(config.WORKSPACE)
    assert calls[0]["cmd"][0] == sys.executable


def test_finds_a_project_scaffolded_one_level_down(monkeypatch):
    """The exact shape an app-builder BUILD step leaves behind: nothing at the workspace root,
    a real project under projects/<name>/."""
    calls = _fake_run(monkeypatch)
    proj = config.WORKSPACE / "projects" / "crypto-trading-bot-backtester"
    (proj / "tests").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    out = dev.run_tests(".")
    assert "no test suite detected" not in out
    assert "using projects/crypto-trading-bot-backtester/" in out
    assert calls[0]["cwd"] == str(proj)


def test_prefers_a_project_local_venv_interpreter(monkeypatch):
    calls = _fake_run(monkeypatch)
    proj = config.WORKSPACE / "projects" / "app"
    (proj / "tests").mkdir(parents=True)
    venv_py = proj / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\n")
    out = dev.run_tests(".")
    assert "using projects/app/" in out
    assert calls[0]["cmd"][0] == str(venv_py)
    assert calls[0]["cmd"][0] != sys.executable


def test_falls_back_to_oceanos_own_interpreter_without_a_project_venv(monkeypatch):
    calls = _fake_run(monkeypatch)
    proj = config.WORKSPACE / "projects" / "app"
    (proj / "tests").mkdir(parents=True)
    dev.run_tests(".")
    assert calls[0]["cmd"][0] == sys.executable


def test_multiple_candidate_projects_asks_for_an_explicit_path_instead_of_guessing(monkeypatch):
    calls = _fake_run(monkeypatch)
    for name in ("app-one", "app-two"):
        (config.WORKSPACE / "projects" / name / "tests").mkdir(parents=True)
    out = dev.run_tests(".")
    assert not calls                    # never ran anything — refused to guess
    assert "multiple test suites found" in out
    assert "app-one" in out and "app-two" in out
    assert "pass path=" in out


def test_no_test_suite_anywhere_reports_the_original_message(monkeypatch):
    calls = _fake_run(monkeypatch)
    (config.WORKSPACE / "projects" / "empty").mkdir(parents=True)
    (config.WORKSPACE / "notes.md").write_text("hi")
    out = dev.run_tests(".")
    assert not calls
    assert out == "(no test suite detected — looked for pytest, package.json, Cargo.toml, Makefile)"


def test_search_skips_dependency_and_vcs_directories(monkeypatch):
    """A stray tests/ folder inside node_modules or .venv must never get picked up as THE
    project's suite — it belongs to a dependency, not the code being built."""
    calls = _fake_run(monkeypatch)
    (config.WORKSPACE / "node_modules" / "somepkg" / "tests").mkdir(parents=True)
    (config.WORKSPACE / ".venv" / "lib" / "tests").mkdir(parents=True)
    out = dev.run_tests(".")
    assert not calls
    assert "no test suite detected" in out


def test_search_is_bounded_in_depth(monkeypatch):
    calls = _fake_run(monkeypatch)
    deep = config.WORKSPACE / "a" / "b" / "c" / "d" / "tests"
    deep.mkdir(parents=True)
    out = dev.run_tests(".")
    assert not calls
    assert "no test suite detected" in out


def test_an_explicit_path_with_its_own_suite_is_used_directly_no_search(monkeypatch):
    calls = _fake_run(monkeypatch)
    proj = config.WORKSPACE / "projects" / "app"
    (proj / "tests").mkdir(parents=True)
    out = dev.run_tests("projects/app")
    assert "using" not in out
    assert calls[0]["cwd"] == str(proj)
