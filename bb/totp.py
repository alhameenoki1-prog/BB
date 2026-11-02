"""RFC 6238-compatible TOTP implementation for BB account MFA."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def current_code(secret, *, timestamp=None, step=30):
    value = (secret or "").strip().upper().replace(" ", "")
    if not value:
        raise ValueError("TOTP secret missing")
    raw = base64.b32decode(value + "=" * (-len(value) % 8))
    count = int(timestamp if timestamp is not None else time.time()) // step
    digest = hmac.new(raw, struct.pack(">Q", count), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    num = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{num % 1000000:06d}"


def verify(secret, code, *, timestamp=None):
    code = str(code or "").replace(" ", "").strip()
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    now = int(timestamp if timestamp is not None else time.time())
    return any(hmac.compare_digest(code, current_code(secret, timestamp=now + delta * 30))
               for delta in (-1, 0, 1))


def provisioning_uri(email, secret):
    return (f"otpauth://totp/{quote('BB:' + email)}?secret={quote(secret)}"
            "&issuer=BB&algorithm=SHA1&digits=6&period=30")
