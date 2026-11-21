"""Local SQLite regression tests for atomic job failure transitions."""

import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_fail_retry_and_terminal_state_are_consistent(tmp_path):
    db = MailDB(tmp_path / "fail_atomic.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", mailbox_id="worker.test", max_attempts=2)
    first = await jobs.claim()
    assert first["id"] == jid and first["attempts"] == 1
    assert await jobs.fail(jid, "transient") == "queued"
    row = await db.one("SELECT status, attempts, last_error FROM jobs WHERE id = ?", (jid,))
    assert row == {"status": "queued", "attempts": 1, "last_error": "transient"}
    assert await jobs.fail(jid, "stale callback") == "queued"
    assert (await db.one("SELECT last_error FROM jobs WHERE id = ?", (jid,)))["last_error"] == "transient"
    await db.execute("UPDATE jobs SET run_at = '2000-01-01T00:00:00Z' WHERE id = ?", (jid,))
    second = await jobs.claim()
    assert second["id"] == jid and second["attempts"] == 2
    assert await jobs.fail(jid, "permanent") == "dead"
    assert await jobs.fail(jid, "late failure") == "dead"
    assert (await db.one("SELECT last_error FROM jobs WHERE id = ?", (jid,)))["last_error"] == "permanent"


@pytest.mark.asyncio
async def test_parallel_failures_only_transition_running_job_once(tmp_path):
    db = MailDB(tmp_path / "parallel_fail.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", mailbox_id="worker.test", max_attempts=3)
    assert (await jobs.claim())["id"] == jid
    outcomes = await asyncio.gather(
        Jobs(MailDB(db.path)).fail(jid, "failure-a"),
        Jobs(MailDB(db.path)).fail(jid, "failure-b"),
    )
    assert outcomes == ["queued", "queued"]
    row = await db.one("SELECT status, attempts, last_error FROM jobs WHERE id = ?", (jid,))
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["last_error"] in {"failure-a", "failure-b"}
    # This protects the read/transition race, not stale callbacks after a re-claim.
