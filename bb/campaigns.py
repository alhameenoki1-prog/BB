"""The outreach campaigns of one user, readable from any process.

An outreach campaign is a Harvey workspace of its own (its own
harvey.yaml, lead list, outbox and worker) bound to one account. The
main campaign is the user's original workspace; the others live under
``campaigns/<id>/`` in it. This file is the registry the site writes and
the mail worker reads: which campaigns exist, which account each sends
from, and so who owns a given lead on a given account (a reply campaign
or a scan on that account leaves that lead to the outreach campaign).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

MAIN = "main"


def registry_path(user_root: Path) -> Path:
    return Path(user_root) / "data" / "campaigns.json"


def ledger_path(user_root: Path) -> Path:
    return Path(user_root) / "data" / "sendlog.db"


def campaign_root(user_root: Path, campaign_id: str) -> Path:
    user_root = Path(user_root)
    if not campaign_id or campaign_id == MAIN:
        return user_root
    if not valid_id(campaign_id):
        raise ValueError("bad campaign id")
    return user_root / "campaigns" / campaign_id


def valid_id(campaign_id: str) -> bool:
    return campaign_id == MAIN or (campaign_id.startswith("c-") and campaign_id[2:].isalnum() and len(campaign_id) <= 24)


def new_id() -> str:
    return "c-" + secrets.token_hex(5)


def load(user_root: Path) -> list[dict]:
    try:
        data = json.loads(registry_path(user_root).read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("campaigns") if isinstance(data, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and valid_id(str(r.get("id") or ""))]


def save(user_root: Path, rows: list[dict]):
    path = registry_path(user_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"campaigns": rows}, indent=2))
    tmp.replace(path)


def get(user_root: Path, campaign_id: str) -> dict | None:
    return next((r for r in load(user_root) if r["id"] == campaign_id), None)


def upsert(user_root: Path, row: dict):
    rows = [r for r in load(user_root) if r["id"] != row["id"]]
    rows.append({"created_at": int(time.time()), **row})
    rows.sort(key=lambda r: (r["id"] != MAIN, r.get("created_at") or 0))
    save(user_root, rows)


def remove(user_root: Path, campaign_id: str):
    save(user_root, [r for r in load(user_root) if r["id"] != campaign_id])


def on_account(user_root: Path, account: str) -> list[dict]:
    account = (account or "").strip().lower()
    return [r for r in load(user_root) if (r.get("account") or "").lower() == account and account]


def answering(user_root: Path, campaign_id: str) -> bool:
    """Is this campaign's worker answering its leads: running, meant to run,
    or paused for edits (it comes back)? A stopped one is not."""
    import os
    data = campaign_root(user_root, campaign_id) / "data"
    if (data / "worker.autostart").exists() or (data / "worker.paused").exists():
        return True
    try:
        os.kill(int((data / "harvey.pid").read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def outreach_owner(user_root: Path, account: str, email: str) -> dict | None:
    """The outreach campaign on ``account`` that has ``email`` on its lead
    list (not just in its inbox) and is answering its leads, or None. A stopped campaign's leads are
    left to the reply campaign on the account, so nobody goes unanswered."""
    email = (email or "").strip().lower()
    if not email:
        return None
    for row in on_account(user_root, account):
        if not answering(user_root, row["id"]):
            continue
        db = campaign_root(user_root, row["id"]) / "data" / "harvey.db"
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            # Someone who only wrote in to the inbox is not on the lead list:
            # with a reply campaign on the account, answering them is its job.
            hit = con.execute("SELECT 1 FROM prospects WHERE lower(email) = ? AND COALESCE(source, '') != 'inbox' "
                              "LIMIT 1", (email,)).fetchone()
        except sqlite3.Error:
            hit = None
        finally:
            con.close()
        if hit:
            return row
    return None


def profile_for_account(user_root: Path, account: str) -> str:
    """The campaign whose goal and brief an account writes with: the
    outreach campaign on that account, else the main one."""
    rows = on_account(user_root, account)
    return rows[0]["id"] if rows else MAIN


def profile_root(user_root: Path, campaign_id: str) -> Path:
    """Where a run's goal, brief and reply files live: that campaign's
    folder while it exists with a profile in it, else the user's main one
    (a run outlives a campaign that was removed)."""
    user_root = Path(user_root)
    if not campaign_id or campaign_id == MAIN or not valid_id(campaign_id) or not get(user_root, campaign_id):
        return user_root
    root = campaign_root(user_root, campaign_id)
    return root if (root / "harvey.yaml").exists() else user_root


def _campaign_dbs(user_root: Path) -> list[Path]:
    """Every Harvey database of this user: the main one, each campaign's,
    and those of campaigns that were removed (kept under .removed)."""
    user_root = Path(user_root)
    out = [user_root / "data" / "harvey.db"]
    base = user_root / "campaigns"
    for folder in (base, base / ".removed"):
        if folder.is_dir():
            out += [d / "data" / "harvey.db" for d in sorted(folder.iterdir()) if d.is_dir() and d.name != ".removed"]
    return [p for p in out if p.exists()]


def backfill_suppressed(user_root: Path) -> int:
    """Put every opt-out this user already has into the shared list, so
    each campaign honours them: people marked opted out in any campaign,
    and the addresses on any account's excluded list. Opt-outs from before
    the shared list existed (or from a campaign since removed) would
    otherwise reach only the campaign that recorded them. Returns how
    many addresses it looked at."""
    from harvey.ledger import Ledger
    user_root = Path(user_root)
    found: dict[str, str] = {}
    for db in _campaign_dbs(user_root):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            continue
        try:
            for (email,) in con.execute("SELECT lower(email) FROM prospects WHERE status = 'opted_out' AND email LIKE '%@%'"):
                found.setdefault(email.strip(), "opted out in a campaign")
        except sqlite3.Error:
            pass
        finally:
            con.close()
    mail_db = user_root / "data" / "mail.db"
    if mail_db.exists():
        try:
            con = sqlite3.connect(f"file:{mail_db}?mode=ro", uri=True, timeout=5)
            try:
                for (raw,) in con.execute("SELECT settings_json FROM mailboxes"):
                    try:
                        excluded = (json.loads(raw or "{}") or {}).get("excluded") or []
                    except (TypeError, ValueError):
                        continue
                    for x in excluded if isinstance(excluded, list) else []:
                        x = str(x or "").strip().lower()
                        if "@" in x and not x.startswith("@"):
                            found.setdefault(x, "on an account's excluded list")
            finally:
                con.close()
        except sqlite3.Error:
            pass
    if not found:
        return 0
    led = Ledger(ledger_path(user_root))
    already = led.suppressed_among(list(found))
    for email, reason in found.items():
        if email not in already:
            led.suppress(email, reason, "backfill")
    return len(found)
