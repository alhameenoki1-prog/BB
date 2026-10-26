"""Tests for the native sending path: gate, outbox lifecycle, stage/drain,
stop-on-reply, bounces, and the kill switch."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from harvey.gate import pre_send_check
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbound = []
        self.fail_next = False

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **kw):
        if self.fail_next:
            self.fail_next = False
            return SendResult(ok=False, error="smtp boom")
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@x>", thread_ref="t1")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


class Cfg:
    class persona:
        name = "Harvey"; email = "harvey@x.co"; company = "X"; role = "BD"; tone = "direct"

    class product:
        name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
        objection_responses = {}

    class channels:
        class email:
            enabled = True
            provider = "smtp"
            max_daily_sends = 50
            send_to_risky = False
            require_approval = True
            max_bounce_rate = 0.05

        class linkedin:
            enabled = False


class Env:
    instantly_api_key = ""


def make_sender(state, provider, require_approval=True):
    from harvey.agents.sender import Sender
    cfg = Cfg()
    cfg.channels.email.require_approval = require_approval
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = provider  # override registry lookup
    sender.send_pacing = False  # no inter-send sleeps in tests
    return sender


class StubBrain:
    """Minimal brain: canned intent + reply text, no subprocess."""
    def __init__(self, intent="question", reply="Sure, happy to help."):
        self._intent = intent
        self._reply = reply

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        return self._intent if task == "classify_intent" or "category" in prompt.lower() else self._reply


def make_handler(state, provider, intent="question"):
    from harvey.agents.handler import Handler
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=Cfg(), env=Env())
    handler.provider = provider
    return handler


async def seed_prospect(state, email="jane@acme.com", status="new", estatus="verified"):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme",
        email=email, email_status=estatus, email_verified=(estatus == "verified"),
        status=status,
    ))


async def seed_campaign(state, prospect_ids):
    campaign = Campaign(
        id="", name="test-campaign", channel="email",
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}", body="Body for {{company}}. Question?", delay_days=0),
            EmailStep(step=2, subject="follow", body="Another angle {{first_name}}.", delay_days=3),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── gate ──


def test_gate_blocks_bad_content():
    p = Prospect(first_name="J", last_name="D", title="VP",
                 email="jane@acme.com", email_status="verified")
    ok = pre_send_check("jane@acme.com", "hello", "Short and fine. Question?", p)
    assert ok.ok

    bad = pre_send_check("jane@acme.com", "hello", "Hi {{first_name}}, act now!", p)
    assert not bad.ok
    assert any("merge" in r for r in bad.reasons)
    assert any("banned" in r for r in bad.reasons)

    guess = Prospect(first_name="J", last_name="D", title="VP",
                     email="jane@acme.com", email_status="guess")
    blocked = pre_send_check("jane@acme.com", "hello", "Fine body.", guess)
    assert not blocked.ok

    # Replies to someone who emailed us are exempt from the status gate
    reply_ok = pre_send_check("jane@acme.com", "Re: hi", "Thanks!", guess, kind="reply")
    assert reply_ok.ok


def test_gate_link_and_html_limits():
    r = pre_send_check("a@b.co", "s", "See https://a.com and https://b.com")
    assert any("links" in x for x in r.reasons)
    r2 = pre_send_check("a@b.co", "s", "<div>hello</div>")
    assert any("HTML" in x for x in r2.reasons)


def test_bounce_detection():
    assert looks_like_bounce("mailer-daemon@googlemail.com", "anything")
    assert looks_like_bounce("mx@x.com", "Delivery Status Notification (Failure)")
    assert not looks_like_bounce("jane@acme.com", "Re: your note")


# ── staging ──


@pytest.mark.asyncio
async def test_stage_campaign_renders_and_respects_approval(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider(), require_approval=True)

    await sender._run_native()

    pending = await state.get_outbox(status="pending_review")
    assert len(pending) == 2  # both steps staged
    step1 = next(i for i in pending if i["step"] == 1)
    assert step1["subject"] == "hi Jane"
    assert "Acme" in step1["body"]
    # Nothing sent while awaiting approval
    assert sender.provider.sent == []

    # Idempotent: re-running stages nothing new
    campaign2 = await state.get_campaigns_by_status("active")
    assert campaign2[0].id == campaign.id
    await sender._run_native()
    assert len(await state.get_outbox(status="pending_review")) == 2


@pytest.mark.asyncio
async def test_stage_skips_unverified(state):
    pid = await seed_prospect(state, email="guess@acme.com", estatus="guess")
    await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider())
    await sender._run_native()
    assert await state.get_outbox(status="pending_review") == []


# ── drain ──


@pytest.mark.asyncio
async def test_drain_sends_approved_due_items(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)

    await sender._run_native()  # stage as approved + drain in one pass

    sent = await state.get_outbox(status="sent")
    assert len(sent) == 1                     # step 1 due now; step 2 in 3 days
    assert provider.sent[0]["to"] == "jane@acme.com"
    assert provider.sent[0]["subject"] == "hi Jane"
    prospect = await state.get_prospect(pid)
    assert prospect.status == "contacted"
    approved_left = await state.get_outbox(status="approved")
    assert len(approved_left) == 1 and approved_left[0]["step"] == 2


@pytest.mark.asyncio
async def test_drain_respects_kill_switch(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await state.set_setting("sending_paused", "test pause")

    await sender._run_native()
    assert provider.sent == []
    assert await state.get_outbox(status="sent") == []


@pytest.mark.asyncio
async def test_drain_gate_failure_marks_failed(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Unrendered {{first_name}} tag",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert "gate" in failed[0]["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_send_failure_recorded(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Fine body. Question?",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    provider.fail_next = True
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert failed and "smtp boom" in failed[0]["error"]


# ── handler: stop-on-reply, bounces, kill switch ──


@pytest.mark.asyncio
async def test_reply_cancels_pending_outbox(state):
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=2,
    )
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="unsubscribe please",
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    cancelled = await state.get_outbox(status="cancelled")
    assert len(cancelled) == 1
    prospect = await state.get_prospect(pid)
    assert prospect.status == "opted_out"   # opt-out keywords honored


@pytest.mark.asyncio
async def test_bounce_marks_invalid_and_kill_switch(state, monkeypatch):
    import harvey.agents.handler as handler_mod
    monkeypatch.setattr(handler_mod, "MIN_SENDS_FOR_KILL_SWITCH", 10)   # a small sample, for the test
    pid = await seed_prospect(state, status="contacted")
    # A sent item whose message-id the bounce references
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    await state.update_outbox_item(
        item_id, status="sent", message_id="<m1@x>", sent_at=_now_iso()
    )
    # Ten more sent items so the kill-switch minimum-sample rule is met
    for i in range(10):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
            send_at=_now_iso(), status="approved", campaign_id=f"c{i+2}", step=1,
        )
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())

    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="couldn't be delivered\n\nThe response was:\n550 5.1.1 The email account that you "
             "tried to reach does not exist.", in_reply_to="<m1@x>", is_bounce=True,
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "invalid"
    # The sent item carries the receiving server's words and what to do.
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "sent" and item["error"].startswith("Bounced: 550 5.1.1")
    assert "marked invalid" in item["error"]
    # 1 bounce / 11 sent = 9% > 5% threshold with >= 10 sends → kill switch
    assert await state.get_setting("sending_paused") != ""


@pytest.mark.asyncio
async def test_inbound_dedup(state):
    await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="we're all set thanks",
    )]
    handler = make_handler(state, provider, intent="not_interested")
    await handler._run_native()
    first = await state.get_conversations_by_status("closed")
    await handler._run_native()   # same message again → ignored via dedup
    second = await state.get_conversations_by_status("closed")
    assert len(first) == len(second) == 1


class SlowProvider(FakeProvider):
    """Records how many sends are in flight at once."""
    def __init__(self):
        super().__init__()
        self.in_flight = 0; self.peak = 0

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **kw):
        import asyncio as _a
        self.in_flight += 1; self.peak = max(self.peak, self.in_flight)
        try:
            await _a.sleep(0.02)
            return await super().send_email(to_email, subject, body, thread_ref, in_reply_to)
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_drain_sends_several_at_once_when_the_channel_allows(state):
    """send_concurrency lanes run side by side; 1 keeps the one-at-a-time order."""
    pids = [await seed_prospect(state, email=f"p{i}@acme{i}.com") for i in range(8)]
    await seed_campaign(state, pids)
    provider = SlowProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.channels.email.sends_per_cycle = 8
    sender.config.channels.email.send_concurrency = 4
    sender.config.channels.email.send_jitter_seconds = 0
    await sender._run_native()
    assert len(await state.get_outbox(status="sent")) == 8 and 2 <= provider.peak <= 4
    assert len({s["to"] for s in provider.sent}) == 8
    # A second batch, one lane: never more than one in flight.
    pids = [await seed_prospect(state, email=f"q{i}@acme{i}.com") for i in range(4)]
    await seed_campaign(state, pids)
    provider.peak = 0
    sender.config.channels.email.send_concurrency = 1
    await sender._run_native()
    assert provider.peak == 1 and len(await state.get_outbox(status="sent")) == 12


@pytest.mark.asyncio
async def test_outbox_rows_are_claimed_once_and_stale_sends_are_failed(state):
    from datetime import datetime, timedelta, timezone
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    assert len(provider.sent) == 1
    item = (await state.get_outbox(status="approved"))[0]          # step 2, days ahead
    assert await state.claim_outbox_item(item["id"]) is True and await state.claim_outbox_item(item["id"]) is False
    assert (await state.get_outbox_item(item["id"]))["status"] == "sending"
    # A row stuck in 'sending' for a long time is failed, not resent.
    old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)).isoformat()
    async with state._connect() as db:
        await db.execute("UPDATE outbox SET updated_at = ? WHERE id = ?", (old, item["id"])); await db.commit()
    assert await state.release_stale_sending() == 1
    assert (await state.get_outbox_item(item["id"]))["status"] == "failed"
    # _send_one on a row that is not approved sends nothing.
    assert await sender._send_one({**item, "status": "failed"}, await state.get_prospect(pid), "") is False
    assert len(provider.sent) == 1


class FlakyProvider(FakeProvider):
    """Refuses with a temporary error until told otherwise."""
    def __init__(self):
        super().__init__()
        self.throttle = True
    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **kw):
        if self.throttle:
            return SendResult(ok=False, error="454 4.7.0 Too many login attempts, please try again later", retryable=True, scope="mailbox")
        return await super().send_email(to_email, subject, body, thread_ref, in_reply_to)


@pytest.mark.asyncio
async def test_temporary_refusals_are_retried_later_not_buried(state):
    from harvey.agents.sender import MAX_SEND_RETRIES
    pids = [await seed_prospect(state, email=f"r{i}@acme{i}.com") for i in range(3)]
    await seed_campaign(state, pids)
    provider = FlakyProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.channels.email.send_concurrency = 2
    sender.config.channels.email.send_jitter_seconds = 0
    try:
        await sender._run_native()
    finally:
        # Cfg is a shared class: leave no lanes behind for the next test.
        del sender.config.channels.email.send_concurrency
        del sender.config.channels.email.send_jitter_seconds
    rows = await state.get_outbox(status="approved")
    retried = [r for r in rows if (r["error"] or "").startswith("retry 1/")]
    assert len(retried) >= 1 and all(r["send_at"] > _now_iso() for r in retried)   # pushed out, still approved
    assert not await state.get_outbox(status="failed")
    assert len(rows) == 6 and provider.sent == []                                 # the pass ended at the first refusal
    # Once the provider recovers, the next drain sends them.
    provider.throttle = False
    for r in rows:
        if r["step"] == 1:
            await state.update_outbox_item(r["id"], send_at=_now_iso())
    await sender._drain_due()
    assert len(provider.sent) == 3
    # After the last allowed retry the row is failed for good.
    item = (await state.get_outbox(status="approved"))[0]
    await state.update_outbox_item(item["id"], error=f"retry {MAX_SEND_RETRIES}/{MAX_SEND_RETRIES}: x", send_at=_now_iso())
    provider.throttle = True
    await sender._drain_due()
    assert (await state.get_outbox_item(item["id"]))["status"] == "failed"


def test_smtp_errors_are_classified_and_relays_have_no_inbox():
    import aiosmtplib
    from harvey.integrations.smtp_mail import SmtpImapProvider, classify_smtp_error
    from harvey.integrations.mail_provider import is_relay_host
    throttle = classify_smtp_error(aiosmtplib.SMTPResponseException(421, "4.7.0 Try again later"))
    assert throttle["retryable"] and throttle["scope"] == "mailbox"
    assert not classify_smtp_error(aiosmtplib.SMTPResponseException(550, "5.1.1 User unknown"))["retryable"]
    auth = classify_smtp_error(aiosmtplib.SMTPAuthenticationError(535, "Username and Password not accepted"))
    assert auth["auth_failed"] and auth["scope"] == "mailbox"
    grey = classify_smtp_error(aiosmtplib.SMTPResponseException(450, "4.7.1 Greylisted, try again later"))
    assert grey["retryable"] and grey["scope"] == "mailbox"   # "try again" wording: the server is pacing the session
    grey2 = classify_smtp_error(aiosmtplib.SMTPResponseException(450, "4.2.2 Mailbox full"))
    assert grey2["retryable"] and grey2["scope"] == "row"
    limit = classify_smtp_error(aiosmtplib.SMTPResponseException(550, "5.4.5 Daily user sending limit exceeded"))
    assert limit["daily_limit"] and limit["retryable"] and limit["scope"] == "mailbox"
    assert classify_smtp_error(aiosmtplib.SMTPConnectError("refused"))["scope"] == "mailbox" and classify_smtp_error(OSError("reset"))["retryable"]
    assert is_relay_host("email-smtp.eu-west-1.amazonaws.com") and is_relay_host("smtp-relay.brevo.com")
    assert not is_relay_host("imap.gmail.com") and not is_relay_host("imap.resend.community") and not is_relay_host("mail.mailjet.company")

    class Env:
        smtp_host = "email-smtp.eu-west-1.amazonaws.com"; smtp_port = 2587; smtp_username = "AKIA"; smtp_password = "x"
        imap_host = ""; imap_port = 993; imap_username = ""; imap_password = ""; smtp_reply_to = ""
    p = SmtpImapProvider(Cfg(), Env())
    assert p._no_inbox() and p._start_tls() is True and not p._use_tls()     # 2587 is a submission port: must upgrade
    Env.smtp_port = 25
    assert SmtpImapProvider(Cfg(), Env())._start_tls() is None
    Env.smtp_port = 465
    assert SmtpImapProvider(Cfg(), Env())._use_tls()


@pytest.mark.asyncio
async def test_daily_limit_refusals_wait_for_tomorrow(state):
    from datetime import datetime, timedelta, timezone
    pid = await seed_prospect(state, email="d@acme.com")
    await seed_campaign(state, [pid])

    class LimitProvider(FakeProvider):
        async def send_email(self, *a, **k):
            return SendResult(ok=False, error="550 5.4.5 Daily user sending limit exceeded", retryable=True,
                              scope="mailbox", daily_limit=True)
    sender = make_sender(state, LimitProvider(), require_approval=False)
    sender.config.channels.email.send_jitter_seconds = 0
    await sender._run_native()
    row = [r for r in await state.get_outbox(status="approved") if r["step"] == 1][0]
    tomorrow = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=23)).isoformat()
    assert not row["error"].startswith("retry") and "limit" in row["error"] and row["send_at"] > tomorrow   # not a strike against the email

    # A refused login: rows stay approved and untouched, the pass ends, the reason is recorded for the dashboard.
    class AuthProvider(FakeProvider):
        async def send_email(self, *a, **k):
            return SendResult(ok=False, error="535 5.7.8 Username and Password not accepted", retryable=True,
                              scope="mailbox", auth_failed=True)
    pid2 = await seed_prospect(state, email="e@acme.com")
    await seed_campaign(state, [pid2])
    sender = make_sender(state, AuthProvider(), require_approval=False)
    sender.config.channels.email.send_jitter_seconds = 0
    await sender._run_native()
    rows = [r for r in await state.get_outbox(status="approved") if r["prospect_id"] == pid2]
    assert len(rows) == 2 and not await state.get_outbox(status="failed")
    assert any((r["error"] or "").startswith("waiting:") for r in rows)
    assert "refused the login" in (await state.get_setting("sender_skip_reason") or "")


@pytest.mark.asyncio
async def test_a_mailbox_that_cannot_be_read_holds_sending(state):
    """An IMAP failure is not "no new replies": the handler reports it and the sender waits."""
    from harvey.integrations.mail_provider import MailReadError
    from harvey.integrations.smtp_mail import SmtpImapProvider

    class Env:
        smtp_host = "smtp.example.com"; smtp_port = 587; smtp_username = "me@example.com"; smtp_password = "x"
        imap_host = "imap.example.com"; imap_port = 993; imap_username = ""; imap_password = ""; smtp_reply_to = ""
    provider = SmtpImapProvider(Cfg(), Env())

    def boom(limit):
        raise OSError("connection refused")
    provider._fetch_unseen = boom
    with pytest.raises(MailReadError):
        await provider.get_replies()
    handler = make_handler(state, provider)
    assert await handler._run_native() is False
    assert handler.last_problem.startswith("the mailbox could not be read: ")
    # A paused model with a reply to classify: the reply is left for later,
    # and the reason names the model, not the mailbox.
    pid = await seed_prospect(state, status="contacted")
    fake = FakeProvider()
    fake.inbound = [InboundMessage(provider_id="in9", from_email="jane@acme.com", subject="Re: hi", body="Tell me more?")]
    paused = make_handler(state, fake)

    class _Paused(StubBrain):
        paused_reason = "LLM API usage limit reached for this plan"
        async def think(self, *a, **k):
            return ""
    paused.brain = _Paused()
    assert await paused._run_native() is False
    assert paused.last_problem == "the AI can't answer right now: LLM API usage limit reached for this plan"
    assert not await state.is_reply_processed("in9")           # read again once the model is back
    # A pass that reads everything clears it.
    fake.inbound = []
    assert await paused._run_native() is True and paused.last_problem == ""


@pytest.mark.asyncio
async def test_replies_go_when_the_daily_limit_is_used_up(state, tmp_path, monkeypatch):
    """The daily limit, the warm-up and the account's share are for first
    emails and follow-ups. Replies have no limit and do not use it up."""
    from harvey.ledger import Ledger
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", "harvey@x.co")
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    led = Ledger(tmp_path / "sendlog.db")
    led.set_limit("harvey@x.co", 2)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.channels.email.max_daily_sends = 2
    try:
        ids = [await seed_prospect(state, email=f"p{i}@acme.com") for i in range(4)]
        await seed_campaign(state, ids)
        await sender._run_native()
        assert len(provider.sent) == 2                       # two of four first emails: the limit
        # Three people wrote back; their answers go although the day's limit is used.
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        for i in range(3):
            await state.add_outbox_item(prospect_id=ids[i], to_email=f"p{i}@acme.com", subject="Re: hi", body="Thanks, Tuesday works.",
                                        send_at=now, status="approved", kind="reply", in_reply_to=f"<in{i}@acme.com>")
        await sender._run_native()
        assert len(provider.sent) == 5
        assert [s["to"] for s in provider.sent[2:]] == ["p0@acme.com", "p1@acme.com", "p2@acme.com"]
        # The first emails still wait, and the replies used none of the limit.
        assert await state.count_outbox_sent_today(kind="sequence") == 2
        assert led.summary("harvey@x.co")["total"] == 2 and led.summary("harvey@x.co")["answers"] == 3
        await sender._run_native()
        assert len(provider.sent) == 5
    finally:
        sender.config.channels.email.max_daily_sends = 50


@pytest.mark.asyncio
async def test_a_shared_limit_used_up_mid_pass_holds_first_emails_not_replies(state, tmp_path, monkeypatch):
    from harvey.ledger import Ledger
    monkeypatch.setenv("HARVEY_LEDGER", str(tmp_path / "sendlog.db"))
    monkeypatch.setenv("HARVEY_LEDGER_ACCOUNT", "harvey@x.co")
    monkeypatch.setenv("HARVEY_LEDGER_CAMPAIGN", "c-1")
    Ledger(tmp_path / "sendlog.db").set_limit("harvey@x.co", 10)
    real = Ledger.reserve

    def taken(self, *a, free=False, **kw):     # another campaign takes the last slot meanwhile
        return real(self, *a, free=free, **kw) if free else (None, {"reason": "the account's daily limit of 10 is reached"})
    monkeypatch.setattr(Ledger, "reserve", taken)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    ids = [await seed_prospect(state, email=f"p{i}@acme.com") for i in range(2)]
    campaign = await seed_campaign(state, ids)
    await sender._stage_campaign_native(campaign)          # the first emails are queued first...
    await __import__("asyncio").sleep(0.01)
    await state.add_outbox_item(prospect_id=ids[0], to_email="p0@acme.com", subject="Re: hi", body="Thanks, Tuesday works.",
                                send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), status="approved",
                                kind="reply", in_reply_to="<in0@acme.com>")   # ...and the reply after them
    due = await state.get_outbox(status="approved", due_before=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    assert [i["kind"] for i in sorted(due, key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""))] == ["sequence", "sequence", "reply"]
    await sender._drain_due()
    # The first emails wait for room; the reply, queued after them, still went.
    assert [s["to"] for s in provider.sent] == ["p0@acme.com"] and provider.sent[0]["subject"] == "Re: hi"


@pytest.mark.asyncio
async def test_the_mailbox_maximum_counts_replies_and_a_pass_ends_in_time(state, monkeypatch):
    import harvey.agents.sender as sender_mod
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.channels.email.mailbox_daily_ceiling = 10
    try:
        ids = [await seed_prospect(state, email=f"p{i}@acme.com") for i in range(12)]
        await seed_campaign(state, ids)
        await sender._run_native()
        assert len(provider.sent) == 8                  # per pass: 8 (and first emails may go up to 9 of 10)
        await sender._run_native()
        assert len(provider.sent) == 9                  # the tenth is kept for a reply
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        for i in range(3):
            await state.add_outbox_item(prospect_id=ids[i], to_email=f"p{i}@acme.com", subject="Re: hi", body="Thanks, Tuesday works.",
                                        send_at=now, status="approved", kind="reply", in_reply_to=f"<r{i}@acme.com>")
        await sender._run_native()
        # One reply fits under the mailbox's maximum; the others wait (the provider would refuse them).
        assert len(provider.sent) == 10 and provider.sent[-1]["subject"] == "Re: hi"
    finally:
        sender.config.channels.email.mailbox_daily_ceiling = 0
    # A pass stops taking new emails once its time is up; the rest stay approved for the next one.
    monkeypatch.setattr(sender_mod, "PASS_SECONDS", -1)
    before = len(provider.sent)
    await sender._run_native()
    assert len(provider.sent) == before and [i for i in await state.get_outbox(status="approved") if i["kind"] == "reply"]


@pytest.mark.asyncio
async def test_first_only_holds_follow_ups_and_sends_first_emails_and_replies(state):
    """While the model cannot run (rate-limited, out of allowance, replies
    unread), a follow-up could land on someone who has answered, so it waits.
    A first email needs no model and a reply is already written: both go."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    first = await seed_prospect(state, email="new@acme.com")
    second = await seed_prospect(state, email="old@acme.com", status="contacted")
    third = await seed_prospect(state, email="wrote@acme.com", status="replied")
    await state.add_outbox_item(prospect_id=first, to_email="new@acme.com", subject="hello",
                                body="A first email. Interested?", send_at=now, status="approved", campaign_id="c1", step=1)
    await state.add_outbox_item(prospect_id=second, to_email="old@acme.com", subject="hello again",
                                body="Bumping this. Any thoughts?", send_at=now, status="approved", campaign_id="c1", step=2)
    await state.add_outbox_item(prospect_id=third, to_email="wrote@acme.com", subject="Re: hello", body="Thanks, Tuesday works.",
                                send_at=now, status="approved", kind="reply", in_reply_to="<in1@acme.com>")
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender.run(first_only=True)
    assert sorted(s["to"] for s in provider.sent) == ["new@acme.com", "wrote@acme.com"]
    left = await state.get_outbox(status="approved")
    assert len(left) == 1 and left[0]["step"] == 2 and left[0]["to_email"] == "old@acme.com"
    # The model is back: a plain pass sends the follow-up.
    await sender.run()
    assert sorted(s["to"] for s in provider.sent) == ["new@acme.com", "old@acme.com", "wrote@acme.com"]
    assert await state.get_outbox(status="approved") == []



@pytest.mark.asyncio
async def test_first_only_finds_first_emails_behind_a_crowd_of_due_follow_ups(state):
    """Held follow-ups pile up at the front of the queue while the model is
    down; the query itself leaves them out, so a first email behind them
    still goes."""
    yesterday = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)).isoformat()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    for i in range(60):
        pid = await seed_prospect(state, email=f"old{i}@acme.com", status="contacted")
        await state.add_outbox_item(prospect_id=pid, to_email=f"old{i}@acme.com", subject="again",
                                    body="Bumping this. Any thoughts?", send_at=yesterday, status="approved",
                                    campaign_id="c1", step=2)
    new = await seed_prospect(state, email="new@acme.com")
    await state.add_outbox_item(prospect_id=new, to_email="new@acme.com", subject="hello",
                                body="A first email. Interested?", send_at=now, status="approved", campaign_id="c2", step=1)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.config.channels.email.sends_per_cycle = 2          # a fetch window of 6, all follow-ups
    try:
        await sender.run(first_only=True)
    finally:
        del sender.config.channels.email.sends_per_cycle          # the shared test config, as it was
    assert [m["to"] for m in provider.sent] == ["new@acme.com"]


@pytest.mark.asyncio
async def test_first_only_stages_only_what_needs_no_model(state):
    """On autopilot, staging an AI-written campaign runs the spam check, whose
    fix is a model call: with the model out of reach it stays a draft. The
    operator's own message is staged and sent."""
    ai = await seed_prospect(state, email="ai@acme.com")
    mine = await seed_prospect(state, email="mine@acme.com")
    await seed_campaign(state, [ai])                                   # "test-campaign"
    await state.add_campaign(Campaign(id="", name="your message · other · owner", channel="email",
                                      sequence=[EmailStep(step=1, subject="hi {{first_name}}",
                                                          body="Body for {{company}}. Question?", delay_days=0)],
                                      prospect_ids=[mine], status="draft"))
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender.run(first_only=True)
    assert [m["to"] for m in provider.sent] == ["mine@acme.com"]
    assert [c.name for c in await state.get_campaigns_by_status("draft")] == ["test-campaign"]
    await sender.run()                                                 # the model is back
    assert sorted(m["to"] for m in provider.sent) == ["ai@acme.com", "mine@acme.com"]


@pytest.mark.asyncio
async def test_without_the_model_bounces_are_still_read_and_replies_wait(state):
    """A pass with no model (over budget) handles bounces (address invalid,
    queued follow-ups cancelled) and leaves human replies for later; a reply
    the model cannot answer no longer stops the bounces behind it."""
    async def sent_to(email, mid):
        pid = await seed_prospect(state, email=email, status="contacted")
        item = await state.add_outbox_item(prospect_id=pid, to_email=email, subject="s", body="b",
                                           send_at=_now_iso(), status="approved", campaign_id="c1", step=1)
        await state.update_outbox_item(item, status="sent", message_id=mid, sent_at=_now_iso())
        await state.add_outbox_item(prospect_id=pid, to_email=email, subject="again", body="b",
                                    send_at=_now_iso(), status="approved", campaign_id="c1", step=2)
        return pid

    def bounce_of(mid, pid_):
        return InboundMessage(provider_id=pid_, from_email="mailer-daemon@googlemail.com",
                              subject="Delivery Status Notification (Failure)",
                              body="couldn't be delivered\n\nThe response was:\n550 5.1.1 The email account that you "
                                   "tried to reach does not exist.", in_reply_to=mid, is_bounce=True)

    await seed_prospect(state, email="jane@acme.com", status="contacted")
    dead = await sent_to("gone@acme.com", "<m1@x>")
    reply = InboundMessage(provider_id="r1", from_email="jane@acme.com", subject="Re: hi", body="Tell me more?")
    provider = FakeProvider()
    provider.inbound = [reply, bounce_of("<m1@x>", "b1")]
    handler = make_handler(state, provider)
    assert await handler.run(backlog=False, model=False) is False
    assert (await state.get_prospect(dead)).email_status == "invalid"
    assert await state.is_reply_processed("b1") and not await state.is_reply_processed("r1")
    assert handler.last_problem == "the AI budget is used up, so replies wait for it"
    # The model turning out to be down mid-pass: the bounce behind the reply is still read.
    class _Paused(StubBrain):
        paused_reason = "LLM API usage limit reached for this plan"
        async def think(self, *a, **k):
            return ""
    other = await sent_to("gone2@acme.com", "<m2@x>")
    provider.inbound = [reply, bounce_of("<m2@x>", "b2")]
    handler.brain = _Paused()
    assert await handler.run(backlog=False) is False
    assert (await state.get_prospect(other)).email_status == "invalid" and not await state.is_reply_processed("r1")
    assert handler.last_problem.startswith("the AI can't answer right now")


@pytest.mark.asyncio
async def test_instantly_deploys_nothing_while_replies_cannot_be_read(state):
    """Instantly runs the whole sequence once deployed: there is no
    first-emails-only, so a first_only pass deploys nothing."""
    from harvey.agents.sender import Sender
    sender = make_sender(state, FakeProvider())
    sender.config.channels.email.provider = "instantly"
    deployed = []

    async def deploy(campaign):
        deployed.append(campaign.name)
    sender._deploy_campaign = deploy
    try:
        pid = await seed_prospect(state)
        await seed_campaign(state, [pid])
        assert not sender.is_native
        await Sender.run(sender, first_only=True)
        assert deployed == []
    finally:
        sender.config.channels.email.provider = "smtp"
