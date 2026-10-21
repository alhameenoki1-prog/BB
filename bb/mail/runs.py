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

from bb.mail import service
from bb.mail.db import MailDB, new_id, now_iso
from bb.mail.jobs import Jobs

MODES = ("review", "auto", "dry_run")
PACE_SECONDS = (20, 60)   # a random pause between two sends from one mailbox


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


async def create_run(db: MailDB, mailboxes: list[dict], thread_ids: list[str], mode: str = "review",
                     reply_to: str = "", instructions: str = "", created_by: str = "") -> dict:
    """Insert the run and its items, queue one job per mailbox. Returns the
    run row with ``per_mailbox`` counts; a run with no items is not created."""
    if mode not in MODES:
        mode = "review"
    boxes = {m["id"]: m for m in mailboxes}
    marks = ",".join("?" for _ in thread_ids) or "''"
    rows = await db.query(f"SELECT id, mailbox_id, last_from FROM threads WHERE id IN ({marks}) AND state = 'needs_reply'", thread_ids)
    rows = [r for r in rows if r["mailbox_id"] in boxes]
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
        await db.log("info", "Reply run finished: " + (", ".join(parts) or "nothing to do") + ".", run_id=run_id)
    else:
        await db.execute("UPDATE reply_runs SET done = ?, summary_json = ?, updated_at = ?, status = CASE WHEN status = 'queued' THEN 'running' ELSE status END WHERE id = ?",
                         (done, json.dumps(summary), now, run_id))


async def process_mailbox(db: MailDB, secret: str, env: dict, brain, jobs: Jobs | None, job_id: str,
                          run_id: str, mailbox_id: str, scope: dict | None = None) -> dict:
    """The worker's job: every queued item of this run for this mailbox,
    in order. Drafts, routes by mode and confidence, sends with pacing."""
    from bb.mail.brain import BrainError
    from bb.mail.drafting import draft_reply
    from bb.mail.sending import SendRefused, send_draft, sent_today

    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))
    mailbox = await db.mailbox(mailbox_id)
    if not run or not mailbox:
        return {"skipped": "run or mailbox gone"}
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
        if not thread or thread["state"] != "needs_reply":
            await mark(item, "skipped", "already answered or no longer needs a reply")
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
        them = next((m for m in reversed(messages) if m["direction"] == "inbound"), None)
        if not them:
            await mark(item, "skipped", "no message from them to answer")
            continue
        try:
            result = await draft_reply(brain, settings, mailbox, thread, messages, instructions=run.get("instructions") or "",
                                       scope=scope)
        except BrainError as e:
            await mark(item, "failed", f"drafting failed: {e}")
            continue
        except Exception as e:   # a provider outage or a bad key: this item fails, the run goes on
            await mark(item, "failed", f"drafting failed: {type(e).__name__}: {str(e)[:200]}")
            continue
        subject = thread["subject"] if thread["subject"].lower().startswith("re:") else f"Re: {thread['subject']}"
        draft_id, now = new_id(), now_iso()
        await db.execute(
            "INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, reply_to, subject, body, in_reply_to, references_text, "
            "status, qa_json, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)",
            (draft_id, mailbox_id, thread["id"], run_id, json.dumps([{"name": them["from_name"], "email": them["from_email"]}]),
             run.get("reply_to") or settings.reply_to, subject[:200], result["body"], them.get("message_id") or "",
             them.get("references_text") or "", json.dumps({**result["qa"], "attempts": result["attempts"],
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
