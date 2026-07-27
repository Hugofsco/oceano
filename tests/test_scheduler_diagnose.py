"""A failed scheduled task should produce a proposed fix in the Suggestions queue (via the
'improve' delegate), not just a red row — and the diagnosis must be best-effort: transient
failures file nothing, a broken delegate never raises into the drain worker."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import scheduler  # noqa: E402 - after the sys.path bootstrap


def _capture(monkeypatch, output, ok=True, enabled=True):
    filed = []
    monkeypatch.setattr("oceano.delegate.enabled", lambda: enabled)
    monkeypatch.setattr("oceano.delegate.run",
                        lambda *a, **k: {"ok": ok, "output": output, "error": "" if ok else "boom"})
    monkeypatch.setattr("oceano.suggestions.add",
                        lambda kind, title, detail="", source="": filed.append(
                            {"kind": kind, "title": title, "detail": detail, "source": source}) or 1)
    return filed


def test_failure_files_a_suggestion(monkeypatch):
    filed = _capture(monkeypatch, "root cause: context overflow. fix: pin the task to claude.")
    scheduler._diagnose_failure(7, "task:7", "do the thing", "ValueError: too big")
    assert len(filed) == 1
    s = filed[0]
    assert s["source"] == "task:7" and "task:7" in s["title"]
    assert "context overflow" in s["detail"] and "ValueError: too big" in s["detail"]


def test_transient_failures_file_nothing(monkeypatch):
    filed = _capture(monkeypatch, "TRANSIENT — ntfy.sh timed out, nothing to fix.")
    scheduler._diagnose_failure(7, "task:7", "do the thing", "Timeout: read timed out")
    assert filed == []


def test_disabled_delegation_files_nothing(monkeypatch):
    filed = _capture(monkeypatch, "diagnosis", enabled=False)
    scheduler._diagnose_failure(7, "task:7", "do the thing", "boom")
    assert filed == []


def test_delegate_error_files_nothing(monkeypatch):
    filed = _capture(monkeypatch, "", ok=False)
    scheduler._diagnose_failure(7, "task:7", "do the thing", "boom")
    assert filed == []


def test_diagnosis_never_raises(monkeypatch):
    monkeypatch.setattr("oceano.delegate.enabled", lambda: True)

    def explode(*a, **k):
        raise RuntimeError("reviewer down")

    monkeypatch.setattr("oceano.delegate.run", explode)
    scheduler._diagnose_failure(7, "task:7", "do the thing", "boom")   # must not raise
