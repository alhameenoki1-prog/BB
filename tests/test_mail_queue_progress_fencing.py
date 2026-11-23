"""Local SQLite regression coverage for attempt-scoped sync progress."""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_stale_sync_progress_does_not_overwrite_reclaimed_attempt(tmp_path):
    db = MailDB(tmp_path / "progress_fencing.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("sync_incremental", mailbox_id="sender.test")
    first = await jobs.claim()
    assert first["attempts"] == 1
    await jobs.progress(jid, expected_attempt=first["attempts"], done=1, total=10)

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, jid))
    second = await jobs.claim()
    assert second["id"] == jid and second["attempts"] == 2
    await jobs.progress(jid, expected_attempt=second["attempts"], done=7, total=10)
    await jobs.progress(jid, expected_attempt=first["attempts"], done=2, total=10)

    row = await db.one("SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (jid,))
    import json
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert json.loads(row["progress_json"]) == {"done": 7, "total": 10}


@pytest.mark.asyncio
async def test_fenced_progress_ignores_settled_job(tmp_path):
    db = MailDB(tmp_path / "settled_progress.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("sync_incremental", mailbox_id="sender.test")
    claimed = await jobs.claim()
    await jobs.progress(jid, expected_attempt=claimed["attempts"], done=3)
    await jobs.complete(jid, expected_attempt=claimed["attempts"], ok=True)
    await jobs.progress(jid, expected_attempt=claimed["attempts"], done=99)
    row = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (jid,))
    import json
    assert row["status"] == "done"
    assert json.loads(row["progress_json"]) == {"done": 3, "ok": True}
