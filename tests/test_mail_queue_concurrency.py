"""SQLite-only tests for atomic claims and mailbox lane exclusion.

These tests do not connect to a provider or deliver messages.
"""
import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_concurrent_workers_claim_distinct_jobs(tmp_path):
    """Two independent workers cannot claim the same queued job."""
    db = MailDB(tmp_path / "concurrent.db")
    await db.init()
    first, second = Jobs(db), Jobs(db)
    job_id = await first.enqueue("check_mailbox", mailbox_id="one.test")

    claims = await asyncio.gather(first.claim(), second.claim())
    assert sorted(job["id"] for job in claims if job) == [job_id]
    assert sum(job is None for job in claims) == 1

    row = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "running"
    assert row["attempts"] == 1


@pytest.mark.asyncio
async def test_running_mailbox_blocks_second_claim_but_not_other_mailbox(tmp_path):
    """A busy mailbox does not block an independent mailbox."""
    db = MailDB(tmp_path / "lanes.db")
    await db.init()
    first, second = Jobs(db), Jobs(db)
    a = await first.enqueue("check_mailbox", mailbox_id="a.test")
    b = await first.enqueue("check_mailbox", mailbox_id="a.test")
    c = await first.enqueue("check_mailbox", mailbox_id="b.test")

    claimed_a = await first.claim()
    assert claimed_a and claimed_a["id"] == a
    claimed_c = await second.claim()
    assert claimed_c and claimed_c["id"] == c
    assert await first.claim() is None

    await first.complete(a)
    claimed_b = await second.claim()
    assert claimed_b and claimed_b["id"] == b
    assert claimed_b["attempts"] == 1


@pytest.mark.asyncio
async def test_distinct_explicit_lanes_on_same_mailbox_can_run_together(tmp_path):
    """Explicit lanes permit independent work on the same mailbox."""
    db = MailDB(tmp_path / "explicit_lanes.db")
    await db.init()
    jobs = Jobs(db)
    a = await jobs.enqueue("check_mailbox", mailbox_id="a.test", lane="scan")
    b = await jobs.enqueue("check_mailbox", mailbox_id="a.test", lane="campaign")
    assert (await jobs.claim())["id"] == a
    assert (await jobs.claim())["id"] == b
    assert await jobs.claim() is None
