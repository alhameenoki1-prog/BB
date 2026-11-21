"""Local-only regression for recovery of interrupted non-delivery work.

A recovered safe job must get a new attempt number so a delayed callback
from the abandoned worker cannot settle its replacement.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_recovered_safe_job_fences_abandoned_worker_callbacks(tmp_path):
    path = tmp_path / "safe_recovery.db"
    db = MailDB(path)
    await db.init()
    abandoned = Jobs(MailDB(path))
    recovered = Jobs(MailDB(path))
    job_id = await abandoned.enqueue(
        "check_mailbox", mailbox_id="recovery.test", max_attempts=3
    )

    first = await abandoned.claim()
    assert first is not None and first["id"] == job_id
    assert first["attempts"] == 1

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace(
        "+00:00", "Z"
    )
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))

    second = await recovered.claim()
    assert second is not None and second["id"] == job_id
    assert second["attempts"] == 2
    assert second["kind"] == "check_mailbox"

    await abandoned.progress(job_id, expected_attempt=1, abandoned=True)
    await abandoned.complete(job_id, expected_attempt=1, abandoned=True)
    assert await abandoned.fail(job_id, "late failure", expected_attempt=1) == "stale"
    await abandoned.release(job_id, expected_attempt=1)

    row = await db.one(
        "SELECT status, attempts, progress_json, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert "abandoned" not in (row["progress_json"] or "")
    assert row["last_error"] == "worker lost mid-run"

    await recovered.complete(job_id, expected_attempt=2, recovered=True)
    final = await db.one(
        "SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (job_id,)
    )
    assert final["status"] == "done"
    assert final["attempts"] == 2
    assert '"recovered": true' in final["progress_json"]
