"""Encryption-at-rest for the small set of genuine secrets kept under ./data (mail
app-passwords, remembered SSH credentials/passphrases, API keys, the calendar feed
URL). This is a layer ON TOP of the 0600 file permissions those stores already get
(atomicio.write_text / atomicio.secure) — not a replacement for them.

Key: OCEANO_DATA_KEY env var first (lets a deployment inject it from a vault/secret
manager). Otherwise a key is generated once and persisted to oceano.env.key, at the
repo root next to oceano.env — deliberately NOT under ./data, so a leak/copy/backup of
the data folder alone doesn't also hand over the key that unlocks it.

Values are tagged with a version prefix so decrypt() passes legacy plaintext (or an
absent/empty value) through unchanged: no bulk migration script — a field upgrades to
ciphertext the next time it's normally saved through its own edit flow.

No key-rotation tooling yet: rotating means re-encrypting every already-wrapped field,
better done as a future one-off command once fields are actually wrapped in practice.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from oceano import atomicio

_PREFIX = "enc:v1:"
_KEY_FILE = Path(__file__).resolve().parent.parent / "oceano.env.key"

_key_cache = None


def load_key():
    """Resolve the data-encryption key (env var, then the persisted key file, else
    generate + persist a new one). Cached in-process after the first call."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    env_key = os.environ.get("OCEANO_DATA_KEY", "").strip()
    if env_key:
        _key_cache = env_key.encode()
        return _key_cache
    if _KEY_FILE.exists():
        _key_cache = _KEY_FILE.read_text().strip().encode()
        return _key_cache
    key = Fernet.generate_key()
    atomicio.write_text(_KEY_FILE, key.decode())
    _key_cache = key
    return _key_cache


def _fernet():
    return Fernet(load_key())


def encrypt(plaintext):
    """"" -> "" (never wrap an empty/absent-secret sentinel — callers test these with
    plain truthiness). Already-wrapped values pass through unchanged (idempotent)."""
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        return plaintext
    return _PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt(value):
    """Legacy plaintext and empty values pass through unchanged. A value that fails to
    decrypt (wrong/rotated key, corruption) is returned as-is rather than raising — the
    caller sees an auth failure using the raw string, not an unhandled exception."""
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except (InvalidToken, ValueError):
        return value
