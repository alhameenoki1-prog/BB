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
        "attempts": 0,
        "last_error": "Retry queued after delivery review.",
    }
    assert (await jobs.claim())["id"] == job_id
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
