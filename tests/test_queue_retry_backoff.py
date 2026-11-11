"""Deterministic SQLite queue retry tests; no network or mail transport."""

from datetime import datetime, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, Jobs


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_retry_backoff_doubles_and_caps_without_early_claim(tmp_path):
    db = MailDB(tmp_path / "backoff.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "reader.test", max_attempts=9)

    for attempt in range(1, 9):
        claimed = await jobs.claim()
        assert claimed is not None and claimed["id"] == job_id
        assert claimed["attempts"] == attempt
        before = datetime.now(timezone.utc)
        assert await jobs.fail(job_id, "transient local test failure") == "queued"
        after = datetime.now(timezone.utc)
        row = await db.one(
            "SELECT status, attempts, run_at, locked_at, last_error FROM jobs WHERE id = ?",
            (job_id,),
        )
        delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        assert row["status"] == "queued"
        assert row["attempts"] == attempt
        assert row["locked_at"] is None
        assert row["last_error"] == "transient local test failure"
        assert before.timestamp() + delay - 1 <= _parse(row["run_at"]).timestamp()
        assert _parse(row["run_at"]).timestamp() <= after.timestamp() + delay + 1
        assert await jobs.claim() is None
        # Advance the due time in the isolated database, never wait or send.
        await db.execute(
            "UPDATE jobs SET run_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (job_id,),
        )

    claimed = await jobs.claim()
    assert claimed is not None and claimed["attempts"] == 9
    assert await jobs.fail(job_id, "permanent local test failure") == "dead"
    dead = await db.one(
        "SELECT status, attempts, locked_at, finished_at, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert dead["status"] == "dead"
    assert dead["attempts"] == 9
    assert dead["locked_at"] is None
    assert dead["finished_at"] is not None
    assert dead["last_error"] == "permanent local test failure"
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_cancelled_job_requires_explicit_retry(tmp_path):
    db = MailDB(tmp_path / "discard.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "reader.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    assert await jobs.fail(job_id, "local test error") == "dead"
    assert await jobs.discard(job_id) is True
    assert await jobs.retry(job_id) is True  # explicit manual retry of cancelled work
    assert (await jobs.claim())["id"] == job_id


@pytest.mark.asyncio
async def test_stale_send_is_quarantined_but_stale_read_recovers(tmp_path):
    """Crash recovery must never silently replay a possibly accepted send."""
    db = MailDB(tmp_path / "crash-recovery.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender.test", payload={"draft_id": "local.test"})
    read_id = await jobs.enqueue("check_mailbox", "reader.test")
    claimed_ids = {(await jobs.claim())["id"], (await jobs.claim())["id"]}
    assert claimed_ids == {send_id, read_id}

    # Simulate worker death with expired locks, without waiting or sending.
    for job_id in (send_id, read_id):
        await db.execute(
            "UPDATE jobs SET locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (job_id,),
        )
    recovered = await jobs.claim()
    assert recovered is not None and recovered["id"] == read_id
    assert recovered["attempts"] == 2

    send = await db.one(
        "SELECT status, locked_at, finished_at, last_error FROM jobs WHERE id = ?",
        (send_id,),
    )
    assert send["status"] == "dead"
    assert send["locked_at"] is None
    assert send["finished_at"] is not None
    assert "may have succeeded" in send["last_error"]
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_release_interrupted_send_requires_manual_retry(tmp_path):
    db = MailDB(tmp_path / "release-recovery.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender.test")
    assert (await jobs.claim())["id"] == send_id
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
    assert await jobs.retry(send_id) is True
    retried = await jobs.claim()
    assert retried is not None and retried["id"] == send_id
    assert retried["attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", ["done", "dead", "cancelled"])
async def test_late_worker_failure_cannot_requeue_settled_job(tmp_path, settled):
    """A delayed error callback cannot resurrect a completed or held send."""
    db = MailDB(tmp_path / "late-failure.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("send_draft", "sender.test", {"draft_id": "local.test"})
    assert (await jobs.claim())["id"] == job_id
    if settled == "done":
        await jobs.complete(job_id, status="sent")
    elif settled == "dead":
        await jobs.release(job_id)
    else:
        await jobs.release(job_id)
        assert await jobs.discard(job_id) is True

    before = await db.one(
        "SELECT status, attempts, finished_at, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert before["status"] == settled
    assert await jobs.fail(job_id, "late mock transport timeout") == settled
    after = await db.one(
        "SELECT status, attempts, finished_at, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert after == before
    assert await jobs.claim() is None
