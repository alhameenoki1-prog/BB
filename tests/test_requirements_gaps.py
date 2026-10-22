"""Campaigns side by side, the gaps the requirements check found: the
command center's own cap is split between its runs, people who only wrote
in are answered by the reply campaign, a restarted outreach campaign never
answers again what another campaign answered, every earlier opt-out holds
everywhere, one closing note per no, a scan's stale follow-up gives way to
their new message, a night of quiet hours keeps a campaign's share, and a
new account that is still syncing says so."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bb import campaigns as campaigns_mod
from bb.mail import runs
from bb.mail.db import MailDB
from bb.mail.ingest import Ingest
from bb.mail.models import MailboxSettings
from bb.mail.providers.base import RawMessage
from bb.mail.sending import SendRefused, send_draft
from harvey.integrations.mail_provider import InboundMessage
from harvey.ledger import Ledger
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_mail_reply import SentBox
from tests.test_outbox_native import FakeProvider, StubBrain, make_sender, state  # noqa: F401
from tests.test_bb_web import client, settings  # noqa: F401

ME = "me@mine.example"


async def _box(tmp_path, **settings):
    db = MailDB(tmp_path / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok",
                               settings=MailboxSettings(signature_html="J", project_link="https://p.example", **settings).model_dump())
    return db, mid, await db.mailbox(mid)


async def _mail_in(db, box, n, when, body="Can we do Tuesday?"):
    await Ingest(db, box).store_batch([RawMessage(provider_id=f"in{n}", thread_key="T1", message_id=f"<a{n}@x>", from_email="ann@x.example",
                                                  from_name="Ann", subject="Site visit", body=body,
                                                  sent_at=when, to=[{"name": "", "email": ME}])])


async def _run(db, mid, kind, status="running", mode="auto", created="2026-01-01T00:00:00Z"):
    rid = f"r-{kind}-{status}"
    await db.execute("INSERT INTO reply_runs(id, mailbox_ids_json, mode, status, total, created_at, updated_at, kind) "
                     "VALUES (?, ?, ?, ?, 0, ?, ?, ?)", (rid, json.dumps([mid]), mode, status, created, created, kind))
    return rid


async def _sent(db, mid, rid, n):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(n):
        await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, status, sent_at, "
                         "created_at, updated_at) VALUES (?, ?, '', ?, 'reply', '[]', 's', 'b', 'sent', ?, ?, ?)",
                         (f"{rid}-{i}", mid, rid, today, today, today))


# ── 1. the command center's cap is split, like the account's limit ──

@pytest.mark.asyncio
async def test_a_scan_cannot_use_the_whole_cap_and_leave_the_reply_campaign_none(tmp_path):
    db, mid, box = await _box(tmp_path, daily_cap=4)
    scan = await _run(db, mid, "reconnect")
    replies = await _run(db, mid, "reply_campaign")
    await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, updated_at) VALUES ('i1', ?, ?, 't', 'queued', 'x')",
                     (scan, mid))
    await _sent(db, mid, scan, 2)                                   # the scan used its part of four
    full, why = await runs.cap_room(db, mid, scan, 4)
    assert not full and "part of the command center's daily cap of 4 is used (2 of 2)" in why
    assert await runs.cap_room(db, mid, replies, 4) == (False, "")  # the reply campaign still has its two
    await _sent(db, mid, replies, 2)
    full, why = await runs.cap_room(db, mid, replies, 4)
    assert full and "daily cap of 4 for this account is reached" in why
    # Sends by a run that is over (or by hand) are nobody's part: two live
    # runs never hold each other's part with the cap half used.
    await db.execute("DELETE FROM drafts")
    old = await _run(db, mid, "reconnect", status="done")
    await _sent(db, mid, old, 2)
    assert await runs.cap_room(db, mid, replies, 4) == (False, "")
    assert await runs.cap_room(db, mid, scan, 4) == (False, "")
    await db.execute("DELETE FROM drafts WHERE run_id = ?", (old,))
    # A scan that is done on this account holds nothing back.
    await db.execute("UPDATE run_items SET status = 'sent'")
    await db.execute("DELETE FROM drafts WHERE run_id = ?", (replies,))
    assert await runs.cap_room(db, mid, replies, 4) == (False, "")


@pytest.mark.asyncio
async def test_a_reply_campaign_waits_for_its_part_instead_of_dropping(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path, daily_cap=2)
    scan = await _run(db, mid, "reconnect")                          # a scan on the same account, with work left
    await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, updated_at) VALUES ('s1', ?, ?, 't', 'queued', 'x')",
                     (scan, mid))
    run = await runs.create_reply_campaign(db, box, mode="auto")
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    await _sent(db, mid, run["id"], 1)                               # its part of two
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    await runs.collect_campaign_threads(db, await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],)), box)
    out = await runs.process_mailbox(db, "s", {}, None, None, "j", run["id"], mid)
    assert "part of the command center's daily cap" in out["waiting"] and (await runs.run_summary(db, run["id"]))["queued"] == 1


# ── 2. people who only wrote in are the reply campaign's ──

def _campaign(tmp_path, cid="c-aaaa1", account=ME):
    campaigns_mod.upsert(tmp_path, {"id": cid, "name": "Spring", "account": account})
    root = campaigns_mod.campaign_root(tmp_path, cid)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "worker.autostart").write_text("1")
    return root


@pytest.mark.asyncio
async def test_inbox_correspondents_are_not_an_outreach_campaigns_leads(tmp_path):
    root = _campaign(tmp_path)
    sm = StateManager(str(root / "data" / "harvey.db")); await sm.init_db()
    await sm.add_prospect(Prospect(first_name="Lea", email="lead@x.example", email_status="provided", status="contacted"))
    await sm.add_prospect(Prospect(first_name="Sam", email="sam@x.example", source="inbox", status="replied"))
    assert campaigns_mod.outreach_owner(tmp_path, ME, "lead@x.example")["id"] == "c-aaaa1"
    # No reply campaign on the account: the outreach campaign answers its inbox itself.
    assert campaigns_mod.outreach_owner(tmp_path, ME, "sam@x.example")["id"] == "c-aaaa1"
    Ledger(campaigns_mod.ledger_path(tmp_path)).join(ME, "rc-1", "reply", "Replies")
    assert campaigns_mod.outreach_owner(tmp_path, ME, "sam@x.example") is None      # now the reply campaign's
    assert campaigns_mod.outreach_owner(tmp_path, ME, "lead@x.example")["id"] == "c-aaaa1"


@pytest.mark.asyncio
async def test_the_outreach_worker_leaves_inbox_people_to_the_reply_campaign(state, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_inbox_autoreply import _handler, _InboxBrain, _mail
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", "jane@acme.test")
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    Ledger(tmp_path / "sendlog.db").join("jane@acme.test", "reply-1", "reply", "Replies")
    await state.add_prospect(Prospect(first_name="Sam", email="sam@newclient.example", source="inbox", status="replied"))
    provider = FakeProvider()
    provider.inbound = [_mail(1, "sam@newclient.example", "Re: What do you do?", "One more question about timing.")]
    brain = _InboxBrain([])
    handler = _handler(state, provider, brain, inbox_autoreply=False)
    await handler._run_native()
    assert await state.get_outbox() == [] and brain.prompts == []


# ── 3. what another campaign answered is not answered again ──

def test_answered_messages_are_kept_by_message_id(tmp_path):
    led = Ledger(tmp_path / "sendlog.db")
    led.mark_answered(ME, "run-1", ["<A1@X>", "", "<b2@x>"])
    assert led.answered_by("<a1@x>") == "run-1" and led.answered_by("a1@x") == "run-1"
    led.mark_answered(ME, "c-1", ["<a1@x>"])
    assert led.answered_by("<a1@x>") == "run-1" and led.answered_by("<zz@x>") is None and led.answered_by("") is None


@pytest.mark.asyncio
async def test_the_command_center_notes_what_it_answered(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    now = "2026-01-02T00:00:00Z"
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, in_reply_to, status, "
                     "created_at, updated_at) VALUES ('d1', ?, ?, '', 'reply', ?, 'Re: Site visit', 'Tuesday works.', '<a1@x>', "
                     "'approved', ?, ?)", (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    await send_draft(db, "s", {"HARVEY_HOME": str(tmp_path)}, await db.one("SELECT * FROM drafts WHERE id = 'd1'"), pressed=True)
    assert Ledger(tmp_path / "data" / "sendlog.db").answered_by("<a1@x>") == "manual"


@pytest.mark.asyncio
async def test_a_restarted_outreach_campaign_does_not_answer_again(state, tmp_path, monkeypatch):  # noqa: F811
    from harvey.agents.handler import Handler
    from harvey.config import EnvConfig
    from tests.test_outreach import _config
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", ME)
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    led = Ledger(tmp_path / "sendlog.db")
    led.mark_answered(ME, "reply-run-7", ["<r1@x>"])     # the reply campaign answered while this one was stopped
    pid = await state.add_prospect(Prospect(first_name="Dana", email="dana@build.example", email_status="provided",
                                            company="Build Co", status="contacted"))
    later = (datetime.now(timezone.utc) + timedelta(days=3)).replace(tzinfo=None).isoformat()
    await state.add_outbox_item(prospect_id=pid, to_email="dana@build.example", subject="follow", body="b", send_at=later,
                                status="approved", campaign_id="cmp", step=2)
    handler = Handler(StubBrain(intent="question", reply="Sure."), state, _config(), EnvConfig())
    handler.provider = FakeProvider()
    await handler._process_reply("dana@build.example", "Yes, send the drawings.", reply_uuid="",
                                 reply_meta={"subject": "Re: Towson", "message_id": "<R1@X>"})
    items = await state.get_outbox()
    assert all(i["status"] == "cancelled" for i in items) and not [i for i in items if i["kind"] == "reply"]
    assert (await state.get_prospect(pid)).status == "replied"
    # Their next message is new: it is answered as usual.
    await handler._process_reply("dana@build.example", "Also, what about Thursday?", reply_uuid="",
                                 reply_meta={"subject": "Re: Towson", "message_id": "<r2@x>"})
    assert [i for i in await state.get_outbox() if i["kind"] == "reply"]


@pytest.mark.asyncio
async def test_a_queued_reply_answered_elsewhere_is_cancelled_not_sent(state, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", "harvey@x.co")
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    led = Ledger(tmp_path / "sendlog.db")
    pid = await state.add_prospect(Prospect(first_name="Jo", email="jo@acme.com", email_status="verified", status="replied"))
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    for n in (1, 2):
        await state.add_outbox_item(prospect_id=pid, to_email="jo@acme.com", subject="Re: hi", body="Thanks Jo, Tuesday works.",
                                    send_at=now, status="approved", kind="reply", in_reply_to=f"<m{n}@x>")
    led.mark_answered("harvey@x.co", "reply-run-7", ["<m1@x>"])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    items = {i["in_reply_to"]: i for i in await state.get_outbox()}
    assert items["<m1@x>"]["status"] == "cancelled" and "another campaign" in items["<m1@x>"]["error"]
    assert items["<m2@x>"]["status"] == "sent" and led.answered_by("<m2@x>") == "c-1"


# ── 4. every earlier opt-out holds everywhere ──

@pytest.mark.asyncio
async def test_earlier_opt_outs_are_copied_into_the_shared_list(tmp_path):
    root = _campaign(tmp_path)
    sm = StateManager(str(root / "data" / "harvey.db")); await sm.init_db()
    await sm.add_prospect(Prospect(first_name="No", email="No@Thanks.example", status="opted_out"))
    await sm.add_prospect(Prospect(first_name="Ok", email="ok@x.example", status="contacted"))
    removed = tmp_path / "campaigns" / ".removed" / "c-bbbb2" / "data"
    removed.mkdir(parents=True)
    old = StateManager(str(removed / "harvey.db")); await old.init_db()
    await old.add_prospect(Prospect(first_name="Gone", email="gone@x.example", status="opted_out"))
    # "Never auto-reply to" holds a boss, not an opt-out: never copied. A
    # thread the command center put away because they asked is one.
    db, mid, box = await _box(tmp_path, excluded=["boss@mine.example", "stop@x.example", "spam.example"])
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    await db.execute("UPDATE messages SET from_email = 'stop@x.example'")
    await db.execute("UPDATE threads SET state = 'dismissed', state_reason = 'they asked not to be contacted'")
    await db.log("warning", "stop@x.example asked not to be contacted: on the excluded list for me@mine.example; nothing is sent.", mid)
    led = Ledger(campaigns_mod.ledger_path(tmp_path))
    led.suppress("boss@mine.example", "on an account's excluded list", "backfill")   # what the first version copied
    assert campaigns_mod.backfill_suppressed(tmp_path) == 3
    assert led.suppressed_among(["no@thanks.example", "gone@x.example", "stop@x.example", "ok@x.example", "boss@mine.example"]) == \
        {"no@thanks.example", "gone@x.example", "stop@x.example"}
    assert campaigns_mod.backfill_suppressed(tmp_path) == 3          # again: nothing doubled, nothing lost
    # A person takes one off on purpose: the old record does not put it back,
    # but asking again does.
    led.unsuppress("gone@x.example")
    campaigns_mod.backfill_suppressed(tmp_path)
    assert not led.is_suppressed("gone@x.example")
    led.suppress("gone@x.example", "asked not to be contacted", "c-1")
    assert led.is_suppressed("gone@x.example")


# ── 5. one closing note per no ──

def test_a_second_no_after_the_closing_note_gets_nothing():
    cold = {"direction": "outbound", "body": "We need a GC for a 24-unit block in Towson."}
    no1 = {"direction": "inbound", "body": "Not interested, thanks."}
    note = {"direction": "outbound", "body": "Thanks for the straight answer. No action needed on your side."}
    no2 = {"direction": "inbound", "body": "Like I said, we're fully booked through next summer."}
    ask = {"direction": "inbound", "body": "Actually, could you send the drawings?"}
    assert runs.closed_after_no([cold, no1, note, no2], no2)
    assert not runs.closed_after_no([cold, no1], no1)                 # the first no gets its note
    assert not runs.closed_after_no([cold, no1, note, ask], ask)      # something else is answered
    assert not runs.closed_after_no([cold, no1, no2], no2)            # no note went yet
    # An out-of-office is not a no; a no that asks for something is answered.
    away = {"direction": "inbound", "automated": 1, "body": "I am out of the office and not at the moment checking email."}
    step2 = {"direction": "outbound", "body": "Following up on the Towson block."}
    real_no = {"direction": "inbound", "body": "Not interested, thanks."}
    assert not runs.closed_after_no([cold, away, step2, real_no], real_no)
    but = {"direction": "inbound", "body": "We can't take this on until March. Could you send the drawings anyway so we can price it?"}
    assert not runs.closed_after_no([cold, no1, note, but], but)


# ── 6. a scan's stale follow-up gives way to their new message ──

@pytest.mark.asyncio
async def test_a_new_message_supersedes_a_scans_waiting_follow_up(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2025-06-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    scan = await _run(db, mid, "reconnect", status="done", mode="review", created="2026-01-01T00:00:00Z")
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('f1', ?, ?, ?, 'reply', ?, 'Re: Site visit', 'Picking this up again.', 'pending_review', ?, ?)",
                     (mid, tid, scan, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, draft_id, updated_at) "
                     "VALUES ('i1', ?, ?, ?, 'needs_review', 'f1', '2026-01-01T00:00:00Z')", (scan, mid, tid))
    run = await runs.create_reply_campaign(db, box, mode="review")
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],))
    assert await runs.collect_campaign_threads(db, run, box) == 0     # the follow-up still waits; they have not written
    await _mail_in(db, box, 2, "2026-02-01T10:00:00Z", body="Good timing, we have a new project.")
    with pytest.raises(SendRefused, match="wrote again"):         # the scan never sends it now
        await send_draft(db, "s", {"HARVEY_HOME": str(tmp_path)}, await db.one("SELECT * FROM drafts WHERE id = 'f1'"))
    assert await runs.collect_campaign_threads(db, run, box) == 1     # their message is answered as a reply
    f1 = await db.one("SELECT status, error FROM drafts WHERE id = 'f1'")
    assert f1["status"] == "rejected" and "wrote again" in f1["error"]
    assert (await db.one("SELECT status FROM run_items WHERE id = 'i1'"))["status"] == "skipped"


# ── 7. quiet hours keep the campaign's share ──

@pytest.mark.asyncio
async def test_quiet_hours_check_in_keeps_the_campaign_on_the_account(tmp_path):
    import time as _t
    from harvey.agents.sender import COOLOFF_KEY, KILL_SWITCH_KEY
    from harvey.main import _ledger_checkin
    led = Ledger(tmp_path / "sendlog.db")
    sender = SimpleNamespace(ledger=led, ledger_account=ME, ledger_campaign="c-1", ledger_label="Spring")
    sm = StateManager(str(tmp_path / "h.db")); await sm.init_db()
    await _ledger_checkin(sender, sm)
    assert led.members(ME) == []                                      # never starts a membership
    led.join(ME, "c-1", "outreach", "Spring", now=_t.time() - 5 * 3600)   # its last send, five hours ago
    await _ledger_checkin(sender, sm)
    assert led.members(ME)[0]["active_until"] > _t.time() + 5 * 3600 # kept for the night
    # Sending paused, or standing down after a spam block: not kept.
    led.join(ME, "c-1", "outreach", "Spring", now=_t.time() - 5 * 3600)
    await sm.set_setting(KILL_SWITCH_KEY, "paused from the web app")
    await _ledger_checkin(sender, sm)
    assert led.members(ME)[0]["active_until"] < _t.time() + 3600
    await sm.set_setting(KILL_SWITCH_KEY, "")
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None).isoformat()
    await sm.set_setting(COOLOFF_KEY, later)
    await _ledger_checkin(sender, sm)
    assert led.members(ME)[0]["active_until"] < _t.time() + 3600
    await _ledger_checkin(SimpleNamespace(ledger=None))               # a worker on its own: nothing to do


# ── 8. a new account still syncing says so ──

def test_a_scan_on_an_account_still_syncing_says_so():
    assert "still reading its past mail" in runs.nobody_note({"address": ME, "sync": {}}, 6)
    assert "no past conversations to continue in the last 6 months" in \
        runs.nobody_note({"address": ME, "sync": {"backfill_done": True}}, 6)


# ── the second look at these fixes ──

@pytest.mark.asyncio
async def test_a_reply_marks_only_the_messages_it_could_have_seen(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    await _mail_in(db, box, 2, "2026-01-03T10:00:00Z", body="Can you confirm the price by Friday?")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, in_reply_to, status, "
                     "created_at, updated_at) VALUES ('d1', ?, ?, '', 'reply', ?, 'Re: Site visit', 'Tuesday works.', '<a1@x>', "
                     "'approved', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}])))
    # (Its thread was answered after it was written? No: their newer message is not an answer.)
    await send_draft(db, "s", {"HARVEY_HOME": str(tmp_path)}, await db.one("SELECT * FROM drafts WHERE id = 'd1'"), pressed=True)
    led = Ledger(tmp_path / "data" / "sendlog.db")
    assert led.answered_by("<a1@x>") == "manual" and led.answered_by("<a2@x>") is None


@pytest.mark.asyncio
async def test_a_scan_leaves_a_conversation_that_moved_on_and_is_finished_once(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2025-06-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    scan = await _run(db, mid, "reconnect", status="running", mode="auto", created="2026-01-01T00:00:00Z")
    await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, updated_at, contact) "
                     "VALUES ('i1', ?, ?, ?, 'queued', 'x', 'ann@x.example')", (scan, mid, tid))
    # After the scan started, they wrote and were answered: the thread waits on them now.
    await _mail_in(db, box, 2, "2026-01-02T10:00:00Z", body="Can you call me about the new job?")
    await db.execute("UPDATE threads SET state = 'awaiting_them'")
    out = await runs.process_mailbox(db, "s", {}, None, None, "j", scan, mid)
    assert out["sent"] == 0 and (await db.one("SELECT status, detail FROM run_items WHERE id = 'i1'"))["detail"].startswith(
        "the conversation moved on")
    done = await db.one("SELECT status, finished_at FROM reply_runs WHERE id = ?", (scan,))
    assert done["status"] == "done"
    finished = await db.query("SELECT text FROM activity WHERE text LIKE 'Follow-up run finished%'")
    await runs._finish_if_done(db, scan)                              # an item changes later: not finished again
    assert (await db.one("SELECT finished_at FROM reply_runs WHERE id = ?", (scan,)))["finished_at"] == done["finished_at"]
    assert len(await db.query("SELECT text FROM activity WHERE text LIKE 'Follow-up run finished%'")) == len(finished)


# ── the requirements check's second look ──

@pytest.mark.asyncio
async def test_a_draft_approved_on_review_is_not_a_second_answer(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    campaigns_mod.upsert(tmp_path, {"id": "c-aaaa1", "name": "Spring", "account": ME})
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    run = await runs.create_reply_campaign(db, box, mode="review")
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, in_reply_to, status, "
                     "created_at, updated_at) VALUES ('d1', ?, ?, ?, 'reply', ?, 'Re: Site visit', 'Tuesday works.', '<a1@x>', "
                     "'approved', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')",
                     (mid, tid, run["id"], json.dumps([{"name": "Ann", "email": "ann@x.example"}])))
    Ledger(tmp_path / "data" / "sendlog.db").mark_answered(ME, "c-aaaa1", ["<a1@x>"])   # the outreach campaign answered it
    from bb.mail.sending import AnsweredElsewhere
    with pytest.raises(AnsweredElsewhere, match="Spring"):
        await send_draft(db, "s", {"HARVEY_HOME": str(tmp_path)}, await db.one("SELECT * FROM drafts WHERE id = 'd1'"), pressed=True)


@pytest.mark.asyncio
async def test_a_review_mode_campaign_keeps_its_part_of_the_cap(tmp_path):
    db, mid, box = await _box(tmp_path, daily_cap=4)
    scan = await _run(db, mid, "reconnect")
    await db.execute("INSERT INTO run_items(id, run_id, mailbox_id, thread_id, status, updated_at) VALUES ('i1', ?, ?, 't', 'queued', 'x')",
                     (scan, mid))
    await _run(db, mid, "reply_campaign", mode="review")             # a person approves its replies
    await _sent(db, mid, scan, 2)
    full, why = await runs.cap_room(db, mid, scan, 4)
    assert not full and "(2 of 2)" in why


@pytest.mark.asyncio
async def test_a_stale_follow_up_gives_way_to_a_reply_run_too(tmp_path):
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2025-06-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    scan = await _run(db, mid, "reconnect", status="done", mode="review")
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('f1', ?, ?, ?, 'reply', '[]', 'Re: Site visit', 'Picking this up.', 'pending_review', "
                     "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (mid, tid, scan))
    assert tid in await runs.threads_in_live_runs(db)
    await _mail_in(db, box, 2, "2026-02-01T10:00:00Z", body="Is the job still open?")
    assert tid not in await runs.threads_in_live_runs(db)             # free for a reply run or the command bar


def test_a_no_answered_by_another_campaign_counts_as_answered():
    cold = {"direction": "outbound", "body": "We need a GC for a 24-unit block in Towson."}
    no1 = {"direction": "inbound", "body": "Not interested, thanks.", "message_id": "<n1@x>"}
    no2 = {"direction": "inbound", "body": "As I said, we're fully booked.", "message_id": "<n2@x>"}
    assert not runs.closed_after_no([cold, no1, no2], no2)
    assert runs.closed_after_no([cold, no1, no2], no2, frozenset({"<n1@x>"}))


def test_an_account_that_needs_signing_in_is_not_still_syncing():
    note = runs.nobody_note({"address": ME, "status": "reconnect", "sync": {}}, 6)
    assert "signed in again" in note and "still reading" not in note


@pytest.mark.asyncio
async def test_a_restarted_campaign_closes_a_no_the_reply_campaign_answered(state, tmp_path, monkeypatch):  # noqa: F811
    from harvey.agents.handler import Handler
    from harvey.config import EnvConfig
    from tests.test_no_and_reconnect import NEEDS
    from tests.test_outreach import _config
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", ME)
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    Ledger(tmp_path / "sendlog.db").mark_answered(ME, "reply-run-7", ["<n1@x>"])
    pid = await state.add_prospect(Prospect(first_name="Dana", email="dana@build.example", email_status="provided",
                                            company="Build Co", industry="construction", status="contacted"))
    cfg = _config(mode="inquiry", intent="Services for a 24-unit block in Towson.", sector_needs=NEEDS, ask="zoom")
    handler = Handler(StubBrain(intent="not_interested", reply="Thanks."), state, cfg, EnvConfig())
    handler.provider = FakeProvider()
    await handler._process_reply("dana@build.example", "Not interested, thanks.", reply_uuid="",
                                 reply_meta={"subject": "Re: Towson", "message_id": "<n1@x>"})
    assert (await state.get_prospect(pid)).status == "lost"
    await handler._process_reply("dana@build.example", "As I said, we're fully booked.", reply_uuid="",
                                 reply_meta={"subject": "Re: Towson", "message_id": "<n2@x>"})
    assert not [i for i in await state.get_outbox() if i["kind"] == "reply"]      # no second closing note


@pytest.mark.asyncio
async def test_answers_from_before_the_record_are_filled_in(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    root = _campaign(tmp_path)
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, in_reply_to, status, "
                     "sent_at, created_at, updated_at) VALUES ('d1', ?, '', 'run-9', 'reply', '[]', 's', 'b', '<a1@x>', 'sent', ?, ?, ?)",
                     (mid, recent, recent, recent))
    sm = StateManager(str(root / "data" / "harvey.db")); await sm.init_db()
    p = await sm.add_prospect(Prospect(first_name="Jo", email="jo@x.example", status="replied"))
    item = await sm.add_outbox_item(prospect_id=p, to_email="jo@x.example", subject="Re: hi", body="b", send_at=recent,
                                    status="approved", kind="reply", in_reply_to="<j1@x>")
    await sm.update_outbox_item(item, status="sent", sent_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    assert campaigns_mod.backfill_answered(tmp_path) == 2
    led = Ledger(campaigns_mod.ledger_path(tmp_path))
    assert led.answered_by("<a1@x>") == "run-9" and led.answered_by("<j1@x>") == "c-aaaa1"


# ── the third look ──

def test_a_no_is_a_no_and_a_request_is_a_request():
    from harvey import enquiry
    nos = ["No thanks. Please don't send anything else.", "Not interested, we don't quote residential work.",
           "Not interested. Why do you keep writing to us?", "We'll pass. Please don't forward my details to anyone.",
           "Not right now, let me know if anything changes."]
    asks = ["We can't take this on until March. Could you send the drawings anyway so we can price it?",
            "Not right now, but please send the full spec and we will quote next quarter.",
            "Not right now, but could you send me the pricing sheet?"]
    assert not any(enquiry.asks_for_something(t) for t in nos)
    assert all(enquiry.asks_for_something(t) for t in asks)
    cold = {"direction": "outbound", "body": "We need a GC for a 24-unit block in Towson."}
    no1 = {"direction": "inbound", "body": "Not interested, thanks."}
    note = {"direction": "outbound", "body": "Thanks for the straight answer."}
    for t in nos[:4]:
        again = {"direction": "inbound", "body": t}
        if enquiry.is_soft_no(t):
            assert runs.closed_after_no([cold, no1, note, again], again), t


@pytest.mark.asyncio
async def test_a_scan_follows_up_people_who_only_wrote_in(tmp_path):
    root = _campaign(tmp_path)
    sm = StateManager(str(root / "data" / "harvey.db")); await sm.init_db()
    await sm.add_prospect(Prospect(first_name="Sam", email="sam@x.example", source="inbox", status="replied"))
    await sm.add_prospect(Prospect(first_name="Lea", email="lead@x.example", email_status="provided", status="contacted"))
    assert campaigns_mod.outreach_owner(tmp_path, ME, "sam@x.example")["id"] == "c-aaaa1"              # answering: the campaign's
    assert campaigns_mod.outreach_owner(tmp_path, ME, "sam@x.example", for_answers=False) is None        # a scan may follow him up
    assert campaigns_mod.outreach_owner(tmp_path, ME, "lead@x.example", for_answers=False)["id"] == "c-aaaa1"


@pytest.mark.asyncio
async def test_the_back_fill_takes_only_the_person_who_asked(tmp_path):
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    await db.execute("UPDATE threads SET state = 'dismissed', state_reason = 'they asked not to be contacted'")
    await db.log("warning", "ann@x.example asked not to be contacted: on the excluded list for me@mine.example; nothing is sent.", mid)
    await Ingest(db, box).store_batch([RawMessage(provider_id="in9", thread_key="T1", message_id="<pa@x>", from_email="pa@x.example",
                                                  from_name="PA", subject="Site visit", body="Forwarding to Ann.",
                                                  sent_at="2025-12-31T10:00:00Z", to=[{"name": "", "email": ME}])])
    campaigns_mod.backfill_suppressed(tmp_path)
    led = Ledger(campaigns_mod.ledger_path(tmp_path))
    assert led.is_suppressed("ann@x.example") and not led.is_suppressed("pa@x.example")


@pytest.mark.asyncio
async def test_a_stale_settings_page_never_lifts_a_new_opt_out(client, monkeypatch):
    """Opened before ann asked to be removed, saved after: ann stays out."""
    import asyncio as _asyncio
    from bb.accounts import Accounts
    from bb.workspaces import Workspace
    from tests.test_bb_web import USER, _as_user
    from tests.test_mail_foundation import _FakeAccount
    monkeypatch.setattr("bb.mail.service.account_for", lambda m, c, e: _FakeAccount(m, c, e))
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    client.post("/mail/accounts/imap", data={"email": ME, "password": "pw"})
    db = ws.mail_db()
    box = (await db.mailboxes())[0]
    page = client.get(f"/mail/accounts/{box['id']}").text
    assert 'name="excluded_shown"' in page
    await runs.suppress(db, box, "ann@x.example")                       # while the page was open
    r = client.post(f"/mail/accounts/{box['id']}", data={"display_name": "Jane", "signature_html": "Jane",
                                                         "project_link": "https://p.example", "excluded": "", "excluded_shown": ""})
    assert r.status_code == 303
    box = await db.mailbox(box["id"])
    assert "ann@x.example" in box["settings"]["excluded"]                # kept, not dropped by the old page
    assert ws.ledger().is_suppressed("ann@x.example")
    # Taking it off on purpose (it was on the page) lifts it.
    client.post(f"/mail/accounts/{box['id']}", data={"display_name": "Jane", "signature_html": "Jane",
                                                     "project_link": "https://p.example", "excluded": "",
                                                     "excluded_shown": "ann@x.example"})
    assert not ws.ledger().is_suppressed("ann@x.example")


@pytest.mark.asyncio
async def test_approve_all_never_sends_a_follow_up_they_wrote_back_to(tmp_path, monkeypatch):
    """Review shows no thread: a scan's follow-up from before their new
    message is refused there, and put away rather than left on Review."""
    from bb.mail.jobs import Jobs
    from bb.mail.worker import Handlers
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2025-06-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    scan = await _run(db, mid, "reconnect", status="done", mode="review")
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('f1', ?, ?, ?, 'reply', ?, 'Re: Site visit', 'Picking this up.', 'approved', "
                     "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (mid, tid, scan, json.dumps([{"name": "Ann", "email": "ann@x.example"}])))
    await _mail_in(db, box, 2, "2026-02-01T10:00:00Z", body="We signed with another firm, please hold off.")
    h = Handlers(db, Jobs(db), "s", {"HARVEY_HOME": str(tmp_path)}, tmp_path)
    out = await h.send_draft({"payload": {"draft_id": "f1"}, "mailbox_id": mid})       # pressed, but not from the thread page
    assert out["status"] == "rejected"
    d = await db.one("SELECT status, error FROM drafts WHERE id = 'f1'")
    assert d["status"] == "rejected" and "wrote again" in d["error"] and "fresh reply" in d["error"]


def test_a_body_posted_with_crlf_still_goes_out_on_words():
    from harvey.integrations.mail_provider import body_to_html, links_to_text
    body = "Here's the project brief with the full scope.\r\n\r\nhttps://glaze-icing-26700938.figma.site/\r\n\r\nThanks."
    html = body_to_html(body)
    assert '>the project brief</a> with the full scope.' in html and "figma.site/</a>" not in html and "\r" not in html
    assert "\r" not in links_to_text("a [b](https://x.example/b)\r\nc")
