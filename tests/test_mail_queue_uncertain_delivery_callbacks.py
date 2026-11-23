"""Local-only regression for late callbacks after uncertain delivery quarantine."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_SEND_NOTE, UNCERTAIN_INVITE_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "note"),
    [("send_draft", UNCERTAIN_SEND_NOTE), ("calendar_invite", UNCERTAIN_INVITE_NOTE)],
)
async def test_late_delivery_callbacks_cannot_clear_uncertain_quarantine(tmp_path, kind, note):
    path = tmp_path / "uncertain_delivery.db"
    await MailDB(path).init()
    worker = Jobs(MailDB(path))
    observer = Jobs(MailDB(path))
    job_id = await worker.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    claimed = await worker.claim()
    assert claimed is not None and claimed["attempts"] == 1

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await MailDB(path).execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))
    assert await observer.claim() is None

    await worker.progress(job_id, expected_attempt=1, late=True)
    await worker.complete(job_id, expected_attempt=1, late=True)
    assert await worker.fail(job_id, "late failure", expected_attempt=1) == "dead"
    await worker.release(job_id, expected_attempt=1)

    row = await MailDB(path).one(
        "SELECT status, attempts, last_error, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["last_error"] == note
    assert not json.loads(row["progress_json"] or "{}").get("late")
    assert await observer.claim() is None
