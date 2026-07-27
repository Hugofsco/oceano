"""Tests for the SSH keychain (oceano/hosts.py): CRUD + the auth-merge regression where
editing a host without resupplying its private key used to blank the key out.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oceano import hosts  # noqa: E402 - after the sys.path bootstrap


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hosts, "STORE", tmp_path / "hosts.json")
    monkeypatch.setattr(hosts, "KEY_DIR", tmp_path / "hosts")


def test_editing_a_host_without_resupplying_auth_keeps_the_custodied_key(tmp_path, monkeypatch):
    """Regression: the edit form always PATCHes an `auth` object (type + key_path) but never
    resends `key_file` (that's only ever set via set_key()/the key-upload endpoint). update()
    used to normalize the incoming partial auth BEFORE merging, which fills every omitted field
    with None and then overwrites the existing auth wholesale — so saving an unrelated field
    (name, policy, description) silently deleted the stored private key reference."""
    _isolate(tmp_path, monkeypatch)
    h = hosts.create("prod-web", "203.0.113.10", "deploy")
    assert hosts.set_key(h["id"], "synthetic-key-fixture\n")
    assert hosts.get(h["id"])["has_key"] is True

    # editing something unrelated, the way the UI does: resend type + key_path, no key_file
    hosts.update(h["id"], name="prod-web-2", policy="trusted", auth={"type": "key", "key_path": None})

    updated = hosts.get(h["id"])
    assert updated["name"] == "prod-web-2"
    assert updated["policy"] == "trusted"
    assert updated["has_key"] is True           # the custodied key must survive the edit
    raw = hosts._raw(h["id"])
    assert raw["auth"]["key_file"] == f"hosts/{h['id']}.key"


def test_auth_can_still_be_explicitly_replaced(tmp_path, monkeypatch):
    """The merge-preservation fix must not prevent a real replacement: supplying a new
    key_path explicitly should still take effect."""
    _isolate(tmp_path, monkeypatch)
    h = hosts.create("stage", "203.0.113.20", "deploy", auth={"type": "key", "key_path": "/old/path"})
    assert hosts._raw(h["id"])["auth"]["key_path"] == "/old/path"

    hosts.update(h["id"], auth={"type": "key", "key_path": "/new/path"})
    assert hosts._raw(h["id"])["auth"]["key_path"] == "/new/path"


def test_switching_to_password_auth_does_not_touch_unrelated_fields(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    h = hosts.create("db1", "203.0.113.30", "deploy")
    hosts.set_key(h["id"], "synthetic-key-fixture\n")

    hosts.update(h["id"], auth={"type": "password"})
    updated = hosts.get(h["id"])
    assert updated["auth_type"] == "password"
    assert updated["needs_secret"] is True      # no password stored → must be supplied to arm
