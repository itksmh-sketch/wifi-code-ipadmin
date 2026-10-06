"""ENCRYPTION_KEY guard: the backend and worker must refuse to start without a
key, with a published key, or with an invalid one, rather than run with a key
anyone can read. Pure unit tests: no server, no DB.

The startup cases run the real import in a subprocess, because the key is read
once at import. The public-key case uses a freshly generated key whose
fingerprint is added to the refusal list, so no published key appears here.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.utils import encryption

BACKEND = Path(__file__).resolve().parents[1]


def _import_with_key(key: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ENCRYPTION_KEY"}
    if key is not None:
        env["ENCRYPTION_KEY"] = key
    return subprocess.run(
        [sys.executable, "-c", "import src.utils.encryption"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("key", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_startup_refuses_a_missing_or_empty_key(key):
    proc = _import_with_key(key)
    assert proc.returncode != 0
    assert "ENCRYPTION_KEY is not set. Refusing to start." in proc.stderr


def test_startup_refuses_an_invalid_key():
    proc = _import_with_key("not-a-fernet-key")
    assert proc.returncode != 0
    assert "ENCRYPTION_KEY is invalid." in proc.stderr


def test_startup_accepts_a_valid_private_key():
    proc = _import_with_key(Fernet.generate_key().decode())
    assert proc.returncode == 0, proc.stderr


def test_a_published_key_is_refused(monkeypatch):
    key = Fernet.generate_key().decode()
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    monkeypatch.setattr(encryption, "PUBLIC_KEY_SHA256", encryption.PUBLIC_KEY_SHA256 | {fingerprint})
    with pytest.raises(RuntimeError, match="public example key published in this repository"):
        encryption.load_fernet(key)


def test_the_old_compose_fallback_is_on_the_refusal_list():
    assert "8600d4116676677a4ce01c01f1842a52dcff23c37623d7d30914cae4d98fe451" in encryption.PUBLIC_KEY_SHA256


def test_a_valid_key_round_trips():
    f = encryption.load_fernet(Fernet.generate_key().decode())
    assert f.decrypt(f.encrypt(b"secret")) == b"secret"
