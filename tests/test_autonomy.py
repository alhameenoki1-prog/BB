"""Sending without a person in the loop: subjects a real person would type,
the pre-send fix, a Rewrite on every queued email, the campaign goal in
every brief, the brief attached to command-center replies, the mailbox
health check, and an address the mail server refuses at send time."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from harvey import enquiry, listcheck
from harvey.config import EnvConfig, HarveyConfig
from harvey.integrations.mail_provider import SendResult
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.spamcheck import SUBJECT_REVIEW, subject_from_need, subject_risk
from tests.test_outbox_native import Cfg, Env, FakeProvider, make_sender, state  # noqa: F401
from tests.test_outreach import BASE, _Brain, _config

VADE = ("[SMTPRecipientRefused(550, '5.7.1 Recipient rejected - OXSUS003_407 - "
        "https://postmaster-oxsus.vadesecure.com/outbound_error_codes/#_407', 'yejn6oig@ss1.xfourrich.com')]")
BODY = ("Hi Luis,\n\nI need about 40 tons of rebar a month for a year, delivered to a site in Baltimore. I run "
        "Greystone Development here.\n\nCan you supply that much every month, and where should the spec go?\n\nJane")
NEED = "About 40 tons of rebar a month for a year, delivered to a Baltimore site."


def test_subjects_are_judged_on_what_a_person_would_type():
    for s in ("Quick question", "Exciting opportunity", "Partnership proposal", "Following up", "Hello",
              "Introduction", "URGENT: REPLY NOW", "Looking to start work with your team", "Rebar"):
        assert subject_risk(s, BODY, NEED)[0] >= SUBJECT_REVIEW, s
    for s in ("Rebar, about 40 tons a month for a year", "Steel supply for a Baltimore site"):
        assert subject_risk(s, BODY, NEED)[0] < SUBJECT_REVIEW, s
    # A fine subject for another email is not fine for this one.
    assert subject_risk("Builder for a 24-unit block in Towson", BODY, NEED)[0] >= SUBJECT_REVIEW
    # A subject that shares no word with the email it sits on, judged only on a real email.
    assert subject_risk("Advice on a family portfolio", BODY, "")[0] >= SUBJECT_REVIEW
    assert subject_risk("Advice on a family portfolio", "hi", "")[0] < SUBJECT_REVIEW
    assert subject_risk("", BODY)[0] >= SUBJECT_REVIEW and "no subject" in subject_risk("", BODY)[1]
    assert subject_from_need("A builder for a 24-unit residential block in Towson, drawings done, start in April.") == \
        "A builder for a 24-unit residential block in Towson"
    assert subject_from_need(NEED) == "About 40 tons of rebar a month for a year"
    assert subject_from_need("", "Hi Dana,\n\nI need a builder for a 24-unit block in Towson. Drawings done.") == \
        "A builder for a 24-unit block in Towson"
    assert subject_from_need("") == ""


@pytest.mark.asyncio
async def test_the_goal_stands_in_every_brief_and_subjects_are_fixed_by_the_writer(state):  # noqa: F811
    from harvey.agents.handler import Handler
    from harvey.agents.writer import Writer
    from harvey.brain import Brain

    goal = ("The goal of the first email is to get permission to send the project brief. End on an open question "
            "offering it.")
    cfg = HarveyConfig(**{**BASE, "outreach": {"mode": "inquiry", "intent": NEED, "goal": goal, "ask": "zoom",
                                               "sender_line": "I run Greystone Development in Baltimore."}})
    writer = Writer(Brain(state, cfg.brain), state, cfg, EnvConfig())
    writer.skills = ""
    p = writer._base_prompt("manufacturing")
    assert "CAMPAIGN GOAL, in Jane's words" in p and goal in p and "outranks the shape of the examples" in p
    cfg.outreach.goal = ""
    assert "CAMPAIGN GOAL" not in writer._base_prompt("manufacturing")
    # The writer swaps a boilerplate subject for a plain one from the need; a good one stays.
    assert writer._fix_subject("Quick question", BODY, NEED) == "About 40 tons of rebar a month for a year"
    assert writer._fix_subject("Rebar for a Baltimore site, 40 tons a month", BODY, NEED) == "Rebar for a Baltimore site, 40 tons a month"
    # Offer mode carries the goal too.
    cfg2 = HarveyConfig(**{**BASE, "outreach": {"mode": "offer", "goal": "Book a 15 minute call."}})
    w2 = Writer(Brain(state, cfg2.brain), state, cfg2, EnvConfig())
    w2.skills = ""
    assert "CAMPAIGN GOAL" in w2._base_prompt() and "Book a 15 minute call." in w2._base_prompt()

    # The handler: replies and inbox answers move toward the goal.
    handler = Handler(_Brain(), state, _config(mode="inquiry", intent=NEED, goal=goal), EnvConfig())
    ctx = handler._outreach_context([])
    assert "CAMPAIGN GOAL" in ctx and goal in ctx and "answer their message first" in ctx


class _FixBrain:
    """Rewrites whatever the sender hands it into a clean email."""

    def __init__(self, reply=None):
        self.prompts = []
        self.reply = reply

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


@pytest.mark.asyncio
async def test_the_sender_fixes_what_a_filter_would_junk_and_only_then_asks_for_review(state):  # noqa: F811
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.brain = _FixBrain({"subject": "Rebar, about 40 tons a month for a year", "body": BODY})
    for i, e in enumerate(["a@one.example", "b@two.example"]):
        await state.add_prospect(Prospect(id=f"f{i}", first_name="Luis", email=e, email_status="provided", industry="manufacturing"))
    spam = ("{{first_name}} FREE guarantee!!! Click here now, limited time offer, act now, winner, 100% risk free, "
            "best-in-class solutions, boost your revenue, unlock exclusive discount, don't miss out!!!")
    c = Campaign(id="", name="seg", channel="email", prospect_ids=["f0"], status="draft",
                 sequence=[EmailStep(step=1, subject="Exciting opportunity", body=spam)])
    c.id = await state.add_campaign(c)
    await sender._run_native()
    sent = provider.sent
    assert len(sent) == 1 and sent[0]["subject"] == "Rebar, about 40 tons a month for a year"
    assert "40 tons of rebar" in sent[0]["body"] and sent[0]["body"].startswith("Hi Luis,")   # the greeting line survives
    assert "This email is about to be sent" in sender.brain.prompts[0] and "Exciting opportunity" in sender.brain.prompts[0]

    # The model could not fix it: the email waits for a person, and nothing else does.
    sender.brain = _FixBrain({"subject": "Exciting opportunity", "body": spam.replace("{{first_name}}", "Hi Luis,")})
    c2 = Campaign(id="", name="seg2", channel="email", prospect_ids=["f1"], status="draft",
                  sequence=[EmailStep(step=1, subject="Exciting opportunity", body=spam),
                            EmailStep(step=2, subject="Exciting opportunity", body=spam, delay_days=3)])
    c2.id = await state.add_campaign(c2)
    await sender._run_native()
    held = await state.get_outbox(status="pending_review")
    assert held and all(h["error"].startswith("Needs review: even after a rewrite") for h in held)
    assert "everything else keeps sending" in held[0]["error"]
    assert len(provider.sent) == 1

    # Only the subject is wrong: a plain one from the email itself, no model call.
    sender.brain = None
    await state.add_prospect(Prospect(id="f2", first_name="Luis", email="c@three.example", email_status="provided"))
    plain_body = ("{{first_name}} I need about 40 tons of rebar a month for a year delivered to a site in Baltimore, "
                  "and I run a small development company here. Can you supply that much every month, and where should the spec go?\n\nJane")
    c3 = Campaign(id="", name="seg3", channel="email", prospect_ids=["f2"], status="draft",
                  sequence=[EmailStep(step=1, subject="Quick question", body=plain_body)])
    c3.id = await state.add_campaign(c3)
    await sender._run_native()
    assert len(provider.sent) == 2 and provider.sent[-1]["subject"] != "Quick question"
    assert "rebar" in provider.sent[-1]["subject"].lower()


def test_a_permanent_recipient_refusal_is_a_bad_address():
    import aiosmtplib

    from harvey.integrations.smtp_mail import classify_smtp_error
    refused = aiosmtplib.SMTPRecipientRefused(550, "5.7.1 Recipient rejected - OXSUS003_407", "yejn6oig@ss1.xfourrich.com")
    assert classify_smtp_error(aiosmtplib.SMTPRecipientsRefused([refused]))["bad_address"] is True
    assert classify_smtp_error(refused)["bad_address"] is True and not classify_smtp_error(refused)["retryable"]
    grey = aiosmtplib.SMTPRecipientRefused(450, "4.2.0 greylisted, try later", "x@y.example")
    assert classify_smtp_error(aiosmtplib.SMTPRecipientsRefused([grey])) == {
        "retryable": True, "scope": "row", "auth_failed": False, "daily_limit": False, "bad_address": False}


class _RefusingProvider(FakeProvider):
    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **kw):
        if to_email.endswith("@ss1.xfourrich.com"):
            return SendResult(ok=False, error=VADE, bad_address=True)
        return await super().send_email(to_email, subject, body, thread_ref, in_reply_to, **kw)


@pytest.mark.asyncio
async def test_an_address_the_mail_server_refuses_is_handled_like_a_bounce(state):  # noqa: F811
    provider = _RefusingProvider()
    sender = make_sender(state, provider, require_approval=False)
    people = [("r0", "yejn6oig@ss1.xfourrich.com"), ("r1", "pat@ss1.xfourrich.com"), ("r2", "sam@fine.example")]
    for pid, e in people:
        await state.add_prospect(Prospect(id=pid, first_name="P", email=e, email_status="provided"))
    c = Campaign(id="", name="seg", channel="email", prospect_ids=[p for p, _ in people], status="draft",
                 sequence=[EmailStep(step=1, subject="Rebar, about 40 tons a month", body="{{first_name}} plain words about rebar " * 6),
                           EmailStep(step=2, subject="Rebar, about 40 tons a month", body="{{first_name}} following up on the rebar " * 6, delay_days=4)])
    c.id = await state.add_campaign(c)
    await sender._run_native()
    assert (await state.get_prospect("r0")).email_status == "invalid"
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1 and failed[0]["to_email"] == "yejn6oig@ss1.xfourrich.com"
    assert failed[0]["error"].startswith("Bounced:") and "domain has no mail server" in failed[0]["error"]
    assert [i["to_email"] for i in await state.get_outbox(status="cancelled")] == ["yejn6oig@ss1.xfourrich.com"]
    # A dead domain holds the other first email to it; the rest went.
    held = await state.get_outbox(status="pending_review")
    assert [(i["to_email"], i["step"]) for i in held] == [("pat@ss1.xfourrich.com", 1)]
    assert "does not exist" in held[0]["error"]
    assert [s["to"] for s in provider.sent] == ["sam@fine.example"]
    assert await state.get_setting("bounce_count") == "1"
    assert await state.get_setting("mailbox_last_sent_at")


@pytest.mark.asyncio
async def test_every_uploaded_address_gets_the_free_domain_check(state, monkeypatch):  # noqa: F811
    for i, e in enumerate(["ok@live.example", "dead@gone.example", "later@slow.example", "two@gone.example"]):
        await state.add_prospect(Prospect(id=f"d{i}", first_name="X", email=e, email_status="provided",
                                          personalization_notes="Runs a steel yard."))
    calls = []

    async def lookup(domain):
        calls.append(domain)
        return {"gone.example": ("none", None), "slow.example": ("error", None)}.get(domain, ("mx", "mx.live.example"))

    monkeypatch.setattr(listcheck, "lookup_domain", lookup)
    counts = await listcheck.check_domains(state)
    assert counts == {"checked": 4, "invalid": 2, "deferred": 1}
    assert calls.count("gone.example") == 1        # one lookup per domain per cycle
    statuses = {p.email: p.email_status for p in await state.get_prospects_by_status("new")}
    assert statuses == {"ok@live.example": "provided", "dead@gone.example": "invalid", "later@slow.example": "provided",
                        "two@gone.example": "invalid"}
    dead = await state.get_prospect("d1")
    assert "Domain check" in dead.personalization_notes and "no mail server" in dead.personalization_notes
    assert enquiry.clean_notes(dead.personalization_notes) == "Runs a steel yard."   # housekeeping never reaches the model
    again = await listcheck.check_domains(state)
    assert again == {"checked": 1, "invalid": 0, "deferred": 1}        # only the deferred one is looked at again


class _Mailbox(FakeProvider):
    def __init__(self, ok=True, msg="connected"):
        super().__init__()
        self.answer = (ok, msg)
        self.checks = 0

    async def test_connection(self):
        self.checks += 1
        return self.answer


@pytest.mark.asyncio
async def test_the_mailbox_health_check_raises_and_clears_an_alert(state):  # noqa: F811
    from harvey import health

    box = _Mailbox()
    out = await health.run_check(state, box, Cfg())
    assert out["ok"] and out["alert"] == "" and box.checks == 1
    # Not again within the half hour.
    await health.run_check(state, box, Cfg())
    assert box.checks == 1
    # A failed login: the alert says when and why.
    box.answer = (False, "SMTP login failed: 535 authentication failed")
    out = await health.run_check(state, box, Cfg(), force=True)
    assert not out["ok"] and "failed: SMTP login failed: 535" in out["alert"] and "Nothing is sent until" in out["alert"]
    assert (await health.picture(state))["alert"] == out["alert"]
    # Back to normal: the alert clears.
    box.answer = (True, "connected")
    out = await health.run_check(state, box, Cfg(), force=True)
    assert out["ok"] and out["alert"] == "" and (await health.picture(state))["alert"] == ""

    # Approved emails are due, nothing has left for hours, no pause explains it: an alert.
    await state.add_prospect(Prospect(id="h1", first_name="A", email="a@h.example", email_status="provided"))
    old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)).isoformat()
    await state.add_outbox_item(prospect_id="h1", to_email="a@h.example", subject="s", body="b", send_at=old, status="approved")
    await state.set_setting(health.LAST_SENT_KEY, old)
    out = await health.run_check(state, box, Cfg())
    assert "Nothing has been delivered since" in out["alert"] and "1 approved email is due" in out["alert"]
    # A deliberate pause is not a stall; quiet hours are not either.
    await state.set_setting("sender_skip_reason", "quiet hours until 08:00")
    assert (await health.run_check(state, box, Cfg()))["alert"] == ""
    await state.set_setting("sender_skip_reason", "")
    assert (await health.run_check(state, box, Cfg(), quiet=True))["alert"] == ""
    await state.set_setting(health.LAST_SENT_KEY, datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    assert (await health.run_check(state, box, Cfg()))["alert"] == ""


def test_the_command_center_knows_the_goal_and_the_brief(tmp_path):
    from bb.mail import scope as scope_mod

    cfg = {"persona": {"name": "Jane Doe"},
           "outreach": {"mode": "inquiry", "intent": NEED, "goal": "Get the brief into their hands.",
                        "reply_attachments": ["brief.pdf", "missing.pdf"], "attach_on": "first_reply"}}
    scope = scope_mod.from_config(cfg)
    assert scope["goal"] == "Get the brief into their hands." and scope["attachments"] == ["brief.pdf", "missing.pdf"]
    block = scope_mod.context_block(scope)
    assert "CAMPAIGN GOAL, in Jane Doe's words" in block and "THE PROJECT BRIEF IS ATTACHED" in block and "brief.pdf" in block
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "brief.pdf").write_bytes(b"%PDF")
    assert scope_mod.attachment_paths(tmp_path, scope, prior_outbound=0) == [str(tmp_path / "attachments" / "brief.pdf")]
    assert scope_mod.attachment_paths(tmp_path, scope, prior_outbound=1) == []
    scope["attach_on"] = "every_reply"
    assert scope_mod.attachment_paths(tmp_path, scope, prior_outbound=3) == [str(tmp_path / "attachments" / "brief.pdf")]
    scope["attach_on"] = "never"
    assert scope_mod.attachment_paths(tmp_path, scope, prior_outbound=0) == [] and "ATTACHED" not in scope_mod.context_block(scope)


@pytest.mark.asyncio
async def test_a_command_center_reply_carries_the_brief_as_a_file(tmp_path, monkeypatch):
    import yaml

    from bb.mail import crypto, sending
    from tests.test_mail_reply import ME, MailDB, MailboxSettings, _seed_thread, service

    class _Box:
        sent = []

        def __init__(self, mailbox, creds, env):
            pass

        async def send(self, **kw):
            _Box.sent.append(kw)
            return "<out1@mine>", "", ""

    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: _Box(m, c, e))
    root = tmp_path / "ws"
    (root / "attachments").mkdir(parents=True)
    (root / "attachments" / "brief.pdf").write_bytes(b"%PDF")
    (root / "harvey.yaml").write_text(yaml.safe_dump({"outreach": {"mode": "inquiry", "intent": NEED, "reply_attachments": ["brief.pdf"],
                                                                     "attach_on": "first_reply"}, "persona": {"name": "Jane"}}))
    db = MailDB(root / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds=crypto.seal_json("s", {}), status="ok")
    await db.update_mailbox(mid, settings=MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://acme.test").model_dump())
    tid = await _seed_thread(db, mid)
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, 'reply', ?, 'Re: Site visit', 'Tuesday works; the brief is attached so you have the whole thing.', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    draft = await db.one("SELECT * FROM drafts WHERE id = 'd1'")
    env = {"HARVEY_HOME": str(root)}
    assert (await sending.send_draft(db, "s", env, draft))["status"] == "sent"
    assert _Box.sent[-1]["attachments"] == [str(root / "attachments" / "brief.pdf")]
    assert any("Attached brief.pdf" in a["text"] for a in await db.activity())
    # The second reply in the same thread: the brief went already.
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('d2', ?, ?, 'reply', ?, 'Re: Site visit', 'Ten works for me as well, see you then.', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    await db.update_mailbox(mid, settings=MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://acme.test", daily_cap=0).model_dump())
    draft2 = await db.one("SELECT * FROM drafts WHERE id = 'd2'")
    assert (await sending.send_draft(db, "s", env, draft2))["status"] == "sent"
    assert _Box.sent[-1]["attachments"] is None
