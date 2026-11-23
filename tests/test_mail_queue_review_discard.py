"""SQLite-only races between human review and discard of uncertain deliveries.

No provider calls, credentials, or real email addresses are used.
"""
import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_review_and_discard_cannot_both_win(tmp_path, kind):
    db = MailDB(tmp_path / "review_discard_race.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)

    reviewed, discarded = await asyncio.gather(
        jobs.retry(job_id, delivery_reviewed=True),
        jobs.discard(job_id),
    )
    # A discard can win before review, or review can win before discard.
    # Neither path may leave a cancelled delivery available for a new claim.
    row = await db.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if reviewed and discarded:
        assert row["status"] == "cancelled"
    elif reviewed:
        assert row["status"] == "queued"
    else:
        assert discarded
        assert row["status"] == "cancelled"
    if row["status"] == "cancelled":
        assert not await jobs.retry(job_id, delivery_reviewed=True)
        assert await jobs.claim() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_discarded_uncertain_delivery_rejects_review_retry(tmp_path, kind):
    db = MailDB(tmp_path / "discarded_review.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)
    assert await jobs.discard(job_id)
    assert not await jobs.retry(job_id, delivery_reviewed=True)
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (job_id,)))["status"] == "cancelled"
