"""SQLite restart regression: uncertain delivery is never replayed automatically.

No network calls, live accounts, or external recipients are used.
"""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_restart_quarantines_uncertain_send_and_keeps_safe_work_available(tmp_path):
    path = tmp_path / "restart.db"
    original = MailDB(path)
    await original.init()
    first_worker = Jobs(original)
    send_id = await first_worker.enqueue(
        "send_draft", "sender.test", {"draft_id": "draft.test"}
    )
    safe_id = await first_worker.enqueue("check_mailbox", "reader.test")
    claimed = await first_worker.claim()
    assert claimed is not None and claimed["id"] == send_id
    assert claimed["attempts"] == 1

    # Simulate process loss: persist an expired lease, then create a new
    # database handle and worker rather than reusing the original instance.
    await original.execute(
        "UPDATE jobs SET locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    restarted = MailDB(path)
    await restarted.init()
    second_worker = Jobs(restarted)

    recovered = await second_worker.claim()
    assert recovered is not None and recovered["id"] == safe_id
    assert await second_worker.claim() is None

    held = await restarted.one(
        "SELECT status, attempts, locked_at, finished_at, last_error "
        "FROM jobs WHERE id = ?", (send_id,),
    )
    assert held["status"] == "dead"
    assert held["attempts"] == 1
    assert held["locked_at"] is None
    assert held["finished_at"] is not None
    assert "may have succeeded" in held["last_error"]

    # Repeated recovery checks must not silently release the send.
    assert await Jobs(MailDB(path)).claim() is None
    assert (await restarted.one(
        "SELECT status FROM jobs WHERE id = ?", (send_id,)
    ))["status"] == "dead"


@pytest.mark.asyncio
async def test_manual_retry_after_restart_is_explicit_and_single_claim(tmp_path):
    path = tmp_path / "manual-retry.db"
    db = MailDB(path)
    await db.init()
    jobs = Jobs(db)
    send_id = await jobs.enqueue("send_draft", "sender.test", {"draft_id": "draft.test"})
    assert (await jobs.claim())["id"] == send_id
    await db.execute(
        "UPDATE jobs SET locked_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (send_id,),
    )
    restarted = Jobs(MailDB(path))
    assert await restarted.claim() is None
    assert await restarted.retry(send_id) is True
    assert await restarted.retry(send_id) is False

    claimed = await restarted.claim()
    assert claimed is not None and claimed["id"] == send_id
    assert claimed["attempts"] == 1
    assert await restarted.claim() is None
