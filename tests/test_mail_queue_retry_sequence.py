"""Local SQLite regression for retry exhaustion and monotonic worker attempts."""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, Jobs


@pytest.mark.asyncio
async def test_retry_sequence_increases_delay_and_ends_in_dead_letter(tmp_path):
    db = MailDB(tmp_path / "retry_sequence.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="retry.test", max_attempts=9)

    for attempt in range(1, 10):
        claimed = await jobs.claim()
        assert claimed is not None and claimed["id"] == job_id
        assert claimed["attempts"] == attempt

        before = datetime.now(timezone.utc)
        result = await jobs.fail(job_id, "local test failure", expected_attempt=attempt)
        after = datetime.now(timezone.utc)
        row = await db.one("SELECT status, attempts, run_at FROM jobs WHERE id = ?", (job_id,))
        assert row["attempts"] == attempt

        if attempt == 9:
            assert result == "dead"
            assert row["status"] == "dead"
            assert await jobs.claim() is None
            return

        assert result == "queued"
        assert row["status"] == "queued"
        delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        due = datetime.fromisoformat(row["run_at"].replace("Z", "+00:00"))
        assert before + timedelta(seconds=delay - 1) <= due
        assert due <= after + timedelta(seconds=delay)
        assert await jobs.claim() is None
        await db.execute(
            "UPDATE jobs SET run_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (job_id,),
        )

    pytest.fail("retry exhaustion was not reached")
