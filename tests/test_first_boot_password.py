"""First boot must not ship a known credential.

The web UI binds 0.0.0.0 by default (LAN / Tailscale reach) and the agent runs shell commands. With
the old 'admin'/'admin' seed, whoever reached the port first between install and the owner's first
login could sign in. The forced-change gate did NOT stop that: it confines the session to
/api/account, which is exactly the call an attacker needs to take the account over.

So: the seed password is random, and `must_change` still forces the change-password flow on first
sign-in (preserving the old UX without a guessable default).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano.web import routes_auth, state  # noqa: E402
from oceano.web.server import _require_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STORE", tmp_path / "web.json")
    monkeypatch.delenv("OCEANO_INITIAL_PASSWORD", raising=False)
    routes_auth._LOGIN_FAILS.clear()


def _app():
    app = FastAPI()
    app.middleware("http")(_require_auth)
    app.include_router(routes_auth.router)
    return app


def test_seed_password_is_random_and_not_the_old_default(tmp_path):
    a = state._auth_seed()
    b = state._auth_seed()
    # no shipped credential
    assert not state._hash_pw("admin", a["salt"]) == a["pwhash"]
    # and not a fixed value either — two seeds differ
    assert a["pwhash"] != b["pwhash"]
    assert a["must_change"] is True


def test_seeded_password_is_written_0600_and_usable_to_log_in(tmp_path):
    state.load()                                    # seeds the store
    pw_file = state._initial_pw_file()
    assert pw_file.exists(), "the generated password must be recoverable"
    assert oct(pw_file.stat().st_mode & 0o777) == "0o600"
    pw = pw_file.read_text().strip()
    assert len(pw) >= 12

    client = TestClient(_app())
    assert client.post("/api/login", json={"user": "admin", "password": "admin"}).status_code == 401
    r = client.post("/api/login", json={"user": "admin", "password": pw})
    assert r.status_code == 200 and r.json()["must_change"] is True


def test_first_boot_session_is_confined_to_the_change_password_call():
    state.load()
    pw = state._initial_pw_file().read_text().strip()
    client = TestClient(_app())
    assert client.post("/api/login", json={"user": "admin", "password": pw}).status_code == 200
    # every other authenticated API path is refused while must_change stands
    assert client.post("/api/logout").status_code == 403
    # and the change itself is allowed
    assert client.post("/api/account",
                       json={"current_password": pw, "new_password": "a-strong-passphrase"}).status_code == 200
    # gate self-clears, normal operation resumes
    assert client.post("/api/logout").status_code == 200
    assert not state._initial_pw_file().exists(), "the seeded password file must be removed once changed"
    assert state._is_default_pw(state.load()["auth"]) is False


def test_legacy_admin_installs_are_still_flagged_after_upgrade():
    # An install seeded before this change has pwhash=hash('admin') and no must_change flag; the
    # forced-change gate must still fire for it.
    salt = "00" * 16
    auth = {"user": "admin", "salt": salt, "pwhash": state._hash_pw("admin", salt), "secret": "x"}
    assert state._is_default_pw(auth) is True


def test_env_override_lets_a_deployment_inject_a_known_initial_password(monkeypatch):
    monkeypatch.setenv("OCEANO_INITIAL_PASSWORD", "from-the-vault")
    auth = state._auth_seed()
    assert state._hash_pw("from-the-vault", auth["salt"]) == auth["pwhash"]
    assert auth["must_change"] is True


def test_seeding_never_writes_into_the_real_data_dir(tmp_path, monkeypatch):
    """Regression: _initial_pw_file() must follow STORE.

    It was a module-level constant pinned to the real data/ dir, so any test that pointed STORE at a
    tmp path and then seeded (test_mcp_auth, test_chat_agent_access, test_suggestions_api,
    test_secretcrypto_wiring) silently wrote a generated password into the developer's own
    data/initial-password. Deriving it from STORE means redirecting the store redirects both.
    """
    import config
    real = config.WORKSPACE.parent / "data" / "initial-password"
    monkeypatch.setattr(state, "STORE", tmp_path / "sub" / "web.json")
    state.load()                                        # seeds, writing the password file
    assert state._initial_pw_file() == tmp_path / "sub" / "initial-password"
    assert state._initial_pw_file().exists()
    assert not real.exists(), "seeding a temp store must not touch the real data/ dir"
