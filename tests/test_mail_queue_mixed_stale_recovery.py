"""SQLite-only regression for mixed stale worker recovery.

An interrupted delivery is ambiguous and must be quarantined; a safe read job
may be reclaimed. A quarantined delivery must not block its mailbox lane.
No transport is invoked.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_SEND_NOTE, UNCERTAIN_INVITE_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_kind,note", [
    ("send_draft", UNCERTAIN_SEND_NOTE),
    ("calendar_invite", UNCERTAIN_INVITE_NOTE),
])
async def test_stale_delivery_quarantine_frees_lane_for_safe_job(tmp_path, delivery_kind, note):
    db = MailDB(tmp_path / "mixed_recovery.db")
    await db.init()
    first_worker = Jobs(MailDB(db.path))
    second_worker = Jobs(MailDB(db.path))
    mailbox = "recovery.test"

    delivery_id = await first_worker.enqueue(
        delivery_kind, mailbox_id=mailbox, max_attempts=1
    )
    delivery = await first_worker.claim()
    assert delivery is not None and delivery["id"] == delivery_id
    assert delivery["attempts"] == 1

    safe_id = await second_worker.enqueue(
        "check_mailbox", mailbox_id=mailbox, max_attempts=3
    )
    assert await second_worker.claim() is None  # same mailbox lane is occupied

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace(
        "+00:00", "Z"
    )
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, delivery_id))

    safe = await second_worker.claim()
    assert safe is not None and safe["id"] == safe_id
    assert safe["attempts"] == 1

    quarantined = await db.one(
        "SELECT status, attempts, last_error, locked_at FROM jobs WHERE id = ?",
        (delivery_id,),
    )
    assert quarantined["status"] == "dead"
    assert quarantined["attempts"] == 1
    assert quarantined["last_error"] == note
    assert quarantined["locked_at"] is None

    await first_worker.complete(delivery_id, expected_attempt=1, delivered=True)
    assert await first_worker.fail(delivery_id, "late callback", expected_attempt=1) == "dead"
    assert await second_worker.claim() is None  # safe worker still owns the lane

    await second_worker.complete(safe_id, expected_attempt=1)
    final = await db.one(
        "SELECT status, last_error FROM jobs WHERE id = ?", (delivery_id,)
    )
    assert final["status"] == "dead"
    assert final["last_error"] == note
