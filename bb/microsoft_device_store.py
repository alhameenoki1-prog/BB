"""Short-lived server-only Microsoft device grants in each BB workspace."""
import secrets
import sqlite3
import time
from pathlib import Path

from bb.mail import crypto

DDL = """
CREATE TABLE IF NOT EXISTS device_flows (
  id TEXT PRIMARY KEY,
  code_ciphertext TEXT NOT NULL,
  client_id TEXT NOT NULL,
  user_code TEXT NOT NULL,
  verify_uri TEXT NOT NULL,
  interval_seconds INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  last_poll_at INTEGER NOT NULL DEFAULT 0
);
"""


def _db(user_data_dir):
    path = Path(user_data_dir) / "microsoft_device_flows.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(DDL)
    return con


def save(user_data_dir, secret, grant, *, now=None):
    stamp = int(now or time.time())
    ident = secrets.token_urlsafe(18)
    with _db(user_data_dir) as con:
        con.execute("DELETE FROM device_flows WHERE expires_at <= ?", (stamp,))
        con.execute(
            "INSERT INTO device_flows(id, code_ciphertext, client_id, user_code, verify_uri, "
            "interval_seconds, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (ident, crypto.seal(secret, grant["device_code"]),
             grant["client_id"], grant["user_code"], grant["verification_uri"],
             int(grant["interval"]), stamp, stamp + int(grant["expires_in"])),
        )
    return ident


def read(user_data_dir, ident, *, now=None):
    with _db(user_data_dir) as con:
        r = con.execute("SELECT * FROM device_flows WHERE id=? AND expires_at>?",
                        (ident, int(now or time.time()))).fetchone()
        return dict(r) if r else None


def claim_poll(user_data_dir, ident, *, now=None):
    """Database-side throttle: only one Microsoft token poll per interval."""
    stamp = int(now or time.time())
    with _db(user_data_dir) as con:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE device_flows SET last_poll_at=? WHERE id=? AND expires_at>? "
            "AND last_poll_at <= ? - interval_seconds",
            (stamp, ident, stamp, stamp),
        )
        if cur.rowcount != 1:
            return None
        row = con.execute("SELECT * FROM device_flows WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None


def slow_down(user_data_dir, ident):
    with _db(user_data_dir) as con:
        con.execute("UPDATE device_flows SET interval_seconds = MIN(interval_seconds + 5, 60) WHERE id=?", (ident,))


def remove(user_data_dir, ident):
    with _db(user_data_dir) as con:
        con.execute("DELETE FROM device_flows WHERE id=?", (ident,))
