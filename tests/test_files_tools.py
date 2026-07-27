"""edit_file: an exact-substring patch over a confined workspace file. The property pinned here is
uniqueness — a `find` that matches more than once is ambiguous and must be REFUSED (not silently
applied to every occurrence, which quietly corrupts a file when the model expected one edit),
unless the caller opts into replace_all.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import turnctx  # noqa: E402
from oceano.tools import files  # noqa: E402


@pytest.fixture
def ws(tmp_path):
    with turnctx.push(workspace=tmp_path):
        yield tmp_path


def test_unique_find_is_replaced(ws):
    (ws / "a.txt").write_text("hello world\n")
    out = files.edit_file("a.txt", "world", "there")
    assert "replaced 1 occurrence" in out
    assert (ws / "a.txt").read_text() == "hello there\n"


def test_ambiguous_find_is_refused_and_file_untouched(ws):
    (ws / "a.txt").write_text("x = 1\ny = 1\nz = 1\n")
    out = files.edit_file("a.txt", " = 1", " = 2")
    assert out.startswith("ERROR") and "3 places" in out
    assert (ws / "a.txt").read_text() == "x = 1\ny = 1\nz = 1\n"   # nothing changed


def test_replace_all_opts_into_multi_edit(ws):
    (ws / "a.txt").write_text("x = 1\ny = 1\nz = 1\n")
    out = files.edit_file("a.txt", " = 1", " = 2", replace_all=True)
    assert "replaced 3 occurrence" in out
    assert (ws / "a.txt").read_text() == "x = 2\ny = 2\nz = 2\n"


def test_missing_find_reports_error(ws):
    (ws / "a.txt").write_text("hello\n")
    out = files.edit_file("a.txt", "nope", "x")
    assert out.startswith("ERROR") and "not found verbatim" in out
    assert (ws / "a.txt").read_text() == "hello\n"
