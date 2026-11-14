"""Users and sessions for the web app, in one SQLite file.

Accounts are created by an admin (there is no self-registration): the site
owner makes a login and password for each person they let in. Passwords are
hashed with scrypt; sessions are random tokens stored hashed, with an
expiry. Plain ``sqlite3`` is enough here — a handful of rows, read on each
request.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 14
ROLES = ("admin", "user")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS totp_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    encrypted_secret TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mfa_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mfa_challenges_user ON mfa_challenges(user_id);
"""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hash_password(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(candidate.split("$")[2], digest_hex)
    except (ValueError, TypeError):
        return False


def validate_new_account(email: str, password: str) -> str:
    """"" when acceptable, else the reason it isn't."""
    if not EMAIL_RE.match(email or ""):
        return "That doesn't look like an email address."
    if len(password or "") < MIN_PASSWORD:
        return f"Passwords need at least {MIN_PASSWORD} characters."
    return ""


class Accounts:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ── users ──

    def count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, email: str, password: str, role: str = "user") -> dict:
        email = (email or "").strip().lower()
        problem = validate_new_account(email, password)
        if problem:
            raise ValueError(problem)
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}.")
        user_id = secrets.token_hex(8)
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO users (id, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, email, hash_password(password), role, int(time.time())),
                )
        except sqlite3.IntegrityError:
            raise ValueError("An account with that email already exists.")
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def get_by_email(self, email: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?",
                             ((email or "").strip().lower(),)).fetchone()
            return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM users ORDER BY created_at")]

    def authenticate(self, email: str, password: str) -> dict | None:
        user = self.get_by_email(email)
        if not user or user["disabled"] or not verify_password(password, user["password_hash"]):
            return None
        with self._connect() as db:
            db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (int(time.time()), user["id"]))
        return user

    def set_password(self, user_id: str, password: str):
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"Passwords need at least {MIN_PASSWORD} characters.")
        with self._connect() as db:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM mfa_challenges WHERE user_id = ?", (user_id,))

    def set_role(self, user_id: str, role: str):
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}.")
        with self._connect() as db:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def set_disabled(self, user_id: str, disabled: bool):
        with self._connect() as db:
            db.execute("UPDATE users SET disabled = ? WHERE id = ?", (1 if disabled else 0, user_id))
            if disabled:
                db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                db.execute("DELETE FROM mfa_challenges WHERE user_id = ?", (user_id,))

    # ── TOTP security ──

    def totp_enabled(self, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT confirmed FROM totp_settings WHERE user_id=?", (user_id,)).fetchone()
            return bool(row and row["confirmed"])

    def totp_secret(self, user_id: str, key: str, *, confirmed_only=True) -> str:
        from bb.mail.crypto import open_sealed
        with self._connect() as db:
            row = db.execute(
                "SELECT encrypted_secret, confirmed, created_at FROM totp_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if not row or (confirmed_only and not row["confirmed"]):
            return ""
        if not row["confirmed"] and row["created_at"] < int(time.time()) - 600:
            return ""
        return open_sealed(key, row["encrypted_secret"])

    def begin_totp_enrollment(self, user_id: str, key: str) -> str:
        from bb.mail.crypto import seal
        from bb.totp import generate_secret
        secret = generate_secret()
        with self._connect() as db:
            changed = db.execute(
                "INSERT INTO totp_settings(user_id, encrypted_secret, confirmed, created_at) "
                "VALUES(?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET "
                "encrypted_secret=excluded.encrypted_secret, created_at=excluded.created_at "
                "WHERE totp_settings.confirmed=0",
                (user_id, seal(key, secret), int(time.time())),
            )
            if not changed.rowcount:
                raise ValueError("MFA is already enabled.")
        return secret

    def confirm_totp(self, user_id: str, key: str, code: str) -> bool:
        from bb.totp import verify
        from bb.mail.crypto import open_sealed

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT encrypted_secret FROM totp_settings WHERE user_id=? AND confirmed=0 AND created_at>=?",
                (user_id, int(time.time()) - 600),
            ).fetchone()
            if not row or not verify(open_sealed(key, row["encrypted_secret"]), code):
                return False
            db.execute("UPDATE totp_settings SET confirmed=1 WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return True

    def verify_totp(self, user_id: str, key: str, code: str) -> bool:
        from bb.totp import verify
        secret = self.totp_secret(user_id, key, confirmed_only=True)
        return bool(secret and verify(secret, code))

    def new_mfa_challenge(self, user_id: str) -> str:
        ticket = secrets.token_urlsafe(32)
        stamp = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM mfa_challenges WHERE expires_at<?", (stamp,))
            db.execute("DELETE FROM mfa_challenges WHERE user_id=?", (user_id,))
            db.execute(
                "INSERT INTO mfa_challenges(token_hash,user_id,expires_at) VALUES(?,?,?)",
                (self._hash_token(ticket), user_id, stamp + 300),
            )
        return ticket

    def complete_mfa_challenge(self, ticket: str, key: str, code: str) -> dict | None:
        if not ticket:
            return None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT user_id, attempts FROM mfa_challenges WHERE token_hash=? AND expires_at>?",
                (self._hash_token(ticket), int(time.time())),
            ).fetchone()
            if not row or row["attempts"] >= 5:
                return None
            db.execute("UPDATE mfa_challenges SET attempts=attempts+1 WHERE token_hash=?",
                       (self._hash_token(ticket),))
            user_id = row["user_id"]
            if not self.verify_totp(user_id, key, code):
                return None
            db.execute("DELETE FROM mfa_challenges WHERE token_hash=?", (self._hash_token(ticket),))
        user = self.get_user(user_id)
        return user if user and not user["disabled"] else None

    def disable_totp(self, user_id: str, key: str, code: str) -> bool:
        if not self.verify_totp(user_id, key, code):
            return False
        with self._connect() as db:
            db.execute("DELETE FROM totp_settings WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM mfa_challenges WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return True

    # ── sessions ──

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, user_id: str, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (self._hash_token(token), user_id, now, now + ttl_seconds),
            )
        return token

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at >= ? AND u.disabled = 0""",
                (self._hash_token(token), int(time.time())),
            ).fetchone()
            return dict(row) if row else None

    def delete_session(self, token: str | None):
        if not token:
            return
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_token(token),))


class LoginThrottle:
    """A small in-memory brake on password guessing: N attempts per window
    per client address."""

    def __init__(self, limit: int = 10, window_seconds: int = 600):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        return len(hits) < self.limit

    def record(self, key: str):
        self._hits.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str):
        self._hits.pop(key, None)
