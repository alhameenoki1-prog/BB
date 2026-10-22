"""Review fixes, fourth round: links rendered from the raw text, Microsoft
sign-in in the mail worker, Reconnect that clears and only shows for a
refused sign-in, alerts, the goal-change rewrite, runs that cannot stick,
the Microsoft 365 admin app as a real mailbox, and the shared limit."""

import asyncio

import pytest

from bb.mail import runs
from bb.mail.db import MailDB
from bb.mail.jobs import Jobs
from bb.mail.models import MailboxSettings
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, body_to_html, link_labels
from harvey.ledger import Ledger
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from tests.test_mail_reply import FakeBrain, SentBox, _seed_thread
from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect, state  # noqa: F401

ME = "me@mine.example"
L = {"https://docs.example/brief": "the project brief"}


def test_links_keep_the_sentence_and_never_break_the_markup():
    html = body_to_html('The brief is "https://docs.example/brief" if useful.', L)
    assert '&quot;<a href="https://docs.example/brief"' in html and "</a>&quot; if useful" in html
    assert "See <a href" in body_to_html("See <https://docs.example/brief> for the rest.", L)
    assert '">the project brief</a>?' in body_to_html("Did you get a chance to open https://docs.example/brief?", L)
    assert 'href="https://en.wikipedia.org/wiki/Rebar_(steel)"' in body_to_html("See https://en.wikipedia.org/wiki/Rebar_(steel) now.", {})
    # A sentence ending in a colon keeps its words, and the document it names is the link.
    long_line = body_to_html("We need 40 t of B500B by 3 March; the spec, with grades and dates: https://docs.example/rebar", {})
    assert ('We need 40 t of B500B by 3 March; <a href="https://docs.example/rebar" style="color:#0a60c2">the spec</a>, '
            "with grades and dates.") in long_line
    # One that names nothing keeps the address's own link after it.
    assert "Pick any slot that suits you here: <a" in body_to_html("Pick any slot that suits you here: https://cal.example/jane", {})
    assert ">View the project brief</a>" in body_to_html("Project brief: https://docs.example/brief", L)
    assert ">View the project brief</a>" in body_to_html("Pricing: 2026: https://docs.example/brief", L)
    assert ">this link</a>." in body_to_html("Grab a time at https://cal.example/jane.", {})
    assert 'href="https://www.greystone.example"' in body_to_html("Our site is www.greystone.example for the rest.", {})

    class O:
        inbox_links = []
    assert link_labels(O(), "https://cal.example/jane")["https://cal.example/jane"] == "a time in my calendar"


def test_the_admin_app_mailbox_is_a_real_mailbox():
    assert "microsoft_app" in NATIVE_PROVIDERS


def test_the_mail_worker_keeps_the_site_microsoft_app(monkeypatch, tmp_path):
    import bb.mail.worker as worker_mod
    seen = {}

    class FakeWorker:
        def __init__(self, root, secret, env):
            seen.update(env)
            self.stop = asyncio.Event()

        async def loop(self):
            return None

    monkeypatch.setattr(worker_mod, "Worker", FakeWorker)
    monkeypatch.setenv("BB_SECRET_KEY", "s")
    monkeypatch.setenv("MS_CLIENT_ID", "ms-cid")
    monkeypatch.setenv("MS_CLIENT_SECRET", "ms-sec")
    monkeypatch.setenv("MS_TENANT", "common")
    worker_mod.main([str(tmp_path)])
    assert seen["MS_CLIENT_ID"] == "ms-cid" and seen["MS_CLIENT_SECRET"] == "ms-sec" and seen["MS_TENANT"] == "common"


@pytest.mark.asyncio
async def test_a_new_kind_of_connection_backfills_again(tmp_path, monkeypatch):
    import bb.mail.service as service

    class Ok:
        def __init__(self, m, c, e):
            self.creds, self.creds_changed = c, False

        async def test(self):
            return "ok"

    monkeypatch.setattr(service, "account_for", lambda m, c, e: Ok(m, c, e))
    db = MailDB(tmp_path / "mail.db"); await db.init()
    mid = await db.add_mailbox(address="jane@outlook.com", provider="imap", creds="", status="ok")
    await db.update_mailbox(mid, sync={"folders": {"INBOX": {"last_uid": 9}}, "backfill_done": True})
    await service.connect_microsoft(db, "s", address="jane@outlook.com", refresh_token="rt", env={})
    assert (await db.mailbox(mid))["sync"] == {}
    await db.update_mailbox(mid, sync={"cursor": "2026-01-01T00:00:00Z", "backfill_done": True})
    await service.connect_microsoft(db, "s", address="jane@outlook.com", refresh_token="rt2", env={})
    assert (await db.mailbox(mid))["sync"]["cursor"] == "2026-01-01T00:00:00Z"     # same kind: kept


@pytest.mark.asyncio
async def test_a_follow_up_waits_for_its_first_email(state):  # noqa: F811
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    pid = await seed_prospect(state)
    c = Campaign(id="", name="seg", sequence=[EmailStep(step=1, subject="s", body="Hi,\n\nA question?\n\nJ"),
                                              EmailStep(step=2, subject="f", body="Hi,\n\nAnother angle.\n\nJ")],
                 prospect_ids=[pid], status="active")
    c.id = await state.add_campaign(c)
    past = "2026-01-01T00:00:00"
    first = await state.add_outbox_item(prospect_id=pid, campaign_id=c.id, step=1, to_email="jane@acme.com", subject="s",
                                        body="Hi,\n\nA question?\n\nJ", send_at=past, status="pending_review")
    second = await state.add_outbox_item(prospect_id=pid, campaign_id=c.id, step=2, to_email="jane@acme.com", subject="f",
                                         body="Hi,\n\nAnother angle.\n\nJ", send_at=past, status="approved")
    await sender._drain_due()
    assert provider.sent == [] and (await state.get_outbox_item(second))["status"] == "approved"   # waits
    await state.update_outbox_item(first, status="rejected")
    await sender._drain_due()
    item = await state.get_outbox_item(second)
    assert provider.sent == [] and item["status"] == "cancelled" and "never went out" in item["error"]


@pytest.mark.asyncio
async def test_a_template_fix_with_spaced_merge_fields_still_fills_them(state):  # noqa: F811
    sender = make_sender(state, FakeProvider(), require_approval=False)

    class B:
        async def think_json(self, prompt, **kw):
            return {"subject": "Rebar for {{ company }}", "body": "Could {{company }} supply 40 tons a month from spring?\n\nJane"}

    sender.brain = B()
    fixed = await sender._rewrite_template_for_filter("x", "Hi,\n\nFREE!!! {{company}}", ["hype"], "")
    assert fixed == ("Rebar for {{company}}", "Could {{company}} supply 40 tons a month from spring?\n\nJane")
    p = Prospect(first_name="Ann", company="Acme", email="ann@acme.example")
    assert sender._render("Could {{ company }} help?", p) == "Could Acme help?"
    assert sender._passes("Rebar for {{company}}", "body", "") is None       # a raw tag never passes


@pytest.mark.asyncio
async def test_pause_pressed_while_a_reply_is_paced_holds_it(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    SentBox.sent = []
    db = MailDB(tmp_path / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok",
                               settings=MailboxSettings(signature_html="J", project_link="https://p.example").model_dump())
    tid = await _seed_thread(db, mid)
    run = await runs.create_run(db, [await db.mailbox(mid)], [tid], mode="auto")
    answer = {"body": "Tuesday at ten works, Ann. I'll bring the drawings so we can walk the site together.",
              "confidence": "high", "confidence_reason": "clear", "stance": "engaged"}

    # The pause lands between drafting and sending.
    import bb.mail.sending as sending_mod
    real_send = sending_mod.send_draft

    async def send_after_pause(*a, **k):
        raise AssertionError("must not send after Pause")

    await db.execute("UPDATE reply_runs SET status = 'running' WHERE id = ?", (run["id"],))

    class PausingBrain(FakeBrain):
        async def complete(self, system, user, max_tokens=1200):
            out = await super().complete(system, user, max_tokens)
            await db.execute("UPDATE reply_runs SET status = 'paused' WHERE id = ?", (run["id"],))
            return out

    monkeypatch.setattr(sending_mod, "send_draft", send_after_pause)
    out = await runs.process_mailbox(db, "s", {"HARVEY_HOME": str(tmp_path)}, PausingBrain([answer]), None, "j", run["id"], mid)
    item = await db.one("SELECT * FROM run_items WHERE run_id = ?", (run["id"],))
    draft = await db.one("SELECT * FROM drafts WHERE id = ?", (item["draft_id"],))
    assert out["sent"] == 0 and item["status"] == "needs_review" and "paused before it was sent" in item["detail"]
    assert draft["status"] == "pending_review" and SentBox.sent == []
    monkeypatch.setattr(sending_mod, "send_draft", real_send)


@pytest.mark.asyncio
async def test_a_run_never_sticks(tmp_path):
    db = MailDB(tmp_path / "data" / "mail.db"); await db.init()
    jobs = Jobs(db)
    a = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok")
    b = await db.add_mailbox(address="b@mine.example", provider="imap", creds="", status="ok")
    tid = await _seed_thread(db, a)
    run = await runs.create_run(db, [await db.mailbox(a), await db.mailbox(b)], [tid], mode="review")
    # The worker stops mid-job: the try is not counted.
    job = await jobs.claim()
    await jobs.release(job["id"])
    again = await db.one("SELECT status, attempts FROM jobs WHERE id = ?", (job["id"],))
    assert again["status"] == "queued" and again["attempts"] == 0
    # Its job is lost altogether: reconcile gives it a new one.
    await db.execute("UPDATE jobs SET status = 'dead'")
    await runs.reconcile(db, jobs)
    assert [j for j in await jobs.list("queued") if j["kind"] == "reply_run"]
    # The job dies for good: what was left fails and the run finishes.
    await runs.job_died(db, run["id"], a, "RuntimeError: boom")
    assert (await db.one("SELECT status FROM reply_runs WHERE id = ?", (run["id"],)))["status"] == "done"
    # A reply campaign whose account is removed is stopped by reconcile.
    rc = await runs.create_reply_campaign(db, await db.mailbox(b), mode="auto")
    await db.remove_mailbox(b)
    await runs.reconcile(db, jobs)
    assert (await db.one("SELECT status FROM reply_runs WHERE id = ?", (rc["id"],)))["status"] == "cancelled"


def test_a_limit_smaller_than_the_campaigns_still_sends(tmp_path):
    lg = Ledger(tmp_path / "s.db")
    lg.set_limit("a@x.com", 2)
    for c in ("c1", "c2", "c3"):
        lg.join("a@x.com", c, "outreach")
    assert lg.reserve("a@x.com", "c1", "outreach")[0] and lg.reserve("a@x.com", "c2", "outreach")[0]
    slot, pic = lg.reserve("a@x.com", "c3", "outreach")
    assert slot is None and "daily limit of 2 is reached" in pic["reason"]
