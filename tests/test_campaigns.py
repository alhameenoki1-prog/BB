"""Several campaigns at once: one outreach campaign per account with its
own leads, goal and brief; a reply campaign and a mailbox scan per
account; all side by side, one account's daily limit shared between
whatever runs on it, and one page that shows and controls them all."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import bb.mail.service as service
from bb import campaigns as campaigns_mod
from bb.accounts import Accounts
from bb.app import create_app
from bb.mail import runs
from bb.mail.db import MailDB
from bb.mail.jobs import Jobs
from bb.mail.models import MailboxSettings
from bb.workspaces import Workspace
from harvey.ledger import Ledger
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_bb_web import PROFILE, USER, _as_user, _dns_stub, _settings
from tests.test_mail_foundation import _FakeAccount
from tests.test_mail_reply import FakeBrain, SentBox, _seed_thread

ME = "me@mine.example"
SMTP = {"smtp_username": "", "smtp_password": "pw", "smtp_host": "smtp.acme.example", "smtp_port": "587",
        "imap_host": "imap.acme.example", "imap_port": "993", "imap_username": "", "imap_password": ""}


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


def _user(client):
    return Accounts(client.settings.accounts_db).get_by_email(USER["email"])


async def _accounts(ws, *addresses):
    db = ws.mail_db()
    await db.init()
    ids = []
    for a in addresses:
        mid, _ = await service.connect_imap(db, client_secret(ws), address=a, smtp=dict(SMTP), env={})
        await db.update_mailbox(mid, settings=MailboxSettings(signature_html="Jane", project_link="https://p.example").model_dump())
        ids.append(mid)
    return db, ids


def client_secret(ws):
    return ws.settings.secret_key()


def _set_llm(ws):
    ws.save_llm("anthropic", "sk-ant-test-key")


# ── outreach: one per account, its own leads, goal and brief ──

def test_outreach_campaigns_for_several_accounts_at_once(client):
    _as_user(client)
    assert client.post("/profile", data={**PROFILE, "campaign_goal": "Get a reply."}).status_code == 303
    main = Workspace(client.settings, _user(client))
    _set_llm(main)
    (main.attachments_dir / "brief.pdf").write_bytes(b"%PDF-1.4 brief")
    db, ids = asyncio.run(_accounts(main, "a@acme.example", "b@acme.example"))

    csv = "email,first_name,company\n" + "\n".join(f"lead{i}@co{i}.example,Ann{i},Co{i}" for i in range(6))
    r = client.post("/campaigns/new", data={"feature": "outreach", "mailbox_ids": ids, "name": "Spring",
                                            "goal": "Get permission to send the brief.", "split": "turns"},
                    files={"leads": ("leads.csv", csv.encode(), "text/csv")})
    loc = r.headers["location"]
    assert r.status_code == 303 and "Created%202%20outreach%20campaigns" in loc, loc
    rows = [c for c in campaigns_mod.load(main.user_root) if c["id"] != "main"]
    assert sorted(c["account"] for c in rows) == ["a@acme.example", "b@acme.example"]
    camps = main.campaign_workspaces()
    for ws in camps:
        cfg = ws.config()
        assert cfg["persona"]["email"] == ws.campaign_row()["account"]           # sends as its own account
        assert cfg["outreach"]["goal"] == "Get permission to send the brief."     # its own goal
        assert cfg["channels"]["email"]["provider"] == "smtp" and ws.mailbox_connected()
        assert (ws.attachments_dir / "brief.pdf").exists()                         # its own copy of the brief
        assert ws.llm()["provider"] == "anthropic"                                 # one AI provider per user
        env = ws.worker_env()
        assert env["HARVEY_HOME"] == str(ws.root) and env["SMTP_USERNAME"] == ws.campaign_row()["account"]
        assert env["HARVEY_LEDGER"].endswith("sendlog.db") and env["HARVEY_LEDGER_CAMPAIGN"] == ws.campaign_id
        assert env["HARVEY_LEDGER_ACCOUNT"] == ws.campaign_row()["account"]
    leads = {ws.campaign_row()["account"]: asyncio.run(ws.state().get_prospects_by_status("new")) for ws in camps}
    assert [len(v) for v in leads.values()] == [3, 3]
    assert not ({p.email for p in leads["a@acme.example"]} & {p.email for p in leads["b@acme.example"]})
    assert main.config()["outreach"]["goal"] == "Get a reply."                    # the source keeps its own

    # One outreach campaign per account.
    r = client.post("/campaigns/new", data={"feature": "outreach", "mailbox_ids": [ids[0]]})
    assert "already%20has%20an%20outreach%20campaign" in r.headers["location"]

    # The pages follow the campaign picked in the menu; a lead already on
    # another campaign's list is not added to this one.
    a = next(ws for ws in camps if ws.campaign_row()["account"] == "a@acme.example")
    assert client.post("/campaigns/select", data={"campaign": a.campaign_id, "next": "/recipients"}).headers["location"] == "/recipients"
    page = client.get("/").text
    assert a.campaign_name() in page and "campaign-switch" in page
    other = next(p.email for p in leads["b@acme.example"])
    r = client.post("/recipients", data={"pasted": f"{other}\nfresh@new.example"})
    assert "Added%201%20recipient" in r.headers["location"] and "another%20campaign" in r.headers["location"]
    # Its Mailbox page shows the account it sends from, not the connect forms.
    page = client.get("/mailbox").text
    assert "sends from a@acme.example" in page and "Log in with your email" not in page and "Tell me when it stops" in page
    assert "sends%20from%20an%20account" in client.post("/mailbox/smtp", data={}).headers["location"]

    # The Campaigns page lists both with controls; Open switches to it.
    page = client.get("/campaigns").text
    assert "Outreach from a@acme.example" in page or "Spring · a@acme.example" in page
    assert page.count('action="/campaigns/outreach/') >= 4 and "Start campaigns" in page
    assert client.post(f"/campaigns/outreach/{a.campaign_id}/open").headers["location"] == "/"
    assert main.selected().campaign_id == a.campaign_id
    # Remove sets the folder aside.
    r = client.post(f"/campaigns/outreach/{a.campaign_id}/remove")
    assert "Removed" in r.headers["location"] and not a.root.exists() and campaigns_mod.get(main.user_root, a.campaign_id) is None
    assert main.selected().is_main and list((main.user_root / "campaigns" / ".removed").iterdir())


def test_leads_go_to_the_account_named_in_the_file(client):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    main = Workspace(client.settings, _user(client))
    _set_llm(main)
    db, ids = asyncio.run(_accounts(main, "a@acme.example", "b@acme.example"))
    csv = ("email,account\nx1@t.example,a@acme.example\nx2@t.example,B@acme.example\nx3@t.example,b@acme.example\n"
           "x4@t.example,nobody@else.example\n")
    r = client.post("/campaigns/new", data={"feature": "outreach", "mailbox_ids": ids, "split": "column"},
                    files={"leads": ("l.csv", csv.encode(), "text/csv")})
    assert "1%20row%28s%29%20named%20no%20account" in r.headers["location"]
    got = {ws.campaign_row()["account"]: sorted(p.email for p in asyncio.run(ws.state().get_prospects_by_status("new")))
           for ws in main.campaign_workspaces()}
    assert got == {"a@acme.example": ["x1@t.example"], "b@acme.example": ["x2@t.example", "x3@t.example"]}


def test_a_suppressed_address_is_never_added_to_any_campaign(client):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    main = Workspace(client.settings, _user(client))
    main.ledger().suppress("gone@t.example", "asked not to be contacted", "outreach-x")
    r = client.post("/recipients", data={"pasted": "gone@t.example\nok@t.example"})
    assert "Added%201%20recipient" in r.headers["location"] and "never%20to%20be%20contacted" in r.headers["location"]


# ── reply campaigns and scans: one per account, side by side ──

@pytest.mark.asyncio
async def test_a_reply_campaign_keeps_answering_leaves_outreach_leads_alone_and_pauses(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    SentBox.sent = []
    db = MailDB(tmp_path / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok",
                               settings=MailboxSettings(signature_html="Jane", project_link="https://p.example").model_dump())
    box = await db.mailbox(mid)
    env = {"HARVEY_HOME": str(tmp_path)}
    run = await runs.create_reply_campaign(db, box, mode="auto", created_by="me")
    assert run["kind"] == "reply_campaign" and run["status"] == "running"
    assert (await runs.create_reply_campaign(db, box))["busy"] == run["id"]          # one per account
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],))
    await _seed_thread(db, mid)
    assert await runs.collect_campaign_threads(db, run, box) == 1
    assert await runs.collect_campaign_threads(db, run, box) == 0                   # never twice

    brain = FakeBrain([{"body": "Tuesday at ten works, Ann. I'll bring the drawings so we can walk the site together.",
                        "confidence": "high", "confidence_reason": "clear", "stance": "engaged"}])
    out = await runs.process_mailbox(db, "s", env, brain, Jobs(db), "job", run["id"], mid)
    assert out["sent"] == 1 and SentBox.sent[-1]["to"] == ["ann@x.example"]
    fresh = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],))
    assert fresh["status"] == "running"                                              # it does not finish by itself
    led = Ledger(tmp_path / "data" / "sendlog.db")
    assert led.used_by(run["id"]) == 1 and led.has_feature(ME, "reply")

    # A lead of an outreach campaign on this account is that campaign's to answer.
    cid = campaigns_mod.new_id()
    campaigns_mod.upsert(tmp_path, {"id": cid, "name": "Outreach A", "mailbox_id": mid, "account": ME})
    sm = StateManager(str(campaigns_mod.campaign_root(tmp_path, cid) / "data" / "harvey.db")); await sm.init_db()
    (campaigns_mod.campaign_root(tmp_path, cid) / "data" / "worker.autostart").write_text("1")    # it is running
    await sm.add_prospect(Prospect(id="p1", first_name="Bo", email="bo@lead.example", email_status="provided", status="contacted"))
    from bb.mail.ingest import Ingest
    from bb.mail.providers.base import RawMessage
    await Ingest(db, box).store_batch([RawMessage(provider_id="in2", thread_key="T2", message_id="<b1@x>", from_email="bo@lead.example",
                                                  from_name="Bo", subject="Re: enquiry", body="Yes please send the brief.",
                                                  sent_at="2026-02-01T10:00:00Z", to=[{"name": "", "email": ME}])])
    assert await runs.collect_campaign_threads(db, fresh, box) == 1
    out = await runs.process_mailbox(db, "s", env, FakeBrain([]), Jobs(db), "job", run["id"], mid)
    item = await db.one("SELECT * FROM run_items WHERE run_id = ? AND status = 'skipped'", (run["id"],))
    assert out["skipped"] == 1 and "lead of the outreach campaign \"Outreach A\"" in item["detail"]
    # Stopped, that campaign answers nobody: its leads are this campaign's again.
    (campaigns_mod.campaign_root(tmp_path, cid) / "data" / "worker.autostart").unlink()
    assert campaigns_mod.outreach_owner(tmp_path, ME, "bo@lead.example") is None

    # Pause: nothing more is done and its part of the limit goes back; resume queues it again.
    assert await runs.pause_run(db, run["id"], env)
    assert not led.has_feature(ME, "reply")
    assert (await runs.process_mailbox(db, "s", env, FakeBrain([]), None, "job", run["id"], mid))["skipped"] == "run is paused"
    assert await runs.resume_run(db, run["id"])
    queued = [j for j in await Jobs(db).list("queued") if j["kind"] == "reply_campaign"]
    assert queued and queued[-1]["lane"] == runs.lane(mid, run["id"])
    assert await runs.cancel_run(db, run["id"], env)
    assert (await db.one("SELECT status FROM reply_runs WHERE id = ?", (run["id"],)))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_two_runs_on_one_account_have_their_own_lanes(tmp_path):
    db = MailDB(tmp_path / "mail.db"); await db.init()
    jobs = Jobs(db)
    await jobs.enqueue("reply_campaign", "m1", {"run_id": "r1"}, lane=runs.lane("m1", "r1"))
    await jobs.enqueue("reply_run", "m1", {"run_id": "r2"}, lane=runs.lane("m1", "r2"))
    await jobs.enqueue("sync_incremental", "m1")
    first = await jobs.claim()
    second = await jobs.claim(exclude_mailboxes={first["lane"] or first["mailbox_id"]})
    third = await jobs.claim(exclude_mailboxes={first["lane"] or first["mailbox_id"], second["lane"] or second["mailbox_id"]})
    assert {first["kind"], second["kind"], third["kind"]} == {"reply_campaign", "reply_run", "sync_incremental"}
    # The same lane never twice at once.
    await jobs.enqueue("reply_run", "m1", {"run_id": "r2"}, lane=runs.lane("m1", "r2"))
    assert await jobs.claim(exclude_mailboxes={runs.lane("m1", "r2")}) is None


@pytest.mark.asyncio
async def test_scans_start_one_per_account(tmp_path):
    from tests.test_no_and_reconnect import _seed_history
    db = MailDB(tmp_path / "mail.db"); await db.init()
    a = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok")
    b = await db.add_mailbox(address="other@mine2.example", provider="imap", creds="", status="ok")
    await _seed_history(db, a)
    out = await runs.create_reconnect_runs(db, [await db.mailbox(a), await db.mailbox(b)], months=6, profile_id="main", name="Q4")
    assert out[0].get("id") and out[0]["mailbox_id"] == a and json.loads(out[0]["mailbox_ids_json"]) == [a]
    assert not out[1].get("id") and out[1]["address"] == "other@mine2.example"      # nothing to continue there
    again = await runs.create_reconnect_runs(db, [await db.mailbox(a)], months=6)
    assert again[0]["busy"] == out[0]["id"]


# ── the shared daily limit across the command center and outreach ──

@pytest.mark.asyncio
async def test_replies_go_whatever_the_shared_limit_and_do_not_use_it_up(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    SentBox.sent = []
    db = MailDB(tmp_path / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok",
                               settings=MailboxSettings(signature_html="Jane", project_link="https://p.example", account_limit=2,
                                                        daily_cap=1).model_dump())
    box = await db.mailbox(mid)
    env = {"HARVEY_HOME": str(tmp_path)}
    led = Ledger(tmp_path / "data" / "sendlog.db")
    led.set_limit(ME, 2)
    led.join(ME, "outreach-a", "outreach", "Outreach A")          # an outreach campaign also works on this account
    for i in range(2):
        assert led.reserve(ME, "outreach-a", "outreach")[0]      # ...and has used the whole limit today
    run = await runs.create_reply_campaign(db, box, mode="auto")
    await db.execute("UPDATE reply_runs SET since = '2000-01-01T00:00:00Z' WHERE id = ?", (run["id"],))
    run = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],))
    from bb.mail.ingest import Ingest
    from bb.mail.providers.base import RawMessage
    await Ingest(db, box).store_batch([
        RawMessage(provider_id=f"in{i}", thread_key=f"T{i}", message_id=f"<m{i}@x>", from_email=f"p{i}@x.example", from_name=f"P{i}",
                   subject=f"Question {i}", body="Could you send the details?", sent_at=f"2026-02-0{i + 1}T10:00:00Z",
                   to=[{"name": "", "email": ME}]) for i in range(3)])
    assert await runs.collect_campaign_threads(db, run, box) == 3
    answer = {"body": "Here is how the project runs, step by step, with the dates we have so far for each stage of it.",
              "confidence": "high", "confidence_reason": "clear", "stance": "engaged"}
    out = await runs.process_mailbox(db, "s", env, FakeBrain([answer] * 3), Jobs(db), "job", run["id"], mid)
    # The account's limit and the command center's cap are both used up, and all three replies went.
    assert out["sent"] == 3 and not out["waiting"] and len(SentBox.sent) == 3
    summary = led.summary(ME)
    assert summary["total"] == 2 and summary["answers"] == 3       # written down apart, none of the limit used


@pytest.mark.asyncio
async def test_the_outreach_sender_takes_slots_from_the_shared_limit(tmp_path, monkeypatch):
    from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign, seed_prospect
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", "harvey@x.co")
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    led = Ledger(tmp_path / "sendlog.db")
    led.set_limit("harvey@x.co", 4)
    led.join("harvey@x.co", "scan-1", "scan", "Scan")                   # a mailbox scan on the same account
    led.suppress("gone@acme.com", "asked not to be contacted", "reply-1")
    sm = StateManager(str(tmp_path / "h.db")); await sm.init_db()
    provider = FakeProvider()
    sender = make_sender(sm, provider, require_approval=False)
    ids = [await seed_prospect(sm, email=f"p{i}@acme.com") for i in range(4)] + [await seed_prospect(sm, email="gone@acme.com")]
    await seed_campaign(sm, ids)
    await sender._run_native()
    # Two of four for outreach (the scan keeps its two); the suppressed one is cancelled.
    assert len(provider.sent) == 2 and "gone@acme.com" not in {s["to"] for s in provider.sent}
    assert not [i for i in await sm.get_outbox() if i["to_email"] == "gone@acme.com"]          # never even staged
    assert (await sm.get_prospect_by_email("gone@acme.com")).status == "opted_out"
    assert led.used_by("c-1") == 2
    # The next pass finds the part used and says why, in words the health check knows are not a stall.
    await sender._run_native()
    assert len(provider.sent) == 2
    assert "shared with other campaigns" in (await sm.get_setting("sender_skip_reason") or "")


# ── adding several accounts at once ──

def test_several_accounts_at_once(client, monkeypatch):
    class Picky(_FakeAccount):
        async def test(self):
            if self.mailbox["address"].startswith("bad"):
                from bb.mail.providers import AuthError
                raise AuthError("bad password")
            return "ok"

    monkeypatch.setattr(service, "account_for", lambda m, c, e: Picky(m, c, e))
    _as_user(client)
    lines = "a@acme-mail.example, pw1\nb@acme-mail.example\tpw 2\tsmtp.acme-mail.example:465\timap.acme-mail.example:993\nbad@acme-mail.example pw3\n"
    monkeypatch.setattr("bb.mail.routes.guess_settings", lambda e: (dict(SMTP), "guessed"))
    r = client.post("/mail/accounts/bulk", data={"lines": lines})
    loc = r.headers["location"]
    assert "Connected%202%20accounts" in loc and "bad%40acme-mail.example%3A%20sign-in%20refused" in loc
    main = Workspace(client.settings, _user(client))
    boxes = {m["address"]: m for m in asyncio.run(main.mail_db().mailboxes())}
    assert set(boxes) == {"a@acme-mail.example", "b@acme-mail.example"}
    creds = service.open_creds(client.settings.secret_key(), boxes["b@acme-mail.example"])
    assert creds["password"] == "pw 2" and creds["smtp_host"] == "smtp.acme-mail.example" and int(creds["smtp_port"]) == 465
    assert "Add several at once" in client.get("/mail/accounts").text


def test_alerts_cover_every_campaign(client, monkeypatch):
    from bb import alerts
    _as_user(client)
    client.post("/profile", data=PROFILE)
    main = Workspace(client.settings, _user(client))
    _set_llm(main)
    db, ids = asyncio.run(_accounts(main, "a@acme.example"))
    client.post("/campaigns/new", data={"feature": "outreach", "mailbox_ids": ids})
    camp = main.campaign_workspaces()[0]
    camp.autostart_file.write_text("1")

    async def health(self):
        return {"alert": "Nothing has been delivered since 01:00 although 3 approved emails are due."} if not self.is_main else {}

    monkeypatch.setattr(Workspace, "mailbox_health", health)
    got = asyncio.run(alerts.current_alerts(main))
    assert list(got) == [f"campaign:{camp.campaign_id}"] and "a@acme.example" in got[f"campaign:{camp.campaign_id}"]
