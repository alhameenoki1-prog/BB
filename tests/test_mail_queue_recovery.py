"""SQLite-only regression tests for interrupted delivery job recovery.

No provider credentials, network access, or real email recipients are used.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_SEND_NOTE, UNCERTAIN_INVITE_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_note"),
    [
        ("send_draft", UNCERTAIN_SEND_NOTE),
        ("calendar_invite", UNCERTAIN_INVITE_NOTE),
    ],
)
async def test_stale_delivery_is_quarantined_and_late_worker_cannot_complete(
    tmp_path, kind, expected_note
):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="mailbox-1", payload={"test": True})
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id

    old_lock = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (old_lock, job_id))

    # A second worker's claim triggers recovery. It must not re-send.
    assert await jobs.claim() is None
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "dead"
    assert row["locked_at"] is None
    assert row["last_error"] == expected_note
    assert row["finished_at"]

    await jobs.progress(job_id, phase="late")
    await jobs.complete(job_id, delivered=True)
    assert await jobs.fail(job_id, "late error") == "dead"
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "dead"
    assert not row["progress_json"]


@pytest.mark.asyncio
async def test_stale_read_job_recovers_without_resending_delivery(tmp_path):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)
    read_id = await jobs.enqueue("check_mailbox", mailbox_id="mailbox-1")
    assert (await jobs.claim())["id"] == read_id
    old_lock = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (old_lock, read_id))

    recovered = await jobs.claim()
    assert recovered is not None and recovered["id"] == read_id
    assert recovered["kind"] == "check_mailbox"
    assert recovered["attempts"] == 2
    await jobs.complete(read_id, recovered=True)
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (read_id,))
    assert row["status"] == "done"
