"""A soft no still gets the brief, once; a hard no gets nothing, ever. And
the follow-up run that continues past conversations with the project."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import bb.mail.service as service
from bb.mail import crypto, runs
from bb.mail import scope as scope_mod
from bb.mail.db import MailDB
from bb.mail.ingest import Ingest
from bb.mail.models import MailboxSettings
from bb.mail.providers.base import RawMessage
from bb.mail.sending import SendRefused, send_draft
from harvey import enquiry
from harvey.config import EnvConfig
from harvey.models.prospect import Prospect
from tests.test_inquiry_mail import CFG
from tests.test_inquiry_voice import NEEDS
from tests.test_mail_foundation import mail_client  # noqa: F401
from tests.test_mail_reply import FakeBrain, SentBox
from tests.test_outbox_native import FakeProvider, StubBrain, state  # noqa: F401
from tests.test_outreach import _config

ME = "me@mine.example"


def test_the_two_kinds_of_no_are_told_apart():
    soft = ["Not interested, thanks.", "We're fully booked through next summer.", "That's not something we do, sorry.",
            "We don't have the capacity this year.", "I'll have to pass on this one."]
    hard = ["Please remove me from your list.", "Stop emailing me.", "Unsubscribe", "This is spam and I'm reporting you.",
            "How did you get my email? Unsubscribe me.", "Leave us alone.", "I'll have my lawyer look at this."]
    for t in soft:
        assert enquiry.is_soft_no(t) and not enquiry.is_opt_out(t), t
    for t in hard:
        assert enquiry.is_opt_out(t), t
    assert not enquiry.is_decline("Sure, send over the drawings and we'll price it.")
    assert enquiry.closing_scope_sentence("the Towson brief").startswith("No action needed on your side. I'll leave the Towson brief")


class _RouteBrain(StubBrain):
    """Classifies as told; answers every reply with a note that carries the marker."""

    def __init__(self, intent):
        super().__init__(intent=intent, reply="Hi Dana,\n\nThanks for the straight answer.\n[SCOPE LINK]\n\nShall I check back next year?\n\nJane")
        self.prompts = []

    def load_prompt(self, name, **kw):
        return f"[{name} prompt]"

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append(prompt)
        return await super().think(prompt, session_id=session_id, agent=agent, task=task)


async def _handler(state, intent):  # noqa: F811
    from harvey.agents.handler import Handler
    cfg = _config(mode="inquiry", intent="Services for a 24-unit block in Towson.", sector_needs=NEEDS, ask="zoom",
                  sender_line="I run Greystone Development in Baltimore.")
    handler = Handler(_RouteBrain(intent), state, cfg, EnvConfig())
    handler.provider = FakeProvider()
    return handler


@pytest.mark.asyncio
async def test_a_soft_no_gets_one_closing_note_with_the_brief_and_then_silence(state):  # noqa: F811
    for pid, email in (("n1", "dana@build.example"), ("n2", "sam@build.example")):
        await state.add_prospect(Prospect(id=pid, first_name="Dana", email=email, email_status="provided",
                                          industry="construction", company="Build Co", status="contacted"))
    handler = await _handler(state, "not_interested")
    await handler._process_reply("dana@build.example", "Thanks, but we're not taking on new work this year.",
                                 reply_uuid="", reply_meta={"subject": "Re: Towson block", "message_id": "<r1@x>"})
    items = await state.get_outbox()
    assert len(items) == 1 and items[0]["kind"] == "reply"
    body = items[0]["body"]
    assert body.count("https://docs.example/towson") == 1 and "No action needed on your side" in body and "?" not in body
    assert body.rstrip().endswith("Jane") and "CLOSING NOTE" in handler.brain.prompts[-1]
    convo = (await state.get_conversations_by_status("closed"))[0]
    assert convo.stage == "closed_lost" and (await state.get_prospect("n1")).status == "lost"

    # An objection ("can't take it on") gets the same note and the same close.
    handler = await _handler(state, "objection")
    await handler._process_reply("sam@build.example", "We can't take this on, our books are full.",
                                 reply_uuid="", reply_meta={"subject": "Re: Towson block", "message_id": "<r2@x>"})
    mine = [i for i in await state.get_outbox() if i["to_email"] == "sam@build.example"]
    assert len(mine) == 1 and mine[0]["body"].count("https://docs.example/towson") == 1 and "?" not in mine[0]["body"]
    assert (await state.get_prospect("n2")).status == "lost"


@pytest.mark.asyncio
async def test_a_hard_no_is_suppressed_and_gets_nothing(state):  # noqa: F811
    for pid, email in (("h1", "a@one.example"), ("h2", "b@two.example"), ("h3", "c@three.example")):
        await state.add_prospect(Prospect(id=pid, first_name="A", email=email, email_status="provided",
                                          industry="construction", status="contacted"))
    # The model would call it "not interested"; the words say unsubscribe. Code wins.
    handler = await _handler(state, "not_interested")
    await handler._process_reply("a@one.example", "Not interested. Please remove me from your list.",
                                 reply_uuid="", reply_meta={"subject": "Re: x", "message_id": "<u1@x>"})
    assert await state.get_outbox() == [] and (await state.get_prospect("h1")).status == "opted_out"
    # Hostility is an opt-out too.
    handler = await _handler(state, "question")
    await handler._process_reply("b@two.example", "This is spam. Stop emailing me.",
                                 reply_uuid="", reply_meta={"subject": "Re: x", "message_id": "<u2@x>"})
    assert await state.get_outbox() == [] and (await state.get_prospect("h2")).status == "opted_out"
    # A legal threat goes to a human AND is suppressed.
    handler = await _handler(state, "question")
    await handler._process_reply("c@three.example", "My attorney will be in touch about this harassment.",
                                 reply_uuid="", reply_meta={"subject": "Re: x", "message_id": "<u3@x>"})
    assert await state.get_outbox() == [] and (await state.get_prospect("h3")).status == "opted_out"
    assert (await state.get_conversations_by_status("needs_human"))


def _raw(pid, thread, mid, frm, name, subject, body, days_ago, outbound=False, **kw):
    sent = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return RawMessage(provider_id=pid, thread_key=thread, message_id=mid, from_email=frm, from_name=name, subject=subject,
                      body=body, sent_at=sent, outbound=outbound, to=[{"name": "", "email": ME if not outbound else frm}], **kw)


async def _seed_history(db, mid):
    box = await db.mailbox(mid)
    ing = Ingest(db, box)
    raws = [
        # T1: a two-way business thread, 60 days ago: the one to continue.
        _raw("p1", "T1", "<t1a@x>", "dana@steel.example", "Dana Whitfield", "Rebar pricing", "Here is our price list for #5 bar, valid to March.", 62),
        _raw("p2", "T1", "<t1b@x>", ME, "Jane", "Re: Rebar pricing", "Thanks Dana, that is helpful. I will come back once the design is final.", 60, outbound=True, in_reply_to="<t1a@x>"),
        # T2: the same contact, older: only the most recent thread per contact.
        _raw("p3", "T2", "<t2a@x>", "dana@steel.example", "Dana Whitfield", "Mill certs", "Attached are the mill certs you asked for.", 120),
        _raw("p4", "T2", "<t2b@x>", ME, "Jane", "Re: Mill certs", "Got them, thank you.", 119, outbound=True),
        # T3: personal: the model declines it.
        _raw("p5", "T3", "<t3a@x>", "mum@family.example", "Mum", "Sunday lunch", "Are you coming on Sunday? Bring the kids.", 40),
        _raw("p6", "T3", "<t3b@x>", ME, "Jane", "Re: Sunday lunch", "Yes, we will be there at one.", 39, outbound=True),
        # T4: a newsletter we once replied to: automated, left out.
        _raw("p7", "T4", "<t4a@x>", "newsletter@list.example", "Weekly Steel", "Weekly Steel: prices up", "Prices rose 3% this week.", 30),
        _raw("p8", "T4", "<t4b@x>", ME, "Jane", "Re: Weekly Steel", "Please change my address.", 29, outbound=True),
        # T5: a colleague at our own domain: left out.
        _raw("p9", "T5", "<t5a@x>", "colleague@mine.example", "Col", "Lunch?", "Lunch at noon?", 20),
        _raw("p10", "T5", "<t5b@x>", ME, "Jane", "Re: Lunch?", "Sure.", 19, outbound=True),
        # T6: they wrote, we never answered: not a conversation (a reply run's job, not this one's).
        _raw("p11", "T6", "<t6a@x>", "bob@x.example", "Bob", "Question", "Do you still need a surveyor?", 10),
        # T7: a two-way thread outside the window.
        _raw("p12", "T7", "<t7a@x>", "old@x.example", "Old", "Last year", "Here is the quote from last year.", 240),
        _raw("p13", "T7", "<t7b@x>", ME, "Jane", "Re: Last year", "Thanks.", 239, outbound=True),
        # T8: a two-way thread whose last word from them is an opt-out.
        _raw("p14", "T8", "<t8a@x>", "quit@x.example", "Quin", "Our chat", "Please remove me from your list, I am retiring.", 80),
        _raw("p15", "T8", "<t8b@x>", ME, "Jane", "Re: Our chat", "Of course, all the best.", 79, outbound=True),
    ]
    await ing.store_batch(raws)


@pytest.mark.asyncio
async def test_the_follow_up_run_continues_past_conversations_once_per_contact(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    SentBox.sent.clear()
    db = MailDB(tmp_path / "mail.db"); await db.init()
    ready = MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://greystone.example").model_dump()
    mid = await db.add_mailbox(address=ME, provider="imap", creds=crypto.seal_json("s", {}), status="ok", settings=ready)
    await _seed_history(db, mid)
    boxes = await db.mailboxes()
    cands = await runs.reconnect_candidates(db, boxes, 6)
    # Two-way human threads in the window, newest first, one per contact (Dana's most recent
    # thread only); the family thread is a candidate too: the model, reading it, is what rules it out.
    assert [(c["contact"], c["subject"]) for c in cands] == [("mum@family.example", "Sunday lunch"),
                                                             ("dana@steel.example", "Rebar pricing"), ("quit@x.example", "Our chat")]
    assert [c["contact"] for c in await runs.reconnect_candidates(db, boxes, 12)] == \
        ["mum@family.example", "dana@steel.example", "quit@x.example", "old@x.example"]

    scope = scope_mod.from_config(CFG)
    # Items are processed oldest first: T8 (opt-out, no model call), T1 (Dana), T3 (family).
    brain = FakeBrain([
        {"suitable": True, "why": "", "confidence": "high", "confidence_reason": "natural bridge",
         "body": "Hi Dana,\n\nYour price list for #5 bar came in handy; the design for the Towson block is final now and rebar is next "
                 "on the list, so I'd like to pick that conversation back up. The whole scope is in the spec:\n[SCOPE LINK 2]\n\n"
                 "Would the same terms still stand?\n\nJane"},
        {"suitable": False, "why": "a family thread, nothing to do with work", "body": "", "confidence": "low", "confidence_reason": ""},
    ])
    run = await runs.create_reconnect_run(db, boxes, months=6, mode="auto", instructions="offer the spec, no call", created_by="me")
    assert run["kind"] == "reconnect" and run["total"] == 3 and run["months"] == 6
    out = await runs.process_mailbox(db, "s", {}, brain, None, "", run["id"], mid, scope=scope)
    assert out["sent"] == 1 and out["skipped"] == 2
    items = {i["contact"]: i for i in await db.query("SELECT * FROM run_items WHERE run_id = ?", (run["id"],))}
    assert items["quit@x.example"]["status"] == "skipped" and "asked not to be contacted" in items["quit@x.example"]["detail"]
    assert items["mum@family.example"]["status"] == "skipped" and "not suitable: a family thread" in items["mum@family.example"]["detail"]
    assert items["dana@steel.example"]["status"] == "sent"
    sent = SentBox.sent[-1]
    assert sent["to"] == ["dana@steel.example"] and sent["subject"] == "Re: Rebar pricing"
    assert sent["in_reply_to"] == "<t1b@x>" and "<t1b@x>" in sent["references"]        # continues the thread from its last message
    assert sent["body_text"].count("https://docs.example/rebar") == 1 and "price list" in sent["body_text"]
    system, user = brain.prompts[-2]          # Dana's draft; the family thread was the last call
    assert "THE SITUATION: this is a conversation" in system and "offer the spec, no call" in user and "Rebar pricing" in user
    # The opt-out joined the excluded list; the thread is put away; a later send to them is refused.
    box = await db.mailbox(mid)
    assert "quit@x.example" in box["settings"]["excluded"]
    assert (await db.one("SELECT state FROM threads WHERE provider_thread_id = 'T8'"))["state"] == "dismissed"
    # A second scan does not write to Dana again, nor to the suppressed address.
    assert await runs.reconnect_candidates(db, await db.mailboxes(), 6) == []
    activity = " ".join(a["text"] for a in await db.activity())
    assert "Follow-up run finished: 1 sent, 2 skipped" in activity and "asked not to be contacted" in activity


@pytest.mark.asyncio
async def test_a_follow_up_that_does_not_stand_on_the_thread_waits_for_review(tmp_path):
    from bb.mail.drafting import draft_reconnect
    settings = MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://greystone.example")
    thread = {"subject": "Sunday lunch"}
    messages = [{"direction": "inbound", "from_email": "mum@family.example", "from_name": "Mum", "body": "Are you coming on Sunday?", "sent_at": "2026-08-01"},
                {"direction": "outbound", "from_email": ME, "body": "Yes, at one.", "sent_at": "2026-08-01"}]
    brain = FakeBrain([{"suitable": False, "why": "a family thread", "body": "", "confidence": "low", "confidence_reason": ""}])
    r = await draft_reconnect(brain, settings, {"address": ME}, thread, messages, scope=scope_mod.from_config(CFG))
    assert r["suitable"] is False and r["why"] == "a family thread"
    # A body that shares nothing with the thread is a template: it fails the check and is asked for again.
    thread = {"subject": "Rebar pricing"}
    messages = [{"direction": "inbound", "from_email": "dana@steel.example", "from_name": "Dana", "body": "Here is our price list for #5 bar.", "sent_at": "2026-08-01"},
                {"direction": "outbound", "from_email": ME, "body": "Thanks Dana.", "sent_at": "2026-08-02"}]
    brain = FakeBrain([
        {"suitable": True, "why": "", "confidence": "high", "confidence_reason": "",
         "body": "Hi Dana,\n\nI have been building something new and wanted to share it with you. Details here:\n[SCOPE LINK 2]\n\nJane"},
        {"suitable": True, "why": "", "confidence": "high", "confidence_reason": "",
         "body": "Hi Dana,\n\nYour price list for #5 bar is still on my desk; the Towson design is final and rebar is next, "
                 "so I'd like to pick that back up. The spec has the tonnage and the schedule:\n[SCOPE LINK 2]\n\nJane"},
    ])
    r = await draft_reconnect(brain, settings, {"address": ME}, thread, messages, scope=scope_mod.from_config(CFG))
    assert r["attempts"] == 2 and r["qa"]["ok"] and "price list" in r["body"] and r["scope_link"] == "https://docs.example/rebar"
    assert "does not pick up anything from the thread" in brain.prompts[-1][1]


def test_the_follow_up_run_has_its_own_form_and_page(mail_client, monkeypatch):
    from bb.accounts import Accounts
    from bb.workspaces import Workspace
    from tests.test_bb_web import USER, _accept_key, _as_user

    client = mail_client
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: 1)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "started"))
    client.post("/apikey", data={"api_key": "sk-ant-abcdefghijklmnop"})
    client.post("/mail/accounts/imap", data={"email": ME, "password": "pw"})
    db = ws.mail_db()
    mid = asyncio.run(db.mailboxes())[0]["id"]
    asyncio.run(_seed_history(db, mid))

    page = client.get("/mail/runs").text
    assert "Follow up with past contacts" in page and "3 contacts</b>" in page and "Rebar pricing" in page and "Sunday lunch" in page
    assert "Weekly Steel" not in page and "Lunch?" not in page and "Last year" not in page       # automated, own domain, outside the window
    assert "4 contacts</b>" in client.get("/mail/runs?months=12").text
    r = client.post("/mail/runs/reconnect", data={"mode": "review", "months": "6", "instructions": "offer the spec"})
    assert r.headers["location"].startswith("/mail/runs/") and "Follow-up%20run%20started" in r.headers["location"], r.headers["location"]
    run_id = r.headers["location"].split("/mail/runs/")[1].split("?")[0]
    page = client.get(f"/mail/runs/{run_id}").text
    assert "Follow-up run" in page and "Following up with 1 of 3" in page and "offer the spec" in page
    run = asyncio.run(db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,)))
    assert run["kind"] == "reconnect" and run["months"] == 6 and run["total"] == 3
    assert "follow-up · review" in client.get("/mail/runs").text
    # A second scan while this one is still going opens this one.
    r = client.post("/mail/runs/reconnect", data={"mode": "review", "months": "6"})
    assert r.headers["location"].startswith(f"/mail/runs/{run_id}") and "still%20going" in r.headers["location"]
    # Nothing left to follow up with once a run holds every contact.
    asyncio.run(db.execute("UPDATE reply_runs SET status = 'done' WHERE id = ?", (run_id,)))
    r = client.post("/mail/runs/reconnect", data={"mode": "review", "months": "6"})
    assert "No%20past%20conversations" in r.headers["location"]
