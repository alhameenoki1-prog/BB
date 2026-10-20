"""Inbox auto-reply: answering mail from people who are not on the list,
following the operator's instructions and sharing the right documents."""

import json

import pytest

from harvey.config import HarveyConfig, OutreachConfig
from harvey.integrations.mail_provider import InboundMessage, looks_automated
from harvey.models.prospect import Prospect

from tests.test_outbox_native import Env, FakeProvider, state  # noqa: F401
from tests.test_outreach import BASE


def test_automated_mail_is_recognised():
    assert looks_automated("noreply@github.com")
    assert looks_automated("notifications@slack.com")
    assert looks_automated("news@shop.example", "Weekly digest")
    assert looks_automated("jane@acme.com", "Automatic reply: out of office")
    assert looks_automated("jane@acme.com", "hello", {"Auto-Submitted": "auto-replied"})
    assert looks_automated("jane@acme.com", "hello", {"Precedence": "bulk"})
    assert looks_automated("jane@acme.com", "hello", {"List-Unsubscribe": "<mailto:x>"})
    assert not looks_automated("jane@acme.com", "Quote for a kitchen", {"from": "Jane Doe <jane@acme.com>"})
    assert not looks_automated("team.lead@acme.com", "hello")   # "team" as a real person's prefix is fine


def test_inbox_config_fields():
    cfg = OutreachConfig(inbox_autoreply=True, inbox_links=[{"label": "Brochure", "url": "drive.google.com/x", "when": "asked"}])
    assert cfg.inbox_links[0].url == "https://drive.google.com/x" and cfg.inbox_links[0].label == "Brochure"
    assert OutreachConfig().inbox_autoreply is False and OutreachConfig().inbox_links == []


class _InboxBrain:
    """Answers the inbox prompt with a canned decision; records prompts."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []
        self.paused_reason = ""

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, **kw):
        return "question"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.decisions.pop(0)


def _handler(state, provider, brain, **outreach):
    from harvey.agents.handler import Handler
    cfg = HarveyConfig(**{**BASE, "persona": {**BASE["persona"], "name": "Jane Doe", "role": "Architect",
                                               "email": "jane@acme.test"},
                          "channels": {"email": {**BASE["channels"]["email"], "require_approval": False}},
                          "outreach": {"inbox_autoreply": True,
                                       "inbox_instructions": "Send the brochure when they ask what we do. Never quote a price.",
                                       "inbox_links": [{"label": "Brochure", "url": "https://acme.test/brochure.pdf",
                                                        "when": "they ask what we do or about prices"},
                                                       {"label": "Booking page", "url": "https://cal.com/jane",
                                                        "when": "they want a call"}],
                                       "reply_attachments": ["brief.pdf"], **outreach}})
    handler = Handler(brain=brain, state=state, config=cfg, env=Env())
    handler.provider = provider
    return handler


def _mail(i, sender, subject, body, headers=None):
    return InboundMessage(provider_id=f"in{i}", from_email=sender, subject=subject, body=body,
                          message_id=f"<{i}@theirs>", thread_ref=f"t{i}", headers=headers or {})


@pytest.mark.asyncio
async def test_inbox_mail_gets_an_answer_with_the_right_link(state):  # noqa: F811
    provider = FakeProvider()
    provider.inbound = [
        _mail(1, "sam@newclient.example", "What do you do?", "Hi, we saw your work. Could you tell us what services you offer and roughly what things cost?",
              headers={"from": "Sam Okafor <sam@newclient.example>"}),
        _mail(2, "noreply@github.com", "[GitHub] Security alert", "Something happened."),
        _mail(3, "vendor@seo-spam.example", "Rank #1 on Google!!!", "We can boost your rankings."),
    ]
    brain = _InboxBrain([
        {"action": "reply", "reason": "asks about services", "share": [1],
         "reply": "Hi Sam,\n\nThanks for getting in touch. We design and manage residential builds and extensions. I have attached our brochure, which covers the kind of work we do. Prices depend on the site and size, so I would rather talk those through once I know more about your project.\n\nBest,\nJane"},
        {"action": "ignore", "reason": "sales pitch", "reply": "", "share": []},
    ])
    handler = _handler(state, provider, brain)
    assert await handler._run_native() is True
    # One prompt for Sam, one for the pitch; the GitHub notice never reached the model.
    assert len(brain.prompts) == 2
    assert "Jane Doe'S INSTRUCTIONS" not in brain.prompts[0] and "JANE DOE'S INSTRUCTIONS" in brain.prompts[0]
    assert "[1] Brochure: share when they ask what we do or about prices" in brain.prompts[0]
    assert "roughly what things cost" in brain.prompts[0]

    items = await state.get_outbox()
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "inbox_reply" and item["to_email"] == "sam@newclient.example"
    assert item["subject"] == "Re: What do you do?" and item["status"] == "approved"
    assert item["in_reply_to"] == "<1@theirs>" and item["thread_ref"] == "t1"
    body = item["body"]
    assert body.startswith("Hi Sam,\n\n") and body.count("Hi Sam,") == 1
    assert "Brochure: https://acme.test/brochure.pdf" in body and "cal.com" not in body
    assert body.rstrip().endswith("Jane") and "Best,\nJane\n" not in body
    assert not json.loads(item["attachments_json"] or "[]")   # attach_files is off

    # The sender is recorded as an inbox contact, not an upload, with a conversation.
    p = await state.get_prospect_by_email("sam@newclient.example")
    assert p.source == "inbox" and p.first_name == "Sam" and p.last_name == "Okafor" and p.status == "replied"
    convos = await state.get_conversations_by_status("open")
    assert any(c.prospect_id == p.id and c.intent == "inbox" and len(c.thread) == 2 for c in convos)

    # Writing again within three days: no second reply, no model call.
    provider.inbound = [_mail(4, "sam@newclient.example", "Re: What do you do?", "Thanks, one more thing...")]
    await handler._run_native()
    assert len(brain.prompts) == 2 and len(await state.get_outbox()) == 1


@pytest.mark.asyncio
async def test_inbox_reply_waits_for_approval_and_attaches_files(state):  # noqa: F811
    provider = FakeProvider()
    provider.inbound = [_mail(7, "lee@north-west-build.co.uk", "Booking a call", "Can we set up a call next week?")]
    brain = _InboxBrain([{"action": "reply", "reason": "wants a call", "share": [2, 9],
                          "reply": "Happy to. Pick any slot that suits you on my booking page and I will confirm."}])
    handler = _handler(state, provider, brain, inbox_attach_files=True)
    handler.config.channels.email.require_approval = True
    await handler._run_native()
    [item] = await state.get_outbox()
    assert item["status"] == "pending_review"
    assert item["body"].startswith("Hi there,\n\n")          # lee@ alone is not a name
    assert "Booking page: https://cal.com/jane" in item["body"] and "brochure" not in item["body"].lower()
    assert json.loads(item["attachments_json"]) == ["brief.pdf"]
    p = await state.get_prospect_by_email("lee@north-west-build.co.uk")
    assert p.company == "North West Build"


@pytest.mark.asyncio
async def test_inbox_autoreply_off_leaves_strangers_alone(state):  # noqa: F811
    provider = FakeProvider()
    provider.inbound = [_mail(1, "sam@newclient.example", "Hello", "Anyone there?")]
    brain = _InboxBrain([])
    handler = _handler(state, provider, brain, inbox_autoreply=False)
    await handler._run_native()
    assert brain.prompts == [] and await state.get_outbox() == []

    # Someone on the list is still handled as a reply to outreach, not as inbox mail.
    await state.add_prospect(Prospect(first_name="Ann", last_name="Lee", email="ann@listed.example",
                                      email_status="provided", status="contacted", title="CEO", company="Listed"))
    provider.inbound = [_mail(2, "ann@listed.example", "Re: your note", "Tell me more?")]
    handler = _handler(state, provider, _InboxBrain([]))
    await handler._run_native()
    assert not [i for i in await state.get_outbox() if i["kind"] == "inbox_reply"]


# ── backlog: older mail nobody answered ──


@pytest.mark.asyncio
async def test_backlog_scan_answers_old_unanswered_mail_a_few_at_a_time(state):  # noqa: F811
    provider = FakeProvider()
    provider.inbound = []
    old = [_mail(10 + i, f"p{i}@client{i}.example", f"Question {i}", f"Could you tell me about job {i}?")
           for i in range(7)]
    answered_by_hand = _mail(30, "done@client.example", "Old thread", "Already handled by BB earlier")
    bounce = InboundMessage(provider_id="b1", from_email="mailer-daemon@x", subject="Delivery Status Notification",
                            body="failed", is_bounce=True, message_id="<b1@x>")

    async def get_unanswered(days=30, limit=100):
        get_unanswered.calls.append(days)
        return old + [answered_by_hand, bounce]
    get_unanswered.calls = []
    provider.get_unanswered = get_unanswered

    # BB already replied to the "done" thread: its outbox item references that Message-ID.
    await state.add_prospect(Prospect(id="d", email="done@client.example", source="inbox", status="replied", email_status="provided"))
    await state.add_outbox_item(prospect_id="d", to_email="done@client.example", subject="Re: Old thread", body="x",
                                send_at="2026-01-01T00:00:00", status="sent", kind="inbox_reply", in_reply_to="<30@theirs>")

    decisions = [{"action": "reply", "reason": "ok", "share": [], "reply": f"Happy to help with job {i}."} for i in range(7)]
    brain = _InboxBrain(decisions)
    handler = _handler(state, provider, brain, inbox_backlog_days=30)
    assert await handler._run_native() is True
    assert get_unanswered.calls == [30]
    replies = [i for i in await state.get_outbox() if i["kind"] == "inbox_reply" and i["in_reply_to"] != "<30@theirs>"]
    assert len(replies) == 5                      # five per cycle, not the whole backlog at once
    assert len(brain.prompts) == 5
    assert not any(i["to_email"] == "done@client.example" and i["in_reply_to"] == "<30@theirs>" and i["status"] != "sent"
                   for i in await state.get_outbox())

    # Next cycle picks up the remaining two and nothing twice.
    await handler._run_native()
    replies = [i for i in await state.get_outbox() if i["kind"] == "inbox_reply" and i["in_reply_to"] != "<30@theirs>"]
    assert len(replies) == 7 and len(brain.prompts) == 7
    await handler._run_native()
    assert len(brain.prompts) == 7

    # With the option off, the backlog is never read.
    provider2 = FakeProvider(); provider2.inbound = []
    calls = []
    async def never(days=30, limit=100):
        calls.append(days); return old
    provider2.get_unanswered = never
    await _handler(state, provider2, _InboxBrain([]), inbox_backlog_days=0)._run_native()
    assert calls == []


def test_backlog_days_is_bounded():
    assert OutreachConfig(inbox_backlog_days="90").inbox_backlog_days == 90
    assert OutreachConfig(inbox_backlog_days=-5).inbox_backlog_days == 0
    assert OutreachConfig(inbox_backlog_days=9999).inbox_backlog_days == 365
    assert OutreachConfig(inbox_backlog_days="x").inbox_backlog_days == 0


@pytest.mark.asyncio
async def test_gmail_unanswered_skips_threads_i_replied_in():
    from harvey.integrations.gmail import GmailProvider

    def message(mid, sender, subject="s", labels=("INBOX",)):
        return {"id": mid, "threadId": "t-" + mid[0], "labelIds": list(labels),
                "payload": {"mimeType": "text/plain", "headers": [
                    {"name": "From", "value": f"X <{sender}>"}, {"name": "Subject", "value": subject},
                    {"name": "Message-ID", "value": f"<{mid}@m>"}],
                    "body": {"data": "aGVsbG8="}}}

    threads = {
        "t-a": [message("a1", "ann@x.example", "Need a quote")],                                        # unanswered
        "t-b": [message("b1", "bob@x.example", "Hi"), message("b2", "me@mine.example", "Re: Hi", ("SENT",))],  # I replied
        "t-c": [message("c1", "cy@x.example", "First"), message("c2", "cy@x.example", "Second")],        # two from them
    }

    async def fake_request(method, path, **kw):
        if path == "/threads":
            assert "newer_than:14d" in kw["params"]["q"]
            return {"threads": [{"id": t} for t in threads]}
        if path == "/profile":
            return {"emailAddress": "me@mine.example"}
        if path.startswith("/threads/"):
            return {"messages": threads[path.split("/")[-1]]}
        raise AssertionError(path)

    p = GmailProvider.__new__(GmailProvider)
    p._request = fake_request
    out = await p.get_unanswered(days=14)
    assert [m.from_email for m in out] == ["ann@x.example", "cy@x.example"]
    assert out[1].provider_id == "c2"      # the newest incoming message of the thread


def test_imap_unanswered_uses_the_sent_folder(monkeypatch):
    import imaplib

    from harvey.integrations import smtp_mail

    raw = lambda mid, sender, subj: (  # noqa: E731
        f"From: {sender}\r\nTo: me@mine.example\r\nSubject: {subj}\r\nMessage-ID: <{mid}@m>\r\n"
        f"Date: Mon, 1 Jan 2026 10:00:00 +0000\r\nContent-Type: text/plain\r\n\r\nbody {mid}\r\n").encode()

    class FakeIMAP:
        folder = ""
        inbox = {b"1": raw("a1", "ann@x.example", "Need a quote"), b"2": raw("b1", "bob@x.example", "Hi"),
                 b"3": raw("m1", "me@mine.example", "my own")}
        sent = {b"9": b"In-Reply-To: <b1@m>\r\nReferences: <b0@m> <b1@m>\r\n\r\n"}

        def __init__(self, host, port):
            FakeIMAP.host = (host, port)
        def login(self, u, p):
            FakeIMAP.login_as = u
        def select(self, folder, readonly=False):
            name = folder.strip('"')
            if name == "INBOX" or name == "[Gmail]/Sent Mail":
                self.folder = name
                return "OK", [b"3"]
            return "NO", [b"nope"]
        def search(self, charset, query):
            assert 'SINCE "' in query
            ids = self.inbox if self.folder == "INBOX" else self.sent
            return "OK", [b" ".join(ids.keys())]
        def fetch(self, mid, what):
            data = (self.inbox if self.folder == "INBOX" else self.sent)[mid]
            return "OK", [(b"1 (BODY[] {%d})" % len(data), data)]
        def logout(self):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)

    class Env:
        smtp_host = "smtp.mine.example"; smtp_port = 587; smtp_username = "me@mine.example"; smtp_password = "pw"
        smtp_reply_to = ""; imap_host = "imap.mine.example"; imap_port = 993; imap_username = ""; imap_password = ""

    p = smtp_mail.SmtpImapProvider(config=None, env=Env())
    out = p._fetch_unanswered(days=30, limit=100)
    assert [m.from_email for m in out] == ["ann@x.example"]     # bob was answered from Sent; my own mail skipped
    assert FakeIMAP.login_as == "me@mine.example"

    # No Sent folder at all: only unread inbox mail is offered, never read-and-ignored mail.
    class NoSent(FakeIMAP):
        def select(self, folder, readonly=False):
            if folder.strip('"') == "INBOX":
                self.folder = "INBOX"; return "OK", [b"3"]
            return "NO", [b"nope"]
        def search(self, charset, query):
            if "UNSEEN" in query:
                return "OK", [b"2"]
            return "OK", [b"1 2 3"]
    monkeypatch.setattr(imaplib, "IMAP4_SSL", NoSent)
    out = smtp_mail.SmtpImapProvider(config=None, env=Env())._fetch_unanswered(days=30, limit=100)
    assert [m.from_email for m in out] == ["bob@x.example"]
