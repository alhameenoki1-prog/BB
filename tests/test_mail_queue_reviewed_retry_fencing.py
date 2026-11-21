"""Local-only regression: stale delivery workers cannot settle a reviewed retry."""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_INVITE_NOTE, UNCERTAIN_SEND_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "note"),
    [("send_draft", UNCERTAIN_SEND_NOTE), ("calendar_invite", UNCERTAIN_INVITE_NOTE)],
)
async def test_reviewed_retry_rejects_callbacks_from_original_attempt(tmp_path, kind, note):
    path = tmp_path / "reviewed_retry.db"
    await MailDB(path).init()
    original = Jobs(MailDB(path))
    replacement = Jobs(MailDB(path))
    job_id = await original.enqueue(kind, mailbox_id="owned-sender.test", max_attempts=1)

    first = await original.claim()
    assert first is not None and first["id"] == job_id
    assert first["attempts"] == 1

    stale_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await MailDB(path).execute(
        "UPDATE jobs SET locked_at = ? WHERE id = ?", (stale_time, job_id)
    )
    assert await replacement.claim() is None
    quarantined = await MailDB(path).one(
        "SELECT status, last_error FROM jobs WHERE id = ?", (job_id,)
    )
    assert quarantined["status"] == "dead"
    assert quarantined["last_error"] == note

    assert not await replacement.retry(job_id, delivery_reviewed=False)
    assert await replacement.retry(job_id, delivery_reviewed=True)
    second = await replacement.claim()
    assert second is not None and second["id"] == job_id
    assert second["attempts"] == 2

    await original.progress(job_id, expected_attempt=1, stale_progress=True)
    await original.complete(job_id, expected_attempt=1, stale_result=True)
    assert await original.fail(job_id, "late failure", expected_attempt=1) == "stale"
    await original.release(job_id, expected_attempt=1)

    still_running = await MailDB(path).one(
        "SELECT status, attempts, progress_json, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert still_running["status"] == "running"
    assert still_running["attempts"] == 2
    assert "stale_progress" not in (still_running["progress_json"] or "")
    assert "stale_result" not in (still_running["progress_json"] or "")
    assert still_running["last_error"] != "late failure"

    await replacement.complete(job_id, expected_attempt=2, acknowledged=True)
    finished = await MailDB(path).one(
        "SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (job_id,)
    )
    assert finished["status"] == "done"
    assert finished["attempts"] == 2
    assert '"acknowledged": true' in finished["progress_json"]
