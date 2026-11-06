"""Recovery behavior for interrupted SMTP/API sends.

All tests operate on a temporary SQLite database. No network services or
public recipients are involved.
"""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_lost_send_is_held_for_review_not_automatically_redelivered(tmp_path):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)

    uncertain = await jobs.enqueue(
        "send_draft", "box-a", {"draft_id": "outgoing-1"}, max_attempts=5
    )
    recoverable = await jobs.enqueue("check_mailbox", "box-b")
    for job_id in (uncertain, recoverable):
        await db.execute(
            "UPDATE jobs SET status = 'running', attempts = 1, "
            "locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (job_id,),
        )

    # A claim must recover safe work, but not replay an unknown send result.
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == recoverable
    unknown = await db.one("SELECT * FROM jobs WHERE id = ?", (uncertain,))
    assert unknown["status"] == "dead"
    assert unknown["locked_at"] is None
    assert unknown["finished_at"] is not None
    assert "may have succeeded" in unknown["last_error"]
    assert await jobs.claim() is None

    # A human can reconcile the Sent folder and deliberately retry the job.
    assert await jobs.retry(uncertain) is True
    retried = await jobs.claim()
    assert retried and retried["id"] == uncertain


@pytest.mark.asyncio
async def test_cancelled_send_is_held_but_read_only_work_is_requeued(tmp_path):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)

    send_id = await jobs.enqueue("send_draft", "box-a", {"draft_id": "d1"})
    claimed = await jobs.claim()
    assert claimed and claimed["id"] == send_id
    await jobs.release(send_id, "worker stopped")

    held = await db.one("SELECT status, last_error FROM jobs WHERE id = ?", (send_id,))
    assert held["status"] == "dead"
    assert "Check the mailbox Sent folder" in held["last_error"]
    assert await jobs.claim() is None

    check_id = await jobs.enqueue("check_mailbox", "box-b")
    check = await jobs.claim()
    assert check and check["id"] == check_id
    assert check["attempts"] == 1
    await jobs.release(check_id)
    recovered = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (check_id,))
    assert recovered == {"status": "queued", "attempts": 0}


@pytest.mark.asyncio
async def test_normal_pending_send_can_still_be_claimed(tmp_path):
    db = MailDB(tmp_path / "queue.db")
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "box-a", {"draft_id": "d1"})
    claim = await jobs.claim()
    assert claim and claim["id"] == send_id and claim["payload"]["draft_id"] == "d1"
    await jobs.complete(send_id, status="sent")
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (send_id,)))["status"] == "done"


def test_sender_send_window_daytime_boundaries():
    """Sending windows include endpoints and exclude off-hours."""
    from datetime import datetime
    from types import SimpleNamespace
    from bb.mail.sending import within_hours

    settings = SimpleNamespace(hours_start="09:00", hours_end="17:00")
    assert within_hours(settings, datetime(2026, 1, 1, 9, 0))
    assert within_hours(settings, datetime(2026, 1, 1, 17, 0))
    assert not within_hours(settings, datetime(2026, 1, 1, 8, 59))
    assert not within_hours(settings, datetime(2026, 1, 1, 17, 1))


def test_sender_send_window_crosses_midnight():
    """A window spanning midnight must not become an all-day permission."""
    from datetime import datetime
    from types import SimpleNamespace
    from bb.mail.sending import within_hours

    settings = SimpleNamespace(hours_start="22:00", hours_end="06:00")
    assert within_hours(settings, datetime(2026, 1, 1, 22, 0))
    assert within_hours(settings, datetime(2026, 1, 2, 2, 0))
    assert within_hours(settings, datetime(2026, 1, 2, 6, 0))
    assert not within_hours(settings, datetime(2026, 1, 1, 21, 59))
    assert not within_hours(settings, datetime(2026, 1, 2, 6, 1))
    assert not within_hours(settings, datetime(2026, 1, 2, 12, 0))


def test_sender_without_configured_window_is_unrestricted_by_hours():
    from datetime import datetime
    from types import SimpleNamespace
    from bb.mail.sending import within_hours

    settings = SimpleNamespace(hours_start="", hours_end="")
    assert within_hours(settings, datetime(2026, 1, 1, 3, 0))
