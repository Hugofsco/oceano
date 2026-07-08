"""oceano.secretcrypto: the encrypt/decrypt round-trip, the legacy-plaintext passthrough
that makes migration lazy/opportunistic (no bulk rewrite script), and key persistence."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import secretcrypto  # noqa: E402 - after the sys.path bootstrap


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    """Every test gets its own key file and a cleared cache, so tests never touch the
    real oceano.env.key and never see another test's cached key."""
    monkeypatch.setattr(secretcrypto, "_KEY_FILE", tmp_path / "oceano.env.key")
    monkeypatch.setattr(secretcrypto, "_key_cache", None)
    monkeypatch.delenv("OCEANO_DATA_KEY", raising=False)
    yield
    monkeypatch.setattr(secretcrypto, "_key_cache", None)


def test_round_trip():
    ct = secretcrypto.encrypt("hunter2")
    assert ct != "hunter2"
    assert ct.startswith("enc:v1:")
    assert secretcrypto.decrypt(ct) == "hunter2"


def test_empty_string_passes_through_unwrapped():
    assert secretcrypto.encrypt("") == ""
    assert secretcrypto.decrypt("") == ""


def test_legacy_plaintext_decrypts_as_passthrough():
    assert secretcrypto.decrypt("an old plaintext password") == "an old plaintext password"


def test_encrypt_is_idempotent_on_already_wrapped_value():
    ct = secretcrypto.encrypt("hunter2")
    assert secretcrypto.encrypt(ct) == ct


def test_decrypt_of_corrupt_ciphertext_returns_raw_value_not_raise():
    bad = "enc:v1:not-actually-a-fernet-token"
    assert secretcrypto.decrypt(bad) == bad


def test_key_persists_across_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(secretcrypto, "_KEY_FILE", tmp_path / "oceano.env.key")
    monkeypatch.setattr(secretcrypto, "_key_cache", None)
    k1 = secretcrypto.load_key()
    monkeypatch.setattr(secretcrypto, "_key_cache", None)   # simulate a fresh process
    k2 = secretcrypto.load_key()
    assert k1 == k2
    key_path = secretcrypto._KEY_FILE
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_env_var_key_takes_precedence(monkeypatch):
    from cryptography.fernet import Fernet
    forced = Fernet.generate_key().decode()
    monkeypatch.setenv("OCEANO_DATA_KEY", forced)
    monkeypatch.setattr(secretcrypto, "_key_cache", None)
    assert secretcrypto.load_key() == forced.encode()
