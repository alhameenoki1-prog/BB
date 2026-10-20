"""Phase 8: Microsoft 365 through Graph (client credentials) — token,
test, backfill, incremental sync, replies with createReply and new mail
as drafts; the connect route and reconnect form; the audit log; retry-all."""

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import bb.mail.service as service
from bb.mail import crypto
from bb.mail.db import MailDB
from bb.mail.jobs import Jobs
from bb.mail.providers import AuthError
from bb.mail.providers.graph import GraphAccount
from bb.mail.sending import send_draft
from bb.mail.models import MailboxSettings

from tests.test_mail_foundation import _FakeAccount, mail_client  # noqa: F401
from tests.test_bb_web import USER, _as_user, _accept_key  # noqa: F401
from bb.accounts import Accounts
from bb.workspaces import Workspace

ME = "ops@contoso.example"
CREDS = {"tenant_id": "tenant-1", "client_id": "client-1", "client_secret": "good"}


def _msg(i, *, outbound=False, folder="INBOXID", conv="CONV1"):
    who = {"emailAddress": {"name": "Ops", "address": ME}} if outbound else {"emailAddress": {"name": f"Ann {i}", "address": f"ann{i}@x.example"}}
    return {"id": f"G{i}", "conversationId": conv if i < 2 else f"CONV{i}", "internetMessageId": f"<g{i}@x>", "subject": f"Hello {i}",
            "bodyPreview": "preview", "body": {"contentType": "html", "content": f"<p>Body <b>{i}</b></p>"}, "from": who,
            "toRecipients": [{"emailAddress": {"name": "", "address": ME}}], "ccRecipients": [], "isDraft": False,
            "receivedDateTime": f"2026-03-0{i + 1}T10:00:00Z", "sentDateTime": f"2026-03-0{i + 1}T10:00:00Z", "parentFolderId": "SENTID" if outbound else folder}


class FakeGraph:
    """Enough of login.microsoftonline.com and graph.microsoft.com for the adapter."""
    def __init__(self):
        self.messages = [_msg(0), _msg(1, outbound=True), _msg(2), {**_msg(3), "isDraft": True}]
        self.calls = []
        self.sent = []
        self.drafts = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url)); q = parse_qs(url.query)
        self.calls.append((request.method, url.path, q))
        if url.netloc == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            if form.get("client_secret") != ["good"]:
                return httpx.Response(401, json={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(401, json={"error": {"message": "no token"}})
        p = url.path
        if "/users/nobody@contoso.example/" in p:
            return httpx.Response(404, json={})
        if p.endswith("/mailFolders/inbox"):
            return httpx.Response(200, json={"id": "INBOXID", "totalItemCount": 3})
        if p.endswith("/mailFolders/sentitems"):
            return httpx.Response(200, json={"id": "SENTID"})
        if p.endswith("/messages") and request.method == "GET":
            flt = (q.get("$filter") or [""])[0]
            if flt.startswith("internetMessageId eq"):
                mid = flt.split("'")[1]
                return httpx.Response(200, json={"value": [{"id": m["id"], "conversationId": m["conversationId"]} for m in self.messages if m["internetMessageId"] == mid]})
            since = flt.split(" ge ")[1] if " ge " in flt else ""
            rows = [m for m in self.messages if m["receivedDateTime"] >= since]
            desc = "desc" in (q.get("$orderby") or [""])[0]
            rows.sort(key=lambda m: m["receivedDateTime"], reverse=desc)
            page = int((q.get("page") or ["1"])[0])
            chunk = rows[(page - 1) * 2: page * 2]
            body = {"value": chunk}
            if len(rows) > page * 2:
                body["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0{p}?page={page + 1}&$filter={flt}&$orderby={(q.get('$orderby') or [''])[0]}"
            return httpx.Response(200, json=body)
        if p.endswith("/createReply"):
            orig = p.split("/messages/")[1].split("/")[0]
            d = {"id": f"D-{orig}", "internetMessageId": f"<reply-{orig}@contoso.example>", "conversationId": next(m["conversationId"] for m in self.messages if m["id"] == orig)}
            self.drafts[d["id"]] = d
            return httpx.Response(201, json=d)
        if p.endswith("/messages") and request.method == "POST":
            d = {"id": "D-new", "internetMessageId": "<new-1@contoso.example>", "conversationId": "CONVNEW", **json.loads(request.content)}
            self.drafts["D-new"] = d
            return httpx.Response(201, json=d)
        if request.method == "PATCH" and "/messages/" in p:
            did = p.rsplit("/", 1)[1]
            self.drafts[did].update(json.loads(request.content))
            return httpx.Response(200, json=self.drafts[did])
        if p.endswith("/send"):
            did = p.split("/messages/")[1].split("/")[0]
            self.sent.append(self.drafts[did])
            return httpx.Response(202)
        return httpx.Response(404, json={"error": {"message": p}})


@pytest.fixture
def graph(monkeypatch):
    fake = FakeGraph()
    real = httpx.AsyncClient
    monkeypatch.setattr("bb.mail.providers.graph.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw))
    return fake


@pytest.mark.asyncio
async def test_graph_adapter_test_backfill_sync_and_send(graph):
    acct = GraphAccount({"address": ME, "provider": "graph"}, CREDS, {})
    assert await acct.test() == "Inbox reachable (3 messages)."
    with pytest.raises(AuthError, match="Invalid client secret"):
        await GraphAccount({"address": ME, "provider": "graph"}, {**CREDS, "client_secret": "bad"}, {}).test()
    with pytest.raises(AuthError, match="no mailbox"):
        await GraphAccount({"address": "nobody@contoso.example", "provider": "graph"}, CREDS, {}).test()

    batches = []
    async def on_batch(msgs, done, total):
        batches.append((msgs, done, total))
    state = await acct.backfill(365, on_batch)
    got = [m for b in batches for m in b[0]]
    assert [m.provider_id for m in got] == ["G2", "G1", "G0"]          # newest first, the draft left out
    assert got[1].outbound and got[1].folder == "SENT" and got[1].from_email == ME
    assert got[2].body == "Body 0" and got[2].thread_key == "CONV1" and got[2].message_id == "<g0@x>"
    assert got[2].to == [{"name": "", "email": ME}] and got[2].from_name == "Ann 0"
    assert state["backfill_done"] and state["cursor"] == "2026-03-04T10:00:00Z" and state["sent_folder"] == "SENTID"
    assert batches[-1][1] == 4 and batches[-1][2] == 4   # 4 read (one a draft), 3 kept

    # Incremental: a new message after the cursor arrives; the overlap repeats nothing new to the store.
    graph.messages.append(_msg(4))
    batches.clear()
    state = await acct.sync(state, on_batch)
    ids = [m.provider_id for b in batches for m in b[0]]
    assert "G4" in ids and state["cursor"] == "2026-03-05T10:00:00Z"
    assert "asc" in [c for c in graph.calls if c[0] == "GET" and c[1].endswith("/messages") and c[2].get("$orderby")][-1][2]["$orderby"][0]

    # Reply: found by Message-ID → createReply → patched → sent, keeping the conversation.
    mid, pid, conv = await acct.send(to=["ann0@x.example"], subject="Re: Hello 0", body_text="Hi", body_html="<p>Hi</p>",
                                     in_reply_to="<g0@x>", reply_to="team@contoso.example", from_name="Ops")
    assert mid == "<reply-G0@contoso.example>" and conv == "CONV1"
    sent = graph.sent[-1]
    assert sent["body"] == {"contentType": "HTML", "content": "<p>Hi</p>"} and sent["replyTo"][0]["emailAddress"]["address"] == "team@contoso.example"
    assert sent["toRecipients"][0]["emailAddress"]["address"] == "ann0@x.example"
    # New mail (or a reply whose original is unknown) goes out as a fresh draft.
    mid, pid, conv = await acct.send(to=["new@x.example"], subject="Hello", body_text="Hi", body_html="", in_reply_to="<unknown@x>")
    assert mid == "<new-1@contoso.example>" and conv == "CONVNEW" and graph.sent[-1]["subject"] == "Hello"
    assert graph.sent[-1]["body"]["content"] == "Hi"


@pytest.mark.asyncio
async def test_connect_graph_run_sync_and_send_through_the_service(graph, tmp_path):
    db = MailDB(tmp_path / "mail.db"); await db.init()
    with pytest.raises(ValueError):
        await service.connect_graph(db, "s", address=ME, tenant_id="t", client_id="", client_secret="x", env={})
    mid, note = await service.connect_graph(db, "s", address=ME, tenant_id="t", client_id="c", client_secret="good", env={})
    box = await db.mailbox(mid)
    assert box["provider"] == "graph" and box["mode"] == "full" and box["status"] == "ok" and "Inbox reachable" in note
    assert service.masked_creds("s", box)["client_secret"] != "good"
    jobs = Jobs(db)
    jid = await jobs.enqueue("sync_backfill", mid, {})
    job = await jobs.claim()
    out = await service.run_sync(db, "s", box, {}, jobs, job["id"], backfill=True)
    assert out["new"] == 3
    box = await db.mailbox(mid)
    assert box["counts"]["needs_reply"] == 1 and box["counts"]["total"] == 3 and box["sync"]["cursor"]
    t = await db.one("SELECT * FROM threads WHERE provider_thread_id = 'CONV2'")
    assert t["state"] == "needs_reply"
    await db.update_mailbox(mid, settings=MailboxSettings(display_name="Ops", signature_html="Ops", project_link="https://c.test").model_dump())
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, in_reply_to, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, 'reply', ?, 'Re: Hello 2', 'Thanks, Tuesday works for me and I will bring it along.', '<g2@x>', 'approved', ?, ?)",
                     (mid, t["id"], json.dumps([{"name": "Ann 2", "email": "ann2@x.example"}]), now, now))
    r = await send_draft(db, "s", {}, await db.one("SELECT * FROM drafts WHERE id = 'd1'"))
    assert r["status"] == "sent" and r["message_id"] == "<reply-G2@contoso.example>"
    assert (await db.one("SELECT state FROM threads WHERE id = ?", (t["id"],)))["state"] == "awaiting_them"
    assert "https://c.test" in graph.sent[-1]["body"]["content"]
    # A wrong secret on reconnect leaves the stored one alone and says why.
    with pytest.raises(AuthError):
        await service.connect_graph(db, "s", address=ME, tenant_id="t", client_id="c", client_secret="bad", env={})
    assert service.open_creds("s", await db.mailbox(mid))["client_secret"] == "good"


def test_graph_connect_page_audit_log_and_retry_all(mail_client, graph, monkeypatch):
    client = mail_client
    _as_user(client)
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: 1)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "started"))
    import bb.mail.providers as providers
    monkeypatch.setattr(service, "account_for", providers.account_for)   # the real adapter, against the fake Graph
    page = client.get("/mail/accounts").text
    assert "Connect Microsoft 365" in page and "Mail.Send" in page
    r = client.post("/mail/accounts/graph", data={"email": ME, "tenant_id": "t", "client_id": "c", "client_secret": "bad"})
    assert "Microsoft%20refused" in r.headers["location"]
    r = client.post("/mail/accounts/graph", data={"email": ME, "tenant_id": "t", "client_id": "c", "client_secret": "good"})
    assert r.headers["location"].startswith("/mail/accounts/") and "Microsoft%20365" in r.headers["location"]
    mid = r.headers["location"].split("/mail/accounts/")[1].split("?")[0]
    page = client.get(f"/mail/accounts/{mid}").text
    assert "Microsoft 365 through the Graph API" in page and "Change the app credentials" in page and 'value="t"' in page
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    db = Workspace(client.settings, user).mail_db()
    asyncio.run(db.update_mailbox(mid, status="reconnect", status_detail="secret expired"))
    page = client.get(f"/mail/accounts/{mid}").text
    assert "needs reconnecting" in page and "client secret again" in page
    # Audit log: the connection line is there, filters work, sends table present.
    page = client.get("/mail/audit").text
    assert f"Connected {ME} through Microsoft 365." in page and "Emails sent, dry-run or held" in page
    assert "Connected" in client.get("/mail/audit?kind=connected").text and f"Connected {ME}" not in client.get("/mail/audit?kind=warning").text
    assert "Audit log" in client.get("/mail").text
    # Retry all failed jobs.
    jobs = Jobs(db)
    jid = asyncio.run(jobs.enqueue("check_mailbox", mid, {}, max_attempts=1))
    job = asyncio.run(jobs.claim()); asyncio.run(jobs.fail(job["id"], "boom"))
    assert asyncio.run(jobs.counts())["dead"] == 1
    assert "Retry all 1 failed" in client.get("/mail/jobs").text
    r = client.post("/mail/jobs/retry-all")
    assert "Queued%201%20failed%20job%20again" in r.headers["location"] and asyncio.run(jobs.counts())["dead"] == 0
