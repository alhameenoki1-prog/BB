"""Review fixes, fourth round (second review): reply campaigns that take a
conversation up again, never answer twice, wait out passing limits; a
person's Send that never re-joins a run; forms that act on the campaign
their page showed; and the rules that keep one campaign per account."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import bb.mail.service as service
from bb import campaigns as campaigns_mod
from bb.accounts import Accounts
from bb.app import create_app
from bb.mail import runs
from bb.mail.db import MailDB
from bb.mail.ingest import Ingest
from bb.mail.jobs import Jobs
from bb.mail.models import MailboxSettings
from bb.mail.providers.base import RawMessage
from bb.mail.routes import parse_bulk_accounts
from bb.mail.sending import SendRefused, send_draft
from bb.workspaces import Workspace
from harvey.ledger import Ledger
from tests.test_bb_web import PROFILE, USER, _as_user, _dns_stub, _settings
from tests.test_campaigns import SMTP, _accounts, _set_llm, _user
from tests.test_mail_foundation import _FakeAccount
from tests.test_mail_reply import FakeBrain, SentBox

ME = "me@mine.example"
ANSWER = {"body": "Tuesday at ten works, Ann. I'll bring the drawings so we can walk the site together.",
          "confidence": "high", "confidence_reason": "clear", "stance": "engaged"}


async def _box(tmp_path, **settings):
    db = MailDB(tmp_path / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok",
                               settings=MailboxSettings(signature_html="J", project_link="https://p.example", **settings).model_dump())
    return db, mid, await db.mailbox(mid)


async def _mail_in(db, box, n, when):
    await Ingest(db, box).store_batch([RawMessage(provider_id=f"in{n}", thread_key="T1", message_id=f"<a{n}@x>", from_email="ann@x.example",
                                                  from_name="Ann", subject="Site visit", body=f"Message {n}: can we do Tuesday?",
                                                  sent_at=when, to=[{"name": "", "email": ME}])])


@pytest.mark.asyncio
async def test_a_reply_campaign_takes_a_conversation_up_again_once_its_draft_is_dealt_with(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    run = await runs.create_reply_campaign(db, box, mode="review")
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],))
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    assert await runs.collect_campaign_threads(db, run, box) == 1
    await runs.process_mailbox(db, "s", {"HARVEY_HOME": str(tmp_path)}, FakeBrain([ANSWER]), None, "j", run["id"], mid)
    assert await runs.collect_campaign_threads(db, run, box) == 0          # its draft waits on Review
    await db.execute("UPDATE drafts SET status = 'rejected'")
    assert await runs.collect_campaign_threads(db, run, box) == 0          # rejected: not again until they write
    await _mail_in(db, box, 2, "2030-01-01T10:00:00Z")
    assert await runs.collect_campaign_threads(db, run, box) == 1          # they wrote again: taken up


@pytest.mark.asyncio
async def test_no_second_reply_while_one_waits_and_none_after_the_thread_was_answered(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    # A reply drafted by hand waits on Review: a reply campaign leaves the thread alone.
    now = "2026-01-02T00:00:00Z"
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, '', 'reply', ?, 'Re: Site visit', 'Tuesday works.', 'pending_review', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    run = await runs.create_reply_campaign(db, box, mode="auto")
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    assert await runs.collect_campaign_threads(db, await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],)), box) == 0
    assert tid in await runs.threads_in_live_runs(db)
    # The thread gets answered another way; the old draft is then refused at send time.
    await Ingest(db, box).store_batch([RawMessage(provider_id="out1", thread_key="T1", message_id="<o1@x>", from_email=ME, from_name="Me",
                                                  subject="Re: Site visit", body="Tuesday is fine.", sent_at="2026-01-03T00:00:00Z",
                                                  outbound=True, to=[{"name": "", "email": "ann@x.example"}])])
    draft = await db.one("SELECT * FROM drafts WHERE id = 'd1'")
    with pytest.raises(SendRefused, match="second reply"):
        await send_draft(db, "s", {"HARVEY_HOME": str(tmp_path)}, draft)


@pytest.mark.asyncio
async def test_a_reply_campaign_waits_out_the_daily_cap_instead_of_dropping(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setattr(runs, "sent_today", lambda db, mid: asyncio.sleep(0, result=5), raising=False)
    import bb.mail.sending as sending_mod
    monkeypatch.setattr(sending_mod, "sent_today", lambda db, mid: asyncio.sleep(0, result=5))
    db, mid, box = await _box(tmp_path, daily_cap=5)
    run = await runs.create_reply_campaign(db, box, mode="auto")
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    await runs.collect_campaign_threads(db, await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],)), box)
    out = await runs.process_mailbox(db, "s", {"HARVEY_HOME": str(tmp_path)}, FakeBrain([]), None, "j", run["id"], mid)
    assert "daily cap of 5" in out["waiting"] and (await runs.run_summary(db, run["id"]))["queued"] == 1


@pytest.mark.asyncio
async def test_a_person_s_send_never_brings_a_stopped_run_back(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db, mid, box = await _box(tmp_path)
    await _mail_in(db, box, 1, "2026-01-01T10:00:00Z")
    tid = (await db.one("SELECT id FROM threads"))["id"]
    run = await runs.create_reply_campaign(db, box, mode="review")
    await runs.cancel_run(db, run["id"])
    now = "2026-01-02T00:00:00Z"
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, run_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, ?, 'reply', ?, 'Re: Site visit', 'Tuesday works.', 'approved', ?, ?)",
                     (mid, tid, run["id"], json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    await send_draft(db, "s", {"HARVEY_HOME": str(tmp_path)}, await db.one("SELECT * FROM drafts WHERE id = 'd1'"), pressed=True)
    led = Ledger(tmp_path / "data" / "sendlog.db")
    assert led.members(ME) == [] and led.used_by(run["id"]) == 1        # counted, not a member again


@pytest.mark.asyncio
async def test_resume_is_never_lost_and_states_change_only_from_the_right_state(tmp_path):
    db, mid, box = await _box(tmp_path)
    run = await runs.create_reply_campaign(db, box, mode="auto")
    jobs = Jobs(db)
    running = await jobs.claim()                           # its tick is finishing when Resume is pressed
    assert await runs.pause_run(db, run["id"])
    assert not await runs.pause_run(db, run["id"])         # already paused
    assert await runs.resume_run(db, run["id"])
    queued = [j for j in await jobs.list("queued") if j["kind"] == "reply_campaign"]
    assert queued and queued[0]["id"] != running["id"]
    assert await runs.cancel_run(db, run["id"]) and not await runs.resume_run(db, run["id"])


def test_bulk_lines_keep_the_password_whole():
    got = parse_bulk_accounts("sales@acme.example, Tr0ub,4dor&3Secret\na@x.com pw smtp.x.com:587 imap.x.com:993\ne@x.com pass word")
    assert got[0] == {"email": "sales@acme.example", "password": "Tr0ub,4dor&3Secret"}
    assert got[1]["password"] == "pw" and got[1]["smtp_host"] == "smtp.x.com" and got[1]["imap_port"] == "993"
    assert got[2]["password"] == "pass word"


def test_relays_have_no_made_up_limit():
    box = {"address": "news@acme.example", "provider": "imap", "settings": {}}
    assert service.account_daily_limit(box, {"smtp_host": "email-smtp.eu-west-1.amazonaws.com"}) == 0
    assert service.account_daily_limit(box, {"smtp_host": "smtp-relay.gmail.com"}) == 0
    assert service.account_daily_limit(box, {"smtp_host": "smtp.gmail.com"}) == 1800
    assert service.account_daily_limit({**box, "settings": {"account_limit": 120}}, {"smtp_host": "smtp.gmail.com"}) == 120


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(service, "account_for", lambda m, c, e: _FakeAccount(m, c, e))
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "Started (pid 1)."))
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def test_a_form_acts_on_the_campaign_its_page_showed(client):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    main = Workspace(client.settings, _user(client))
    _set_llm(main)
    db, ids = asyncio.run(_accounts(main, "a@acme.example"))
    client.post("/campaigns/new", data={"feature": "outreach", "mailbox_ids": ids})
    other = main.campaign_workspaces()[0]
    main.select(other.campaign_id)                       # another tab switched to the new campaign
    page = client.get("/").text
    assert "u.searchParams.set('c'" in page
    r = client.post("/recipients?c=main", data={"pasted": "lead@x.example"})     # this tab still showed the main campaign
    assert "Added%201" in r.headers["location"]
    assert [p.email for p in asyncio.run(main.state().get_prospects_by_status("new"))] == ["lead@x.example"]
    assert not other.db_path.exists() or not asyncio.run(other._table_exists("prospects")) or \
        asyncio.run(other.state().get_prospects_by_status("new")) == []


def test_one_campaign_per_account_from_every_side(client, monkeypatch):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    main = Workspace(client.settings, _user(client))
    _set_llm(main)
    db, ids = asyncio.run(_accounts(main, "a@acme.example", "b@acme.example"))
    client.post("/campaigns/new", data={"feature": "outreach", "mailbox_ids": [ids[0]]})
    camp = main.campaign_workspaces()[0]
    # The main campaign cannot take the account the new campaign sends from.
    monkeypatch.setattr("bb.app.test_smtp", lambda smtp: asyncio.sleep(0, result=(True, "ok")))
    r = client.post("/mailbox/smtp?c=main", data={**SMTP, "smtp_username": "a@acme.example", "smtp_password": "pw"})
    assert "one%20campaign%20per%20account" in r.headers["location"]
    # A disabled account is refused for a rebind, and nothing changes.
    asyncio.run(db.update_mailbox(ids[1], enabled=0))
    r = client.post(f"/campaigns/{camp.campaign_id}/account?c={camp.campaign_id}", data={"mailbox_id": ids[1]})
    assert "disabled" in r.headers["location"] and camp.campaign_row()["account"] == "a@acme.example"
    # People a removed campaign already wrote to stay taken.
    sm = camp.state()
    from harvey.models.prospect import Prospect
    asyncio.run(sm.init_db())
    asyncio.run(sm.add_prospect(Prospect(id="p1", email="ann@lead.example", email_status="provided", status="contacted")))
    client.post(f"/campaigns/outreach/{camp.campaign_id}/remove")
    taken, _ = main.leads_elsewhere(["ann@lead.example"])
    assert taken == {"ann@lead.example"}
