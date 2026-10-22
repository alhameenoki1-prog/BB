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


class AnsweredElsewhere(SendRefused):
    """Another campaign on the account already answered this message."""


class StaleFollowUp(SendRefused):
    """A scan's follow-up, and they wrote again after it was written."""


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


def _campaign_name(env: dict, campaign: str) -> str:
    """A campaign id from the ledger, in words."""
    root = (env or {}).get("HARVEY_HOME") or ""
    if root and campaign:
        from bb import campaigns as campaigns_mod
        try:
            row = campaigns_mod.get(root, campaign)
        except Exception:
            row = None
        if row:
            return row.get("name") or ("the main campaign" if campaign == campaigns_mod.MAIN else campaign)
    return "a reply run" if campaign else "another campaign"


async def editor_body(db: MailDB, env: dict, draft: dict) -> tuple[str, str]:
    """(the draft's text with every link on its words, the link a new link
    starts from) for the editor: the recipient's view of the links, named
    from the profile this draft writes for."""
    from harvey.integrations.mail_provider import embed_links
    mailbox = await db.mailbox(draft["mailbox_id"]) if draft.get("mailbox_id") else None
    labels = {}
    if mailbox:
        labels = _link_labels(await env_for_draft(db, env, draft, mailbox))
    default = next(iter(labels), "") or ((service.settings_of(mailbox).project_link or "") if mailbox else "")
    return embed_links(draft.get("body") or "", labels), default


def preview_document(html: str) -> str:
    """A whole page around an email's HTML for the editors' preview frame:
    links open in a new tab, and nothing in it can reach the page."""
    if not html:
        return ""
    return ('<!doctype html><html><head><meta charset="utf-8"><base target="_blank">'
            '<style>body{margin:14px 16px;background:#fff}a{cursor:pointer}</style></head><body>' + html + "</body></html>")


async def preview_html(db: MailDB, env: dict, draft: dict, body: str) -> str:
    """The HTML of this draft exactly as it would go out now (greeting,
    links on their words, signature), for the editor's preview."""
    import json as _json
    mailbox = await db.mailbox(draft["mailbox_id"])
    if not mailbox:
        return ""
    env = await env_for_draft(db, env, draft, mailbox)
    settings = service.settings_of(mailbox)
    to = _json.loads(draft.get("to_json") or "[]")
    name = (to[0].get("name") if to else "") or ""
    return finalize(body or "", settings, greeting_name=name, link_labels=_link_labels(env))[1]


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


async def send_draft(db: MailDB, secret: str, env: dict, draft: dict, *, dry_run: bool = False, pressed: bool = False,
                     saw_thread: bool = False) -> dict:
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
    if draft.get("kind", "reply") == "reply" and draft.get("thread_id") and draft.get("created_at"):
        later = await db.one("SELECT 1 FROM messages WHERE thread_id = ? AND direction = 'outbound' AND sent_at > ? LIMIT 1",
                             (draft["thread_id"], draft["created_at"]))
        if later:
            raise SendRefused("the thread was answered after this draft was written; sending it would be a second reply")
    if not saw_thread and draft.get("run_id") and draft.get("thread_id") and draft.get("created_at"):
        # (A person who pressed Send on the thread page saw the thread as it
        # is now: theirs to send. Approve all on Review shows no thread.)
        scan = await db.one("SELECT 1 FROM reply_runs WHERE id = ? AND kind = 'reconnect'", (draft["run_id"],))
        if scan and await db.one("SELECT 1 FROM messages WHERE thread_id = ? AND direction = 'inbound' AND sent_at > ? LIMIT 1",
                                 (draft["thread_id"], draft["created_at"])):
            raise StaleFollowUp("they wrote again after this follow-up was written; sending it would ignore what they said")
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
        # A reply a run wrote (approved on Review or sent on its own) checks
        # whether another campaign answered the same message meanwhile: on a
        # mailbox that keeps no copy of what an outreach campaign sends, the
        # thread here would never show that answer. (A reply a person wrote
        # from scratch is theirs to send.)
        if ((not pressed or draft.get("run_id")) and draft.get("kind", "reply") == "reply" and draft.get("in_reply_to")
                and (run or {}).get("kind") != "reconnect"):    # a scan writes a new note, it answers nothing
            by = led.answered_by(draft["in_reply_to"])
            if by and by not in ((run or {}).get("id"), "manual"):
                raise AnsweredElsewhere(f"another campaign on this account ({_campaign_name(env, by)}) already answered "
                                        "this message; sending this too would be a second answer")
        if run and not pressed and run["status"] in ("queued", "running"):
            slot, pic = led.reserve(mailbox["address"], run["id"], FEATURES.get(run.get("kind") or "reply", "reply-run"),
                                    run_label(run, mailbox["address"]), kind=draft.get("kind") or "reply")
        else:
            # A person pressed Send (from Review or a thread), or the run is
            # over: held only to the account's own limit, counted under the
            # run, and never making a stopped run a member again.
            slot, pic = led.reserve(mailbox["address"], (run or {}).get("id") or "manual", "manual", "Sent by hand",
                                    priority=True, kind=draft.get("kind") or "reply")
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
    if led is not None and draft.get("kind", "reply") == "reply" and draft.get("thread_id"):
        # The messages this reply answers: an outreach campaign that reads
        # them later (it was stopped while they came in) leaves them be.
        try:
            # (Only what was there when it was written: a message that came in
            # after it is still waiting for an answer.)
            rows = await db.query("SELECT message_id FROM messages WHERE thread_id = ? AND direction = 'inbound' "
                                  "AND message_id != '' AND sent_at <= ?", (draft["thread_id"], draft.get("created_at") or now_iso()))
            led.mark_answered(mailbox["address"], (run or {}).get("id") or "manual",
                              [r["message_id"] for r in rows] + [draft.get("in_reply_to") or ""])
        except Exception:
            pass   # the send went; a missed note only means a later check reads the thread
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
