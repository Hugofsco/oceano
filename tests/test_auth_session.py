"""Session-cookie lifecycle: a login cookie is an HMAC over the username signed with a
per-install secret (state._make_token). The two properties that matter for revocation:

  1. changing the password rotates that secret, so every OTHER outstanding cookie dies — the
     instinctive "I've been compromised, change my password" actually evicts a stolen cookie;
  2. logout rotates it too, so a logged-out (or copied-off-disk) cookie can't be replayed.

Both used to be false: the secret was never rotated, so a 30-day cookie survived a password
change and a logout. These tests pin the fix.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano.web import routes_auth, state  # noqa: E402
from oceano.web.server import _require_auth  # noqa: E402


SEED_PW = "seeded-first-boot-pw"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STORE", tmp_path / "web.json")
    # The store is seeded with a RANDOM password now, so pin it for the test. The 0600 copy
    # follows STORE automatically (state._initial_pw_file), so tmp_path gets it, not data/.
    monkeypatch.setenv("OCEANO_INITIAL_PASSWORD", SEED_PW)
    routes_auth._LOGIN_FAILS.clear()


def _app():
    app = FastAPI()
    app.middleware("http")(_require_auth)
    app.include_router(routes_auth.router)
    return app


def _login(client, user="admin", pw=SEED_PW):
    r = client.post("/api/login", json={"user": user, "password": pw})
    assert r.status_code == 200 and r.json().get("ok")
    return r.cookies.get(state.SESSION_COOKIE)


def test_password_change_revokes_other_outstanding_cookies():
    client = TestClient(_app())
    stolen = _login(client)                                    # a cookie captured by an attacker
    assert client.get("/api/me", cookies={state.SESSION_COOKIE: stolen}).status_code == 200

    # the real user changes the password (from a different session — its own cookie is re-issued)
    r = client.post("/api/account",
                    json={"current_password": SEED_PW, "new_password": "a-strong-passphrase"})
    assert r.status_code == 200

    # the stolen cookie no longer authenticates — the signing secret rotated out from under it
    assert client.get("/api/me", cookies={state.SESSION_COOKIE: stolen}).status_code == 401


def test_logout_revokes_the_session_server_side():
    client = TestClient(_app())
    _login(client)
    # Move off the seeded first-boot password first — the middleware blocks other state-changing POSTs
    # (like logout) while the default password stands, so this both satisfies that gate and gives
    # us a fresh post-rotation cookie to test logout revocation against.
    assert client.post("/api/account",
                       json={"current_password": SEED_PW, "new_password": "a-strong-passphrase"}).status_code == 200
    cookie = _login(client, pw="a-strong-passphrase")
    assert client.get("/api/me", cookies={state.SESSION_COOKIE: cookie}).status_code == 200

    assert client.post("/api/logout").status_code == 200
    # even replaying the exact cookie (as if copied off the wire) fails after logout
    assert client.get("/api/me", cookies={state.SESSION_COOKIE: cookie}).status_code == 401
