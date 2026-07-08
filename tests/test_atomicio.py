"""atomicio.write_text() already ends up at 0600 today as an implicit side effect of
tempfile.mkstemp() + os.replace() — this pins that as an explicit, tested guarantee (the
`mode=` param), and covers the new `secure()` helper used by the sqlite/log-file call sites
that bypass write_text() entirely.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import atomicio  # noqa: E402 - after the sys.path bootstrap


def test_write_text_creates_file_at_0600_by_default(tmp_path):
    p = tmp_path / "store.json"
    atomicio.write_text(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}'
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_write_text_honors_custom_mode(tmp_path):
    p = tmp_path / "store.json"
    atomicio.write_text(p, "x", mode=0o644)
    assert oct(p.stat().st_mode)[-3:] == "644"


def test_write_text_overwrite_still_ends_up_at_requested_mode(tmp_path):
    p = tmp_path / "store.json"
    p.write_text("old")
    p.chmod(0o644)                       # simulate a pre-existing looser-mode file
    atomicio.write_text(p, "new")
    assert p.read_text() == "new"
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_secure_tightens_an_existing_file(tmp_path):
    p = tmp_path / "thing.db"
    p.write_text("data")
    p.chmod(0o644)
    atomicio.secure(p)
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_secure_is_best_effort_on_missing_file(tmp_path):
    p = tmp_path / "nope.db"
    atomicio.secure(p)   # must not raise
