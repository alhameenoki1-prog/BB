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
        if dedupe:
            row = await self.db.one(
                "SELECT id FROM jobs WHERE kind = ? AND mailbox_id = ? AND lane = ? AND status IN ('queued', 'running') LIMIT 1",
                (kind, mailbox_id, lane))
            if row:
                return row["id"]
        jid = new_id()
        now = datetime.now(timezone.utc)
        await self.db.execute(
            "INSERT INTO jobs(id, kind, mailbox_id, payload_json, status, run_at, max_attempts, created_at, updated_at, lane) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
            (jid, kind, mailbox_id, json.dumps(payload or {}), _iso(now + timedelta(seconds=delay_seconds)),
             max_attempts, _iso(now), _iso(now), lane))
        return jid

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
            await db.execute(
                "UPDATE jobs SET status = 'queued', locked_at = NULL, last_error = 'worker lost mid-run', "
                "updated_at = ? WHERE status = 'running' AND locked_at < ?", (now, stale))
            cur = await db.execute(
                f"""UPDATE jobs SET status = 'running', locked_at = ?, attempts = attempts + 1, updated_at = ?
                    WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_at <= ?{not_busy}
                                ORDER BY run_at LIMIT 1)
                    RETURNING *""", (now, now, now, *exclude))
            row = await cur.fetchone()
            cols = [c[0] for c in cur.description] if cur.description else []
            await db.commit()
        if not row:
            return None
        job = dict(zip(cols, row))
        job["payload"] = json.loads(job.get("payload_json") or "{}")
        return job

    async def progress(self, job_id: str, **progress):
        await self.db.execute("UPDATE jobs SET progress_json = ?, updated_at = ? WHERE id = ?",
                              (json.dumps(progress), now_iso(), job_id))

    async def complete(self, job_id: str, **result):
        """Mark done; the result is merged into the progress already recorded."""
        now = now_iso()
        row = await self.db.one("SELECT progress_json FROM jobs WHERE id = ?", (job_id,))
        try:
            progress = json.loads((row or {}).get("progress_json") or "{}")
        except ValueError:
            progress = {}
        progress.update(result)
        await self.db.execute(
            "UPDATE jobs SET status = 'done', locked_at = NULL, progress_json = ?, updated_at = ?, finished_at = ? "
            "WHERE id = ?", (json.dumps(progress), now, now, job_id))

    async def fail(self, job_id: str, error: str) -> str:
        """Retry with backoff, or mark dead after the last attempt. Returns
        the new status."""
        job = await self.db.one("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,))
        if not job:
            return "missing"
        now = datetime.now(timezone.utc)
        if job["attempts"] >= job["max_attempts"]:
            await self.db.execute(
                "UPDATE jobs SET status = 'dead', locked_at = NULL, last_error = ?, updated_at = ?, finished_at = ? "
                "WHERE id = ?", (error[:600], _iso(now), _iso(now), job_id))
            return "dead"
        wait = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (job["attempts"] - 1)))
        await self.db.execute(
            "UPDATE jobs SET status = 'queued', locked_at = NULL, last_error = ?, run_at = ?, updated_at = ? WHERE id = ?",
            (error[:600], _iso(now + timedelta(seconds=wait)), _iso(now), job_id))
        return "queued"

    async def release(self, job_id: str, note: str = "worker stopped") -> None:
        """Put a job back without counting the try: the worker stopped (a
        restart, a new AI key), not the job. It runs again when the worker
        is back."""
        await self.db.execute(
            "UPDATE jobs SET status = 'queued', locked_at = NULL, attempts = MAX(0, attempts - 1), last_error = ?, "
            "updated_at = ? WHERE id = ? AND status = 'running'", (note[:600], now_iso(), job_id))

    async def retry(self, job_id: str) -> bool:
        now = now_iso()
        n = await self.db.execute(
            "UPDATE jobs SET status = 'queued', attempts = 0, last_error = '', run_at = ?, updated_at = ?, "
            "finished_at = NULL WHERE id = ? AND status IN ('dead', 'cancelled')", (now, now, job_id))
        return n > 0

    async def discard(self, job_id: str) -> bool:
        now = now_iso()
        n = await self.db.execute(
            "UPDATE jobs SET status = 'cancelled', locked_at = NULL, updated_at = ?, finished_at = ? "
            "WHERE id = ? AND status IN ('queued', 'dead')", (now, now, job_id))
        return n > 0

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
