"""What the web pages and the worker share: opening a user's mail
database, sealing and reading credentials, connecting and checking
mailboxes, the account scope, and the migration of the old single
mailbox into the new table."""

import json
import logging
from datetime import datetime, timedelta, timezone

from bb.mail import crypto
from bb.mail.db import MailDB, new_id, now_iso  # noqa: F401  (re-exported for routes)
from bb.mail.jobs import Jobs
from bb.mail.models import MailboxSettings
from bb.mail.providers import AuthError, TransientError, account_for

logger = logging.getLogger("bb.mail")

CHECK_EVERY_MINUTES = 15


def settings_of(mailbox: dict) -> MailboxSettings:
    try:
        return MailboxSettings(**(mailbox.get("settings") or {}))
    except Exception:
        return MailboxSettings()


def ready_to_send(mailbox: dict) -> tuple[bool, str]:
    """A mailbox sends only with a signature and a project link, and only
    while its connection works."""
    ok, why = settings_of(mailbox).ready_to_send()
    if not ok:
        return False, why
    if mailbox.get("status") == "reconnect":
        return False, "This mailbox needs reconnecting before it can send."
    if not mailbox.get("enabled", 1):
        return False, "This mailbox is disabled."
    return True, ""


def open_creds(secret: str, mailbox: dict) -> dict:
    try:
        return crypto.open_json(secret, mailbox.get("creds") or "")
    except Exception:
        return {}


def masked_creds(secret: str, mailbox: dict) -> dict:
    creds = open_creds(secret, mailbox)
    out = {}
    for k, v in creds.items():
        out[k] = crypto.mask(str(v)) if any(s in k for s in ("password", "token", "secret")) else v
    return out


async def connect_imap(db: MailDB, secret: str, *, address: str, smtp: dict, env: dict) -> tuple[str, str]:
    """Verify and store an IMAP/SMTP mailbox ("limited mode"). Returns
    (mailbox id, note). Raises AuthError / TransientError / ValueError."""
    address = (address or "").strip().lower()
    if "@" not in address:
        raise ValueError("Enter the full email address.")
    creds = {
        "username": smtp["smtp_username"] or address, "password": smtp["smtp_password"],
        "smtp_host": smtp["smtp_host"], "smtp_port": int(smtp.get("smtp_port") or 587),
        "imap_host": smtp.get("imap_host") or smtp["smtp_host"], "imap_port": int(smtp.get("imap_port") or 993),
        "imap_username": smtp.get("imap_username") or "", "imap_password": smtp.get("imap_password") or "",
    }
    probe = {"address": address, "provider": "imap"}
    note = await account_for(probe, creds, env).test()
    existing = await db.mailbox_by_address(address)
    sealed = crypto.seal_json(secret, creds)
    if existing:
        await db.update_mailbox(existing["id"], creds=sealed, provider="imap", mode="limited",
                                status="ok", status_detail=note, last_checked_at=now_iso(), enabled=1)
        await db.log("connected", f"Reconnected {address} (limited mode).", existing["id"])
        return existing["id"], note
    mid = await db.add_mailbox(address=address, provider="imap", creds=sealed, mode="limited",
                               settings=MailboxSettings(color=_next_color(await db.mailboxes())).model_dump(),
                               status="ok")
    await db.update_mailbox(mid, status_detail=note, last_checked_at=now_iso())
    await db.log("connected", f"Connected {address} with a password (limited mode).", mid)
    return mid, note


async def connect_gmail(db: MailDB, secret: str, *, address: str, refresh_token: str, env: dict) -> tuple[str, str]:
    """Store a Gmail mailbox from a completed OAuth exchange and verify it."""
    address = (address or "").strip().lower()
    creds = {"refresh_token": refresh_token}
    existing = await db.mailbox_by_address(address)
    sealed = crypto.seal_json(secret, creds)
    probe = {"address": address, "provider": "gmail"}
    note = await account_for(probe, creds, env).test()
    if existing:
        await db.update_mailbox(existing["id"], creds=sealed, provider="gmail", mode="full",
                                status="ok", status_detail=note, last_checked_at=now_iso(), enabled=1)
        await db.log("connected", f"Reconnected {address} through Google.", existing["id"])
        return existing["id"], note
    mid = await db.add_mailbox(address=address, provider="gmail", creds=sealed, mode="full",
                               settings=MailboxSettings(color=_next_color(await db.mailboxes())).model_dump(),
                               status="ok")
    await db.update_mailbox(mid, status_detail=note, last_checked_at=now_iso())
    await db.log("connected", f"Connected {address} through Google.", mid)
    return mid, note


def _next_color(mailboxes: list[dict]) -> str:
    from bb.mail.models import COLORS
    used = {(m.get("settings") or {}).get("color") for m in mailboxes}
    for c in COLORS:
        if c not in used:
            return c
    return COLORS[len(mailboxes) % len(COLORS)]


async def connect_graph(db: MailDB, secret: str, *, address: str, tenant_id: str, client_id: str,
                        client_secret: str, env: dict) -> tuple[str, str]:
    """Verify and store a Microsoft 365 mailbox read through an app
    registration (client credentials). Raises AuthError / TransientError / ValueError."""
    address = (address or "").strip().lower()
    if "@" not in address:
        raise ValueError("Enter the mailbox address.")
    creds = {"tenant_id": tenant_id.strip(), "client_id": client_id.strip(), "client_secret": client_secret.strip()}
    if not all(creds.values()):
        raise ValueError("The tenant id, client id and client secret are all needed.")
    probe = {"address": address, "provider": "graph"}
    note = await account_for(probe, creds, env).test()
    existing = await db.mailbox_by_address(address)
    sealed = crypto.seal_json(secret, creds)
    if existing:
        await db.update_mailbox(existing["id"], creds=sealed, provider="graph", mode="full",
                                status="ok", status_detail=note, last_checked_at=now_iso(), enabled=1)
        await db.log("connected", f"Reconnected {address} through Microsoft 365.", existing["id"])
        return existing["id"], note
    mid = await db.add_mailbox(address=address, provider="graph", creds=sealed, mode="full",
                               settings=MailboxSettings(color=_next_color(await db.mailboxes())).model_dump(),
                               status="ok")
    await db.update_mailbox(mid, status_detail=note, last_checked_at=now_iso())
    await db.log("connected", f"Connected {address} through Microsoft 365.", mid)
    return mid, note


async def check_mailbox(db: MailDB, secret: str, mailbox: dict, env: dict) -> str:
    """Verify one mailbox's credentials and record the result. Returns the
    new status. An auth failure is 'reconnect' (shown as a banner, never a
    crash); a network failure is 'error' and is retried later."""
    creds = open_creds(secret, mailbox)
    previous = mailbox.get("status")
    try:
        note = await account_for(mailbox, creds, env).test()
        status, detail = "ok", note
    except AuthError as e:
        status, detail = "reconnect", str(e)[:300]
    except TransientError as e:
        status, detail = "error", str(e)[:300]
    except Exception as e:  # a bug in an adapter must not take the worker down
        status, detail = "error", f"{type(e).__name__}: {str(e)[:200]}"
    await db.update_mailbox(mailbox["id"], status=status, status_detail=detail, last_checked_at=now_iso())
    if status != previous:
        if status == "reconnect":
            await db.log("warning", f"{mailbox['address']} needs reconnecting: {detail}", mailbox["id"])
        elif status == "ok" and previous in ("reconnect", "error"):
            await db.log("connected", f"{mailbox['address']} is connected again.", mailbox["id"])
        elif status == "error":
            await db.log("warning", f"Could not reach {mailbox['address']}: {detail}", mailbox["id"])
    return status


SYNC_EVERY_SECONDS = 90
DEFAULT_BACKFILL_DAYS = 365


async def run_sync(db: MailDB, secret: str, mailbox: dict, env: dict, jobs: Jobs, job_id: str,
                   backfill: bool) -> dict:
    """Mirror new mail into the database. Progress is written to the job
    as it goes ("Synced 2,340 / 11,200"), the mailbox's sync state is
    saved after every batch so a crash resumes rather than restarts, and
    an auth failure turns into the Reconnect banner."""
    from bb.mail.ingest import Ingest

    creds = open_creds(secret, mailbox)
    account = account_for(mailbox, creds, env)
    ingest = Ingest(db, mailbox)
    totals = {"new": 0, "seen": 0, "total": 0}

    async def on_batch(msgs, done, total):
        totals["new"] += await ingest.store_batch(msgs)
        totals["seen"], totals["total"] = done, max(total, done)
        await jobs.progress(job_id, done=done, total=totals["total"], new=totals["new"], phase="backfill" if backfill else "sync")

    try:
        if backfill:
            days = int((mailbox.get("settings") or {}).get("backfill_days", DEFAULT_BACKFILL_DAYS) or 0)
            await db.log("synced", f"Started syncing {mailbox['address']}"
                         + (f" (the last {days} days)." if days else " (everything)."), mailbox["id"])
            state = await account.backfill(days, on_batch)
        else:
            state = await account.sync(mailbox.get("sync") or {}, on_batch)
    except AuthError as e:
        await db.update_mailbox(mailbox["id"], status="reconnect", status_detail=str(e)[:300], last_checked_at=now_iso())
        await db.log("warning", f"{mailbox['address']} needs reconnecting: {e}", mailbox["id"])
        return {"status": "reconnect", **totals}
    except TransientError as e:
        await db.update_mailbox(mailbox["id"], status="error", status_detail=str(e)[:300], last_checked_at=now_iso())
        raise  # the queue retries with backoff
    merged = {**(mailbox.get("sync") or {}), **state}
    await db.update_mailbox(mailbox["id"], sync=merged, last_sync_at=now_iso(), status="ok",
                            status_detail=mailbox.get("status_detail") or "", last_checked_at=now_iso())
    counts = await ingest.refresh_counts()
    if backfill:
        await db.log("synced", f"Finished syncing {mailbox['address']}: {totals['new']:,} messages in "
                               f"{counts.get('threads', 0):,} conversations, {counts.get('needs_reply', 0):,} waiting for a reply.",
                     mailbox["id"])
    elif totals["new"]:
        await db.log("synced", f"Synced {mailbox['address']}: {totals['new']} new message{'s' if totals['new'] != 1 else ''}.",
                     mailbox["id"])
    return {"status": "ok", **totals}


async def schedule_syncs(db: MailDB, jobs: Jobs):
    """A backfill for every connected mailbox that has none, then an
    incremental sync every SYNC_EVERY_SECONDS. All mailboxes are queued
    independently and the worker runs them side by side."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=SYNC_EVERY_SECONDS)).replace(microsecond=0)
    for m in await db.mailboxes(include_disabled=False):
        if m.get("status") in ("reconnect", "disabled"):
            continue
        if not (m.get("sync") or {}).get("backfill_done"):
            await jobs.enqueue("sync_backfill", m["id"], dedupe=True, max_attempts=8)
            continue
        last = m.get("last_sync_at") or ""
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
        except ValueError:
            last_dt = None
        if last_dt is None or last_dt < cutoff:
            await jobs.enqueue("sync_incremental", m["id"], dedupe=True)


async def schedule_checks(db: MailDB, jobs: Jobs):
    """Queue a connection check for any mailbox not checked recently."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=CHECK_EVERY_MINUTES)).replace(microsecond=0)
    for m in await db.mailboxes(include_disabled=False):
        last = m.get("last_checked_at") or ""
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
        except ValueError:
            last_dt = None
        if last_dt is None or last_dt < cutoff:
            await jobs.enqueue("check_mailbox", m["id"], dedupe=True)


# ── scope: which mailboxes every view works on ──

async def scope(db: MailDB) -> list[str] | None:
    """The selected mailbox ids, or None for all accounts."""
    raw = await db.get_setting("scope", "all")
    if raw == "all":
        return None
    try:
        ids = json.loads(raw)
    except ValueError:
        return None
    known = {m["id"] for m in await db.mailboxes()}
    ids = [i for i in ids if i in known]
    return ids if ids else None


async def set_scope(db: MailDB, ids: list[str] | None):
    await db.set_setting("scope", "all" if ids is None else json.dumps(list(ids)))


async def scoped_mailboxes(db: MailDB) -> list[dict]:
    ids = await scope(db)
    boxes = await db.mailboxes(include_disabled=False)
    return boxes if ids is None else [m for m in boxes if m["id"] in ids]


# ── migration from the single-mailbox setup ──

async def migrate_from_workspace(db: MailDB, secret: str, ws) -> str | None:
    """Carry the workspace's one connected mailbox and profile values into
    the mailboxes table, once. Returns the new mailbox id or None."""
    if await db.get_setting("migrated_v1"):
        return None
    await db.set_setting("migrated_v1", now_iso())
    if await db.mailboxes():
        return None
    profile = ws.profile() if ws.is_configured() else {}
    settings = MailboxSettings(
        display_name=profile.get("name", ""), signature_html=profile.get("signature_html", ""),
        persona_notes=(profile.get("intent", "") or "")[:600], color="blue",
    )
    provider = ws.mail_provider()
    mid = None
    if provider == "smtp" and ws.smtp_configured():
        s = ws.smtp_settings()
        creds = {"username": s["smtp_username"], "password": s["smtp_password"], "smtp_host": s["smtp_host"],
                 "smtp_port": int(s.get("smtp_port") or 587), "imap_host": s.get("imap_host") or s["smtp_host"],
                 "imap_port": int(s.get("imap_port") or 993), "imap_username": s.get("imap_username") or "",
                 "imap_password": s.get("imap_password") or ""}
        settings.reply_to = (s.get("reply_to") or "").strip().lower()
        mid = await db.add_mailbox(address=ws.sending_address() or s["smtp_username"], provider="imap",
                                   creds=crypto.seal_json(secret, creds), mode="limited",
                                   settings=settings.model_dump(), status="new")
    elif provider == "gmail" and ws.gmail_connected():
        token = ws.gmail_token() if hasattr(ws, "gmail_token") else {}
        refresh = (token or {}).get("refresh_token", "")
        if refresh:
            mid = await db.add_mailbox(address=ws.sending_address(), provider="gmail",
                                       creds=crypto.seal_json(secret, {"refresh_token": refresh}), mode="full",
                                       settings=settings.model_dump(), status="new")
    if mid:
        await db.log("info", f"Brought {ws.sending_address()} over from the old mailbox settings. "
                             "Add its project link before sending from it.", mid)
    return mid
