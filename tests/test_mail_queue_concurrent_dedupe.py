"""Concurrent queue deduplication regressions using only local SQLite.

No provider clients, credentials, network sockets, or external recipients.
"""

import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_concurrent_dedupe_across_independent_connections(tmp_path):
    path = tmp_path / "concurrent_dedupe.db"
    await MailDB(path).init()

    # Independent Jobs/DB instances simulate separate local worker processes.
    workers = [Jobs(MailDB(path)) for _ in range(12)]
    ids = await asyncio.gather(*(
        worker.enqueue(
            "check_mailbox",
            mailbox_id="sender.test",
            lane="inbox",
            payload={"local": True},
            dedupe=True,
        )
        for worker in workers
    ))

    assert len(set(ids)) == 1
    rows = await MailDB(path).query(
        "SELECT id, status, attempts FROM jobs WHERE kind = ? AND mailbox_id = ? AND lane = ?",
        ("check_mailbox", "sender.test", "inbox"),
    )
    assert len(rows) == 1
    assert rows[0]["id"] == ids[0]
    assert rows[0]["status"] == "queued"
    assert rows[0]["attempts"] == 0


@pytest.mark.asyncio
async def test_dedupe_does_not_collapse_distinct_lanes_or_mailboxes(tmp_path):
    db = MailDB(tmp_path / "dedupe_scope.db")
    await db.init()
    jobs = Jobs(db)

    first = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="inbox", dedupe=True)
    same = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="inbox", dedupe=True)
    other_lane = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="calendar", dedupe=True)
    other_mailbox = await jobs.enqueue("check_mailbox", mailbox_id="other.test", lane="inbox", dedupe=True)
    other_kind = await jobs.enqueue("sync_contacts", mailbox_id="sender.test", lane="inbox", dedupe=True)

    assert same == first
    assert len({first, other_lane, other_mailbox, other_kind}) == 4
    rows = await db.query("SELECT id FROM jobs")
    assert len(rows) == 4
