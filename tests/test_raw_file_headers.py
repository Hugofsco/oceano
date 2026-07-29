"""/api/raw must never let a workspace file execute in the app's own origin.

The workspace is NOT trusted input: the agent writes files there on instruction from web pages,
emails, and documents it reads. /api/raw used to return a bare FileResponse, so a .html or .svg was
served same-origin with its real content-type and executed with the session cookie. Same-origin
script doesn't need to READ an HttpOnly cookie to use it — it can drive /api/chat or open the
/api/terminal/ws PTY, whose gates (same-origin + cookie) in-origin script satisfies by definition.

Pinned here: scripting types are force-downloaded, passive media stays inline (so chat images keep
working), and nosniff + `CSP: sandbox` are present either way.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from oceano.web import routes_files  # noqa: E402

# 1x1 transparent PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a4944"
    "4154789c63000100000500010d0a2db40000000049454e44ae426082")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(routes_files.config, "WORKSPACE", tmp_path)
    # _wresolve fences against config.WORKSPACE captured in oceano.web.state
    from oceano.web import state
    monkeypatch.setattr(state.config, "WORKSPACE", tmp_path)
    app = FastAPI()
    app.include_router(routes_files.router)
    return TestClient(app)


@pytest.mark.parametrize("name, body", [
    ("evil.html", b"<script>fetch('/api/chat')</script>"),
    ("evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'></svg>"),
    ("evil.xhtml", b"<html xmlns='http://www.w3.org/1999/xhtml'></html>"),
    ("evil.xml", b"<root/>"),
    ("script.js", b"alert(1)"),
])
def test_scripting_types_are_force_downloaded(client, tmp_path, name, body):
    (tmp_path / name).write_bytes(body)
    r = client.get("/api/raw", params={"path": name})
    assert r.status_code == 200
    # never served as a renderable document
    assert r.headers["content-type"].startswith("application/octet-stream"), r.headers["content-type"]
    assert r.headers["content-disposition"].startswith("attachment"), r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.content == body           # the bytes themselves are unchanged


def test_passive_images_stay_inline_so_chat_images_keep_working(client, tmp_path):
    (tmp_path / "shot.png").write_bytes(_PNG)
    r = client.get("/api/raw", params={"path": "shot.png"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["content-disposition"].startswith("inline")
    assert r.content == _PNG
    # hardening still applied to the inline branch
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_plain_text_stays_inline(client, tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    r = client.get("/api/raw", params={"path": "notes.txt"})
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"].startswith("inline")


def test_raw_still_refuses_to_escape_the_workspace(client, tmp_path):
    (tmp_path.parent / "outside.txt").write_text("secret")
    assert client.get("/api/raw", params={"path": "../outside.txt"}).status_code == 400


def test_filename_cannot_break_out_of_the_content_disposition_header(client, tmp_path):
    # A quote/newline in the name must not let an agent-chosen filename inject a second header.
    (tmp_path / 'we"ird.html').write_bytes(b"x")
    r = client.get("/api/raw", params={"path": 'we"ird.html'})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.count('"') == 2 and "\n" not in cd, cd
