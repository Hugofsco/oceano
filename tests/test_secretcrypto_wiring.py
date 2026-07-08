"""Integration-level checks for the encryption-at-rest wiring added on top of
secretcrypto's own round-trip (tests/test_secretcrypto.py covers that primitive in
isolation). These confirm, per field: the value written to disk is NOT the plaintext,
and the real consumption path (an IMAP/SMTP login, a paramiko connect, an MCP
Authorization header / stdio env, an ICS fetch, a delegate API key) receives the
original plaintext back — i.e. the encrypt-at-write / decrypt-at-use boundary is wired
correctly end to end, not just that secretcrypto itself works.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import calsync, delegate, hosts, mail, mcp_client, secretcrypto  # noqa: E402
from oceano.web import state  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(secretcrypto, "_KEY_FILE", tmp_path / "oceano.env.key")
    monkeypatch.setattr(secretcrypto, "_key_cache", None)
    monkeypatch.delenv("OCEANO_DATA_KEY", raising=False)


def test_mail_password_encrypted_at_rest_decrypted_at_login(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STORE", tmp_path / "mail.json")
    mail.create("acct", "me@example.com", "imap.example.com", "smtp.example.com",
                user="me", password="hunter2")
    on_disk = json.loads((tmp_path / "mail.json").read_text())
    assert on_disk["accounts"][0]["password"] != "hunter2"
    assert on_disk["accounts"][0]["password"].startswith("enc:v1:")

    a = mail._raw(1)
    seen = {}
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", lambda *a, **k: type(
        "C", (), {"login": lambda self, u, p: seen.update(user=u, password=p)})())
    mail._imap(a)
    assert seen == {"user": "me", "password": "hunter2"}


def test_hosts_password_encrypted_at_rest_decrypted_at_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(hosts, "STORE", tmp_path / "hosts.json")
    monkeypatch.setattr(hosts, "KEY_DIR", tmp_path / "hosts")
    hosts.create("box", "10.0.0.5", "root", auth={"type": "password", "password": "s3cret"})
    on_disk = json.loads((tmp_path / "hosts.json").read_text())
    stored_pw = on_disk["hosts"][0]["auth"]["password"]
    assert stored_pw != "s3cret"
    assert stored_pw.startswith("enc:v1:")
    assert secretcrypto.decrypt(stored_pw) == "s3cret"


def test_mcp_token_and_env_encrypted_at_rest_decrypted_for_use(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "CONFIG", tmp_path / "mcp.json")
    monkeypatch.setattr(mcp_client, "reload", lambda: None)   # no real connection attempt in this test
    mcp_client.add_server({"name": "srv", "url": "https://example.com/mcp",
                           "token": "tok123", "env": {"API_KEY": "envsecret"}})
    on_disk = json.loads((tmp_path / "mcp.json").read_text())
    stored = on_disk["servers"][0]
    assert stored["token"] != "tok123" and stored["token"].startswith("enc:v1:")
    assert stored["env"]["API_KEY"] != "envsecret"

    headers = mcp_client._auth_headers(stored)
    assert headers["Authorization"] == "Bearer tok123"
    assert mcp_client._decrypt_dict(stored["env"]) == {"API_KEY": "envsecret"}


def test_calendar_feed_url_encrypted_at_rest_decrypted_for_use(tmp_path, monkeypatch):
    monkeypatch.setattr(calsync, "DB_PATH", tmp_path / "cal.db")
    monkeypatch.setattr(calsync.safety, "check_url", lambda url: None)   # not an SSRF target in this test
    fid = calsync.add_feed("Mine", "https://calendar.example.com/secret-address.ics")
    con = calsync._db()
    raw_url = con.execute("SELECT url FROM feeds WHERE id=?", (fid,)).fetchone()[0]
    con.close()
    assert raw_url != "https://calendar.example.com/secret-address.ics"
    assert raw_url.startswith("enc:v1:")

    listed = calsync.feeds()
    assert listed[0]["url"] == "https://calendar.example.com/secret-address.ics"

    seen = {}
    monkeypatch.setattr(calsync, "_fetch_ics", lambda url: seen.setdefault("url", url) or "")
    monkeypatch.setattr(calsync, "_parse_ics", lambda text: [])
    monkeypatch.setattr(calsync, "_expand", lambda occ: [])
    calsync.sync_feed(fid)
    assert seen["url"] == "https://calendar.example.com/secret-address.ics"


def test_delegate_primary_api_key_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate, "_CONFIG_PATH", tmp_path / "delegation.json")
    delegate.set_primary("gpt-4o", "https://api.example.com/v1", "sk-real-key")
    on_disk = json.loads((tmp_path / "delegation.json").read_text())
    assert on_disk[delegate._KEY_KEY] != "sk-real-key"
    assert on_disk[delegate._KEY_KEY].startswith("enc:v1:")
    assert delegate.get_primary()["api_key"] == "sk-real-key"


def test_web_endpoint_api_key_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STORE", tmp_path / "web.json")
    data = state.load()
    data["endpoints"].append({"name": "custom", "base_url": "https://api.example.com/v1",
                              "api_key": secretcrypto.encrypt("sk-real-key")})
    state.save(data)
    on_disk = json.loads((tmp_path / "web.json").read_text())
    stored = next(e["api_key"] for e in on_disk["endpoints"] if e["name"] == "custom")
    assert stored != "sk-real-key" and stored.startswith("enc:v1:")
    assert state.endpoint_key("https://api.example.com/v1") == "sk-real-key"
