"""SQLite-only regression tests for interrupted delivery job recovery.

No provider credentials, network access, or real email recipients are used.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_SEND_NOTE, UNCERTAIN_INVITE_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_note"),
    [
        ("send_draft", UNCERTAIN_SEND_NOTE),
        ("calendar_invite", UNCERTAIN_INVITE_NOTE),
    ],
)
async def test_stale_delivery_is_quarantined_and_late_worker_cannot_complete(
    tmp_path, kind, expected_note
):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="mailbox-1", payload={"test": True})
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id

    old_lock = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (old_lock, job_id))

    # A second worker's claim triggers recovery. It must not re-send.
    assert await jobs.claim() is None
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "dead"
    assert row["locked_at"] is None
    assert row["last_error"] == expected_note
    assert row["finished_at"]
    previous_progress = row["progress_json"]

    await jobs.progress(job_id, phase="late")
    await jobs.complete(job_id, delivered=True)
    assert await jobs.fail(job_id, "late error") == "dead"
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "dead"
    assert row["progress_json"] == previous_progress


@pytest.mark.asyncio
async def test_stale_read_job_recovers_without_resending_delivery(tmp_path):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)
    read_id = await jobs.enqueue("check_mailbox", mailbox_id="mailbox-1")
    assert (await jobs.claim())["id"] == read_id
    old_lock = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (old_lock, read_id))

    recovered = await jobs.claim()
    assert recovered is not None and recovered["id"] == read_id
    assert recovered["kind"] == "check_mailbox"
    assert recovered["attempts"] == 2
    await jobs.complete(read_id, recovered=True)
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (read_id,))
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_failed_read_job_uses_exponential_backoff_and_attempt_limit(tmp_path):
    """Retries must wait, cap their delay, and eventually dead-letter."""
    from bb.mail.jobs import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS

    db = MailDB(tmp_path / "backoff.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="mailbox-1", max_attempts=8)

    for attempt in range(1, 9):
        claimed = await jobs.claim()
        assert claimed is not None and claimed["id"] == job_id
        assert claimed["attempts"] == attempt
        before = datetime.now(timezone.utc)
        status = await jobs.fail(job_id, "temporary failure")
        row = await db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if attempt == 8:
            assert status == "dead"
            assert row["status"] == "dead"
            assert row["finished_at"]
            assert await jobs.claim() is None
            break
        assert status == "queued"
        assert row["status"] == "queued"
        expected = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        scheduled = datetime.fromisoformat(row["run_at"].replace("Z", "+00:00"))
        assert expected - 2 <= (scheduled - before).total_seconds() <= expected + 2
        assert await jobs.claim() is None  # Not due yet.
        await db.execute(
            "UPDATE jobs SET run_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job_id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_release_of_running_delivery_requires_manual_review(tmp_path, kind):
    db = MailDB(tmp_path / "release.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="mailbox-1")
    assert (await jobs.claim())["id"] == job_id

    await jobs.release(job_id)
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "dead"
    assert row["locked_at"] is None
    assert row["finished_at"]
    assert await jobs.claim() is None
    assert await jobs.retry(job_id, delivery_reviewed=False) is False
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (job_id,)))["status"] == "dead"
