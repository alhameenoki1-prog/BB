"""Local-only regression tests for queue recovery boundaries and isolation."""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, STALE_LOCK_SECONDS


@pytest.mark.asyncio
async def test_recent_running_send_is_not_reclaimed_or_quarantined(tmp_path):
    db = MailDB(tmp_path / "recent.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender-a", {"draft_id": "only-local.test"})
    await db.execute(
        "UPDATE jobs SET status = 'running', attempts = 1, locked_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(seconds=STALE_LOCK_SECONDS - 60))
         .replace(microsecond=0).isoformat().replace("+00:00", "Z"), send_id),
    )
    assert await jobs.claim() is None
    row = await db.one("SELECT status, attempts, locked_at FROM jobs WHERE id = ?", (send_id,))
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert row["locked_at"] is not None


@pytest.mark.asyncio
async def test_stale_send_quarantine_does_not_block_other_mailbox(tmp_path):
    db = MailDB(tmp_path / "isolation.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender-a", {"draft_id": "local-only.test"})
    other_id = await jobs.enqueue("check_mailbox", "sender-b")
    await db.execute(
        "UPDATE jobs SET status = 'running', attempts = 1, "
        "locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == other_id
    quarantined = await db.one("SELECT status, last_error FROM jobs WHERE id = ?", (send_id,))
    assert quarantined["status"] == "dead"
    assert "may have succeeded" in quarantined["last_error"]
