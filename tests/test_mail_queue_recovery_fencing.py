"""Attempt-fencing regressions for recovered local jobs (SQLite only)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_recovered_safe_job_rejects_old_worker_callbacks(tmp_path):
    db = MailDB(tmp_path / "recovery_fencing.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", max_attempts=3)
    original = await jobs.claim()
    assert original["attempts"] == 1

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))
    recovered = await Jobs(MailDB(db.path)).claim()
    assert recovered is not None
    assert recovered["id"] == job_id
    assert recovered["attempts"] == 2

    await jobs.progress(job_id, expected_attempt=1, obsolete=True)
    await jobs.complete(job_id, expected_attempt=1, obsolete=True)
    assert await jobs.fail(job_id, "old failure", expected_attempt=1) == "stale"
    await jobs.release(job_id, expected_attempt=1)

    row = await db.one(
        "SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (job_id,)
    )
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert not json.loads(row["progress_json"] or "{}").get("obsolete")

    await jobs.progress(job_id, expected_attempt=2, recovered=True)
    await jobs.complete(job_id, expected_attempt=2, outcome="ok")
    row = await db.one(
        "SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (job_id,)
    )
    assert row["status"] == "done"
    assert row["attempts"] == 2
    assert json.loads(row["progress_json"]) == {"recovered": True, "outcome": "ok"}


@pytest.mark.asyncio
async def test_safe_manual_retry_grants_only_one_new_attempt(tmp_path):
    db = MailDB(tmp_path / "safe_retry_budget.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", max_attempts=1)
    for expected_attempt in (1, 2, 3):
        claimed = await jobs.claim()
        assert claimed is not None
        assert claimed["attempts"] == expected_attempt
        assert await jobs.fail(job_id, "local test failure", expected_attempt=expected_attempt) == "dead"
        assert await jobs.retry(job_id)
        row = await db.one("SELECT attempts, max_attempts, status FROM jobs WHERE id = ?", (job_id,))
        assert row["attempts"] == expected_attempt
        assert row["max_attempts"] == expected_attempt + 1
        assert row["status"] == "queued"
