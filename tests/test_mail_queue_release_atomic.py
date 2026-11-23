"""Local-only release transition tests; never connect to a mail provider."""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs, UNCERTAIN_SEND_NOTE


@pytest.mark.asyncio
async def test_release_quarantines_uncertain_delivery(tmp_path):
    db = MailDB(tmp_path / "release_delivery.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("send_draft", mailbox_id="sender.test", payload={"dry_run": True})
    claimed = await jobs.claim()
    assert claimed is not None and claimed["id"] == jid

    await jobs.release(jid, expected_attempt=claimed["attempts"])
    row = await db.one("SELECT status, attempts, locked_at, last_error FROM jobs WHERE id = ?", (jid,))
    assert row == {"status": "dead", "attempts": 1, "locked_at": None, "last_error": UNCERTAIN_SEND_NOTE}
    assert await jobs.claim() is None


@pytest.mark.asyncio
async def test_release_requeues_safe_job_with_new_claim_generation(tmp_path):
    db = MailDB(tmp_path / "release_safe.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    claimed = await jobs.claim()
    assert claimed is not None and claimed["attempts"] == 1

    await jobs.release(jid, "worker stopped", expected_attempt=claimed["attempts"])
    row = await db.one("SELECT status, attempts, locked_at, last_error FROM jobs WHERE id = ?", (jid,))
    assert row == {"status": "queued", "attempts": 1, "locked_at": None, "last_error": "worker stopped"}
    reclaimed = await jobs.claim()
    assert reclaimed is not None and reclaimed["id"] == jid and reclaimed["attempts"] == 2
