"""Local SQLite regression tests for acknowledgement/review ordering.

No provider connections or real recipients are used.
"""
import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_late_ack_after_review_before_new_claim_is_ignored(tmp_path, kind):
    db = MailDB(tmp_path / "review_ack.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)
    assert await jobs.retry(job_id, delivery_reviewed=True)
    before = await db.one(
        "SELECT status, attempts, last_error, finished_at, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert before["status"] == "queued"
    await jobs.progress(job_id, stale=True)
    await jobs.complete(job_id, accepted=True)
    assert await jobs.fail(job_id, "stale failure") == "queued"
    after = await db.one(
        "SELECT status, attempts, last_error, finished_at, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert after == before
    assert (await jobs.claim())["id"] == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_completed_delivery_cannot_be_retried_or_overwritten(tmp_path, kind):
    db = MailDB(tmp_path / "completed_ack.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.complete(job_id, accepted=True)
    before = await db.one(
        "SELECT status, attempts, last_error, finished_at, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert before["status"] == "done"
    assert not await jobs.retry(job_id, delivery_reviewed=True)
    await jobs.progress(job_id, stale=True)
    await jobs.complete(job_id, accepted=False)
    assert await jobs.fail(job_id, "late failure") == "done"
    after = await db.one(
        "SELECT status, attempts, last_error, finished_at, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert after == before
