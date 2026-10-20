"""Phases 2 and 3: parsing, ingest, threading, the reply-state engine,
IMAP and Gmail backfill + incremental sync against fakes, the worker's
sync jobs, and the thread pages."""

import asyncio
import json

import pytest

import bb.mail.service as service
from bb.mail.db import MailDB
from bb.mail.ingest import Ingest, normalise_subject
from bb.mail.jobs import Jobs
from bb.mail.parse import parse_rfc822
from bb.mail.providers.base import RawMessage
from bb.mail.providers.gmail import GmailAccount
from bb.mail.providers.imap import ImapAccount
from bb.mail.state import compute_state
from bb.mail.worker import Worker

ME = "me@mine.example"


def rfc(mid, sender, to, subject, body, in_reply_to="", date="Mon, 1 Jan 2026 10:00:00 +0000", extra=""):
    return (f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: <{mid}@m>\r\nDate: {date}\r\n"
            + (f"In-Reply-To: <{in_reply_to}@m>\r\nReferences: <{in_reply_to}@m>\r\n" if in_reply_to else "")
            + extra + "Content-Type: text/plain\r\n\r\n" + body + "\r\n").encode()


def test_parse_rfc822_and_subjects():
    msg = parse_rfc822(rfc("a1", "Ann Lee <ann@x.example>", "Me <me@mine.example>", "Re: Quote", "Could you quote?", in_reply_to="a0",
                           extra="List-Unsubscribe: <mailto:x>\r\n"), provider_id="INBOX:1", folder="INBOX")
    assert msg.from_email == "ann@x.example" and msg.from_name == "Ann Lee" and msg.to[0]["email"] == ME
    assert msg.message_id == "<a1@m>" and msg.in_reply_to == "<a0@m>" and msg.sent_at == "2026-01-01T10:00:00Z"
    assert msg.body == "Could you quote?" and msg.headers["list-unsubscribe"] == "<mailto:x>"
    assert normalise_subject("RE: Fwd: re: Quote ") == "quote"
    html = b"From: a@b.c\r\nSubject: h\r\nContent-Type: text/html\r\n\r\n<p>Hello <b>there</b></p><br>Line two"
    assert parse_rfc822(html, provider_id="x").body.startswith("Hello there")
    assert parse_rfc822(b"garbage\x00", provider_id="y") is not None  # tolerant


def test_reply_state_rules():
    them = {"direction": "inbound", "from_email": "ann@x.example", "from_name": "Ann", "subject": "Quote", "headers": {}}
    me = {"direction": "outbound", "from_email": ME, "subject": "Re: Quote", "headers": {}}
    auto = {"direction": "inbound", "from_email": "noreply@shop.example", "subject": "Your receipt", "headers": {}}
    assert compute_state([them])[0] == "needs_reply"
    assert compute_state([them, me])[0] == "awaiting_them"
    assert compute_state([me])[0] == "replied"
    assert compute_state([them, me, them])[0] == "needs_reply"
    assert compute_state([auto])[0] == "no_reply_needed"
    assert compute_state([them, auto])[0] == "no_reply_needed"
    assert compute_state([them], current="snoozed")[0] == "snoozed"
    assert compute_state([])[0] == "unknown"


@pytest.mark.asyncio
async def test_ingest_threads_messages_once_and_keeps_counts(tmp_path):
    db = MailDB(tmp_path / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="v1:x")
    box = await db.mailbox(mid)
    ing = Ingest(db, box)
    a1 = parse_rfc822(rfc("a1", "Ann <ann@x.example>", ME, "Quote", "Could you quote?"), provider_id="INBOX:1", folder="INBOX")
    my_reply = parse_rfc822(rfc("a2", f"Me <{ME}>", "ann@x.example", "Re: Quote", "Sure, Tuesday?", in_reply_to="a1",
                                date="Mon, 1 Jan 2026 11:00:00 +0000"), provider_id="Sent:7", folder="Sent", outbound=True)
    news = parse_rfc822(rfc("n1", "News <news@shop.example>", ME, "Weekly digest", "Deals!", extra="Precedence: bulk\r\n"),
                        provider_id="INBOX:2", folder="INBOX")
    assert await ing.store_batch([a1, news]) == 2
    assert await ing.store_batch([a1, news]) == 0            # idempotent on provider id
    assert await ing.store_batch([my_reply]) == 1
    threads = await db.query("SELECT * FROM threads WHERE mailbox_id = ? ORDER BY created_at", (mid,))
    assert len(threads) == 2
    quote = next(t for t in threads if t["subject"] == "Quote")
    assert quote["message_count"] == 2 and quote["state"] == "awaiting_them" and quote["last_direction"] == "outbound"
    parts = json.loads(quote["participants_json"])
    assert {p["email"]: p["role"] for p in parts} == {"ann@x.example": "them", ME: "me"}
    digest = next(t for t in threads if t["subject"] == "Weekly digest")
    assert digest["state"] == "no_reply_needed"
    counts = (await db.mailbox(mid))["counts"]
    assert counts == {"needs_reply": 0, "awaiting_them": 1, "replied": 0, "no_reply_needed": 1, "total": 3, "threads": 2}
    # They answer: the thread needs a reply again.
    a3 = parse_rfc822(rfc("a3", "Ann <ann@x.example>", ME, "Re: Quote", "Tuesday works.", in_reply_to="a2",
                          date="Mon, 1 Jan 2026 12:00:00 +0000"), provider_id="INBOX:3", folder="INBOX")
    await ing.store_batch([a3])
    quote = await db.one("SELECT * FROM threads WHERE id = ?", (quote["id"],))
    assert quote["state"] == "needs_reply" and quote["message_count"] == 3 and "Ann wrote last" in quote["state_reason"]
    assert (await db.mailbox(mid))["counts"]["needs_reply"] == 1
    # Gmail-style provider thread ids group without headers.
    g1 = RawMessage(provider_id="g1", thread_key="T9", from_email="bo@y.example", subject="Hi", body="x", sent_at="2026-01-02T00:00:00Z")
    g2 = RawMessage(provider_id="g2", thread_key="T9", from_email=ME, subject="Re: Hi", body="y", sent_at="2026-01-02T01:00:00Z", outbound=True)
    await ing.store_batch([g1, g2])
    t9 = await db.one("SELECT * FROM threads WHERE provider_thread_id = 'T9'")
    assert t9["message_count"] == 2 and t9["state"] == "awaiting_them"


# ── IMAP against a fake server ──

class FakeIMAP:
    """Enough of imaplib for backfill + incremental sync: two folders, UIDs, UIDVALIDITY."""
    store = {
        "INBOX": {"validity": 11, "msgs": {
            1: rfc("a1", "Ann <ann@x.example>", ME, "Quote", "Could you quote?"),
            2: rfc("n1", "noreply@shop.example", ME, "Receipt", "Thanks for buying", extra="Auto-Submitted: auto-generated\r\n"),
        }},
        "[Gmail]/Sent Mail": {"validity": 22, "msgs": {
            5: rfc("a2", f"Me <{ME}>", "ann@x.example", "Re: Quote", "Sure, Tuesday?", in_reply_to="a1",
                   date="Mon, 1 Jan 2026 11:00:00 +0000"),
        }},
    }
    logins = []

    def __init__(self, host, port, timeout=None):
        self.folder = None
    def login(self, u, p):
        FakeIMAP.logins.append(u)
        if p != "pw":
            import imaplib
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\Noselect \\HasChildren) "/" "[Gmail]"']
    def select(self, folder, readonly=False):
        name = folder.strip('"')
        if name not in self.store:
            return "NO", [b"nope"]
        self.folder = name
        return "OK", [str(len(self.store[name]["msgs"])).encode()]
    def response(self, key):
        return "OK", [str(self.store[self.folder]["validity"]).encode()]
    def uid(self, cmd, *args):
        msgs = self.store[self.folder]["msgs"]
        if cmd == "SEARCH":
            query = args[-1]
            if query.startswith("UID "):
                lo = int(query[4:].split(":")[0])
                ids = [u for u in msgs if u >= lo]
            else:
                ids = list(msgs)
            return "OK", [" ".join(str(u) for u in sorted(ids)).encode()]
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                u = int(u)
                if u in msgs:
                    out.append((f"{u} (UID {u} FLAGS (\\Seen) BODY[] {{{len(msgs[u])}}}".encode(), msgs[u]))
                    out.append(b")")
            return "OK", out
        raise AssertionError(cmd)
    def logout(self):
        pass


@pytest.mark.asyncio
async def test_imap_backfill_then_incremental(monkeypatch):
    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    creds = {"username": ME, "password": "pw", "smtp_host": "smtp.x", "imap_host": "imap.x", "imap_port": 993}
    acct = ImapAccount({"address": ME, "provider": "imap"}, creds, {})
    batches = []
    async def on_batch(msgs, done, total):
        batches.append((len(msgs), done, total))
    state = await acct.backfill(365, on_batch)
    assert state["backfill_done"] and state["folders"]["INBOX"]["last_uid"] == 2
    assert state["folders"]["[Gmail]/Sent Mail"] == {"uidvalidity": 22, "last_uid": 5, "sent": True}
    assert batches[-1][1:] == (3, 3) and sum(b[0] for b in batches) == 3
    # Nothing new: incremental finds nothing.
    got = []
    async def collect(msgs, done, total):
        got.extend(msgs)
    state = await acct.sync(state, collect)
    assert got == []
    # New mail arrives, the sent folder is rebuilt (new UIDVALIDITY): sync picks up the new UID and re-reads the folder.
    FakeIMAP.store["INBOX"]["msgs"][3] = rfc("a3", "Ann <ann@x.example>", ME, "Re: Quote", "Tuesday works.", in_reply_to="a2")
    FakeIMAP.store["[Gmail]/Sent Mail"]["validity"] = 23
    state = await acct.sync(state, collect)
    assert sorted(m.provider_id for m in got) == ["INBOX:3", "[Gmail]/Sent Mail:5"]
    assert state["folders"]["INBOX"]["last_uid"] == 3 and state["folders"]["[Gmail]/Sent Mail"]["uidvalidity"] == 23
    assert all(m.outbound for m in got if m.folder == "[Gmail]/Sent Mail")
    from bb.mail.providers.base import AuthError
    with pytest.raises(AuthError):
        await ImapAccount({"address": ME, "provider": "imap"}, {**creds, "password": "bad"}, {}).test()


# ── Gmail against a fake API ──

class FakeGmailAPI:
    def __init__(self):
        self.messages = {
            "m1": self.msg("m1", "T1", "ann@x.example", "Quote", "Could you quote?", ["INBOX"]),
            "m2": self.msg("m2", "T1", ME, "Re: Quote", "Sure", ["SENT"]),
            "m3": self.msg("m3", "T2", "news@shop.example", "Digest", "Deals", ["INBOX"], extra=[("List-Id", "<x.list>")]),
        }
        self.history = []
        self.history_id = "100"
        self.calls = []
        self.history_lost = False

    @staticmethod
    def msg(mid, tid, sender, subject, body, labels, extra=()):
        import base64
        headers = [{"name": "From", "value": sender}, {"name": "To", "value": ME}, {"name": "Subject", "value": subject},
                   {"name": "Message-ID", "value": f"<{mid}@g>"}] + [{"name": k, "value": v} for k, v in extra]
        return {"id": mid, "threadId": tid, "labelIds": labels, "internalDate": "1767265200000", "snippet": body[:40],
                "payload": {"mimeType": "text/plain", "headers": headers,
                            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}

    async def get(self, path, params=None):
        self.calls.append(path)
        if path == "/profile":
            return {"emailAddress": ME, "messagesTotal": len(self.messages), "historyId": self.history_id}
        if path == "/messages":
            ids = list(self.messages)
            page = params.get("pageToken")
            if page is None:
                return {"messages": [{"id": i} for i in ids[:2]], "nextPageToken": "p2", "resultSizeEstimate": len(ids)}
            return {"messages": [{"id": i} for i in ids[2:]]}
        if path.startswith("/messages/"):
            mid = path.split("/")[-1]
            if mid not in self.messages:
                raise LookupError(path)
            return self.messages[mid]
        if path == "/history":
            if self.history_lost:
                raise LookupError(path)
            return {"history": [{"messagesAdded": [{"message": {"id": i}} for i in self.history]}], "historyId": self.history_id}
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_gmail_backfill_then_history_sync():
    api = FakeGmailAPI()
    acct = GmailAccount({"address": ME, "provider": "gmail"}, {"refresh_token": "rt"}, {"GMAIL_CLIENT_ID": "c", "GMAIL_CLIENT_SECRET": "s"})
    acct._get = api.get
    got = []
    async def on_batch(msgs, done, total):
        got.extend(msgs)
    state = await acct.backfill(0, on_batch)
    assert state == {"historyId": "100", "backfill_done": True, "total_seen": 3}
    assert [m.provider_id for m in got] == ["m1", "m2", "m3"]
    assert got[1].outbound and got[0].thread_key == "T1" and got[2].headers["list-id"] == "<x.list>"
    api.messages["m4"] = api.msg("m4", "T1", "ann@x.example", "Re: Quote", "Tuesday works", ["INBOX"])
    api.messages["gone"] = api.msg("gone", "T3", "x@y", "x", "x", ["INBOX"])
    api.history = ["m4", "gone"]; del api.messages["gone"]; api.history_id = "120"
    got.clear()
    state = await acct.sync(state, on_batch)
    assert [m.provider_id for m in got] == ["m4"] and state["historyId"] == "120"
    api.history_lost = True; api.history_id = "130"
    got.clear()
    state = await acct.sync(state, on_batch)
    assert len(got) == 4 and state["historyId"] == "130"       # fell back to a re-list


# ── the worker syncs for real (fake IMAP), concurrently, with progress ──

@pytest.mark.asyncio
async def test_worker_backfills_then_syncs_and_counts(tmp_path, monkeypatch):
    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(ImapAccount, "_smtp_check", lambda self: None)
    FakeIMAP.store["INBOX"]["msgs"].pop(3, None); FakeIMAP.store["[Gmail]/Sent Mail"]["validity"] = 22
    root = tmp_path / "ws"
    db = MailDB(root / "data" / "mail.db"); await db.init()
    from bb.mail import crypto
    creds = {"username": ME, "password": "pw", "smtp_host": "smtp.x", "imap_host": "imap.x", "imap_port": 993}
    mid = await db.add_mailbox(address=ME, provider="imap", creds=crypto.seal_json("s", creds), status="ok")
    worker = Worker(root, "s", {})
    jobs = worker.jobs
    await service.schedule_syncs(db, jobs)
    assert [j["kind"] for j in await jobs.list("queued")] == ["sync_backfill"]
    task = asyncio.create_task(worker.loop())
    for _ in range(100):
        await asyncio.sleep(0.1)
        box = await db.mailbox(mid)
        if (box.get("sync") or {}).get("backfill_done"):
            break
    worker.stop.set(); await task
    box = await db.mailbox(mid)
    assert box["sync"]["backfill_done"] and box["counts"]["total"] == 3 and box["counts"]["awaiting_them"] == 1
    assert box["counts"]["no_reply_needed"] == 1 and box["last_sync_at"]
    done = (await jobs.list("done"))[0]
    assert done["progress"]["phase"] == "backfill" and done["progress"]["done"] == 3 and done["progress"]["new"] == 3
    texts = [r["text"] for r in await db.activity()]
    assert any(t.startswith("Finished syncing me@mine.example: 3 messages in 2 conversations") for t in texts)
    # Incremental is scheduled only once the interval has passed.
    await service.schedule_syncs(db, jobs)
    assert not await jobs.list("queued")
    await db.update_mailbox(mid, last_sync_at="2000-01-01T00:00:00Z")
    await service.schedule_syncs(db, jobs)
    assert [j["kind"] for j in await jobs.list("queued")] == ["sync_incremental"]
    # An expired sign-in during sync becomes a reconnect banner, not a dead job.
    FakeIMAP.store["INBOX"]["msgs"][3] = rfc("a3", "Ann <ann@x.example>", ME, "Re: Quote", "Tuesday works.", in_reply_to="a2")
    await db.update_mailbox(mid, creds=crypto.seal_json("s", {**creds, "password": "bad"}))
    job = await jobs.claim()
    result = await worker.handlers.run(job)
    assert result["status"] == "reconnect" and (await db.mailbox(mid))["status"] == "reconnect"
