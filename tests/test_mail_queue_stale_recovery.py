"""Local SQLite recovery invariants; never contacts an email provider."""

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
async def test_stale_delivery_is_quarantined_and_not_reclaimed(tmp_path, kind, expected_note):
    db = MailDB(tmp_path / "stale_delivery.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(kind, mailbox_id="sender.test", payload={"to": "recipient.test"})
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == job_id

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))

    assert await Jobs(MailDB(db.path)).claim() is None
    row = await db.one(
        "SELECT status, attempts, locked_at, last_error FROM jobs WHERE id = ?", (job_id,)
    )
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["locked_at"] is None
    assert row["last_error"] == expected_note
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_stale_safe_job_recovery_reclaims_once(tmp_path):
    db = MailDB(tmp_path / "stale_safe.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    claimed = await jobs.claim()
    assert claimed is not None and claimed["attempts"] == 1

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))

    recovered = await Jobs(MailDB(db.path)).claim()
    assert recovered is not None and recovered["id"] == job_id
    assert recovered["attempts"] == 2
    assert recovered["status"] == "running"
    assert await jobs.claim() is None
