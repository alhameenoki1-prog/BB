"""Batch reply runs: pick unreplied threads across accounts, draft a reply
for each, then either hold them for review, send the confident ones, or
record a dry run. A run is one row in ``reply_runs`` with one
``run_items`` row per thread; the worker takes one job per mailbox so
accounts proceed side by side while each mailbox sends in order, paced
and capped on its own, always from the mailbox that received the mail."""

import asyncio
import json
import os
import random
from datetime import datetime, timedelta, timezone

from bb.mail import service
from bb.mail.db import MailDB, new_id, now_iso
from bb.mail.jobs import Jobs

MODES = ("review", "auto", "dry_run")
RECONNECT_MONTHS = (3, 6, 12)
RECONNECT_DEFAULT_MONTHS = 6
PACE_SECONDS = (20, 60)   # a random pause between two sends from one mailbox


# One run is created at a time per mail database: a double click (or the
# command bar and the page at once) must not put the same threads or
# contacts into two runs. The web site is one process, so a lock per
# database file is enough; the worker's processing-time twin check is the
# second line of defence.
_CREATE_LOCKS: dict[str, asyncio.Lock] = {}


def _create_lock(db: MailDB) -> asyncio.Lock:
    key = str(getattr(db, "path", id(db)))
    lock = _CREATE_LOCKS.get(key)
    if lock is None:
        lock = _CREATE_LOCKS[key] = asyncio.Lock()
    return lock


async def active_reconnect_run(db: MailDB, mailbox_ids: list[str] | None = None) -> dict | None:
    """A follow-up run still queued or running (over any of these
    mailboxes, when given), or None."""
    for run in await db.query("SELECT * FROM reply_runs WHERE kind = 'reconnect' AND status IN ('queued', 'running') "
                              "ORDER BY created_at DESC"):
        if mailbox_ids is None or set(json.loads(run.get("mailbox_ids_json") or "[]")) & set(mailbox_ids):
            return run
    return None


def pace() -> float:
    override = os.environ.get("BB_MAIL_PACE_SECONDS")
    if override is not None:
        try:
            return max(0.0, float(override))
        except ValueError:
            pass
    return random.uniform(*PACE_SECONDS)


def is_excluded(settings, email: str) -> bool:
    email = (email or "").lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return any(x == email or x == domain or (x.startswith(".") and domain.endswith(x)) for x in settings.excluded)


def is_opt_out(message: dict) -> bool:
    """Does this inbound message ask us to stop (unsubscribe, a complaint,
    hostility, a legal threat)? Checked by code, never left to the model."""
    from harvey import enquiry
    text = f"{message.get('subject', '')}\n{message.get('body') or message.get('body_text') or message.get('snippet') or ''}"
    return enquiry.is_opt_out(text)


async def suppress(db: MailDB, mailbox: dict, email: str, thread_id: str = "") -> None:
    """Never write to ``email`` again, from any of this user's mailboxes:
    it joins every mailbox's excluded list (which candidates, runs and
    sending all honour), read fresh so nothing saved meanwhile is lost,
    and its thread is dismissed."""
    email = (email or "").strip().lower()
    if not email:
        return
    for box in await db.mailboxes():
        settings = dict(box.get("settings") or {})
        excluded = [str(x).lower() for x in (settings.get("excluded") or [])]
        if email in excluded:
            continue
        settings["excluded"] = sorted(set(excluded) | {email})
        await db.update_mailbox(box["id"], settings=settings)
        if box["id"] == mailbox.get("id"):
            mailbox["settings"] = settings
    if thread_id:
        await db.execute("UPDATE threads SET state = 'dismissed', state_reason = 'they asked not to be contacted', updated_at = ? WHERE id = ?",
                         (now_iso(), thread_id))
    await db.log("warning", f"{email} asked not to be contacted: on the excluded list for {mailbox['address']}; nothing is sent.",
                 mailbox["id"], thread_id=thread_id)


async def candidates(db: MailDB, mailboxes: list[dict]) -> list[dict]:
    """The threads a run could answer, newest first, with the mailbox and
    the reason they would be skipped (excluded sender) if any."""
    out = []
    for box in mailboxes:
        settings = service.settings_of(box)
        rows = await db.query(f"SELECT t.*, (SELECT m.from_email FROM messages m WHERE m.thread_id = t.id AND m.direction = 'inbound' ORDER BY m.sent_at DESC, m.created_at DESC LIMIT 1) AS last_from_email FROM threads t WHERE t.mailbox_id = ? AND t.state = 'needs_reply' "
                              "ORDER BY t.last_message_at DESC LIMIT 500", (box["id"],))
        for t in rows:
            t["box"] = box
            t["excluded"] = is_excluded(settings, t.get("last_from_email") or "")
            out.append(t)
    return out


async def reconnect_candidates(db: MailDB, mailboxes: list[dict], months: int = RECONNECT_DEFAULT_MONTHS,
                               all_mailboxes: list[dict] | None = None) -> list[dict]:
    """Past conversations the project could follow on from: two-way threads
    (they wrote by hand, we answered) from the last ``months`` months, one
    per contact (the most recent), newest first. Left out: automated or
    bulk senders, our own addresses and colleagues at our own domains,
    excluded and suppressed addresses, threads put away by hand, contacts
    a scan already followed up with, and threads where the brief's link
    has already gone out."""
    from harvey import names
    from harvey.integrations.mail_provider import looks_automated

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * max(1, months))).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    own = {(m.get("address") or "").lower() for m in (all_mailboxes or mailboxes)}
    # Colleagues share a company domain; a gmail.com mailbox shares nothing with other gmail.com people.
    own_domains = {a.rsplit("@", 1)[-1] for a in own if "@" in a} - set(names.PUBLIC_DOMAINS)
    excluded_everywhere = {str(x).lower() for m in (all_mailboxes or mailboxes)
                           for x in ((m.get("settings") or {}).get("excluded") or [])}
    # Already followed up, in hand, or judged (a thread the model found
    # personal, or an address that asked to be left alone) is never asked
    # again; a skip for a passing reason (the daily cap) is.
    done_contacts = {r["contact"] for r in await db.query(
        "SELECT DISTINCT ri.contact FROM run_items ri JOIN reply_runs r ON r.id = ri.run_id "
        "WHERE r.kind = 'reconnect' AND ri.contact != '' AND (ri.status IN ('sent', 'needs_review', 'dry_run', 'queued', 'drafting') "
        "OR (ri.status = 'skipped' AND (ri.detail LIKE 'not suitable%' OR ri.detail LIKE '%asked not to be contacted%')))")}
    pool = []
    for box in mailboxes:
        settings = service.settings_of(box)
        links = [l.lower() for l in [settings.project_link] if l]
        counts = {r["thread_id"]: r for r in await db.query(
            "SELECT thread_id, SUM(CASE WHEN direction = 'inbound' AND automated = 0 THEN 1 ELSE 0 END) AS human_in, "
            "SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS out_n FROM messages WHERE mailbox_id = ? GROUP BY thread_id",
            (box["id"],))}
        rows = await db.query(
            "SELECT t.*, (SELECT m.from_email FROM messages m WHERE m.thread_id = t.id AND m.direction = 'inbound' AND m.automated = 0 "
            "ORDER BY m.sent_at DESC, m.created_at DESC LIMIT 1) AS contact_email, "
            "(SELECT m.from_name FROM messages m WHERE m.thread_id = t.id AND m.direction = 'inbound' AND m.automated = 0 "
            "ORDER BY m.sent_at DESC, m.created_at DESC LIMIT 1) AS contact_name "
            "FROM threads t WHERE t.mailbox_id = ? AND t.last_message_at >= ? "
            "AND t.state NOT IN ('dismissed', 'snoozed', 'no_reply_needed', 'needs_reply') "
            "ORDER BY t.last_message_at DESC LIMIT 20000", (box["id"], cutoff))
        for t in rows:
            c = counts.get(t["id"]) or {}
            if not c.get("human_in") or not c.get("out_n"):
                continue                      # not a conversation: nobody answered, or nobody wrote by hand
            email = (t.get("contact_email") or "").lower()
            if not email or email in own or email.rsplit("@", 1)[-1] in own_domains:
                continue
            if (looks_automated(email, t.get("subject", "")) or is_excluded(settings, email) or email in excluded_everywhere
                    or email in done_contacts):
                continue
            if links:
                sent_text = " ".join((r["body"] or "") for r in await db.query(
                    "SELECT body FROM messages WHERE thread_id = ? AND direction = 'outbound'", (t["id"],))).lower()
                if any(l in sent_text for l in links):
                    continue                  # the link already went to them in this thread
            t["box"], t["contact"], t["contact_name"] = box, email, t.get("contact_name") or ""
            t["excluded"] = False
            pool.append(t)
    # One thread per contact per scan, the most recent across every mailbox.
    pool.sort(key=lambda t: t.get("last_message_at") or "", reverse=True)
    out, seen = [], set()
    for t in pool:
        if t["contact"] in seen:
            continue
        seen.add(t["contact"])
        out.append(t)
    return out


async def create_reconnect_run(db: MailDB, mailboxes: list[dict], months: int = RECONNECT_DEFAULT_MONTHS, mode: str = "review",
                               instructions: str = "", created_by: str = "", all_mailboxes: list[dict] | None = None) -> dict:
    """A run over the past conversations of these mailboxes: one item per
    contact, one job per mailbox. Returns the run row with per-mailbox
    counts, or {} when there is nobody to follow up with."""
    if mode not in MODES:
        mode = "review"
    months = months if months in RECONNECT_MONTHS else RECONNECT_DEFAULT_MONTHS
    async with _create_lock(db):
        busy = await active_reconnect_run(db, [m["id"] for m in mailboxes])
        if busy:
            # One follow-up scan at a time: a second would race the first
            # for the same contacts.
            return {"busy": busy["id"]}
        return await _insert_reconnect_run(db, mailboxes, months, mode, instructions, created_by, all_mailboxes)


async def _insert_reconnect_run(db, mailboxes, months, mode, instructions, created_by, all_mailboxes) -> dict:
    cands = await reconnect_candidates(db, mailboxes, months, all_mailboxes=all_mailboxes)
    if not cands:
        return {}
    run_id, now = new_id(), now_iso()
    involved = sorted({t["mailbox_id"] for t in cands})
    await db.execute(
        "INSERT INTO reply_runs(id, mailbox_ids_json, mode, reply_to, status, total, done, summary_json, instructions, created_by, "
        "created_at, updated_at, kind, months) VALUES (?, ?, ?, '', 'queued', ?, 0, '{}', ?, ?, ?, ?, 'reconnect', ?)",
        (run_id, json.dumps(involved), mode, len(cands), instructions.strip()[:2000], created_by, now, now, months))
    per = {}
    for t in cands:
        await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, updated_at, contact) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                         (new_id(), run_id, t["mailbox_id"], t["id"], now, t["contact"]))
        per[t["mailbox_id"]] = per.get(t["mailbox_id"], 0) + 1
    jobs = Jobs(db)
    for mid in involved:
        await jobs.enqueue("reply_run", mid, {"run_id": run_id}, max_attempts=3)
    await db.log("info", f"Started a follow-up run over the last {months} months: {len(cands)} past conversation{'s' if len(cands) != 1 else ''} "
                         f"across {len(involved)} account{'s' if len(involved) != 1 else ''} ({mode.replace('_', ' ')}).", run_id=run_id)
    return {**(await db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))), "per_mailbox": per}


async def create_run(db: MailDB, mailboxes: list[dict], thread_ids: list[str], mode: str = "review",
                     reply_to: str = "", instructions: str = "", created_by: str = "") -> dict:
    """Insert the run and its items, queue one job per mailbox. Returns the
    run row with ``per_mailbox`` counts; a run with no items is not created."""
    if mode not in MODES:
        mode = "review"
    async with _create_lock(db):
        return await _insert_run(db, mailboxes, thread_ids, mode, reply_to, instructions, created_by)


async def _insert_run(db, mailboxes, thread_ids, mode, reply_to, instructions, created_by) -> dict:
    boxes = {m["id"]: m for m in mailboxes}
    marks = ",".join("?" for _ in thread_ids) or "''"
    rows = await db.query(f"SELECT id, mailbox_id, last_from FROM threads WHERE id IN ({marks}) AND state = 'needs_reply'", thread_ids)
    # A thread already waiting in another run that has not finished is left
    # to that run (a double submit would otherwise draft it twice).
    taken = {r["thread_id"] for r in await db.query(
        "SELECT ri.thread_id FROM run_items ri JOIN reply_runs r ON r.id = ri.run_id "
        "WHERE r.status IN ('queued', 'running') AND ri.status IN ('queued', 'drafting')")}
    rows = [r for r in rows if r["mailbox_id"] in boxes and r["id"] not in taken]
    if not rows:
        return {}
    run_id, now = new_id(), now_iso()
    involved = sorted({r["mailbox_id"] for r in rows})
    await db.execute(
        "INSERT INTO reply_runs(id, mailbox_ids_json, mode, reply_to, status, total, done, summary_json, instructions, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?, 0, '{}', ?, ?, ?, ?)",
        (run_id, json.dumps(involved), mode, (reply_to or "").strip().lower(), len(rows), instructions.strip()[:2000], created_by, now, now))
    for r in rows:
        await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, updated_at) VALUES (?, ?, ?, ?, 'queued', ?)",
                         (new_id(), run_id, r["mailbox_id"], r["id"], now))
    jobs = Jobs(db)
    for mid in involved:
        await jobs.enqueue("reply_run", mid, {"run_id": run_id}, max_attempts=3)
    per = {}
    for r in rows:
        per[r["mailbox_id"]] = per.get(r["mailbox_id"], 0) + 1
    await db.log("info", f"Started a reply run: {len(rows)} thread{'s' if len(rows) != 1 else ''} across "
                         f"{len(involved)} account{'s' if len(involved) != 1 else ''} ({mode.replace('_', ' ')}).", run_id=run_id)
    return {**(await db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))), "per_mailbox": per}


async def cancel_run(db: MailDB, run_id: str) -> bool:
    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))
    if not run or run["status"] in ("done", "cancelled", "failed"):
        return False
    now = now_iso()
    await db.execute("UPDATE reply_runs SET status = 'cancelled', updated_at = ?, finished_at = ? WHERE id = ?", (now, now, run_id))
    await db.execute("UPDATE run_items SET status = 'skipped', detail = 'run cancelled', updated_at = ? WHERE run_id = ? AND status = 'queued'",
                     (now, run_id))
    for job in await db.query("SELECT id, payload_json FROM jobs WHERE kind = 'reply_run' AND status = 'queued'"):
        if json.loads(job.get("payload_json") or "{}").get("run_id") == run_id:
            await db.execute("UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE id = ?", (now, now, job["id"]))
    await db.log("info", "Reply run cancelled.", run_id=run_id)
    return True


async def run_summary(db: MailDB, run_id: str) -> dict:
    rows = await db.query("SELECT status, COUNT(*) AS n FROM run_items WHERE run_id = ? GROUP BY status", (run_id,))
    out = {"queued": 0, "drafting": 0, "needs_review": 0, "sent": 0, "dry_run": 0, "skipped": 0, "failed": 0}
    for r in rows:
        out[r["status"]] = r["n"]
    return out


async def _finish_if_done(db: MailDB, run_id: str):
    summary = await run_summary(db, run_id)
    done = sum(v for k, v in summary.items() if k not in ("queued", "drafting"))
    run = await db.one("SELECT status FROM reply_runs WHERE id = ?", (run_id,))
    if not run:
        return
    now = now_iso()
    if summary["queued"] + summary["drafting"] == 0 and run["status"] not in ("cancelled",):
        await db.execute("UPDATE reply_runs SET status = 'done', done = ?, summary_json = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                         (done, json.dumps(summary), now, now, run_id))
        parts = [f"{summary['sent']} sent"] if summary["sent"] else []
        if summary["dry_run"]:
            parts.append(f"{summary['dry_run']} dry run")
        if summary["needs_review"]:
            parts.append(f"{summary['needs_review']} for review")
        if summary["skipped"]:
            parts.append(f"{summary['skipped']} skipped")
        if summary["failed"]:
            parts.append(f"{summary['failed']} failed")
        full = await db.one("SELECT kind FROM reply_runs WHERE id = ?", (run_id,))
        what = "Follow-up run" if (full or {}).get("kind") == "reconnect" else "Reply run"
        await db.log("info", f"{what} finished: " + (", ".join(parts) or "nothing to do") + ".", run_id=run_id)
    else:
        await db.execute("UPDATE reply_runs SET done = ?, summary_json = ?, updated_at = ?, status = CASE WHEN status = 'queued' THEN 'running' ELSE status END WHERE id = ?",
                         (done, json.dumps(summary), now, run_id))


async def process_mailbox(db: MailDB, secret: str, env: dict, brain, jobs: Jobs | None, job_id: str,
                          run_id: str, mailbox_id: str, scope: dict | None = None) -> dict:
    """The worker's job: every queued item of this run for this mailbox,
    in order. Drafts, routes by mode and confidence, sends with pacing."""
    from bb.mail.brain import BrainError
    from bb.mail.drafting import draft_reconnect, draft_reply
    from bb.mail.sending import SendRefused, send_draft, sent_today

    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))
    mailbox = await db.mailbox(mailbox_id)
    if not run or not mailbox:
        return {"skipped": "run or mailbox gone"}
    reconnect = (run.get("kind") or "reply") == "reconnect"
    settings = service.settings_of(mailbox)
    # An item left 'drafting' by a worker that died mid-draft is picked up again.
    await db.execute("UPDATE run_items SET status = 'queued', updated_at = ? WHERE run_id = ? AND mailbox_id = ? AND status = 'drafting'",
                     (now_iso(), run_id, mailbox_id))
    items = await db.query("SELECT ri.*, t.subject, t.last_from FROM run_items ri JOIN threads t ON t.id = ri.thread_id "
                           "WHERE ri.run_id = ? AND ri.mailbox_id = ? AND ri.status = 'queued' ORDER BY t.last_message_at",
                           (run_id, mailbox_id))
    total_all = run["total"]
    counts = {"sent": 0, "needs_review": 0, "skipped": 0, "failed": 0, "dry_run": 0}
    sent_in_a_row = 0

    async def mark(item, status, detail="", draft_id=""):
        await db.execute("UPDATE run_items SET status = ?, detail = ?, draft_id = COALESCE(NULLIF(?, ''), draft_id), updated_at = ? WHERE id = ?",
                         (status, detail[:300], draft_id, now_iso(), item["id"]))
        if status in counts:
            counts[status] += 1
        await _finish_if_done(db, run_id)
        if jobs is not None:
            done_all = (await run_summary(db, run_id))
            done_n = sum(v for k, v in done_all.items() if k not in ("queued", "drafting"))
            await jobs.progress(job_id, phase="replying", done=done_n, total=total_all, run_id=run_id,
                                current=item.get("subject", ""))

    for item in items:
        fresh = await db.one("SELECT status FROM reply_runs WHERE id = ?", (run_id,))
        if not fresh or fresh["status"] == "cancelled":
            await mark(item, "skipped", "run cancelled")
            continue
        thread = await db.one("SELECT * FROM threads WHERE id = ?", (item["thread_id"],))
        if not thread or (not reconnect and thread["state"] != "needs_reply"):
            await mark(item, "skipped", "already answered or no longer needs a reply")
            continue
        if reconnect and thread["state"] in ("dismissed", "snoozed"):
            await mark(item, "skipped", "the thread was put away by hand")
            continue
        last_in = await db.one("SELECT from_email FROM messages WHERE thread_id = ? AND direction = 'inbound' "
                               "ORDER BY sent_at DESC, created_at DESC LIMIT 1", (thread["id"],))
        if last_in and is_excluded(settings, last_in["from_email"]):
            await mark(item, "skipped", f"{last_in['from_email']} is on this account's excluded list")
            continue
        if run["mode"] != "dry_run" and settings.daily_cap and await sent_today(db, mailbox_id) >= settings.daily_cap:
            if run["mode"] == "auto":
                await mark(item, "skipped", f"daily cap of {settings.daily_cap} reached; nothing drafted")
                continue
        await db.execute("UPDATE run_items SET status = 'drafting', updated_at = ? WHERE id = ?", (now_iso(), item["id"]))
        messages = await db.query("SELECT * FROM messages WHERE thread_id = ? ORDER BY sent_at, created_at", (thread["id"],))
        them = next((m for m in reversed(messages) if m["direction"] == "inbound" and (not reconnect or not m.get("automated"))), None)
        if not them:
            await mark(item, "skipped", "no message from them to answer")
            continue
        # A hard no anywhere in their side of the thread: nothing is sent,
        # now or ever. The address joins the excluded lists and the thread
        # is put away.
        hard_no = next((m for m in messages if m["direction"] == "inbound" and is_opt_out(m)), None)
        if hard_no:
            await suppress(db, mailbox, hard_no["from_email"], thread["id"])
            settings = service.settings_of(mailbox)        # the list just grew
            await mark(item, "skipped", f"{hard_no['from_email']} asked not to be contacted; added to the excluded list, nothing sent")
            continue
        if reconnect and item.get("contact"):
            twin = await db.one(
                "SELECT ri.id FROM run_items ri JOIN reply_runs r ON r.id = ri.run_id WHERE r.kind = 'reconnect' "
                "AND ri.contact = ? AND ri.run_id != ? AND ri.status IN ('sent', 'needs_review', 'dry_run') LIMIT 1",
                (item["contact"], run_id))
            if twin:
                await mark(item, "skipped", "already followed up with in another run")
                continue
        draft_scope = scope
        if scope and scope.get("mode") == "inquiry":
            # The prompt mentions the attached brief only when this very draft will carry it.
            from bb.mail import scope as scope_mod
            from bb.mail.sending import prior_replies
            root = (env or {}).get("HARVEY_HOME") or ""
            will_attach = bool(scope_mod.attachment_paths(root, scope, await prior_replies(db, thread["id"]))) if root else False
            draft_scope = {**scope, "attach_now": will_attach}
        try:
            if reconnect:
                result = await draft_reconnect(brain, settings, mailbox, thread, messages, scope=draft_scope,
                                               instructions=run.get("instructions") or "")
                if not result.get("suitable", True):
                    await mark(item, "skipped", f"not suitable: {result.get('why') or 'a personal or private thread'}")
                    continue
            else:
                result = await draft_reply(brain, settings, mailbox, thread, messages, instructions=run.get("instructions") or "",
                                           scope=draft_scope)
                if result.get("stance") == "opt_out":
                    # The model read a no-contact the regex missed: nothing is sent, and they are suppressed.
                    await suppress(db, mailbox, them["from_email"], thread["id"])
                    settings = service.settings_of(mailbox)
                    await mark(item, "skipped", f"{them['from_email']} asked not to be contacted (the model's reading); "
                                                "added to the excluded list, nothing sent")
                    continue
        except BrainError as e:
            await mark(item, "failed", f"drafting failed: {e}")
            continue
        except Exception as e:   # a provider outage or a bad key: this item fails, the run goes on
            await mark(item, "failed", f"drafting failed: {type(e).__name__}: {str(e)[:200]}")
            continue
        subject = thread["subject"] if thread["subject"].lower().startswith("re:") else f"Re: {thread['subject']}"
        # A reply answers their last message; a follow-up continues the thread
        # from its last message, ours or theirs, so it threads in their client.
        anchor = messages[-1] if reconnect and messages else them
        references = " ".join(x for x in [anchor.get("references_text") or "", anchor.get("message_id") or ""] if x).strip() \
            if reconnect else (them.get("references_text") or "")
        draft_id, now = new_id(), now_iso()
        await db.execute(
            "INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, reply_to, subject, body, in_reply_to, references_text, "
            "status, qa_json, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)",
            (draft_id, mailbox_id, thread["id"], run_id, json.dumps([{"name": them["from_name"], "email": them["from_email"]}]),
             run.get("reply_to") or settings.reply_to, subject[:200], result["body"], anchor.get("message_id") or "",
             references, json.dumps({**result["qa"], "attempts": result["attempts"],
                                     "confidence_reason": result["confidence_reason"]}),
             result["confidence"], now, now))
        confident = result["confidence"] == "high" and result["qa"].get("ok")
        if run["mode"] == "review" or (run["mode"] == "auto" and not confident):
            why = ("" if run["mode"] == "review" else
                   f"confidence {result['confidence']}" + (f": {result['confidence_reason']}" if result["confidence_reason"] else "")
                   if result["confidence"] != "high" else "failed the writing check: " + "; ".join(result["qa"].get("reasons", [])))
            await mark(item, "needs_review", why, draft_id)
            continue
        draft = await db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        await db.execute("UPDATE drafts SET status = 'approved', updated_at = ? WHERE id = ?", (now_iso(), draft_id))
        draft["status"] = "approved"
        try:
            if sent_in_a_row and run["mode"] != "dry_run":
                await asyncio.sleep(pace())
            out = await send_draft(db, secret, env, draft, dry_run=(run["mode"] == "dry_run"))
        except SendRefused as e:
            await db.execute("UPDATE drafts SET status = 'pending_review', error = ?, updated_at = ? WHERE id = ?",
                             (f"Not sent: {e}", now_iso(), draft_id))
            await mark(item, "needs_review", f"held: {e}", draft_id)
            continue
        except Exception as e:   # provider trouble: the draft stays approved for a retry by hand
            await db.execute("UPDATE drafts SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                             (f"{type(e).__name__}: {str(e)[:200]}", now_iso(), draft_id))
            await mark(item, "failed", f"{type(e).__name__}: {str(e)[:200]}", draft_id)
            continue
        if out.get("status") == "dry_run":
            await mark(item, "dry_run", "dry run: nothing sent", draft_id)
        else:
            sent_in_a_row += 1
            await mark(item, "sent", "", draft_id)
    await _finish_if_done(db, run_id)
    return {"run_id": run_id, **counts}
