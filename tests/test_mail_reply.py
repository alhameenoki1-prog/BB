"""Phase 5: drafting with the quality check, signature and project link
enforced at send, the send layer's rules, the composer, review tray and
compose pages, and the worker's send job."""

import asyncio
import json

import pytest

import bb.mail.service as service
from bb.mail.brain import MailBrain, env_from_llm, extract_json
from bb.mail.db import MailDB
from bb.mail.drafting import draft_reply, finalize, qa_check, strip_greeting_and_signoff
from bb.mail.ingest import Ingest
from bb.mail.jobs import Jobs
from bb.mail.models import MailboxSettings
from bb.mail.providers.base import RawMessage
from bb.mail.sending import SendRefused, send_draft, within_hours
from bb.mail.worker import Worker

from tests.test_mail_foundation import _FakeAccount, mail_client  # noqa: F401
from tests.test_bb_web import USER, _as_user, _accept_key  # noqa: F401
from bb.accounts import Accounts
from bb.workspaces import Workspace

ME = "me@mine.example"


class FakeBrain(MailBrain):
    def __init__(self, answers):
        super().__init__({"BB_LLM_PROVIDER": "anthropic", "BB_LLM_API_KEY": "k"})
        self.answers = list(answers); self.prompts = []
    async def complete(self, system, user, max_tokens=1200):
        self.prompts.append((system, user))
        a = self.answers.pop(0)
        return a if isinstance(a, str) else json.dumps(a)


def test_brain_env_and_json_extraction():
    env = env_from_llm({"provider": "gemini", "api_key": "g", "base_url": "https://g/v1", "model": "gemini-2.5-flash"})
    assert env == {"BB_LLM_PROVIDER": "gemini", "BB_LLM_API_KEY": "g", "BB_LLM_BASE_URL": "https://g/v1", "BB_LLM_MODEL": "gemini-2.5-flash"}
    assert MailBrain(env).configured and not MailBrain(env).supports_tools and MailBrain({}).configured is False
    assert MailBrain({"BB_LLM_PROVIDER": "anthropic", "BB_LLM_API_KEY": "k"}).supports_tools
    assert extract_json('Sure! ```json\n{"body": "hi", "n": [1, 2]}\n```') == {"body": "hi", "n": [1, 2]}
    assert extract_json("no json here") is None


def test_quality_check_and_signoff_stripping():
    bad = "I hope this email finds you well. Please do not hesitate to reach out!!! Click here: http://x.com"
    qa = qa_check("Re: hi", bad)
    assert not qa["ok"] and any("boilerplate" in r for r in qa["reasons"]) and qa["score"] >= 4
    good = ("Tuesday at ten works. I'll bring the drawings you asked about and we can walk the site together. "
            "If the surveyor's report arrives before then I'll send it over.")
    qa = qa_check("Re: Site visit", good)
    assert qa["ok"] and not qa["sensitive"]
    assert qa_check("Re: Invoice", good + " The invoice for $400 is attached.")["sensitive"]
    assert strip_greeting_and_signoff("Hi Ann,\n\nTuesday works.\n\nBest regards,\nJane Doe", "Jane Doe") == "Tuesday works."
    assert strip_greeting_and_signoff("Tuesday works.\n\nThanks\nJane", "Jane Doe") == "Tuesday works."


def test_finalize_always_carries_signature_and_project_link():
    s = MailboxSettings(signature_html="Jane Doe<br>Founder, Acme", project_link="https://acme.test")
    plain, html = finalize("Tuesday works.\n\nI'll bring the drawings.", s, greeting_name="Ann Lee")
    assert plain.startswith("Hi Ann,\n\nTuesday works.") and plain.endswith("-- \nJane Doe\nFounder, Acme\nhttps://acme.test")
    assert 'href="https://acme.test"' in html and "<p" in html and "Hi Ann," in html
    s2 = MailboxSettings(signature_html='Jane<br><a href="https://acme.test/about">about us</a>', project_link="https://acme.test/about")
    plain, html = finalize("x", s2)
    assert plain.count("https://acme.test/about") == 1 and html.count("https://acme.test/about") == 1   # not doubled
    assert finalize("x", s2, greeting_name="")[0].startswith("Hi,\n\n")


@pytest.mark.asyncio
async def test_draft_reply_regenerates_once_then_reports():
    settings = MailboxSettings(display_name="Jane Doe", persona_notes="warm but direct")
    thread = {"subject": "Site visit", "id": "t"}
    messages = [{"direction": "inbound", "from_name": "Ann Lee", "from_email": "ann@x", "body": "Can we do Tuesday at ten?", "sent_at": "2026-01-01"}]
    brain = FakeBrain([
        {"body": "Hi Ann,\n\nI hope this email finds you well! Tuesday works.\n\nBest,\nJane", "confidence": "high", "confidence_reason": "clear ask"},
        {"body": "Tuesday at ten works, Ann. I'll bring the drawings you asked about so we can walk the site together.", "confidence": "high", "confidence_reason": "clear ask"},
    ])
    r = await draft_reply(brain, settings, {"address": ME}, thread, messages, instructions="say yes")
    assert r["attempts"] == 2 and r["qa"]["ok"] and r["body"].startswith("Tuesday at ten works")
    assert "failed the pre-send check" in brain.prompts[1][1] and "say yes" in brain.prompts[0][1]
    assert "WRITING STANDARD" in brain.prompts[0][0] and "warm but direct" in brain.prompts[0][0]
    # Still failing after the rewrite: reported, not hidden; money lowers a 'high' confidence.
    brain = FakeBrain([{"body": "Per my last email, click here!!!", "confidence": "high"},
                       {"body": "Per my last email, the invoice is $400 and payment is due.", "confidence": "high"}])
    r = await draft_reply(brain, settings, {"address": ME}, thread, messages)
    assert r["attempts"] == 2 and not r["qa"]["ok"] and r["confidence"] == "medium" and "money" in r["confidence_reason"].lower()


def test_send_window():
    from datetime import datetime
    s = MailboxSettings(hours_start="09:00", hours_end="18:00")
    assert within_hours(s, datetime(2026, 1, 1, 12, 0)) and not within_hours(s, datetime(2026, 1, 1, 20, 0))
    night = MailboxSettings(hours_start="22:00", hours_end="06:00")
    assert within_hours(night, datetime(2026, 1, 1, 23, 0)) and not within_hours(night, datetime(2026, 1, 1, 12, 0))
    assert within_hours(MailboxSettings(), datetime(2026, 1, 1, 3, 0))


async def _seed_thread(db, mid):
    box = await db.mailbox(mid)
    ing = Ingest(db, box)
    await ing.store_batch([RawMessage(provider_id="in1", thread_key="T1", message_id="<a1@x>", from_email="ann@x.example",
                                      from_name="Ann Lee", subject="Site visit", body="Can we do Tuesday at ten?",
                                      sent_at="2026-01-01T10:00:00Z", to=[{"name": "", "email": ME}])])
    return (await db.one("SELECT id FROM threads WHERE provider_thread_id = 'T1'"))["id"]


class SentBox:
    """An adapter whose send records what it got."""
    sent = []
    def __init__(self, mailbox, creds, env):
        self.address = mailbox["address"]
    async def test(self):
        return "ok"
    async def send(self, **kw):
        SentBox.sent.append(kw)
        return "<out1@mine>", "", ""


@pytest.mark.asyncio
async def test_send_layer_enforces_rules_and_updates_the_thread(tmp_path, monkeypatch):
    from bb.mail import crypto
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    SentBox.sent.clear()
    db = MailDB(tmp_path / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds=crypto.seal_json("s", {"x": 1}), status="ok")
    tid = await _seed_thread(db, mid)
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, in_reply_to, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, 'reply', ?, 'Re: Site visit', 'Tuesday at ten works. I will bring the drawings.', '<a1@x>', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann Lee", "email": "ann@x.example"}]), now, now))
    draft = await db.one("SELECT * FROM drafts WHERE id = 'd1'")
    with pytest.raises(SendRefused, match="signature"):
        await send_draft(db, "s", {}, draft)
    await db.update_mailbox(mid, settings=MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://acme.test",
                                                          daily_cap=1, reply_to="inbox@mine.example").model_dump())
    # Dry run: recorded, nothing sent.
    r = await send_draft(db, "s", {}, draft, dry_run=True)
    assert r["status"] == "dry_run" and SentBox.sent == [] and (await db.one("SELECT status FROM drafts WHERE id='d1'"))["status"] == "dry_run"
    assert any("Dry run" in a["text"] for a in await db.activity())
    await db.execute("UPDATE drafts SET status = 'approved' WHERE id = 'd1'")
    r = await send_draft(db, "s", {}, draft)
    assert r["status"] == "sent" and r["message_id"] == "<out1@mine>"
    sent = SentBox.sent[0]
    assert sent["to"] == ["ann@x.example"] and sent["in_reply_to"] == "<a1@x>" and sent["reply_to"] == "inbox@mine.example"
    assert sent["body_text"].startswith("Hi Ann,\n\nTuesday at ten works.") and "https://acme.test" in sent["body_text"]
    assert 'href="https://acme.test"' in sent["body_html"] and sent["from_name"] == "Jane Doe"
    thread = await db.one("SELECT * FROM threads WHERE id = ?", (tid,))
    assert thread["state"] == "awaiting_them" and thread["message_count"] == 2
    msg = await db.one("SELECT * FROM messages WHERE message_id = '<out1@mine>'")
    assert msg["sent_by"] == "ai" and msg["direction"] == "outbound" and msg["thread_id"] == tid
    assert (await db.mailbox(mid))["counts"]["awaiting_them"] == 1
    assert any(a["text"].startswith('Replied to Ann Lee — "Re: Site visit"') for a in await db.activity())
    # The daily cap (1) counts new emails: the first goes, the next is held...
    for d in ("d2", "d4"):
        await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                         "VALUES (?, ?, '', 'new', ?, 'Site visit dates', 'Another.', 'approved', ?, ?)",
                         (d, mid, json.dumps([{"name": "Bo", "email": "bo@x.example"}]), now, now))
    assert (await send_draft(db, "s", {}, await db.one("SELECT * FROM drafts WHERE id = 'd2'")))["status"] == "sent"
    with pytest.raises(SendRefused, match="daily cap"):
        await send_draft(db, "s", {}, await db.one("SELECT * FROM drafts WHERE id = 'd4'"))
    # ...but never a reply: replies have no limit.
    later = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, in_reply_to, status, created_at, updated_at) "
                     "VALUES ('d3', ?, ?, 'reply', ?, 'Re: Site visit', 'And the gate code is 4411.', '<a1@x>', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), later, later))
    assert (await send_draft(db, "s", {}, await db.one("SELECT * FROM drafts WHERE id = 'd3'")))["status"] == "sent"
    # The sync later sees the same sent email by Message-ID: no duplicate.
    ing = Ingest(db, await db.mailbox(mid))
    assert await ing.store_batch([RawMessage(provider_id="Sent:99", thread_key="T1", message_id="<out1@mine>", from_email=ME, subject="Re: Site visit", body="x", outbound=True)]) == 0


@pytest.mark.asyncio
async def test_worker_send_job_holds_refused_drafts(tmp_path, monkeypatch):
    from bb.mail import crypto
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    root = tmp_path / "ws"
    db = MailDB(root / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds=crypto.seal_json("s", {}), status="ok")
    tid = await _seed_thread(db, mid)
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, 'reply', ?, 'Re: Site visit', 'Tuesday works, I will bring the drawings over.', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    worker = Worker(root, "s", {})
    jid = await worker.jobs.enqueue("send_draft", mid, {"draft_id": "d1"})
    job = await worker.jobs.claim()
    assert (await worker.handlers.run(job))["status"] == "held"
    d = await db.one("SELECT status, error FROM drafts WHERE id = 'd1'")
    assert d["status"] == "pending_review" and "signature" in d["error"]
    await db.update_mailbox(mid, settings=MailboxSettings(signature_html="J", project_link="https://a.test").model_dump())
    await db.execute("UPDATE drafts SET status = 'approved' WHERE id = 'd1'")
    job = await worker.jobs.claim() or {"kind": "send_draft", "payload": {"draft_id": "d1"}, "mailbox_id": mid, "id": jid}
    assert (await worker.handlers.run(job))["status"] == "sent"


def test_thread_composer_review_and_compose_pages(mail_client, monkeypatch):
    client = mail_client
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    client.post("/apikey", data={"api_key": "sk-ant-abcdefghijklmnop"})
    client.post("/mail/accounts/imap", data={"email": ME, "password": "pw"})
    db = ws.mail_db()
    mid = asyncio.run(db.mailboxes())[0]["id"]
    tid = asyncio.run(_seed_thread(db, mid))
    answers = [{"body": "Tuesday at ten works, Ann. I'll bring the drawings so we can walk the site together.",
                "confidence": "high", "confidence_reason": "clear ask"}]
    brains = []
    def fake_brain(ws_):
        b = FakeBrain(list(answers)); brains.append(b); return b
    monkeypatch.setattr("bb.mail.routes.MailBrain", FakeBrain, raising=False)
    import bb.mail.routes as routes_mod
    monkeypatch.setattr(routes_mod, "register_mail_routes", routes_mod.register_mail_routes)
    from bb.mail import brain as brain_mod
    monkeypatch.setattr(brain_mod.MailBrain, "complete", FakeBrain(list(answers) * 3).complete)

    page = client.get(f"/mail/threads/{tid}").text
    assert "Draft a reply" in page
    r = client.post(f"/mail/threads/{tid}/draft", data={"instructions": "say yes"})
    assert "Draft%20ready" in r.headers.get("location", ""), (r.status_code, r.text[:500])
    page = client.get(r.headers["location"]).text
    assert "Tuesday at ten works, Ann." in page and "confidence: high" in page and "passed the writing check" in page
    assert "needs signature" in page.lower() or "Set a signature" in page   # mailbox not ready yet
    draft = asyncio.run(db.one("SELECT * FROM drafts WHERE thread_id = ?", (tid,)))
    r = client.post(f"/mail/drafts/{draft['id']}/send", data={"body": "Edited.", "subject": "Re: Site visit"})
    assert "Set%20a%20signature" in r.headers["location"]
    client.post(f"/mail/accounts/{mid}", data={"display_name": "Jane", "project_link": "https://acme.test", "signature_html": "Jane"})
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: 1)
    r = client.post(f"/mail/drafts/{draft['id']}/send", data={"body": "Tuesday at ten works, Ann. Edited by hand.", "reply_to": "inbox@mine.example"})
    assert "Sending" in r.headers["location"]
    d = asyncio.run(db.one("SELECT * FROM drafts WHERE id = ?", (draft["id"],)))
    assert d["status"] == "approved" and d["body"].endswith("Edited by hand.") and d["reply_to"] == "inbox@mine.example"
    jobs = [j for j in asyncio.run(Jobs(db).list("queued")) if j["kind"] == "send_draft"]   # a backfill job may sit beside it
    assert jobs and jobs[0]["payload"] == {"draft_id": draft["id"], "dry_run": False, "saw_thread": False}
    assert "Sending…" in client.get(f"/mail/threads/{tid}").text
    assert "already%20sent" not in client.post(f"/mail/drafts/{draft['id']}/send", data={}).headers["location"]

    # Review tray lists pending drafts across the scope; approve-all queues them.
    now = service.now_iso()
    asyncio.run(db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, confidence, created_at, updated_at) "
                           "VALUES ('p1', ?, ?, 'reply', ?, 'Re: Site visit', 'Pending one.', 'pending_review', 'low', ?, ?)",
                           (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now)))
    page = client.get("/mail/review").text
    assert "Pending one." in page and "Approve all 1" in page and "Recently sent" in page
    assert "Approved%201" in client.post("/mail/review/approve-all").headers["location"]
    assert (asyncio.run(db.one("SELECT status FROM drafts WHERE id = 'p1'")))["status"] == "approved"
    assert "Nothing waiting" in client.get("/mail/review").text

    # Compose a new email.
    monkeypatch.setattr(brain_mod.MailBrain, "complete", FakeBrain([{"subject": "Moving Thursday's call", "body": "Could Thursday's call move to Friday afternoon? I'll send the agenda before.", "confidence": "high", "confidence_reason": ""}]).complete)
    page = client.get("/mail/compose").text
    assert "New email" in page and ME in page
    r = client.post("/mail/compose", data={"mailbox": mid, "to": "john@x.example", "to_name": "John", "instruction": "ask John to move Thursday's call to Friday"})
    assert "Draft%20ready" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "Moving Thursday" in page and "Could Thursday" in page
    new = asyncio.run(db.one("SELECT * FROM drafts WHERE kind = 'new'"))
    assert json.loads(new["to_json"]) == [{"name": "John", "email": "john@x.example"}]
    assert "Draft%20discarded" in client.post(f"/mail/drafts/{new['id']}/reject").headers["location"]


# ── answers that are not the JSON asked for ──


def test_json_is_read_through_what_models_wrap_it_in():
    assert extract_json('<think>Answer {briefly} with [care]</think>\n{"body": "Tuesday works", "confidence": "high"}') == \
        {"body": "Tuesday works", "confidence": "high"}
    # real line breaks inside the string, prose with braces before it, a trailing comma
    assert extract_json('Here is the {draft}: {"body": "Line one\nLine two", "confidence": "medium",}') == \
        {"body": "Line one\nLine two", "confidence": "medium"}
    assert extract_json("{'body': 'hi there', 'confidence': 'low'}") == {"body": "hi there", "confidence": "low"}
    assert extract_json('{"body": "See [SCOPE LINK 1] and {this}", "confidence": "high"}')["body"] == "See [SCOPE LINK 1] and {this}"


@pytest.mark.asyncio
async def test_an_unreadable_answer_is_asked_for_once_more_then_kept_for_a_person():
    settings = MailboxSettings(display_name="Jane Doe")
    thread = {"subject": "Site visit", "id": "t"}
    messages = [{"direction": "inbound", "from_name": "Ann Lee", "from_email": "ann@x", "body": "Can we do Tuesday at ten?", "sent_at": "2026-01-01"}]
    good = {"body": "Tuesday at ten works, Ann. I'll bring the drawings you asked about so we can walk the site together.",
            "confidence": "high", "confidence_reason": "clear ask", "stance": "question"}
    # Not JSON the first time: asked again, plainly, and the second answer is used.
    brain = FakeBrain(["I think Tuesday is fine!", good])
    result = await draft_reply(brain, settings, {"address": ME}, thread, messages)
    assert result["body"].startswith("Tuesday at ten works") and result["confidence"] == "high"
    assert "could not be read as JSON" in brain.prompts[1][1]
    # Cut off both times: the body it wrote is kept, at low confidence, so it waits on Review.
    cut = '{"body": "Tuesday at ten works, Ann. I will bring the drawings you asked about so we can walk the site'
    brain = FakeBrain([cut, cut, cut, cut])
    result = await draft_reply(brain, settings, {"address": ME}, thread, messages)
    assert result["body"].startswith("Tuesday at ten works, Ann.") and result["confidence"] == "low"
    assert "read it before it goes" in result["confidence_reason"]
    # The email written out instead of JSON, twice: kept the same way, without the "Here's a reply" line.
    plain = "Here's a reply:\n\nTuesday at ten works, Ann. I'll bring the drawings you asked about so we can walk the site."
    brain = FakeBrain([plain, plain, plain, plain])
    result = await draft_reply(brain, settings, {"address": ME}, thread, messages)
    assert result["body"].startswith("Tuesday at ten works") and result["confidence"] == "low"
    # A no-contact in their message still stops it, even without the model's reading.
    stop = [{**messages[0], "body": "Please remove me from your list and stop emailing me."}]
    brain = FakeBrain([plain, plain])
    result = await draft_reply(brain, settings, {"address": ME}, thread, stop)
    assert result["stance"] == "opt_out" and result["body"] == ""
    # Nothing usable at all: the item fails with what the model said.
    brain = FakeBrain(["", ""])
    from bb.mail.brain import BrainError
    with pytest.raises(BrainError, match="twice"):
        await draft_reply(brain, settings, {"address": ME}, thread, messages)


@pytest.mark.asyncio
async def test_what_is_never_kept_from_an_unreadable_answer():
    from bb.mail.brain import BrainError, plain_answer
    from bb.mail.drafting import draft_reconnect
    settings = MailboxSettings(display_name="Jane Doe")
    thread = {"subject": "Site visit", "id": "t"}
    # A reasoning model cut off mid-thought: none of it is an email.
    assert plain_answer("<think>Okay, the user wants me to rewrite this email so it is shorter") == ""
    # The model's own no-contact reading stands even when the regex reads a polite no.
    rude = [{"direction": "inbound", "from_name": "Ann", "from_email": "ann@x", "sent_at": "2026-01-01",
             "body": "Not interested. How did you get my email address? Third time this month."}]
    broken = '{"body": "Understood. I won\'t write again, sorry for the "spam".", "confidence": "high", "stance": "opt_out"}'
    result = await draft_reply(FakeBrain([broken, broken]), settings, {"address": ME}, thread, rude)
    assert result["stance"] == "opt_out" and result["body"] == ""
    # A second ask that errors (a free tier's 429): the first answer is still used.
    class Flaky(FakeBrain):
        async def complete(self, system, user, max_tokens=1200):
            if self.prompts:
                raise BrainError("answered 429")
            return await super().complete(system, user, max_tokens)
    ask = [{"direction": "inbound", "from_name": "Bob", "from_email": "bob@x", "body": "Can we do Tuesday?", "sent_at": "2026-01-01"}]
    first = "Tuesday works for the site visit, Bob. I'll bring the drawings you asked about and we can walk it together."
    result = await draft_reply(Flaky([first]), settings, {"address": ME}, thread, ask)
    assert result["body"].startswith("Tuesday works") and result["confidence"] == "low"
    # A follow-up declined in prose is a skip, not a draft to a doctor's office.
    clinic = [{"direction": "inbound", "from_name": "Clinic", "from_email": "desk@clinic.example", "sent_at": "2025-01-01",
               "body": "Your appointment is confirmed for Monday."}]
    prose = "Not suitable: this is a private exchange with a doctor's office about an appointment, so I won't write a follow-up."
    result = await draft_reconnect(FakeBrain([prose, prose]), settings, {"address": ME}, thread, clinic)
    assert result["suitable"] is False and "private exchange" in result["why"]
