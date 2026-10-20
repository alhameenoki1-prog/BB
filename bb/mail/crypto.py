"""Credentials at rest: AES-256-GCM with a key derived from the site's
secret key. A sealed value is ``v1:<base64 nonce||ciphertext>``; anything
else is refused rather than guessed at."""

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "v1:"


def _key(secret: str) -> bytes:
    if not secret:
        raise ValueError("a secret key is required to seal credentials")
    return hashlib.sha256(("bb-mail-credentials:" + secret).encode()).digest()


def seal(secret: str, plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key(secret)).encrypt(nonce, plaintext.encode(), b"bb-mail")
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()


def open_sealed(secret: str, sealed: str) -> str:
    if not sealed or not sealed.startswith(PREFIX):
        raise ValueError("not a sealed value")
    raw = base64.urlsafe_b64decode(sealed[len(PREFIX):].encode())
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_key(secret)).decrypt(nonce, ct, b"bb-mail").decode()


def seal_json(secret: str, data: dict) -> str:
    return seal(secret, json.dumps(data, separators=(",", ":")))


def open_json(secret: str, sealed: str) -> dict:
    data = json.loads(open_sealed(secret, sealed))
    return data if isinstance(data, dict) else {}


def mask(value: str, keep: int = 4) -> str:
    value = value or ""
    return ("•" * 8 + value[-keep:]) if len(value) > keep else ("•" * len(value))
