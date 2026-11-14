"""SQLite-only delivery acknowledgement boundary tests; no network or mail sends."""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_completed_send_is_not_reclaimed_after_stale_lock(tmp_path):
    """A locally acknowledged send stays terminal even after a simulated crash."""
    db = MailDB(tmp_path / "ack.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue(
        "send_draft", "sender.test", payload={"draft_id": "message.test"}
    )
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == send_id
    assert claimed["attempts"] == 1
    await jobs.progress(send_id, transport="mock", accepted=True)
    await jobs.complete(send_id, acknowledged=True)

    # Simulate an old lock timestamp: recovery must be status-gated.
    await db.execute(
        "UPDATE jobs SET locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    assert await jobs.claim() is None
    row = await db.one(
        "SELECT status, attempts, progress_json, finished_at FROM jobs WHERE id = ?",
        (send_id,),
    )
    assert row["status"] == "done"
    assert row["attempts"] == 1
    assert row["finished_at"] is not None
    import json

    assert json.loads(row["progress_json"]) == {
        "transport": "mock", "accepted": True, "acknowledged": True
    }
    assert await jobs.retry(send_id) is False


@pytest.mark.asyncio
async def test_uncertain_send_requires_manual_retry_even_when_due(tmp_path):
    """Crash recovery cannot silently resend after an ambiguous remote ack."""
    db = MailDB(tmp_path / "uncertain.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender.test")
    assert (await jobs.claim())["id"] == send_id
    await db.execute(
        "UPDATE jobs SET locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    assert await jobs.claim() is None
    row = await db.one(
        "SELECT status, attempts, last_error FROM jobs WHERE id = ?", (send_id,)
    )
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert "may have succeeded" in row["last_error"]

    # A due timestamp alone must never override the human-review gate.
    await db.execute(
        "UPDATE jobs SET run_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    assert await jobs.claim() is None
    assert await jobs.retry(send_id) is True
    retried = await jobs.claim()
    assert retried is not None and retried["id"] == send_id
    assert retried["attempts"] == 1
