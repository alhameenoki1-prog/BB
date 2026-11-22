"""SQLite-only regression coverage for retry scheduling and late acknowledgements.

Uses no network transports, credentials, or real recipients.
"""
from datetime import datetime, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import BACKOFF_CAP_SECONDS, Jobs


@pytest.mark.asyncio
async def test_retry_backoff_is_capped_for_high_attempt_count(tmp_path):
    db = MailDB(tmp_path / "backoff_cap.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", max_attempts=20)
    claimed = await jobs.claim()
    assert claimed["id"] == job_id
    # Simulate a long-lived non-delivery job with many previous attempts.
    await db.execute("UPDATE jobs SET attempts = 10 WHERE id = ?", (job_id,))
    before = datetime.now(timezone.utc)
    assert await jobs.fail(job_id, "temporary test failure", expected_attempt=10) == "queued"
    after = datetime.now(timezone.utc)
    row = await db.one("SELECT status, run_at, last_error FROM jobs WHERE id = ?", (job_id,))
    scheduled = datetime.fromisoformat(row["run_at"].replace("Z", "+00:00"))
    assert row["status"] == "queued"
    assert row["last_error"] == "temporary test failure"
    # Timestamp precision is seconds; allow rounding at either boundary.
    assert (before - scheduled).total_seconds() >= -(BACKOFF_CAP_SECONDS + 1)
    assert (scheduled - after).total_seconds() <= BACKOFF_CAP_SECONDS
    assert (scheduled - before).total_seconds() >= BACKOFF_CAP_SECONDS - 1
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_late_ack_cannot_override_backoff_queue(tmp_path):
    db = MailDB(tmp_path / "backoff_late_ack.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    claimed = await jobs.claim()
    assert claimed["id"] == job_id
    attempt = claimed["attempts"]
    assert await jobs.fail(job_id, "retry later", expected_attempt=attempt) == "queued"
    before = await db.one(
        "SELECT status, run_at, last_error, progress_json, finished_at FROM jobs WHERE id = ?",
        (job_id,),
    )
    await jobs.progress(job_id, expected_attempt=attempt, late=True)
    await jobs.complete(job_id, expected_attempt=attempt, late=True)
    assert await jobs.fail(job_id, "late failure", expected_attempt=attempt) == "queued"
    after = await db.one(
        "SELECT status, run_at, last_error, progress_json, finished_at FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert after == before
