"""The send layer: every outgoing email from the mail center passes here.
It refuses a mailbox with no signature or project link, honours the
daily cap and the send window, records a dry run instead of sending,
sends through the mailbox's own provider with proper threading headers
and Reply-To, mirrors the sent message into the database so the thread
state updates at once, and writes the activity line."""

from datetime import datetime, timedelta, timezone

from bb.mail import service
from bb.mail.db import MailDB, now_iso
from bb.mail.drafting import finalize
from bb.mail.ingest import Ingest
from bb.mail.providers import AuthError, TransientError, account_for
from bb.mail.providers.base import RawMessage


class SendRefused(Exception):
    """A rule said no. The draft stays where it is with the reason."""


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def sent_today(db: MailDB, mailbox_id: str) -> int:
    row = await db.one("SELECT COUNT(*) AS n FROM drafts WHERE mailbox_id = ? AND status = 'sent' AND sent_at >= ?",
                       (mailbox_id, _today_utc() + "T00:00:00Z"))
    return row["n"] if row else 0


def within_hours(settings, now: datetime | None = None) -> bool:
    if not settings.hours_start or not settings.hours_end:
        return True
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    if settings.hours_start <= settings.hours_end:
        return settings.hours_start <= hhmm <= settings.hours_end
    return hhmm >= settings.hours_start or hhmm <= settings.hours_end   # a window over midnight


async def send_draft(db: MailDB, secret: str, env: dict, draft: dict, *, dry_run: bool = False) -> dict:
    """Send one approved draft. Returns {status, message_id}. Raises
    SendRefused for a rule, AuthError / TransientError from the provider."""
    mailbox = await db.mailbox(draft["mailbox_id"])
    if not mailbox:
        raise SendRefused("the mailbox no longer exists")
    settings = service.settings_of(mailbox)
    ok, why = service.ready_to_send(mailbox)
    if not ok:
        raise SendRefused(why)
    if not dry_run:   # a dry run shows the email; the cap and the window depend on the day it is really sent
        if settings.daily_cap and await sent_today(db, mailbox["id"]) >= settings.daily_cap:
            raise SendRefused(f"the daily cap of {settings.daily_cap} for {mailbox['address']} is reached; it sends again tomorrow")
        if not within_hours(settings):
            raise SendRefused(f"outside {mailbox['address']}'s send window ({settings.hours_start} to {settings.hours_end})")
    import json
    to = json.loads(draft.get("to_json") or "[]")
    if not to:
        raise SendRefused("no recipient")
    recipient = to[0]
    plain, html = finalize(draft["body"], settings, greeting_name=recipient.get("name") or "")
    reply_to = (draft.get("reply_to") or settings.reply_to or "").strip()
    if dry_run:
        await db.execute("UPDATE drafts SET status = 'dry_run', body = ?, updated_at = ? WHERE id = ?",
                         (plain, now_iso(), draft["id"]))
        await db.log("review", f"Dry run: would have replied to {recipient.get('name') or recipient['email']} — \"{draft['subject']}\"",
                     mailbox["id"], draft_id=draft["id"])
        return {"status": "dry_run", "message_id": ""}
    creds = service.open_creds(secret, mailbox)
    account = account_for(mailbox, creds, env)
    message_id, provider_id, thread_key = await account.send(
        to=[recipient["email"]], subject=draft["subject"], body_text=plain, body_html=html,
        in_reply_to=draft.get("in_reply_to") or "", references=draft.get("references_text") or "",
        reply_to=reply_to, from_name=settings.display_name,
    )
    now = now_iso()
    await db.execute("UPDATE drafts SET status = 'sent', body = ?, sent_message_id = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                     (plain, message_id, now, now, draft["id"]))
    # Mirror the sent message so the thread's state flips right away.
    raw = RawMessage(provider_id=provider_id or f"sent:{message_id}", thread_key=thread_key or "", message_id=message_id,
                     in_reply_to=draft.get("in_reply_to") or "", references=draft.get("references_text") or "",
                     from_email=mailbox["address"], from_name=settings.display_name,
                     to=[{"name": recipient.get("name", ""), "email": recipient["email"]}], subject=draft["subject"],
                     body=plain, folder="SENT", sent_at=now, outbound=True)
    ingest = Ingest(db, mailbox)
    if draft.get("thread_id"):
        raw.thread_key = ""   # attach by the existing thread id below
        await ingest.store_batch([raw])
        await db.execute("UPDATE messages SET thread_id = ?, sent_by = 'ai' WHERE mailbox_id = ? AND provider_id = ?",
                         (draft["thread_id"], mailbox["id"], raw.provider_id))
        stray = await db.one("SELECT thread_id FROM messages WHERE mailbox_id = ? AND provider_id = ?", (mailbox["id"], raw.provider_id))
        await ingest.refresh_thread(draft["thread_id"])
        if stray and stray["thread_id"] != draft["thread_id"]:
            await ingest.refresh_thread(stray["thread_id"])
        await ingest.refresh_counts()
    else:
        await ingest.store_batch([raw])
        await db.execute("UPDATE messages SET sent_by = 'ai' WHERE mailbox_id = ? AND provider_id = ?", (mailbox["id"], raw.provider_id))
    kind = "replied" if draft.get("kind") == "reply" else "sent"
    await db.log("replied", f"{'Replied to' if kind == 'replied' else 'Emailed'} {recipient.get('name') or recipient['email']} — \"{draft['subject']}\"",
                 mailbox["id"], draft_id=draft["id"], message_id=message_id)
    return {"status": "sent", "message_id": message_id}
