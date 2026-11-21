"""Regression tests for late worker completion after queue crash recovery.

SQLite-only: no network calls or email delivery.
"""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_late_completion_cannot_override_uncertain_send_quarantine(tmp_path):
    db = MailDB(tmp_path / "late-completion.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(
        "send_draft", "sender.test", payload={"draft_id": "draft.test"}
    )
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id

    # A separate worker recovers the expired lock and quarantines the send.
    await db.execute(
        "UPDATE jobs SET locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (job_id,),
    )
    assert await jobs.claim() is None
    before = await db.one(
        "SELECT status, last_error, finished_at FROM jobs WHERE id = ?", (job_id,)
    )
    assert before["status"] == "dead"
    assert "may have succeeded" in before["last_error"]

    # The original worker must not mark a quarantined send as successfully done.
    await jobs.complete(job_id, acknowledged=True)
    after = await db.one(
        "SELECT status, last_error, finished_at FROM jobs WHERE id = ?", (job_id,)
    )
    assert after == before
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_late_completion_cannot_resurrect_cancelled_job(tmp_path):
    db = MailDB(tmp_path / "cancelled-completion.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "reader.test")
    assert await jobs.discard(job_id) is True

    await jobs.complete(job_id, acknowledged=True)
    row = await db.one(
        "SELECT status, finished_at FROM jobs WHERE id = ?", (job_id,)
    )
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
