"""Encrypt the page's data with a passphrase, so the published page is unreadable
without it.

- Key: PBKDF2-SHA256 (600,000 rounds) from your passphrase and a salt.
- Cipher: AES-256-GCM, which also detects a wrong passphrase or tampering.
- The browser does the same steps with its built-in Web Crypto to unlock.

Only the data is encrypted. The page's code is public anyway (same as the repo).
Strength depends on the passphrase: use 4-5 random words, not a short PIN, because
anyone can download the page and guess offline.

Used only when PAGE_PASSPHRASE is set (GitHub Actions). Local runs stay unlocked.
"""
from __future__ import annotations

import base64
import hashlib
import os

ITERATIONS = 600_000


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def salt_for(label: str) -> bytes:
    """A fixed per-site salt (doesn't need to be secret, just unique and stable,
    so a phone that remembered the key keeps working day to day)."""
    return hashlib.sha256(f"value-screener:{label}".encode()).digest()[:16]


def derive_key(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def encrypt(plaintext: str, passphrase: str, label: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = salt_for(label)
    iv = os.urandom(12)
    ct = AESGCM(derive_key(passphrase, salt)).encrypt(iv, plaintext.encode("utf-8"), None)
    return {"locked": 1, "salt": _b64(salt), "iter": ITERATIONS, "iv": _b64(iv), "ct": _b64(ct)}


def decrypt(blob: dict, passphrase: str) -> str:
    """Python-side check (used in tests); the browser has its own copy of this."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = derive_key(passphrase, base64.b64decode(blob["salt"]), blob["iter"])
    return AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None).decode()
