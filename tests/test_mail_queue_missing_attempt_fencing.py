"""Missing attempt tokens cannot mutate an active local queue job."""
import pytest
from bb.mail.db import MailDB
from bb.mail.jobs import Jobs

@pytest.mark.asyncio
async def test_missing_attempt_fails_closed_for_every_worker_transition(tmp_path):
    db = MailDB(tmp_path / "fencing.db")
    await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", mailbox_id="owner.test", max_attempts=2)
    claimed = await jobs.claim()
    assert claimed and claimed["id"] == jid and claimed["attempts"] == 1

    await jobs.progress(jid, unauthorized=True)
    await jobs.complete(jid, unauthorized=True)
    assert await jobs.fail(jid, "unauthorized failure") == "stale"
    await jobs.release(jid, "unauthorized release")

    row = await db.one("SELECT status, attempts, progress_json, last_error FROM jobs WHERE id = ?", (jid,))
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert "unauthorized" not in (row["progress_json"] or "")
    assert row["last_error"] in ("", None)

    await jobs.complete(jid, expected_attempt=1, authorized=True)
    done = await db.one("SELECT status, progress_json FROM jobs WHERE id = ?", (jid,))
    assert done["status"] == "done"
    assert '"authorized": true' in done["progress_json"]
