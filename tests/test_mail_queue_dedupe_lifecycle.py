"""Local-only SQLite deduplication lifecycle regressions.

No provider clients, sockets, credentials, or real deliveries are used.
"""

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_dedupe_reuses_running_job_but_allows_new_job_after_completion(tmp_path):
    path = tmp_path / "dedupe_lifecycle.db"
    await MailDB(path).init()
    jobs = Jobs(MailDB(path))
    kwargs = dict(kind="check_mailbox", mailbox_id="sender.test", lane="inbox", dedupe=True)

    first = await jobs.enqueue(**kwargs)
    running = await jobs.claim()
    assert running is not None
    assert running["id"] == first
    assert running["attempts"] == 1

    # A claimed job is still active and must not be duplicated.
    assert await Jobs(MailDB(path)).enqueue(**kwargs) == first

    await jobs.complete(first, expected_attempt=running["attempts"])
    second = await jobs.enqueue(**kwargs)
    assert second != first
    assert await jobs.enqueue(**kwargs) == second

    rows = await MailDB(path).query(
        "SELECT id, status, attempts FROM jobs WHERE mailbox_id = ? AND lane = ? ORDER BY rowid",
        ("sender.test", "inbox"),
    )
    assert len(rows) == 2
    assert rows[0]["id"] == first
    assert rows[0]["status"] == "done"
    assert rows[0]["attempts"] == 1
    assert rows[1]["id"] == second
    assert rows[1]["status"] == "queued"
    assert rows[1]["attempts"] == 0
