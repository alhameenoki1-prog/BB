"""SQLite-only tests: stale worker acknowledgements cannot undo delivery review.

No provider connections, secrets, or real email recipients are used.
"""
import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_late_worker_callbacks_cannot_change_quarantined_delivery(tmp_path, kind):
    db = MailDB(tmp_path / "late_ack.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)
    before = await db.one(
        "SELECT status, attempts, last_error, finished_at, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert before["status"] == "dead"
    await jobs.progress(job_id, submitted=True)
    await jobs.complete(job_id, accepted=True)
    assert await jobs.fail(job_id, "late transport error") == "dead"
    after = await db.one(
        "SELECT status, attempts, last_error, finished_at, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert after == before
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_discarded_delivery_cannot_be_retried_or_acknowledged(tmp_path):
    db = MailDB(tmp_path / "discarded.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("send_draft", mailbox_id="sender.test")
    assert await jobs.discard(job_id)
    assert not await jobs.retry(job_id, delivery_reviewed=True)
    await jobs.progress(job_id, submitted=True)
    await jobs.complete(job_id, accepted=True)
    assert await jobs.fail(job_id, "late failure") == "cancelled"
    row = await db.one(
        "SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (job_id,)
    )
    assert row["status"] == "cancelled"
    assert row["attempts"] == 0
    assert row["progress_json"] == "{}"
