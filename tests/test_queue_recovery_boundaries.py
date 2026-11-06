"""Local-only regression tests for queue recovery boundaries and isolation."""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, STALE_LOCK_SECONDS


@pytest.mark.asyncio
async def test_recent_running_send_is_not_reclaimed_or_quarantined(tmp_path):
    db = MailDB(tmp_path / "recent.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender-a", {"draft_id": "only-local.test"})
    await db.execute(
        "UPDATE jobs SET status = 'running', attempts = 1, locked_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=STALE_LOCK_SECONDS - 60))
         .replace(microsecond=0).isoformat().replace("+00:00", "Z"), send_id),
    )
    assert await jobs.claim() is None
    row = await db.one("SELECT status, attempts, locked_at FROM jobs WHERE id = ?", (send_id,))
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert row["locked_at"] is not None


@pytest.mark.asyncio
async def test_stale_send_quarantine_does_not_block_other_mailbox(tmp_path):
    db = MailDB(tmp_path / "isolation.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender-a", {"draft_id": "local-only.test"})
    other_id = await jobs.enqueue("check_mailbox", "sender-b")
    await db.execute(
        "UPDATE jobs SET status = 'running', attempts = 1, "
        "locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == other_id
    quarantined = await db.one("SELECT status, last_error FROM jobs WHERE id = ?", (send_id,))
    assert quarantined["status"] == "dead"
    assert "may have succeeded" in quarantined["last_error"]


@pytest.mark.asyncio
async def test_cancelled_running_send_requires_manual_review(tmp_path):
    """Stopping a worker must never silently replay an uncertain send."""
    db = MailDB(tmp_path / "cancelled-send.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender-a", {"draft_id": "local.test"})
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == send_id
    await jobs.release(send_id)
    row = await db.one(
        "SELECT status, attempts, locked_at, finished_at, last_error FROM jobs WHERE id = ?",
        (send_id,),
    )
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["locked_at"] is None
    assert row["finished_at"] is not None
    assert "may have succeeded" in row["last_error"]
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_cancelled_safe_job_can_be_reclaimed_without_extra_attempt(tmp_path):
    """A read-only job may resume after cancellation without burning a retry."""
    db = MailDB(tmp_path / "cancelled-safe.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "sender-a")
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id
    assert claimed["attempts"] == 1
    await jobs.release(job_id)
    queued = await db.one(
        "SELECT status, attempts, locked_at FROM jobs WHERE id = ?", (job_id,)
    )
    assert queued["status"] == "queued"
    assert queued["attempts"] == 0
    assert queued["locked_at"] is None
    reclaimed = await jobs.claim()
    assert reclaimed is not None and reclaimed["id"] == job_id
    assert reclaimed["attempts"] == 1


@pytest.mark.asyncio
async def test_stale_read_only_job_recovers_and_can_finish(tmp_path):
    """An abandoned mailbox scan is safe to retry, unlike an uncertain send."""
    db = MailDB(tmp_path / "stale-read.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "reader-a")
    await db.execute(
        "UPDATE jobs SET status = 'running', attempts = 1, "
        "locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (job_id,),
    )
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id
    assert claimed["attempts"] == 2
    assert claimed["status"] == "running"
    await jobs.complete(job_id, scanned=0)
    row = await db.one(
        "SELECT status, attempts, locked_at, finished_at, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert row["status"] == "done"
    assert row["attempts"] == 2
    assert row["locked_at"] is None
    assert row["finished_at"] is not None
    assert row["last_error"] == "worker lost mid-run"
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_uncertain_send_requires_explicit_retry_before_reclaim(tmp_path):
    """Recovery must never silently retry a possibly accepted delivery."""
    db = MailDB(tmp_path / "manual-send.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("send_draft", "sender-a", {"draft_id": "example.test"})
    await db.execute(
        "UPDATE jobs SET status = 'running', attempts = 1, "
        "locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (job_id,),
    )
    assert await jobs.claim() is None
    assert await jobs.claim() is None
    quarantined = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (job_id,))
    assert quarantined["status"] == "dead"
    assert quarantined["attempts"] == 1
    assert await jobs.retry(job_id) is True
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id
    assert claimed["attempts"] == 1


@pytest.mark.asyncio
async def test_concurrent_workers_cannot_claim_same_job(tmp_path):
    """Two independent workers must not both execute one queued delivery."""
    import asyncio

    db = MailDB(tmp_path / "concurrent-claim.db")
    await db.init()
    job_id = await Jobs(db).enqueue(
        "send_draft", "sender-a", {"draft_id": "claim-only.test"}
    )
    first, second = await asyncio.gather(Jobs(db).claim(), Jobs(db).claim())
    claimed = [job for job in (first, second) if job is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == job_id
    row = await db.one(
        "SELECT status, attempts FROM jobs WHERE id = ?", (job_id,)
    )
    assert row["status"] == "running"
    assert row["attempts"] == 1
