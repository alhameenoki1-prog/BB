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


class ShareUsed(SendRefused):
    """The account's shared daily limit has no room for this campaign now."""


def profile_root(env: dict) -> str:
    """Where the goal, brief and reply files come from: the campaign the
    run writes for (HARVEY_PROFILE_HOME), else the user's main profile."""
    return (env or {}).get("HARVEY_PROFILE_HOME") or (env or {}).get("HARVEY_HOME") or ""


async def env_for_draft(db: MailDB, env: dict, draft: dict, mailbox: dict | None = None) -> dict:
    """The worker's env with the profile this draft was written with: the
    run's campaign, else (a draft from the thread page or Compose) the
    outreach campaign on the sending account, else the main profile. The
    same profile then supplies the files and link names at send time."""
    root = (env or {}).get("HARVEY_HOME") or ""
    if not root:
        return env
    from bb import campaigns as campaigns_mod
    pid = ""
    if draft.get("run_id"):
        run = await db.one("SELECT profile_id FROM reply_runs WHERE id = ?", (draft["run_id"],))
        pid = (run or {}).get("profile_id") or ""
    if not pid and mailbox:
        pid = campaigns_mod.profile_for_account(root, mailbox.get("address") or "")
    if not pid or pid == campaigns_mod.MAIN:
        return env
    return {**env, "HARVEY_PROFILE_HOME": str(campaigns_mod.profile_root(root, pid))}


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


def _link_labels(env: dict) -> dict:
    """URL → name for the links the profile knows (the scope documents),
    so the HTML part says "View the Towson brief" instead of the address."""
    root = profile_root(env)
    if not root:
        return {}
    from bb.mail import scope as scope_mod
    scope = scope_mod.load(root)
    return {l["url"]: (l.get("label") or "the full scope") for l in (scope.get("links") or []) if l.get("url")}


async def _reply_attachments(db: MailDB, env: dict, draft: dict) -> list[str]:
    """The project brief (and other reply files) from the sender's profile,
    on the first reply in a thread or every reply as the policy says."""
    root = profile_root(env)
    if not root or draft.get("kind", "reply") != "reply":
        return []
    from bb.mail import scope as scope_mod
    scope = scope_mod.load(root)
    prior = await prior_replies(db, draft.get("thread_id") or "", exclude_draft=draft.get("id") or "")
    return scope_mod.attachment_paths(root, scope, prior)


async def prior_replies(db: MailDB, thread_id: str, exclude_draft: str = "") -> int:
    """How many replies the command center itself has already sent in this
    thread. The cold email that started the thread (synced from Sent) is
    not one: the first reply after it still carries the brief."""
    if not thread_id:
        return 0
    row = await db.one("SELECT COUNT(*) AS n FROM drafts WHERE thread_id = ? AND status = 'sent' AND id != ?",
                       (thread_id, exclude_draft))
    sent_drafts = int((row or {}).get("n") or 0)
    row = await db.one("SELECT COUNT(*) AS n FROM messages WHERE thread_id = ? AND direction = 'outbound' AND sent_by = 'ai'",
                       (thread_id,))
    return max(sent_drafts, int((row or {}).get("n") or 0))


async def send_draft(db: MailDB, secret: str, env: dict, draft: dict, *, dry_run: bool = False) -> dict:
    """Send one approved draft. Returns {status, message_id}. Raises
    SendRefused for a rule, AuthError / TransientError from the provider."""
    mailbox = await db.mailbox(draft["mailbox_id"])
    if not mailbox:
        raise SendRefused("the mailbox no longer exists")
    env = await env_for_draft(db, env, draft, mailbox)
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
    from bb.mail.runs import is_excluded
    if is_excluded(settings, recipient.get("email", "")):
        raise SendRefused(f"{recipient.get('email')} is on {mailbox['address']}'s excluded list (they asked not to be contacted)")
    plain, html = finalize(draft["body"], settings, greeting_name=recipient.get("name") or "",
                           link_labels=_link_labels(env))
    reply_to = (draft.get("reply_to") or settings.reply_to or "").strip()
    if dry_run:
        await db.execute("UPDATE drafts SET status = 'dry_run', body = ?, updated_at = ? WHERE id = ?",
                         (plain, now_iso(), draft["id"]))
        await db.log("review", f"Dry run: would have replied to {recipient.get('name') or recipient['email']} — \"{draft['subject']}\"",
                     mailbox["id"], draft_id=draft["id"])
        return {"status": "dry_run", "message_id": ""}
    # One slot of the account's daily limit, shared with every other
    # campaign on it (the outreach campaign's worker included). A send a
    # person pressed is held to the account's own limit only.
    from bb.mail.runs import FEATURES, ledger_for, run_label
    led, slot = ledger_for(env), None
    if led is not None:
        run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (draft.get("run_id") or "",)) if draft.get("run_id") else None
        led.set_limit(mailbox["address"], service.account_daily_limit(mailbox, service.open_creds(secret, mailbox)))
        if run:
            slot, pic = led.reserve(mailbox["address"], run["id"], FEATURES.get(run.get("kind") or "reply", "reply-run"),
                                    run_label(run, mailbox["address"]), kind=draft.get("kind") or "reply")
        else:
            slot, pic = led.reserve(mailbox["address"], "manual", "manual", "Sent by hand", priority=True,
                                    kind=draft.get("kind") or "reply")
        if slot is None:
            raise ShareUsed(pic["reason"])
        if led.is_suppressed(recipient.get("email", "")):
            led.release(slot)
            raise SendRefused(f"{recipient.get('email')} asked not to be contacted (in another campaign)")
    creds = service.open_creds(secret, mailbox)
    account = account_for(mailbox, creds, env)
    attachments = await _reply_attachments(db, env, draft)
    try:
        message_id, provider_id, thread_key = await account.send(
            to=[recipient["email"]], subject=draft["subject"], body_text=plain, body_html=html,
            in_reply_to=draft.get("in_reply_to") or "", references=draft.get("references_text") or "",
            reply_to=reply_to, from_name=settings.display_name, attachments=attachments or None,
        )
    except BaseException:
        if led is not None:
            led.release(slot)
        raise
    finally:
        await service.persist_creds(db, secret, mailbox, account)   # a rotated Microsoft refresh token
    if attachments:
        await db.log("send", f"Attached {', '.join(a.rsplit('/', 1)[-1] for a in attachments)} to the reply to "
                             f"{recipient.get('name') or recipient['email']}", mailbox["id"], draft_id=draft["id"])
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
