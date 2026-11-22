"""Local SQLite completion/progress regression tests; no real mail delivery."""

import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_completion_preserves_existing_progress_and_result(tmp_path):
    db = MailDB(tmp_path / "completion.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    claimed = await jobs.claim()
    assert claimed["id"] == job_id
    attempt = claimed["attempts"]
    await jobs.progress(job_id, expected_attempt=attempt, scanned=7, skipped=2)
    await jobs.complete(job_id, expected_attempt=attempt, processed=5)
    row = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (job_id,))
    import json
    assert row["status"] == "done"
    assert json.loads(row["progress_json"]) == {"scanned": 7, "skipped": 2, "processed": 5}
    await jobs.progress(job_id, expected_attempt=attempt, scanned=999)
    await jobs.complete(job_id, expected_attempt=attempt, processed=999)
    row = await db.one("SELECT progress_json FROM jobs WHERE id = ?", (job_id,))
    assert json.loads(row["progress_json"]) == {"scanned": 7, "skipped": 2, "processed": 5}


@pytest.mark.asyncio
async def test_concurrent_progress_and_completion_never_reopen_job(tmp_path):
    db = MailDB(tmp_path / "completion_race.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    claimed = await jobs.claim()
    assert claimed["id"] == job_id
    attempt = claimed["attempts"]
    await asyncio.gather(jobs.progress(job_id, expected_attempt=attempt, phase="checking"), jobs.complete(job_id, expected_attempt=attempt, finished=True))
    row = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (job_id,))
    import json
    assert row["status"] == "done"
    assert json.loads(row["progress_json"])["finished"] is True
