"""The third review round: what the adversarial readers found in the
no-handling, the follow-up runs and the autonomy build, pinned."""
import asyncio
import json

import pytest

from bb.mail import crypto, runs
from bb.mail import scope as scope_mod
from bb.mail.db import MailDB
from bb.mail.ingest import Ingest
from bb.mail.models import MailboxSettings
from bb.mail.providers.base import RawMessage
from harvey import enquiry
from harvey.config import EnvConfig
from harvey.integrations.mail_provider import SendResult
from harvey.models.conversation import Conversation
from harvey.models.prospect import Prospect
from tests.test_inquiry_mail import CFG
from tests.test_inquiry_voice import NEEDS
from tests.test_mail_reply import ME as MINE, FakeBrain, SentBox, _seed_thread, service
from tests.test_no_and_reconnect import _handler, _raw
from tests.test_outbox_native import Cfg, FakeProvider, make_sender, state  # noqa: F401
from tests.test_outreach import _config


def test_ordinary_words_are_not_a_no_and_quoted_history_is_not_their_words():
    fine = ["Sorry for the slow reply, your email went to my spam folder. Yes, we'd take this on; send the drawings.",
            "I thought this was a scam at first, glad it's real.", "Our attorney Jane handles the leases; she'd be happy to take this on.",
            "I'm a lawyer, so I can take the lease matter on.", "We go away for the holidays in August; can we talk in September?",
            "Under GDPR we need a DPA; happy to proceed otherwise.", "I've booked you in for Tuesday at ten.",
            "I'll pass this on to our estimator, Mark, who will quote it.", "I'm not able to open the link, can you resend?",
            "Can't today, maybe later this week?", "Not too busy to help, send it over.", "I don't do mornings, could we make it the afternoon?"]
    for t in fine:
        assert not enquiry.is_opt_out(t) and not enquiry.is_soft_no(t), t
    quoted = "Yes, happy to take this on. When can we speak?\n\nOn Mon, Jane wrote:\n> I'm after a lawyer for a lease dispute; unsubscribe link below\n> Not interested in anything else"
    assert enquiry.fresh_text(quoted) == "Yes, happy to take this on. When can we speak?"
    assert not enquiry.is_opt_out(quoted) and not enquiry.is_soft_no(quoted)
    assert enquiry.is_opt_out("Please stop.\n-----Original Message-----\nFrom: Jane") and enquiry.is_opt_out("I'll have my lawyer look at this.")
    # The closing note loses its questions but keeps the marker line, before the link goes in.
    body = "Thanks for the straight answer.\n\nWould it help to have the brief anyway? It is short.\n[SCOPE LINK]\n\nIs there anyone\nelse I should ask?"
    assert enquiry.strip_questions(body) == "Thanks for the straight answer.\n\nIt is short.\n[SCOPE LINK]"


@pytest.mark.asyncio
async def test_a_suppressed_or_closed_person_gets_nothing_and_an_objection_with_a_condition_stays_open(state):  # noqa: F811
    for pid, email, status in (("o1", "a@one.example", "opted_out"), ("o2", "b@two.example", "lost"),
                               ("o3", "c@three.example", "contacted"), ("o4", "d@four.example", "contacted")):
        await state.add_prospect(Prospect(id=pid, first_name="A", email=email, email_status="provided", industry="construction", status=status))
    handler = await _handler(state, "interested")
    await handler._process_reply("a@one.example", "Actually, tell me more.", reply_uuid="", reply_meta={"subject": "Re: x", "message_id": "<1@x>"})
    await handler._process_reply("b@two.example", "Thanks for the note.", reply_uuid="", reply_meta={"subject": "Re: x", "message_id": "<2@x>"})
    assert await state.get_outbox() == []
    assert (await state.get_prospect("o1")).status == "opted_out" and (await state.get_prospect("o2")).status == "lost"
    # The classifier (not the words) said "escalate": a human looks, nobody is suppressed.
    handler = await _handler(state, "escalate")
    await handler._process_reply("c@three.example", "Sounds good, send me the dates and we will see.", reply_uuid="",
                                 reply_meta={"subject": "Re: x", "message_id": "<3@x>"})
    assert (await state.get_prospect("o3")).status == "replied" and await state.get_conversations_by_status("needs_human")
    # An objection that is a condition is answered and the conversation stays open.
    handler = await _handler(state, "objection")
    await handler._process_reply("d@four.example", "We'd need the budget before we can say; what is it?", reply_uuid="",
                                 reply_meta={"subject": "Re: x", "message_id": "<4@x>"})
    mine = [i for i in await state.get_outbox() if i["to_email"] == "d@four.example"]
    assert len(mine) == 1 and "No action needed" not in mine[0]["body"]
    assert (await state.get_prospect("o4")).status == "replied" and not await state.get_conversations_by_status("closed")
    assert "Accepts the condition or concern" in handler.brain.prompts[-1] and "CLOSING NOTE" not in handler.brain.prompts[-1]


@pytest.mark.asyncio
async def test_the_pre_send_rewrite_keeps_the_substance_when_the_model_drops_the_greeting(state):  # noqa: F811
    class _Brain:
        async def think_json(self, prompt, **kw):
            return {"subject": "Rebar, about 40 tons a month", "body": "I need about 40 tons of rebar a month for a year, "
                    "delivered to a Baltimore site.\n\nCan you supply that much every month?\n\nJane"}
    sender = make_sender(state, FakeProvider(), require_approval=False)
    sender.brain = _Brain()
    out = await sender._rewrite_for_filter("Exciting opportunity", "Hi Luis,\n\nFREE guarantee!!! Act now.\n\nJane", ["x"], None, "")
    assert out[1] == ("Hi Luis,\n\nI need about 40 tons of rebar a month for a year, delivered to a Baltimore site.\n\n"
                      "Can you supply that much every month?\n\nJane")

    class _Greets(_Brain):
        async def think_json(self, prompt, **kw):
            return {"subject": "Rebar", "body": "Hello Luis,\n\nPlain words about rebar.\n\nJane"}
    sender.brain = _Greets()
    assert (await sender._rewrite_for_filter("s", "Hi Luis,\n\nx\n\nJane", ["x"], None, ""))[1] == "Hi Luis,\n\nPlain words about rebar.\n\nJane"


def test_only_a_refusal_about_the_recipient_marks_the_address_invalid():
    import aiosmtplib
    from harvey.integrations.smtp_mail import classify_smtp_error
    about_us = aiosmtplib.SMTPRecipientRefused(550, "5.7.1 Relay access denied", "x@y.example")
    assert classify_smtp_error(about_us)["bad_address"] is False and classify_smtp_error(about_us)["retryable"] is False
    policy = aiosmtplib.SMTPRecipientRefused(554, "5.7.1 Sender address rejected: not owned by user", "x@y.example")
    assert classify_smtp_error(policy)["bad_address"] is False
    for msg in ("5.1.1 User unknown", "550 No such user here", "5.7.1 Recipient rejected - OXSUS003_407 - https://postmaster-oxsus.vadesecure.com/outbound_error_codes/#_407",
                "5.4.1 Recipient address rejected: Access denied"):
        assert classify_smtp_error(aiosmtplib.SMTPRecipientRefused(550, msg, "x@y.example"))["bad_address"] is True, msg


def test_attachment_names_are_names_and_the_mime_really_carries_the_file(tmp_path):
    from bb.mail.providers.base import MailAccount
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "brief.pdf").write_bytes(b"%PDF-1.4 brief")
    (tmp_path / "secret.txt").write_text("no")
    scope = {"mode": "inquiry", "attachments": ["brief.pdf", "../secret.txt", "/etc/passwd", ".hidden"], "attach_on": "first_reply"}
    assert scope_mod.attachment_paths(tmp_path, scope, 0) == [str((tmp_path / "attachments" / "brief.pdf").resolve())]
    assert "THE PROJECT BRIEF IS ATTACHED" not in scope_mod.context_block({**scope_mod.from_config(CFG), "attachments": ["brief.pdf"], "attach_now": False})
    assert "THE PROJECT BRIEF IS ATTACHED" in scope_mod.context_block({**scope_mod.from_config(CFG), "attachments": ["brief.pdf"]})

    class _Acct(MailAccount):
        def __init__(self):
            self.address = MINE
    msg = _Acct().build_mime(to=["ann@x.example"], subject="Re: hi", body_text="plain", body_html="<p>plain</p>",
                             attachments=[str(tmp_path / "attachments" / "brief.pdf"), str(tmp_path / "missing.pdf")])
    parts = [p for p in msg.walk() if p.get_filename()]
    assert [p.get_filename() for p in parts] == ["brief.pdf"] and parts[0].get_payload(decode=True) == b"%PDF-1.4 brief"
    assert parts[0].get_content_type() == "application/pdf" and msg.get_content_type() == "multipart/mixed"


@pytest.mark.asyncio
async def test_the_first_reply_after_our_own_cold_email_still_carries_the_brief(tmp_path, monkeypatch):
    import yaml
    from bb.mail import sending
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    SentBox.sent.clear()
    root = tmp_path / "ws"
    (root / "attachments").mkdir(parents=True)
    (root / "attachments" / "brief.pdf").write_bytes(b"%PDF")
    (root / "harvey.yaml").write_text(yaml.safe_dump({"outreach": {"mode": "inquiry", "intent": "x", "reply_attachments": ["brief.pdf"],
                                                                     "attach_on": "first_reply"}, "persona": {"name": "Jane"}}))
    db = MailDB(root / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=MINE, provider="imap", creds=crypto.seal_json("s", {}), status="ok",
                               settings=MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://acme.test", daily_cap=0).model_dump())
    box = await db.mailbox(mid)
    await Ingest(db, box).store_batch([
        _raw("c1", "T9", "<cold@mine>", MINE, "Jane", "Rebar for Towson", "I need about 40 tons of rebar a month.", 10, outbound=True),
        _raw("c2", "T9", "<ans@x>", "dana@steel.example", "Dana", "Re: Rebar for Towson", "Sure, send the spec.", 9, in_reply_to="<cold@mine>"),
    ])
    tid = (await db.one("SELECT id FROM threads WHERE provider_thread_id = 'T9'"))["id"]
    assert await sending.prior_replies(db, tid) == 0            # our cold email is not a reply
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('r1', ?, ?, 'reply', ?, 'Re: Rebar for Towson', 'Gladly, the spec is attached so you have the whole thing.', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Dana", "email": "dana@steel.example"}]), now, now))
    draft = await db.one("SELECT * FROM drafts WHERE id = 'r1'")
    assert (await sending.send_draft(db, "s", {"HARVEY_HOME": str(root)}, draft))["status"] == "sent"
    assert SentBox.sent[-1]["attachments"] == [str((root / "attachments" / "brief.pdf").resolve())]
    assert await sending.prior_replies(db, tid) == 1             # now it has gone, the next reply carries none


@pytest.mark.asyncio
async def test_follow_up_candidates_and_a_hard_no_anywhere_in_the_thread(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    db = MailDB(tmp_path / "mail.db"); await db.init()
    ready = MailboxSettings(display_name="Jane", signature_html="Jane", project_link="https://greystone.example").model_dump()
    # A consumer mailbox: other gmail.com people are not colleagues.
    ma = await db.add_mailbox(address="jane.doe@gmail.com", provider="imap", creds=crypto.seal_json("s", {}), status="ok", settings=ready)
    mb = await db.add_mailbox(address="jane@mine.example", provider="imap", creds=crypto.seal_json("s", {}), status="ok", settings=ready)
    me_a, me_b = "jane.doe@gmail.com", "jane@mine.example"

    def raw(pid, thread, mid, frm, name, subject, body, days, me, outbound=False, **kw):
        r = _raw(pid, thread, mid, frm, name, subject, body, days, outbound=outbound, **kw)
        r.to = [{"name": "", "email": me if not outbound else frm}]
        return r
    boxes = await db.mailboxes()
    await Ingest(db, boxes[0]).store_batch([
        raw("a1", "A1", "<a1@x>", "dana.w@gmail.com", "Dana", "Rebar", "Price list attached for the rebar.", 30, me_a),
        raw("a2", "A1", "<a2@x>", me_a, "Jane", "Re: Rebar", "Thanks.", 29, me_a, outbound=True),
        raw("a3", "A2", "<a3@x>", "pat@steel.example", "Pat", "Mill certs", "Here are the certs for the bar.", 20, me_a),
        raw("a4", "A2", "<a4@x>", me_a, "Jane", "Re: Mill certs", "Got them.", 19, me_a, outbound=True),
        raw("a5", "A3", "<a5@x>", "bob@x.example", "Bob", "Open question", "Still need a surveyor?", 5, me_a),   # needs a reply: a reply run's job
    ])
    await Ingest(db, boxes[1]).store_batch([
        raw("b1", "B1", "<b1@x>", "pat@steel.example", "Pat", "Older certs", "Older certs here.", 60, me_b),
        raw("b2", "B1", "<b2@x>", me_b, "Jane", "Re: Older certs", "Thanks.", 59, me_b, outbound=True),
        raw("b3", "B2", "<b3@x>", "quit@x.example", "Quin", "Our chat", "Please remove me from your list.", 50, me_b),
        raw("b4", "B2", "<b4@x>", me_b, "Jane", "Re: Our chat", "Of course.", 49, me_b, outbound=True),
        raw("b5", "B2", "<b5@x>", "quit@x.example", "Quin", "Re: Our chat", "Thanks, appreciated.", 48, me_b),
        raw("b6", "B2", "<b6@x>", me_b, "Jane", "Re: Our chat", "All the best.", 47, me_b, outbound=True),
    ])
    boxes = await db.mailboxes()
    cands = await runs.reconnect_candidates(db, boxes, 6, all_mailboxes=boxes)
    # Pat's most recent thread wins across mailboxes; Dana (gmail.com) is a contact, not a colleague; Bob's open thread is not here.
    assert [(c["contact"], c["subject"]) for c in cands] == [("pat@steel.example", "Mill certs"), ("dana.w@gmail.com", "Rebar"), ("quit@x.example", "Our chat")]
    brain = FakeBrain([
        {"suitable": True, "why": "", "confidence": "high", "confidence_reason": "",
         "body": "Hi Dana,\n\nYour rebar price list is still on my desk; the Towson design is final and bar is next, so I'd like to pick that back up. The spec is here:\n[SCOPE LINK 2]\n\nJane"},
    ])
    run = await runs.create_reconnect_run(db, boxes, months=6, mode="auto", created_by="me", all_mailboxes=boxes)
    assert run["total"] == 3
    out_b = await runs.process_mailbox(db, "s", {}, brain, None, "", run["id"], mb, scope=scope_mod.from_config(CFG))
    assert out_b["skipped"] == 1 and out_b["sent"] == 0          # Quin's earlier "remove me" counts, whatever came after
    for box in await db.mailboxes():
        assert "quit@x.example" in box["settings"]["excluded"]      # suppressed for every mailbox of the user
    out_a = await runs.process_mailbox(db, "s", {}, brain, None, "", run["id"], ma, scope=scope_mod.from_config(CFG))
    assert out_a["sent"] == 1 and SentBox.sent[-1]["to"] == ["dana.w@gmail.com"]


@pytest.mark.asyncio
async def test_the_models_reading_of_a_hard_no_is_a_second_net(tmp_path, monkeypatch):
    from bb.mail.commands import CommandCenter
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    db = MailDB(tmp_path / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=MINE, provider="imap", creds=crypto.seal_json("s", {}), status="ok",
                               settings=MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://acme.test").model_dump())
    tid = await _seed_thread(db, mid)
    brain = FakeBrain([{"body": "Understood.", "confidence": "high", "confidence_reason": "", "stance": "opt_out"}])
    boxes = await db.mailboxes()
    cmd = CommandCenter(db, brain, boxes, boxes, secret="s", scope=scope_mod.from_config(CFG))
    out = await cmd.generate_draft(thread_id=tid)
    assert "asked not to be contacted" in out["error"] and "excluded" in out["error"]
    assert "ann@x.example" in (await db.mailbox(mid))["settings"]["excluded"]
    assert await db.query("SELECT id FROM drafts") == []
