"""SQLite-only worker-claim concurrency tests; no provider connections."""

import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_parallel_claims_never_claim_same_job(tmp_path):
    db = MailDB(tmp_path / "parallel_claims.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")

    claims = await asyncio.gather(*(Jobs(MailDB(db.path)).claim() for _ in range(8)))
    claimed = [job for job in claims if job is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == job_id
    assert claimed[0]["attempts"] == 1
    row = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (job_id,))
    assert row == {"status": "running", "attempts": 1}


@pytest.mark.asyncio
async def test_parallel_claims_respect_lane_exclusivity_without_blocking_other_lanes(tmp_path):
    db = MailDB(tmp_path / "parallel_lanes.db")
    await db.init()
    jobs = Jobs(db)
    first = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="lane-a")
    second = await jobs.enqueue("sync_incremental", mailbox_id="sender.test", lane="lane-a")
    independent = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="lane-b")

    claims = await asyncio.gather(*(Jobs(MailDB(db.path)).claim() for _ in range(6)))
    claimed = [job for job in claims if job is not None]
    assert {job["id"] for job in claimed} == {first, independent}
    assert all(job["attempts"] == 1 for job in claimed)
    queued = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (second,))
    assert queued == {"status": "queued", "attempts": 0}

    await jobs.complete(first)
    next_job = await jobs.claim()
    assert next_job is not None and next_job["id"] == second
