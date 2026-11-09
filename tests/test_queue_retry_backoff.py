"""Deterministic SQLite queue retry tests; no network or mail transport."""

from datetime import datetime, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, Jobs


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_retry_backoff_doubles_and_caps_without_early_claim(tmp_path):
    db = MailDB(tmp_path / "backoff.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "reader.test", max_attempts=9)

    for attempt in range(1, 9):
        claimed = await jobs.claim()
        assert claimed is not None and claimed["id"] == job_id
        assert claimed["attempts"] == attempt
        before = datetime.now(timezone.utc)
        assert await jobs.fail(job_id, "transient local test failure") == "queued"
        after = datetime.now(timezone.utc)
        row = await db.one(
            "SELECT status, attempts, run_at, locked_at, last_error FROM jobs WHERE id = ?",
            (job_id,),
        )
        delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        assert row["status"] == "queued"
        assert row["attempts"] == attempt
        assert row["locked_at"] is None
        assert row["last_error"] == "transient local test failure"
        assert before.timestamp() + delay - 1 <= _parse(row["run_at"]).timestamp()
        assert _parse(row["run_at"]).timestamp() <= after.timestamp() + delay + 1
        assert await jobs.claim() is None
        # Advance the due time in the isolated database, never wait or send.
        await db.execute(
            "UPDATE jobs SET run_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (job_id,),
        )

    claimed = await jobs.claim()
    assert claimed is not None and claimed["attempts"] == 9
    assert await jobs.fail(job_id, "permanent local test failure") == "dead"
    dead = await db.one(
        "SELECT status, attempts, locked_at, finished_at, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert dead["status"] == "dead"
    assert dead["attempts"] == 9
    assert dead["locked_at"] is None
    assert dead["finished_at"] is not None
    assert dead["last_error"] == "permanent local test failure"
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_cancelled_job_requires_explicit_retry(tmp_path):
    db = MailDB(tmp_path / "discard.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", "reader.test", max_attempts=1)
    assert (await jobs.claim())["id"] == job_id
    assert await jobs.fail(job_id, "local test error") == "dead"
    assert await jobs.discard(job_id) is True
    assert await jobs.retry(job_id) is True  # explicit manual retry of cancelled work
    assert (await jobs.claim())["id"] == job_id
