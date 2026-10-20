"""Phase 1 of the mail command center: sealed credentials, the per-user
database in WAL mode, the job queue with retries and a dead-letter list,
the worker loop, mailbox connection and checks, scope, migration, and
the pages under /mail."""

import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import bb.mail.service as service
import bb.workspaces as ws_mod
from bb.accounts import Accounts
from bb.app import create_app
from bb.mail import crypto
from bb.mail.db import MailDB
from bb.mail.jobs import Jobs
from bb.mail.models import MailboxSettings
from bb.mail.providers import AuthError, TransientError
from bb.mail.worker import Worker
from bb.workspaces import Workspace

from tests.test_bb_web import ADMIN, PROFILE, USER, _as_user, _settings, _accept_key  # noqa: F401


# ── crypto ──

def test_sealed_credentials_round_trip_and_refuse_tampering():
    sealed = crypto.seal_json("site-secret", {"password": "app-pass", "host": "imap.x"})
    assert sealed.startswith("v1:") and "app-pass" not in sealed
    assert crypto.open_json("site-secret", sealed) == {"password": "app-pass", "host": "imap.x"}
    with pytest.raises(Exception):
        crypto.open_json("other-secret", sealed)
    with pytest.raises(ValueError):
        crypto.open_sealed("site-secret", "plain text")
    assert crypto.mask("abcdefghij") == "••••••••ghij"
    with pytest.raises(ValueError):
        crypto.seal("", "x")


# ── database ──

@pytest.mark.asyncio
async def test_mail_db_is_wal_and_has_every_table(tmp_path):
    db = MailDB(tmp_path / "mail.db")
    await db.init()
    await db.init()  # idempotent
    assert (await db.journal_mode()).lower() == "wal"
    names = {r[0] for r in sqlite3.connect(str(db.path)).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"mailboxes", "threads", "messages", "participants", "reply_runs", "run_items", "drafts",
            "activity", "jobs", "rules", "settings"} <= names
    mid = await db.add_mailbox(address="A@X.com", provider="imap", creds="v1:x", mode="limited",
                               settings={"color": "green"})
    m = await db.mailbox(mid)
    assert m["address"] == "a@x.com" and m["settings"]["color"] == "green" and m["status"] == "new"
    assert (await db.mailbox_by_address("a@x.COM"))["id"] == mid
    await db.update_mailbox(mid, status="ok", counts={"needs_reply": 3}, sync={"uid": 9})
    m = await db.mailbox(mid)
    assert m["status"] == "ok" and m["counts"] == {"needs_reply": 3} and m["sync"] == {"uid": 9}
    await db.log("connected", "Connected a@x.com.", mid, note=1)
    await db.log("info", "Worker started.")
    rows = await db.activity(mailbox_ids=[mid])
    assert [r["text"] for r in rows] == ["Worker started.", "Connected a@x.com."]  # newest first, global rows included
    assert (await db.activity(mailbox_ids=["other"]))[0]["text"] == "Worker started."
    await db.remove_mailbox(mid)
    assert await db.mailbox(mid) is None


# ── job queue ──

@pytest.mark.asyncio
async def test_jobs_retry_with_backoff_then_dead_letter(tmp_path):
    db = MailDB(tmp_path / "mail.db"); await db.init()
    jobs = Jobs(db)
    jid = await jobs.enqueue("check_mailbox", "m1", {"x": 1}, max_attempts=2)
    assert await jobs.enqueue("check_mailbox", "m1", dedupe=True) == jid     # deduped
    other = await jobs.enqueue("check_mailbox", "m2")
    assert await jobs.claim(exclude_mailboxes={"m1"}) is not None            # m1 busy: m2 claimed first
    job = await jobs.claim()
    assert job["id"] == jid and job["status"] == "running" and job["attempts"] == 1 and job["payload"] == {"x": 1}
    assert await jobs.claim() is None                                        # nothing else due
    assert await jobs.fail(jid, "boom") == "queued"                          # first failure: retry later
    row = await db.one("SELECT * FROM jobs WHERE id = ?", (jid,))
    assert row["status"] == "queued" and row["run_at"] > row["updated_at"] and row["last_error"] == "boom"
    await db.execute("UPDATE jobs SET run_at = '2000-01-01T00:00:00Z' WHERE id = ?", (jid,))
    job = await jobs.claim()
    assert job["id"] == jid and job["attempts"] == 2
    assert await jobs.fail(jid, "boom again") == "dead"                      # out of attempts
    counts = await jobs.counts()
    assert counts["dead"] == 1 and counts["running"] == 1
    assert await jobs.retry(jid) and (await db.one("SELECT attempts, status FROM jobs WHERE id = ?", (jid,)))["attempts"] == 0
    assert await jobs.discard(jid) and not await jobs.retry(other)           # running jobs can't be retried
    await jobs.complete(other, synced=5)
    assert (await jobs.list("done"))[0]["progress"] == {"synced": 5}
    # A job locked long ago by a dead worker is handed out again.
    stuck = await jobs.enqueue("check_mailbox", "m3")
    await db.execute("UPDATE jobs SET status = 'running', locked_at = '2000-01-01T00:00:00Z' WHERE id = ?", (stuck,))
    assert (await jobs.claim())["id"] == stuck


# ── worker loop ──

@pytest.mark.asyncio
async def test_worker_runs_jobs_concurrently_per_mailbox(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    db = MailDB(root / "data" / "mail.db"); await db.init()
    a = await db.add_mailbox(address="a@x.com", provider="imap", creds=crypto.seal_json("s", {"k": 1}))
    b = await db.add_mailbox(address="b@x.com", provider="imap", creds=crypto.seal_json("s", {"k": 1}))
    order = []

    class Fake:
        def __init__(self, mailbox, creds, env):
            self.mailbox = mailbox
        async def test(self):
            order.append(("start", self.mailbox["address"]))
            await asyncio.sleep(0.3 if self.mailbox["address"] == "a@x.com" else 0.05)
            order.append(("end", self.mailbox["address"]))
            if self.mailbox["address"] == "b@x.com":
                raise AuthError("token expired")
            return "fine"

    monkeypatch.setattr(service, "account_for", lambda m, c, e: Fake(m, c, e))
    worker = Worker(root, "s", {})
    jobs = worker.jobs
    await jobs.enqueue("check_mailbox", a); await jobs.enqueue("check_mailbox", b)
    await jobs.enqueue("bogus_kind", "", max_attempts=1)
    task = asyncio.create_task(worker.loop())
    for _ in range(60):
        await asyncio.sleep(0.1)
        if (await jobs.counts())["queued"] == 0 and (await jobs.counts())["running"] == 0:
            break
    worker.stop.set(); await task
    # b finished while a was still running: one slow mailbox did not block the other.
    assert order.index(("end", "b@x.com")) < order.index(("end", "a@x.com"))
    assert (await db.mailbox(a))["status"] == "ok" and (await db.mailbox(b))["status"] == "reconnect"
    assert "token expired" in (await db.mailbox(b))["status_detail"]
    texts = [r["text"] for r in await db.activity()]
    assert any("needs reconnecting" in t for t in texts) and any("background task" in t for t in texts)
    assert (await jobs.counts())["dead"] == 1
    assert (root / "data" / "mailworker.heartbeat").exists()


# ── service: connect, check, scope, migration ──

class _FakeAccount:
    outcome = "ok"
    def __init__(self, mailbox, creds, env):
        self.mailbox, self.creds = mailbox, creds
    async def test(self):
        if _FakeAccount.outcome == "auth":
            raise AuthError("bad password")
        if _FakeAccount.outcome == "down":
            raise TransientError("no route")
        return f"Signed in to {self.creds.get('smtp_host', 'gmail')}."


@pytest.mark.asyncio
async def test_connect_check_scope_and_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "account_for", lambda m, c, e: _FakeAccount(m, c, e))
    db = MailDB(tmp_path / "mail.db"); await db.init()
    smtp = {"smtp_username": "", "smtp_password": "pw", "smtp_host": "smtp.x.com", "smtp_port": "587",
            "imap_host": "imap.x.com", "imap_port": "993", "imap_username": "", "imap_password": ""}
    mid, note = await service.connect_imap(db, "sec", address="Me@X.com", smtp=smtp, env={})
    m = await db.mailbox(mid)
    assert m["address"] == "me@x.com" and m["mode"] == "limited" and m["status"] == "ok"
    assert service.open_creds("sec", m)["password"] == "pw" and "pw" not in m["creds"]
    assert service.masked_creds("sec", m)["password"] != "pw"
    assert service.ready_to_send(m) == (False, "Set a signature and the project link in this mailbox's settings before sending from it.")
    with pytest.raises(ValueError):
        await service.connect_imap(db, "sec", address="nope", smtp=smtp, env={})
    _FakeAccount.outcome = "auth"
    with pytest.raises(AuthError):
        await service.connect_imap(db, "sec", address="z@x.com", smtp=smtp, env={})
    assert await service.check_mailbox(db, "sec", m, {}) == "reconnect"
    _FakeAccount.outcome = "down"
    assert await service.check_mailbox(db, "sec", await db.mailbox(mid), {}) == "error"
    _FakeAccount.outcome = "ok"
    assert await service.check_mailbox(db, "sec", await db.mailbox(mid), {}) == "ok"
    assert [r["text"] for r in await db.activity()][0] == "me@x.com is connected again."
    gid, _ = await service.connect_gmail(db, "sec", address="g@gmail.com", refresh_token="rt", env={})
    assert (await db.mailbox(gid))["settings"]["color"] != m["settings"]["color"]
    await db.update_mailbox(gid, settings=MailboxSettings(signature_html="G", project_link="g.com").model_dump())
    assert service.ready_to_send(await db.mailbox(gid)) == (True, "")

    assert await service.scope(db) is None
    await service.set_scope(db, [mid, "ghost"])
    assert await service.scope(db) == [mid]
    assert [b["id"] for b in await service.scoped_mailboxes(db)] == [mid]
    await service.set_scope(db, None)
    assert len(await service.scoped_mailboxes(db)) == 2

    # Checks are scheduled only for mailboxes not checked recently.
    jobs = Jobs(db)
    await db.update_mailbox(gid, last_checked_at="2000-01-01T00:00:00Z")
    await service.schedule_checks(db, jobs)
    queued = [j for j in await jobs.list("queued")]
    assert [j["mailbox_id"] for j in queued] == [gid]


@pytest.mark.asyncio
async def test_migration_brings_the_old_mailbox_over_once(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    acc = Accounts(settings.accounts_db); acc.create_user(USER["email"], USER["password"])
    ws = Workspace(settings, acc.get_by_email(USER["email"])).ensure()
    ws.write_profile({**PROFILE, "signature_html": "Jane<br>Acme"})
    ws.save_secrets(smtp_host="smtp.x.com", smtp_port="587", smtp_username="jane@x.com", smtp_password="pw",
                    imap_host="", imap_port="", imap_username="", imap_password="", from_email="", reply_to="in@x.com")
    ws.set_mail_provider("smtp")
    db = ws.mail_db(); await db.init()
    mid = await service.migrate_from_workspace(db, "sec", ws)
    m = await db.mailbox(mid)
    assert m["address"] == "jane@x.com" and m["provider"] == "imap" and m["mode"] == "limited"
    assert m["settings"]["display_name"] == "Jane Doe" and m["settings"]["signature_html"] == "Jane<br>Acme"
    assert m["settings"]["reply_to"] == "in@x.com" and m["settings"]["project_link"] == ""
    assert service.open_creds("sec", m)["password"] == "pw"
    assert await service.migrate_from_workspace(db, "sec", ws) is None and len(await db.mailboxes()) == 1


def test_mailbox_settings_validation():
    s = MailboxSettings(project_link="example.com", excluded="Boss@X.com, competitor.com boss@x.com", daily_cap="7")
    assert s.project_link == "https://example.com" and s.excluded == ["boss@x.com", "competitor.com"] and s.daily_cap == 7
    with pytest.raises(ValueError):
        MailboxSettings(project_link="not a link")
    with pytest.raises(ValueError):
        MailboxSettings(hours_start="9am")
    with pytest.raises(ValueError):
        MailboxSettings(reply_to="nope")
    assert MailboxSettings(color="neon").color == "blue"


# ── the pages ──

@pytest.fixture
def mail_client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "account_for", lambda m, c, e: _FakeAccount(m, c, e))
    monkeypatch.setattr("bb.app.check_domain", lambda d: asyncio.sleep(0, result=None))
    settings = _settings(tmp_path, GMAIL_CLIENT_ID="", GMAIL_CLIENT_SECRET="")
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def test_mail_pages_connect_settings_scope_jobs_and_events(mail_client, monkeypatch):
    client = mail_client
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    started = []
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (started.append(1) or (True, "Started (pid 1).")))
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)

    page = client.get("/mail").text
    assert "Command center" in page and "No accounts in view" in page and "Worker stopped" in page
    page = client.get("/mail/accounts").text
    assert "Email and password" in page and "no Google OAuth client" in page.lower() or "Not switched on" in page

    _FakeAccount.outcome = "auth"
    r = client.post("/mail/accounts/imap", data={"email": "me@gmail.com", "password": "normal"})
    assert "Sign-in%20refused" in r.headers["location"] and "app%20password" in r.headers["location"]
    _FakeAccount.outcome = "ok"
    r = client.post("/mail/accounts/imap", data={"email": "me@gmail.com", "password": "app-pass"})
    assert r.status_code == 303 and "/mail/accounts/" in r.headers["location"] and started == [1]
    mid = r.headers["location"].split("/mail/accounts/")[1].split("?")[0]
    page = client.get(f"/mail/accounts/{mid}").text
    assert "Not ready to send" in page and "limited mode" in page and "app-pass" not in page

    # Settings: the project link is validated, and both it and the signature are required to send.
    r = client.post(f"/mail/accounts/{mid}", data={"display_name": "Jane", "project_link": "not a link", "signature_html": "x"})
    assert "web%20address" in r.headers["location"]
    r = client.post(f"/mail/accounts/{mid}", data={"display_name": "Jane", "project_link": "acme.test",
                                                  "signature_html": "Jane<br>Acme", "daily_cap": "40",
                                                  "excluded": "boss@acme.test", "color": "green", "auto_send": "on",
                                                  "hours_start": "09:00", "hours_end": "18:00"})
    assert "Settings%20saved." in r.headers["location"] and "Set%20" not in r.headers["location"]
    asyncio.run(ws.mail_db().init())
    m = asyncio.run(ws.mail_db().mailbox(mid))
    assert m["settings"]["project_link"] == "https://acme.test" and m["settings"]["auto_send"] is True
    assert service.ready_to_send(m) == (True, "")
    assert "ready" in client.get("/mail/accounts").text

    # Second account, scope selection persists, overview shows badges and the reconnect banner.
    client.post("/mail/accounts/imap", data={"email": "two@fastmail.com", "password": "pw2"})
    boxes = asyncio.run(ws.mail_db().mailboxes())
    other = [b for b in boxes if b["address"] == "two@fastmail.com"][0]
    r = client.post("/mail/scope", data={"mailbox": [mid], "back": "/mail"})
    assert r.headers["location"] == "/mail"
    page = client.get("/mail").text
    assert "me@gmail.com" in page and "two@fastmail.com" in page   # selector lists both
    assert page.count('<tr data-mailbox="') == 1                   # but only one is in view
    _FakeAccount.outcome = "auth"
    client.post(f"/mail/accounts/{other['id']}/check")
    client.post("/mail/scope", data={"all": "on"})
    page = client.get("/mail").text
    assert "two@fastmail.com needs reconnecting" in page and "bad password" in page
    _FakeAccount.outcome = "ok"

    # Jobs page: a dead job can be retried or discarded.
    db = ws.mail_db()
    jobs = Jobs(db)
    jid = asyncio.run(jobs.enqueue("check_mailbox", mid, max_attempts=1))
    asyncio.run(jobs.claim()); asyncio.run(jobs.fail(jid, "it broke"))
    page = client.get("/mail/jobs?status=dead").text
    assert "it broke" in page and "me@gmail.com" in page and "Retry" in page
    assert "Queued%20again" in client.post(f"/mail/jobs/{jid}/retry").headers["location"]
    assert "Discarded" in client.post(f"/mail/jobs/{jid}/discard").headers["location"]
    assert "cannot%20be%20changed" in client.post(f"/mail/jobs/{jid}/discard").headers["location"]

    # The event stream yields activity and state as server-sent events.
    with client.stream("GET", "/mail/events?after=0") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunks, body = 0, ""
        for chunk in resp.iter_text():
            body += chunk; chunks += 1
            if "event: state" in body:
                break
            if chunks > 50:
                break
    assert "event: activity" in body and "Connected me@gmail.com" in body
    state = json.loads(body.split("event: state\ndata: ", 1)[1].split("\n\n", 1)[0])
    assert {b["address"] for b in state["mailboxes"]} == {"me@gmail.com", "two@fastmail.com"}

    # Remove and disable.
    assert "Disabled." in client.post(f"/mail/accounts/{other['id']}/enable").headers["location"]
    assert "Removed" in client.post(f"/mail/accounts/{other['id']}/remove").headers["location"]
    assert client.get(f"/mail/accounts/{other['id']}").status_code == 404


def test_mail_worker_process_starts_and_heartbeats(tmp_path, monkeypatch):
    """The real worker process boots against a workspace and writes its heartbeat."""
    import subprocess, sys, os
    settings = _settings(tmp_path)
    acc = Accounts(settings.accounts_db); acc.create_user(USER["email"], USER["password"])
    ws = Workspace(settings, acc.get_by_email(USER["email"])).ensure()
    ok, detail = ws.start_mail_worker()
    assert ok, detail
    try:
        for _ in range(80):
            time.sleep(0.25)
            if (ws.data_dir / "mailworker.heartbeat").exists():
                break
        assert ws.mail_worker_alive()
        db = ws.mail_db()
        asyncio.run(db.init())
        assert any("Background worker started" in r["text"] for r in asyncio.run(db.activity()))
    finally:
        asyncio.run(ws.stop_mail_worker())
    assert ws.mail_worker_running() is None
