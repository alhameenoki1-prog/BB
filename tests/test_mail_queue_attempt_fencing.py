"""Attempt-scoped acknowledgements use only a local SQLite database."""

from datetime import datetime, timedelta, timezone

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "fail", "release"])
async def test_stale_attempt_cannot_acknowledge_reclaimed_job(tmp_path, operation):
    db = MailDB(tmp_path / "attempts.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    first = await jobs.claim()
    assert first["attempts"] == 1

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale, jid))
    second = await jobs.claim()
    assert second["id"] == jid and second["attempts"] == 2

    if operation == "complete":
        await jobs.complete(jid, expected_attempt=first["attempts"], stale=True)
    elif operation == "fail":
        assert await jobs.fail(jid, "old failure", expected_attempt=first["attempts"]) == "stale"
    else:
        await jobs.release(jid, "old cancellation", expected_attempt=first["attempts"])

    row = await db.one("SELECT status, attempts, last_error FROM jobs WHERE id = ?", (jid,))
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert row["last_error"] == "worker lost mid-run"
    await jobs.complete(jid, expected_attempt=second["attempts"], ok=True)
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (jid,)))["status"] == "done"


@pytest.mark.asyncio
async def test_fenced_release_does_not_reuse_attempt_number(tmp_path):
    db = MailDB(tmp_path / "release.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", mailbox_id="sender.test")
    first = await jobs.claim()
    await jobs.release(jid, expected_attempt=first["attempts"])
    second = await jobs.claim()
    assert second["attempts"] > first["attempts"]
    await jobs.complete(jid, expected_attempt=first["attempts"], stale=True)
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (jid,)))["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["check_mailbox", "send_draft"])
async def test_manual_retry_never_reuses_worker_attempt(tmp_path, kind):
    """A late acknowledgement from before manual review cannot settle a new run."""
    db = MailDB(tmp_path / "manual-retry.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue(kind, mailbox_id="sender.test", max_attempts=1)
    first = await jobs.claim()
    assert first["attempts"] == 1
    assert await jobs.fail(jid, "test failure", expected_attempt=1) == "dead"
    assert await jobs.retry(jid, delivery_reviewed=True)
    second = await jobs.claim()
    assert second["attempts"] == 2
    assert second["max_attempts"] == 2

    await jobs.complete(jid, expected_attempt=first["attempts"], stale=True)
    assert await jobs.fail(jid, "stale error", expected_attempt=first["attempts"]) == "stale"
    await jobs.release(jid, expected_attempt=first["attempts"])
    await jobs.progress(jid, expected_attempt=first["attempts"], stale=True)
    row = await db.one("SELECT status, attempts, progress_json FROM jobs WHERE id = ?", (jid,))
    assert row["status"] == "running"
    assert row["attempts"] == 2
    assert not row["progress_json"] or "stale" not in row["progress_json"]

    await jobs.complete(jid, expected_attempt=second["attempts"], ok=True)
    assert (await db.one("SELECT status FROM jobs WHERE id = ?", (jid,)))["status"] == "done"
