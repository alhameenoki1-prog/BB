"""One account, several campaigns: who may send next.

A user can run an outreach campaign, a reply campaign and a mailbox scan
on the same account at once, each in its own process (an outreach
campaign is its own Harvey worker; replies and scans run in the mail
command center's worker). The account still has one daily limit, and if
one of them used it up the account would be blocked for all of them. So
every process that sends for a user shares this small SQLite file
(``data/sendlog.db`` in the user's workspace) and asks it before each
send:

* **members**: the campaigns working on each account right now (each
  checks in as it works; a paused, stopped or finished one leaves at
  once, and one that has not checked in for six hours is taken to have
  stopped);
* **limits**: each account's daily limit, set by the site from the
  account's own cap or what its provider allows;
* **sends**: every send of the last 24 hours, per account and campaign.

The rule: each active campaign on an account is guaranteed an equal part
of its daily limit. It may send beyond its part only from what nobody
else still has a claim on, so a quiet reply campaign keeps room for the
replies that come in later while a busy outreach campaign uses what is
left. A send is reserved before it goes (one transaction, so two
processes cannot both take the last slot) and released if it fails.

Replies are not limited: an answer to someone who wrote is never held
back by any limit, and does not use up the account's limit for first
emails. It is still written down (its feature starts with ``free:``) so
the Campaigns page can show it, and a campaign that only answers people
(a reply campaign, a reply run) holds no part of the limit.

The same file holds **suppressed** addresses: someone who asks one
campaign never to be contacted again is skipped by all of them.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

WINDOW_SECONDS = 24 * 3600     # providers count a rolling day
ACTIVE_SECONDS = 6 * 3600       # a member that has not checked in for this long has stopped (a paused or finished one leaves at once)
FREE = "free:"                  # a send no limit applies to (an answer): recorded, never counted
ANSWERS_ONLY = ("reply", "reply-run")   # members that only answer people: they take no part of the limit

SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    campaign TEXT NOT NULL,
    feature TEXT DEFAULT '',
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_account ON sends(account, sent_at);
CREATE TABLE IF NOT EXISTS members (
    account TEXT NOT NULL,
    campaign TEXT NOT NULL,
    feature TEXT DEFAULT '',
    label TEXT DEFAULT '',
    active_until REAL NOT NULL,
    PRIMARY KEY (account, campaign)
);
CREATE TABLE IF NOT EXISTS limits (
    account TEXT PRIMARY KEY,
    daily INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS suppressed (
    email TEXT PRIMARY KEY,
    reason TEXT DEFAULT '',
    source TEXT DEFAULT '',
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lifted (
    email TEXT PRIMARY KEY,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS answered (
    message_id TEXT PRIMARY KEY,
    account TEXT DEFAULT '',
    campaign TEXT DEFAULT '',
    at REAL NOT NULL
);
"""


def _key(account: str) -> str:
    return (account or "").strip().lower()


def message_key(message_id: str) -> str:
    """A Message-ID as both sides store it: no angle brackets, lower case."""
    return (message_id or "").strip().strip("<>").strip().lower()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._ready = False

    # ── plumbing ──

    def _con(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        con.execute("PRAGMA busy_timeout=15000")
        if not self._ready:
            # Several processes may open a new ledger at once; switching to
            # WAL needs a moment alone, so a busy answer is simply retried.
            for attempt in range(20):
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.executescript(SCHEMA)
                    break
                except sqlite3.OperationalError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            self._ready = True
        return con

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    # ── limits and members ──

    def set_limit(self, account: str, daily: int, now: float | None = None):
        con = self._con()
        try:
            con.execute("INSERT INTO limits(account, daily, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(account) DO UPDATE SET daily = excluded.daily, updated_at = excluded.updated_at",
                        (_key(account), max(0, int(daily or 0)), self._now(now)))
        finally:
            con.close()

    def limit(self, account: str) -> int:
        con = self._con()
        try:
            row = con.execute("SELECT daily FROM limits WHERE account = ?", (_key(account),)).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()

    def join(self, account: str, campaign: str, feature: str = "", label: str = "", now: float | None = None,
             con: sqlite3.Connection | None = None):
        """Say "I am working on this account" (again). Call it on every pass."""
        own = con is None
        con = con or self._con()
        try:
            t = self._now(now)
            con.execute("INSERT INTO members(account, campaign, feature, label, active_until) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(account, campaign) DO UPDATE SET feature = excluded.feature, "
                        "label = CASE WHEN excluded.label != '' THEN excluded.label ELSE members.label END, "
                        "active_until = excluded.active_until",
                        (_key(account), campaign, feature, label, t + ACTIVE_SECONDS))
        finally:
            if own:
                con.close()

    def refresh(self, account: str, campaign: str, now: float | None = None) -> bool:
        """Still here, nothing sent: extend a membership that is still
        active, never start one (a campaign that stopped checking in because
        it cannot send must not hold a part again). True when extended."""
        con = self._con()
        try:
            t = self._now(now)
            return con.execute("UPDATE members SET active_until = ? WHERE account = ? AND campaign = ? AND active_until > ?",
                               (t + ACTIVE_SECONDS, _key(account), campaign, t)).rowcount > 0
        finally:
            con.close()

    def leave(self, account: str, campaign: str):
        """Paused, stopped or finished: the share goes back to the others now."""
        con = self._con()
        try:
            con.execute("DELETE FROM members WHERE account = ? AND campaign = ?", (_key(account), campaign))
        finally:
            con.close()

    def leave_everywhere(self, campaign: str):
        con = self._con()
        try:
            con.execute("DELETE FROM members WHERE campaign = ?", (campaign,))
        finally:
            con.close()

    def members(self, account: str, now: float | None = None, con: sqlite3.Connection | None = None) -> list[dict]:
        own = con is None
        con = con or self._con()
        try:
            rows = con.execute("SELECT campaign, feature, label, active_until FROM members WHERE account = ? AND active_until > ? "
                               "ORDER BY campaign", (_key(account), self._now(now))).fetchall()
            return [{"campaign": r[0], "feature": r[1], "label": r[2], "active_until": r[3]} for r in rows]
        finally:
            if own:
                con.close()

    def has_feature(self, account: str, feature: str, now: float | None = None) -> bool:
        return any(m["feature"] == feature for m in self.members(account, now))

    # ── the split ──

    def _used(self, con, account: str, since: float, campaign: str | None = None, free: bool = False) -> int:
        """Sends in the window that count against the limit (``free``: the
        answers instead, which do not)."""
        which = "feature LIKE 'free:%'" if free else "feature NOT LIKE 'free:%'"
        if campaign is None:
            row = con.execute(f"SELECT COUNT(*) FROM sends WHERE account = ? AND sent_at > ? AND {which}",
                              (account, since)).fetchone()
        else:
            row = con.execute(f"SELECT COUNT(*) FROM sends WHERE account = ? AND campaign = ? AND sent_at > ? AND {which}",
                              (account, campaign, since)).fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _sharing(members: list[dict]) -> list[dict]:
        """The members that take a part of the limit (not those that only answer)."""
        return [m for m in members if m.get("feature") not in ANSWERS_ONLY]

    def _picture(self, con, account: str, campaign: str, now: float, priority: bool = False) -> dict:
        since = now - WINDOW_SECONDS
        row = con.execute("SELECT daily FROM limits WHERE account = ?", (account,)).fetchone()
        limit = int(row[0]) if row else 0
        members = self._sharing(self.members(account, now, con=con))
        others = [m for m in members if m["campaign"] != campaign]
        total = self._used(con, account, since)
        mine = self._used(con, account, since, campaign)
        n = len(others) + 1
        # An equal part each; a limit too small to give everyone one has no
        # parts at all (first come, first served), rather than parts that
        # add up to more than the limit and block everyone.
        share = limit // n if limit else 0
        held = 0 if priority or not limit or not share else \
            sum(max(0, share - self._used(con, account, since, m["campaign"])) for m in others)
        if not limit:
            room = 10 ** 9            # no limit known: nothing to split
        else:
            room = max(0, limit - total - held)
        if room > 0:
            reason = ""
        elif limit and total >= limit:
            reason = f"the account's daily limit of {limit} is reached ({total} sent in the last 24 hours)"
        else:
            names = ", ".join(m["label"] or m["feature"] or m["campaign"] for m in others)
            reason = (f"this campaign's share of the account's daily limit is used ({mine} of {share}); the rest of the "
                      f"{limit} is held for the {len(others)} other campaign{'s' if len(others) != 1 else ''} on it ({names})")
        return {"ok": room > 0, "room": room if limit else 0, "limited": bool(limit), "limit": limit, "share": share,
                "used": mine, "total": total, "others": len(others), "reason": reason}

    def allowance(self, account: str, campaign: str, feature: str = "", label: str = "",
                  now: float | None = None, priority: bool = False) -> dict:
        """Where this campaign stands on this account (and a check-in).
        {ok, room, limited, limit, share, used, total, others, reason}."""
        t, account = self._now(now), _key(account)
        con = self._con()
        try:
            if feature or label:
                self.join(account, campaign, feature, label, now=t, con=con)
            return self._picture(con, account, campaign, t, priority)
        finally:
            con.close()

    def reserve(self, account: str, campaign: str, feature: str = "", label: str = "",
                now: float | None = None, priority: bool = False, kind: str = "",
                free: bool = False) -> tuple[int | None, dict]:
        """Take one send slot, or say why not. (slot id or None, picture).
        ``priority`` (a person pressing Send) is held only to the account's
        own limit, not to the campaigns' parts. ``free`` (an answer to
        someone who wrote) is never held back and does not count."""
        t, account = self._now(now), _key(account)
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE")
            if not priority:
                self.join(account, campaign, feature, label, now=t, con=con)
            pic = self._picture(con, account, campaign, t, priority)
            if free:
                pic = {**pic, "ok": True, "free": True, "reason": ""}
            if not pic["ok"]:
                con.execute("ROLLBACK")
                return None, pic
            cur = con.execute("INSERT INTO sends(account, campaign, feature, sent_at) VALUES (?, ?, ?, ?)",
                              (account, campaign, (FREE if free else "") + (kind or feature), t))
            con.execute("DELETE FROM sends WHERE sent_at < ?", (t - 2 * WINDOW_SECONDS,))
            con.execute("COMMIT")
            return cur.lastrowid, pic
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    def release(self, slot: int | None):
        """The send did not go: give the slot back."""
        if not slot:
            return
        con = self._con()
        try:
            con.execute("DELETE FROM sends WHERE id = ?", (slot,))
        finally:
            con.close()

    def summary(self, account: str, now: float | None = None) -> dict:
        """For the Campaigns page: the account's limit, what went in the last
        day, and each active member's part and use."""
        t, account = self._now(now), _key(account)
        con = self._con()
        try:
            since = t - WINDOW_SECONDS
            row = con.execute("SELECT daily FROM limits WHERE account = ?", (account,)).fetchone()
            limit = int(row[0]) if row else 0
            members = self.members(account, t, con=con)
            share = limit // max(1, len(self._sharing(members))) if limit else 0
            return {"limit": limit, "total": self._used(con, account, since), "share": share,
                    "answers": self._used(con, account, since, free=True),
                    "members": [{**m, "used": self._used(con, account, since, m["campaign"]),
                                 "answers": self._used(con, account, since, m["campaign"], free=True),
                                 "answers_only": m.get("feature") in ANSWERS_ONLY} for m in members]}
        finally:
            con.close()

    def used_by(self, campaign: str, now: float | None = None) -> int:
        """Every send of this campaign in the window, answers included."""
        con = self._con()
        try:
            row = con.execute("SELECT COUNT(*) FROM sends WHERE campaign = ? AND sent_at > ?",
                              (campaign, self._now(now) - WINDOW_SECONDS)).fetchone()
            return int(row[0] or 0)
        finally:
            con.close()

    # ── nobody writes to them again ──

    def suppress(self, email: str, reason: str = "", source: str = "", now: float | None = None):
        email = (email or "").strip().lower()
        if "@" not in email:
            return
        con = self._con()
        try:
            if source == "backfill":
                # Copied from an old record: one a person lifted stays lifted.
                if con.execute("SELECT 1 FROM lifted WHERE email = ?", (email,)).fetchone():
                    return
            else:
                con.execute("DELETE FROM lifted WHERE email = ?", (email,))   # they asked again: it holds again
            con.execute("INSERT OR IGNORE INTO suppressed(email, reason, source, at) VALUES (?, ?, ?, ?)",
                        (email, reason[:200], source[:80], self._now(now)))
        finally:
            con.close()

    def unsuppress(self, email: str):
        """A person took this address off an opt-out list on purpose: it is
        no longer held back, and old records do not put it back."""
        email = (email or "").strip().lower()
        con = self._con()
        try:
            con.execute("DELETE FROM suppressed WHERE email = ?", (email,))
            con.execute("INSERT OR REPLACE INTO lifted(email, at) VALUES (?, ?)", (email, time.time()))
        finally:
            con.close()

    def forget(self, source: str, reason: str) -> int:
        """Take out the rows one source put in for one reason."""
        con = self._con()
        try:
            return con.execute("DELETE FROM suppressed WHERE source = ? AND reason = ?", (source, reason)).rowcount
        finally:
            con.close()

    def is_suppressed(self, email: str) -> bool:
        con = self._con()
        try:
            return con.execute("SELECT 1 FROM suppressed WHERE email = ?", ((email or "").strip().lower(),)).fetchone() is not None
        finally:
            con.close()

    def suppressed_among(self, emails: list[str]) -> set[str]:
        wanted = sorted({(e or "").strip().lower() for e in emails if e})
        out: set[str] = set()
        con = self._con()
        try:
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                marks = ",".join("?" for _ in chunk)
                out |= {r[0] for r in con.execute(f"SELECT email FROM suppressed WHERE email IN ({marks})", chunk)}
            return out
        finally:
            con.close()

    # ── who answered which message ──

    def mark_answered(self, account: str, campaign: str, message_ids: list[str], now: float | None = None):
        """These inbound messages have been answered (by ``campaign``), so
        no other campaign answers them again, even one that was stopped
        while they came in and reads them when it starts."""
        keys = sorted({message_key(m) for m in message_ids or [] if message_key(m)})
        if not keys:
            return
        con = self._con()
        try:
            t = self._now(now)
            con.executemany("INSERT OR IGNORE INTO answered(message_id, account, campaign, at) VALUES (?, ?, ?, ?)",
                            [(k, _key(account), campaign, t) for k in keys])
            con.execute("DELETE FROM answered WHERE at < ?", (t - 365 * 86400,))
        finally:
            con.close()

    def answered_by(self, message_id: str) -> str | None:
        """The campaign that answered this inbound message, or None."""
        key = message_key(message_id)
        if not key:
            return None
        con = self._con()
        try:
            row = con.execute("SELECT campaign FROM answered WHERE message_id = ?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()


# ── how a Harvey worker finds its place in the ledger ──

ENV_PATH = "HARVEY_LEDGER"
ENV_ACCOUNT = "HARVEY_LEDGER_ACCOUNT"
ENV_CAMPAIGN = "HARVEY_LEDGER_CAMPAIGN"
ENV_LABEL = "HARVEY_LEDGER_LABEL"


def from_env(env: dict | None = None) -> tuple[Ledger | None, str, str, str]:
    """(ledger, account, campaign, label) when the site started this worker
    as one of several campaigns; (None, "", "", "") for a worker on its own."""
    env = os.environ if env is None else env
    path = (env.get(ENV_PATH) or "").strip()
    account = (env.get(ENV_ACCOUNT) or "").strip().lower()
    if not path or not account:
        return None, "", "", ""
    return Ledger(path), account, (env.get(ENV_CAMPAIGN) or "main").strip(), (env.get(ENV_LABEL) or "").strip()
