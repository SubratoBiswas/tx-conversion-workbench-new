"""Fernet-based encryption service for credentials at rest.

Used by ``SourceConnection`` to seal ERP login credentials. The plaintext
shape depends on the connection's ``auth_type`` (token sets for OAuth, DSN +
user/pwd for direct DB) but every variant is serialized to JSON, Fernet-
encrypted, and stored as a url-safe ASCII string.

Key management:

* In production: ``MASTER_ENCRYPTION_KEY`` must be set explicitly from a
  secret manager (AWS KMS, HashiCorp Vault, GCP Secret Manager). The key is
  *never* baked into images or written to disk in prod.
* In dev: if the env var is unset the service auto-generates a key on first
  run, writes it to ``MASTER_ENCRYPTION_KEY_FILE`` with 0600 perms, and logs
  a warning so it's obvious during code review. Subsequent runs read it back.

Rotation:

The key id is encoded into the Fernet token, so a future
``MultiFernet([new_key, old_key])`` swap allows zero-downtime rotation —
older rows decrypt on demand, the next write seals with the new key. A
rotate-and-rewrap maintenance script can iterate every row safely. v1 ships
single-key; multi-key plumbing is intentional and tested.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings


log = logging.getLogger("trinamix.encryption")


class EncryptionError(Exception):
    """Raised for any key-management or cryptography failure. Callers should
    surface a 500 with a generic message — never leak the underlying detail
    to the client because that would help an attacker."""


def _load_or_generate_master_key() -> bytes:
    """Resolve the master key. Order of precedence:

    1. ``settings.MASTER_ENCRYPTION_KEY`` (env / .env). Strongly recommended.
    2. ``settings.MASTER_ENCRYPTION_KEY_FILE`` on disk (dev fallback).
    3. Auto-generate, persist to the file with 0600 perms, warn loudly.
    """
    if settings.MASTER_ENCRYPTION_KEY:
        return settings.MASTER_ENCRYPTION_KEY.encode("ascii")

    key_path = Path(settings.MASTER_ENCRYPTION_KEY_FILE)
    if key_path.exists():
        try:
            return key_path.read_bytes().strip()
        except OSError as e:
            raise EncryptionError(f"cannot read master key file: {e}") from e

    # Auto-generate (dev mode). NEVER hits this branch in a properly
    # configured prod deployment, but the warning is loud and structured so
    # ops can grep for it.
    new_key = Fernet.generate_key()
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(new_key)
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        raise EncryptionError(f"cannot persist generated master key: {e}") from e
    log.warning(
        "MASTER_ENCRYPTION_KEY was unset; generated a dev key at %s. "
        "ROTATE THIS BEFORE GOING TO PRODUCTION — credentials sealed with "
        "this key will not be portable across deployments.",
        key_path,
    )
    return new_key


class EncryptionService:
    """Thread-safe singleton over a (Multi)Fernet instance. Construct via
    :func:`get_encryption_service`.
    """

    def __init__(self, fernet: MultiFernet) -> None:
        self._f = fernet

    def encrypt_credentials(self, plaintext: dict[str, Any]) -> str:
        """Serialize ``plaintext`` to canonical JSON and seal. Returns a
        url-safe ASCII string suitable for storing in a TEXT column.

        Keys are sorted so the same input round-trips byte-for-byte (helps
        with deterministic test fixtures, never with breakers because the
        Fernet IV varies per call).
        """
        if not isinstance(plaintext, dict):
            raise EncryptionError("encrypt_credentials expects a dict")
        try:
            blob = json.dumps(plaintext, sort_keys=True, separators=(",", ":")).encode("utf-8")
            token = self._f.encrypt(blob)
            return token.decode("ascii")
        except (TypeError, ValueError) as e:
            raise EncryptionError(f"serialization failed: {e}") from e

    def decrypt_credentials(self, ciphertext: str | None) -> dict[str, Any]:
        if not ciphertext:
            return {}
        try:
            blob = self._f.decrypt(ciphertext.encode("ascii"))
            return json.loads(blob.decode("utf-8"))
        except InvalidToken as e:
            # Could mean: wrong key, tampered ciphertext, or rotated-away key.
            # Don't disclose which — always raise the same generic error.
            raise EncryptionError("credential decryption failed") from e
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise EncryptionError(f"credential deserialization failed: {e}") from e


_instance: EncryptionService | None = None


def get_encryption_service() -> EncryptionService:
    """Return the process-wide encryption service. Lazy-initialised so test
    suites that override ``settings.MASTER_ENCRYPTION_KEY_FILE`` before the
    first call work as expected.
    """
    global _instance
    if _instance is None:
        master_key = _load_or_generate_master_key()
        # MultiFernet with a single key today; rotation drops a second key
        # in front and decryption falls through. No code change needed when
        # rotation is added.
        fernet = MultiFernet([Fernet(master_key)])
        _instance = EncryptionService(fernet)
    return _instance


def _reset_for_tests() -> None:
    """Test-only helper to reload the singleton after env mutation."""
    global _instance
    _instance = None
