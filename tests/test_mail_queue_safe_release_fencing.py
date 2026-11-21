"""SQLite-only regression for safe-job release attempt fencing."""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_safe_release_without_attempt_keeps_fencing_token(tmp_path):
    db = MailDB(tmp_path / "release_fencing.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("scan_mailbox", mailbox_id="sender.test", max_attempts=3)
    first = await jobs.claim()
    assert first["attempts"] == 1

    # A legacy caller may omit expected_attempt. Releasing must never
    # rewind the token and let that worker acknowledge the next claim.
    await jobs.release(job_id)
    queued = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (job_id,))
    assert queued["status"] == "queued"
    assert queued["attempts"] == 1

    second = await jobs.claim()
    assert second["id"] == job_id
    assert second["attempts"] == 2
    await jobs.complete(job_id, expected_attempt=1, stale=True)
    await jobs.progress(job_id, expected_attempt=1, stale=True)
    assert await jobs.fail(job_id, "stale", expected_attempt=1) == "stale"
    running = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (job_id,))
    assert running["status"] == "running"
    assert "stale" not in (running["progress_json"] or "")

    await jobs.complete(job_id, expected_attempt=2, accepted=True)
    done = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (job_id,))
    assert done["status"] == "done"
    assert '"accepted": true' in done["progress_json"]
