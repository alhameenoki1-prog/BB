"""Local SQLite regression checks for cancellation and explicit delivery review.

No provider is initialized and no message is transmitted.
"""
import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_SEND_NOTE, UNCERTAIN_INVITE_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,note", [
    ("send_draft", UNCERTAIN_SEND_NOTE),
    ("calendar_invite", UNCERTAIN_INVITE_NOTE),
])
async def test_worker_release_quarantines_uncertain_delivery(tmp_path, kind, note):
    db = MailDB(tmp_path / "release.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id

    await jobs.release(job_id)
    row = await db.one("SELECT status, attempts, last_error, finished_at FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["last_error"] == note
    assert row["finished_at"] is not None
    assert await jobs.claim() is None
    assert not await jobs.retry(job_id, delivery_reviewed=False)
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (job_id,)))["status"] == "dead"


@pytest.mark.asyncio
async def test_safe_worker_release_requeues_without_spending_attempt(tmp_path):
    db = MailDB(tmp_path / "safe_release.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    assert (await jobs.claim())["attempts"] == 1
    await jobs.release(job_id)
    row = await db.one("SELECT status, attempts, last_error FROM jobs WHERE id = ?", (job_id,))
    assert row == {"status": "queued", "attempts": 0, "last_error": "worker stopped"}
    claimed = await jobs.claim()
    assert claimed["id"] == job_id
    assert claimed["attempts"] == 1


@pytest.mark.asyncio
async def test_reviewed_delivery_retry_is_explicit_and_single_claim(tmp_path):
    db = MailDB(tmp_path / "review.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("send_draft", mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)
    assert await jobs.retry(job_id, delivery_reviewed=True)
    row = await db.one("SELECT status, attempts, last_error, finished_at FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["last_error"] == "Retry queued after delivery review."
    assert row["finished_at"] is None
    assert (await jobs.claim())["id"] == job_id
    assert await jobs.claim() is None
