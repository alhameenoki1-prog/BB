"""Local-only regression coverage for uncertain delivery after a lost worker.

No provider clients, credentials, network connections, or real recipients.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_INVITE_NOTE, UNCERTAIN_SEND_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_note"),
    [
        ("send_draft", UNCERTAIN_SEND_NOTE),
        ("calendar_invite", UNCERTAIN_INVITE_NOTE),
    ],
)
async def test_stale_delivery_requires_review_before_reclaim(tmp_path, kind, expected_note):
    db = MailDB(tmp_path / "uncertain_delivery.db")
    await db.init()
    jobs = Jobs(db)
    job_id = await jobs.enqueue(
        kind, mailbox_id="sender.test", payload={"recipient": "recipient.test"},
        max_attempts=1,
    )
    first = await jobs.claim()
    assert first["id"] == job_id
    assert first["attempts"] == 1

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, job_id))

    # A new worker must quarantine an ambiguous remote acknowledgement,
    # not automatically send a second copy.
    assert await Jobs(MailDB(db.path)).claim() is None
    row = await db.one(
        "SELECT status, attempts, max_attempts, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["max_attempts"] == 1
    assert row["last_error"] == expected_note

    # Late callbacks from the lost worker cannot settle a quarantined job.
    await jobs.complete(job_id, expected_attempt=1, delivered=True)
    assert await jobs.fail(job_id, "late failure", expected_attempt=1) == "dead"
    await jobs.release(job_id, expected_attempt=1)
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (job_id,)))["status"] == "dead"

    assert not await jobs.retry(job_id, delivery_reviewed=False)
    assert await jobs.claim() is None
    assert await jobs.retry(job_id, delivery_reviewed=True)
    second = await jobs.claim()
    assert second["id"] == job_id
    assert second["attempts"] == 2
    assert second["max_attempts"] == 2

    # The original worker may wake after review and a new claim. Its
    # attempt-scoped callbacks must not mutate or settle the new delivery.
    await jobs.progress(job_id, expected_attempt=1, obsolete=True)
    await jobs.complete(job_id, expected_attempt=1, delivered=True)
    assert await jobs.fail(job_id, "obsolete failure", expected_attempt=1) == "stale"
    await jobs.release(job_id, expected_attempt=1)
    row = await db.one(
        "SELECT status, attempts, progress_json, last_error FROM jobs WHERE id = ?",
        (job_id,),
    )
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert "obsolete" not in (row["progress_json"] or "")
    assert row["last_error"] == "Retry queued after delivery review."

    # Only the freshly claimed worker can acknowledge completion.
    await jobs.progress(job_id, expected_attempt=2, accepted=True)
    await jobs.complete(job_id, expected_attempt=2, delivered=True)
    row = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "done"
    assert '"accepted": true' in row["progress_json"]
    assert '"delivered": true' in row["progress_json"]
