"""Local SQLite concurrency tests for queue claim ownership.

All addresses are reserved .test identifiers. No SMTP/API connections are made.
"""

import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_parallel_claims_respect_lane_ownership_across_connections(tmp_path):
    """Only one running job per lane, even with independent queue clients."""
    path = tmp_path / "parallel_lane_claims.db"
    await MailDB(path).init()
    producer = Jobs(MailDB(path))
    # Multiple due jobs share a lane, but another lane can run concurrently.
    same_lane = [
        await producer.enqueue("check_mailbox", mailbox_id="sender.test", lane="inbox")
        for _ in range(5)
    ]
    other_lane = await producer.enqueue(
        "sync_contacts", mailbox_id="sender.test", lane="contacts"
    )
    workers = [Jobs(MailDB(path)) for _ in range(10)]
    claimed = await asyncio.gather(*(worker.claim() for worker in workers))
    running = [job for job in claimed if job is not None]

    assert len(running) == 2
    assert {job["id"] for job in running} == {same_lane[0], other_lane}
    assert {job["lane"] for job in running} == {"inbox", "contacts"}
    assert all(job["attempts"] == 1 for job in running)

    rows = await MailDB(path).query(
        "SELECT id, lane, status, attempts FROM jobs ORDER BY rowid"
    )
    assert len(rows) == 6
    assert sum(row["status"] == "running" for row in rows) == 2
    assert sum(row["status"] == "queued" for row in rows) == 4
    assert all(row["attempts"] == 0 for row in rows if row["status"] == "queued")

    # Releasing the owner must allow the next queued job to claim the lane.
    await producer.complete(same_lane[0], expected_attempt=1)
    next_job = await Jobs(MailDB(path)).claim()
    assert next_job is not None
    assert next_job["id"] == same_lane[1]
    assert next_job["lane"] == "inbox"
    assert next_job["attempts"] == 1
