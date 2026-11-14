"""SQLite-only regression for concurrent deduplicated enqueue.

No provider access, network delivery, or real recipient addresses.
"""
import asyncio

import pytest

from bb.mail.db import MailDB
from bb.mail.jobs import Jobs


@pytest.mark.asyncio
async def test_concurrent_deduplicated_enqueue_creates_one_job(tmp_path):
    path = tmp_path / "dedupe_race.db"
    db = MailDB(path)
    await db.init()
    # Independent queue objects share a database, as separate workers do.
    first, second = Jobs(MailDB(path)), Jobs(MailDB(path))
    ids = await asyncio.gather(
        *(
            (first if index % 2 else second).enqueue(
                "check_mailbox",
                mailbox_id="sender.test",
                lane="dedupe-lane",
                dedupe=True,
            )
            for index in range(20)
        )
    )
    assert len(set(ids)) == 1
    rows = await db.query(
        "SELECT id FROM jobs WHERE kind = ? AND mailbox_id = ? AND lane = ?",
        ("check_mailbox", "sender.test", "dedupe-lane"),
    )
    assert len(rows) == 1
    assert rows[0]["id"] == ids[0]


@pytest.mark.asyncio
async def test_deduplication_is_scoped_to_kind_mailbox_and_lane(tmp_path):
    db = MailDB(tmp_path / "dedupe_scope.db")
    await db.init()
    jobs = Jobs(db)
    base = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="a", dedupe=True)
    assert await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="a", dedupe=True) == base
    other_lane = await jobs.enqueue("check_mailbox", mailbox_id="sender.test", lane="b", dedupe=True)
    other_kind = await jobs.enqueue("sync_mailbox", mailbox_id="sender.test", lane="a", dedupe=True)
    other_mailbox = await jobs.enqueue("check_mailbox", mailbox_id="other.test", lane="a", dedupe=True)
    assert len({base, other_lane, other_kind, other_mailbox}) == 4
