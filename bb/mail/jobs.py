"""The job queue, in SQLite. A job is claimed with one atomic UPDATE so
two workers never run it twice; a failure schedules a retry with
exponential backoff; after ``max_attempts`` it is dead and stays visible
on the dead-letter page until retried or discarded by a person."""

import json
from datetime import datetime, timedelta, timezone

from bb.mail.db import MailDB, new_id, now_iso

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 30 * 60
STALE_LOCK_SECONDS = 15 * 60   # a job "running" this long was lost with its process
UNCERTAIN_SEND_NOTE = (
    "Send was interrupted; delivery may have succeeded. Check the mailbox Sent folder "
    "and recipient status before manually retrying to avoid duplicate email."
)
UNCERTAIN_INVITE_NOTE = (
    "The invitation may have been created. Check the organizer's Microsoft calendar before retrying."
)
DELIVERY_KINDS = ("send_draft", "calendar_invite")


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Jobs:
    def __init__(self, db: MailDB):
        self.db = db

    async def enqueue(self, kind: str, mailbox_id: str = "", payload: dict | None = None,
                      delay_seconds: int = 0, max_attempts: int = 5, dedupe: bool = False, lane: str = "") -> str:
        """Queue a job. With ``dedupe``, an identical queued or running job for
        the same mailbox (and lane) is reused instead of duplicated.

        ``lane`` is what may not run twice at once; it defaults to the
        mailbox. A campaign's run gets a lane of its own on its mailbox, so
        a reply campaign and a scan on one account go side by side."""
        jid = new_id()
        now = datetime.now(timezone.utc)
        async with self.db._connect() as db:
            if dedupe:
                # Serialize the lookup and insert across independent workers.
                # A pre-transaction SELECT lets concurrent enqueuers both see
                # no job and create duplicates.
                await db.execute("PRAGMA busy_timeout=15000")
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT id FROM jobs WHERE kind = ? AND mailbox_id = ? AND lane = ? "
                    "AND status IN ('queued', 'running') LIMIT 1",
                    (kind, mailbox_id, lane),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    await db.commit()
                    return row[0]
            await self._insert(db, jid, kind, mailbox_id, payload or {}, now, delay_seconds, max_attempts, lane)
            await db.commit()
        return jid

    @staticmethod
    async def _insert(db, jid, kind, mailbox_id, payload, now, delay_seconds, max_attempts, lane):
        await db.execute(
            "INSERT INTO jobs(id, kind, mailbox_id, payload_json, status, run_at, max_attempts, created_at, updated_at, lane) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
            (jid, kind, mailbox_id, json.dumps(payload), _iso(now + timedelta(seconds=delay_seconds)),
             max_attempts, _iso(now), _iso(now), lane))

    async def approve_draft(self, draft: dict, *, body: str, subject: str, reply_to: str,
                            dry_run: bool = False, saw_thread: bool = False, delay_seconds: int = 0) -> bool:
        """Approve and queue a pending draft in the same transaction."""
        now = datetime.now(timezone.utc)
        async with self.db._connect() as db:
            changed = await db.execute(
                "UPDATE drafts SET body = ?, subject = ?, reply_to = ?, status = 'approved', error = '', updated_at = ? "
                "WHERE id = ? AND status = 'pending_review'",
                (body, subject, reply_to, _iso(now), draft["id"]),
            )
            if not changed.rowcount:
                return False
            payload = {"draft_id": draft["id"], "dry_run": dry_run, "saw_thread": saw_thread}
            await self._insert(db, new_id(), "send_draft", draft["mailbox_id"], payload, now, delay_seconds, 1, "")
            await db.commit()
        return True

    async def approve_invite(self, invite: dict) -> bool:
        now, job_id = datetime.now(timezone.utc), new_id()
        async with self.db._connect() as db:
            changed = await db.execute(
                "UPDATE calendar_invites SET status = 'queued', job_id = ?, error = '', updated_at = ? "
                "WHERE id = ? AND status = 'pending_review'", (job_id, _iso(now), invite["id"]),
            )
            if not changed.rowcount:
                return False
            await self._insert(db, job_id, "calendar_invite", invite["mailbox_id"], {"invite_id": invite["id"]}, now, 0, 1, "")
            await db.commit()
        return True

    async def claim(self, exclude_mailboxes: set[str] | None = None, exclude_kinds=()) -> dict | None:
        """The next due job, marked running by the caller in one statement.
        Mailboxes already busy in this worker are skipped so one slow
        mailbox never blocks the others."""
        now = now_iso()
        exclude = sorted(exclude_mailboxes or set())
        marks = ",".join("?" for _ in exclude)
        not_busy = f" AND (CASE WHEN lane != '' THEN lane ELSE mailbox_id END) NOT IN ({marks})" if exclude else ""
        kinds = list(exclude_kinds or ())
        if kinds:
            not_busy += f" AND kind NOT IN ({','.join('?' for _ in kinds)})"
            exclude = exclude + kinds
        async with self.db._connect() as db:
            await db.execute("PRAGMA busy_timeout=15000")
            # Recover jobs whose worker died mid-run.
            stale = _iso(datetime.now(timezone.utc) - timedelta(seconds=STALE_LOCK_SECONDS))
            # An interrupted send is not idempotent: a remote SMTP/API
            # service may have accepted it just before the worker died.
            # Quarantine it in the existing dead-letter review UI rather than
            # automatically deliver the same message a second time.
            await db.execute(
                "UPDATE jobs SET status = 'dead', locked_at = NULL, last_error = CASE kind WHEN 'calendar_invite' THEN ? ELSE ? END, "
                "updated_at = ?, finished_at = ? "
                "WHERE status = 'running' AND kind IN (?, ?) AND locked_at < ?",
                (UNCERTAIN_INVITE_NOTE, UNCERTAIN_SEND_NOTE, now, now, *DELIVERY_KINDS, stale))
            await db.execute(
                "UPDATE jobs SET status = 'queued', locked_at = NULL, last_error = 'worker lost mid-run', "
                "updated_at = ? WHERE status = 'running' AND locked_at < ? AND kind NOT IN (?, ?)",
                (now, stale, *DELIVERY_KINDS))
            cur = await db.execute(
                f"""UPDATE jobs SET status = 'running', locked_at = ?, attempts = attempts + 1, updated_at = ?
                    WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_at <= ?{not_busy}
                                AND NOT EXISTS (SELECT 1 FROM jobs AS running WHERE running.status = 'running'
                                    AND COALESCE(NULLIF(running.lane, ''), running.mailbox_id) =
                                        COALESCE(NULLIF(jobs.lane, ''), jobs.mailbox_id))
                                ORDER BY run_at, rowid LIMIT 1)
                    RETURNING *""", (now, now, now, *exclude))
            row = await cur.fetchone()
            cols = [c[0] for c in cur.description] if cur.description else []
            await db.commit()
        if not row:
            return None
        job = dict(zip(cols, row))
        job["payload"] = json.loads(job.get("payload_json") or "{}")
        return job

    async def progress(self, job_id: str, *, expected_attempt: int | None = None, **progress):
        if expected_attempt is None:
            return  # Unfenced worker callbacks must not mutate a claimed job.
        # Ignore late progress from workers whose jobs were completed,
        # quarantined, or discarded during recovery. Do not alter audit
        # metadata for a settled delivery.
        await self.db.execute(
            "UPDATE jobs SET progress_json = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running' AND (? IS NULL OR attempts = ?)",
            (json.dumps(progress), now_iso(), job_id, expected_attempt, expected_attempt),
        )

    async def complete(self, job_id: str, *, expected_attempt: int | None = None, **result):
        """Mark done; the result is merged into the progress already recorded."""
        if expected_attempt is None:
            return  # Fail closed on missing worker attempt.
        now = now_iso()
        # Serialize the progress read and terminal transition. Otherwise a
        # concurrent progress callback can land between SELECT and UPDATE,
        # silently losing its most recent fields in the completed job.
        async with self.db._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT progress_json FROM jobs WHERE id = ? AND status = 'running' AND (? IS NULL OR attempts = ?)", (job_id, expected_attempt, expected_attempt)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.commit()
                return
            try:
                progress = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                progress = {}
            progress.update(result)
            await db.execute(
                "UPDATE jobs SET status = 'done', locked_at = NULL, progress_json = ?, updated_at = ?, finished_at = ? "
                "WHERE id = ? AND status = 'running' AND (? IS NULL OR attempts = ?)", (json.dumps(progress), now, now, job_id, expected_attempt, expected_attempt))
            await db.commit()

    async def fail(self, job_id: str, error: str, *, expected_attempt: int | None = None) -> str:
        """Atomically inspect the current attempt and schedule retry/death.

        A competing completion or release must not change state between the
        attempt-count read and this transition.
        """
        if expected_attempt is None:
            return "stale"  # Unfenced failure cannot transition a running job.
        now = datetime.now(timezone.utc)
        async with self.db._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT attempts, max_attempts, status FROM jobs WHERE id = ?", (job_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.commit()
                return "missing"
            attempts, max_attempts, status = row
            if status != "running" or (expected_attempt is not None and attempts != expected_attempt):
                await db.commit()
                return "stale" if status == "running" else status
            if attempts >= max_attempts:
                await db.execute(
                    "UPDATE jobs SET status = 'dead', locked_at = NULL, last_error = ?, updated_at = ?, finished_at = ? "
                    "WHERE id = ? AND status = 'running' AND (? IS NULL OR attempts = ?)",
                    (error[:600], _iso(now), _iso(now), job_id, expected_attempt, expected_attempt),
                )
                await db.commit()
                return "dead"
            wait = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)))
            await db.execute(
                "UPDATE jobs SET status = 'queued', locked_at = NULL, last_error = ?, run_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND (? IS NULL OR attempts = ?)",
                (error[:600], _iso(now + timedelta(seconds=wait)), _iso(now), job_id, expected_attempt, expected_attempt),
            )
            await db.commit()
            return "queued"

    async def release(self, job_id: str, note: str = "worker stopped", *, expected_attempt: int | None = None) -> None:
        """Put a job back without counting the try: the worker stopped (a
        restart, a new AI key), not the job. It runs again when the worker
        is back."""
        if expected_attempt is None:
            return  # Missing worker attempt must not release a claimed job.
        # A cancelled send could have already been accepted remotely.
        # Keep it in the dead-letter review instead of silently resending.
        current = now_iso()
        # Both transitions belong to one logical release. A competing claim
        # must not observe the delivery quarantine committed separately from
        # the safe-job requeue.
        async with self.db._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE jobs SET status = 'dead', locked_at = NULL, last_error = CASE kind WHEN 'calendar_invite' THEN ? ELSE ? END, "
                "updated_at = ?, finished_at = ? "
                "WHERE id = ? AND status = 'running' AND kind IN (?, ?) AND (? IS NULL OR attempts = ?)",
                (UNCERTAIN_INVITE_NOTE, UNCERTAIN_SEND_NOTE, current, current, job_id, *DELIVERY_KINDS, expected_attempt, expected_attempt))
            await db.execute(
                "UPDATE jobs SET status = 'queued', locked_at = NULL, last_error = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running' AND kind NOT IN (?, ?) AND (? IS NULL OR attempts = ?)",
                (note[:600], current, job_id, *DELIVERY_KINDS, expected_attempt, expected_attempt))
            await db.commit()

    async def retry(self, job_id: str, *, delivery_reviewed: bool = True) -> bool:
        """Retry one selected job after review, granting a fresh attempt budget.

        Each reviewed retry grants at most one new attempt beyond the current\n        count; unused retry budget is preserved. Keep attempts monotonic:\n        resetting it would let an old worker with
        expected_attempt=1 acknowledge a newly claimed attempt 1.
        """
        now = now_iso()
        n = await self.db.execute(
            "UPDATE jobs SET status = 'queued', max_attempts = MAX(max_attempts, attempts + 1), last_error = CASE WHEN kind IN (?, ?) "
            "THEN 'Retry queued after delivery review.' ELSE '' END, run_at = ?, updated_at = ?, "
            "finished_at = NULL WHERE id = ? AND status IN ('dead', 'cancelled') "
            "AND (kind NOT IN (?, ?) OR (? AND status = 'dead'))",
            (*DELIVERY_KINDS, now, now, job_id, *DELIVERY_KINDS, delivery_reviewed))
        return n > 0

    async def discard(self, job_id: str) -> bool:
        now = now_iso()
        async with self.db._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT kind, status, attempts, last_error, payload_json FROM jobs WHERE id = ? AND status IN ('queued', 'dead')",
                (job_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            kind, previous_status, attempts, last_error, payload = row
            await db.execute("UPDATE jobs SET status = 'cancelled', locked_at = NULL, updated_at = ?, finished_at = ? "
                             "WHERE id = ?", (now, now, job_id))
            if kind in DELIVERY_KINDS:
                unattempted = previous_status == "queued" and attempts == 0 and not last_error
                status = "pending_review" if unattempted else "failed"
                error = "Cancelled before submission." if unattempted else (
                    UNCERTAIN_SEND_NOTE if kind == "send_draft" else UNCERTAIN_INVITE_NOTE
                )
                table, previous, field = ("drafts", "approved", "draft_id") if kind == "send_draft" else (
                    "calendar_invites", "queued", "invite_id"
                )
                await db.execute(f"UPDATE {table} SET status = ?, error = ?, updated_at = ? WHERE id = ? AND status = ?",
                                 (status, error, now, json.loads(payload).get(field), previous))
            await db.commit()
        return True

    async def retryable(self, limit: int = 500) -> list[str]:
        rows = await self.db.query(
            "SELECT id FROM jobs WHERE status = 'dead' AND kind NOT IN (?, ?) ORDER BY updated_at LIMIT ?",
            (*DELIVERY_KINDS, limit),
        )
        return [row["id"] for row in rows]

    async def list(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            rows = await self.db.query("SELECT * FROM jobs WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                                       (status, limit))
        else:
            rows = await self.db.query("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r.get("payload_json") or "{}")
            try:
                r["progress"] = json.loads(r.get("progress_json") or "{}")
            except ValueError:
                r["progress"] = {}
        return rows

    async def counts(self) -> dict:
        rows = await self.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        out = {"queued": 0, "running": 0, "done": 0, "dead": 0, "cancelled": 0}
        for r in rows:
            out[r["status"]] = r["n"]
        return out

    async def prune(self, keep_days: int = 7):
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=keep_days))
        await self.db.execute("DELETE FROM jobs WHERE status IN ('done', 'cancelled') AND finished_at < ?", (cutoff,))
