"""Local-only race regressions for human-reviewed delivery queue transitions.

No provider clients are initialized; all work uses temporary SQLite databases.
"""
import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_concurrent_reviewed_retries_enqueue_only_once(tmp_path, kind):
    db = MailDB(tmp_path / "retry_race.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)

    # Simulate two independent review requests racing for the same dead job.
    results = await asyncio.gather(
        jobs.retry(job_id, delivery_reviewed=True),
        jobs.retry(job_id, delivery_reviewed=True),
    )
    assert sorted(results) == [False, True]
    row = await db.one(
        "SELECT status, attempts, last_error FROM jobs WHERE id = ?", (job_id,)
    )
    assert row == {
        "status": "queued",
        "attempts": 1,
        "last_error": "Retry queued after delivery review.",
    }
    reclaimed = await jobs.claim()
    assert reclaimed["id"] == job_id
    # A reviewed retry must advance the fencing generation, not reuse attempt 1.
    assert reclaimed["attempts"] == 2
    assert await jobs.claim() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_unreviewed_retry_cannot_race_reviewed_retry(tmp_path, kind):
    db = MailDB(tmp_path / "review_gate.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    await jobs.release(job_id)

    results = await asyncio.gather(
        jobs.retry(job_id, delivery_reviewed=False),
        jobs.retry(job_id, delivery_reviewed=True),
    )
    assert results == [False, True]
    assert (await db.one(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ))["status"] == "queued"
    assert (await jobs.claim())["id"] == job_id
    assert await jobs.claim() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send_draft", "calendar_invite"])
async def test_each_reviewed_delivery_retry_grants_only_one_attempt(tmp_path, kind):
    """A second manual review must not accidentally authorize automatic redelivery."""
    db = MailDB(tmp_path / "review_budget.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    first = await jobs.claim()
    assert first["attempts"] == 1
    assert await jobs.fail(job_id, "local mock failure", expected_attempt=1) == "dead"

    for expected in (2, 3):
        assert await jobs.retry(job_id, delivery_reviewed=True)
        claimed = await jobs.claim()
        assert claimed["attempts"] == expected
        assert claimed["max_attempts"] == expected
        assert await jobs.fail(job_id, "local mock failure", expected_attempt=expected) == "dead"
        assert await jobs.claim() is None
