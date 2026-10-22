"""Phase 6: batch reply runs across accounts — review / auto / dry-run
modes, confidence routing, caps, exclusions, cancel, pacing, and the pages."""

import asyncio
import json

import pytest

import bb.mail.service as service
from bb.mail import crypto, runs
from bb.mail.db import MailDB
from bb.mail.ingest import Ingest
from bb.mail.models import MailboxSettings
from bb.mail.providers.base import RawMessage
from bb.mail.worker import Worker

from tests.test_mail_reply import FakeBrain, SentBox
from tests.test_mail_foundation import _FakeAccount, mail_client  # noqa: F401
from tests.test_bb_web import USER, _as_user, _accept_key  # noqa: F401
from bb.accounts import Accounts
from bb.workspaces import Workspace

A, B = "a@mine.example", "b@mine.example"


async def _seed(db, mid, n, who="ann", key="T"):
    box = await db.mailbox(mid)
    ing = Ingest(db, box)
    raws = [RawMessage(provider_id=f"{key}{i}", thread_key=f"{key}{i}", message_id=f"<{key}{i}@x>",
                       from_email=f"{who}{i}@x.example", from_name=f"{who.title()} {i}", subject=f"Question {i}",
                       body="Can we do Tuesday at ten?", sent_at=f"2026-01-0{i + 1}T10:00:00Z", to=[{"name": "", "email": box["address"]}])
            for i in range(n)]
    await ing.store_batch(raws)
    return [r["id"] for r in await db.query("SELECT id FROM threads WHERE mailbox_id = ? ORDER BY last_message_at", (mid,))]


GOOD = {"body": "Tuesday at ten works. I'll bring the drawings you asked about so we can walk the site together.",
        "confidence": "high", "confidence_reason": "clear ask"}
UNSURE = {"body": "Tuesday at ten should work. I'll check the budget figures you mentioned and come back to you on those.",
          "confidence": "low", "confidence_reason": "they asked about pricing"}


@pytest.mark.asyncio
async def test_run_across_accounts_routes_by_mode_and_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    SentBox.sent.clear()
    db = MailDB(tmp_path / "mail.db"); await db.init()
    ready = MailboxSettings(display_name="Jane", signature_html="Jane", project_link="https://acme.test").model_dump()
    ma = await db.add_mailbox(address=A, provider="imap", creds=crypto.seal_json("s", {}), status="ok", settings=ready)
    mb = await db.add_mailbox(address=B, provider="imap", creds=crypto.seal_json("s", {}), status="ok",
                              settings={**ready, "excluded": ["bob1@x.example"]})
    ta = await _seed(db, ma, 2, "ann", "A")
    tb = await _seed(db, mb, 3, "bob", "B")
    boxes = await db.mailboxes()
    cands = await runs.candidates(db, boxes)
    assert len(cands) == 5 and sum(1 for c in cands if c["excluded"]) == 1

    # Review mode: everything drafted, nothing sent.
    run = await runs.create_run(db, boxes, ta + tb, mode="review", instructions="offer a call", created_by="me")
    assert run["total"] == 5 and run["per_mailbox"] == {ma: 2, mb: 3}
    jobs = await db.query("SELECT * FROM jobs WHERE kind = 'reply_run' ORDER BY mailbox_id")
    assert {j["mailbox_id"] for j in jobs} == {ma, mb}
    brain = FakeBrain([GOOD] * 10)
    out = await runs.process_mailbox(db, "s", {}, brain, None, "", run["id"], ma)
    assert out["needs_review"] == 2 and SentBox.sent == []
    assert (await db.one("SELECT status FROM reply_runs WHERE id = ?", (run["id"],)))["status"] == "running"
    assert "offer a call" in brain.prompts[0][1]
    out = await runs.process_mailbox(db, "s", {}, brain, None, "", run["id"], mb)
    assert out["needs_review"] == 2 and out["skipped"] == 1
    r = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["id"],))
    assert r["status"] == "done" and r["done"] == 5 and json.loads(r["summary_json"])["needs_review"] == 4
    drafts = await db.query("SELECT * FROM drafts WHERE run_id = ?", (run["id"],))
    assert len(drafts) == 4 and all(d["status"] == "pending_review" for d in drafts)
    assert any("Reply run finished: 4 for review, 1 skipped." in a["text"] for a in await db.activity())
    skipped = await db.one("SELECT detail FROM run_items WHERE run_id = ? AND status = 'skipped'", (run["id"],))
    assert "excluded" in skipped["detail"]

    # Those drafts are not resent by a second run: the threads still need a
    # reply though, so a new auto run drafts them again and sends the confident ones.
    await db.execute("UPDATE drafts SET status = 'rejected' WHERE run_id = ?", (run["id"],))
    await db.update_mailbox(ma, settings={**ready, "daily_cap": 1})
    run2 = await runs.create_run(db, boxes, ta + tb, mode="auto", reply_to="team@mine.example")
    brain = FakeBrain([GOOD, GOOD, UNSURE, GOOD])
    out = await runs.process_mailbox(db, "s", {}, brain, None, "", run2["id"], ma)
    assert out["sent"] == 2 and out["skipped"] == 0          # a cap of 1, and both go: replies have no limit
    assert len(brain.prompts) == 2
    out = await runs.process_mailbox(db, "s", {}, brain, None, "", run2["id"], mb)
    assert out["sent"] == 1 and out["needs_review"] == 1 and out["skipped"] == 1
    assert len(SentBox.sent) == 3 and all(s["reply_to"] == "team@mine.example" for s in SentBox.sent)
    items = await db.query("SELECT * FROM run_items WHERE run_id = ? ORDER BY status", (run2["id"],))
    held = next(i for i in items if i["status"] == "needs_review")
    assert "confidence low" in held["detail"] and "pricing" in held["detail"]
    r = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run2["id"],))
    assert r["status"] == "done" and json.loads(r["summary_json"]) == {"queued": 0, "drafting": 0, "needs_review": 1, "sent": 3, "dry_run": 0, "skipped": 1, "failed": 0}
    sent_threads = [i["thread_id"] for i in items if i["status"] == "sent"]
    for t in sent_threads:
        assert (await db.one("SELECT state FROM threads WHERE id = ?", (t,)))["state"] == "awaiting_them"

    # Dry run: drafts everything that still needs a reply, sends nothing.
    # (One answered thread gets a new message, so it needs a reply again.)
    again = await db.one("SELECT * FROM threads WHERE id = ?", (sent_threads[0],))
    who = await db.one("SELECT from_email, message_id FROM messages WHERE thread_id = ? AND direction = 'inbound'", (again["id"],))
    await Ingest(db, await db.mailbox(again["mailbox_id"])).store_batch([RawMessage(
        provider_id="again1", thread_key="", message_id="<again1@x>", in_reply_to=who["message_id"], references=who["message_id"],
        from_email=who["from_email"], from_name="Again", subject="Re: " + again["subject"], body="And could we start at nine instead?",
        sent_at="2027-01-01T10:00:00Z", to=[{"name": "", "email": "x@mine.example"}])])
    SentBox.sent.clear()
    remaining = [t for t in ta + tb if t not in sent_threads] + [again["id"]]
    run3 = await runs.create_run(db, boxes, remaining, mode="dry_run")
    brain = FakeBrain([GOOD] * 5)
    for mid in (ma, mb):
        await runs.process_mailbox(db, "s", {}, brain, None, "", run3["id"], mid)
    r = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run3["id"],))
    # The thread whose reply still waits on Review is left out: drafting it
    # again would put a second reply in the tray.
    assert r["status"] == "done" and json.loads(r["summary_json"])["dry_run"] == 1 and SentBox.sent == []
    assert (await db.query("SELECT * FROM drafts WHERE run_id = ? AND status = 'dry_run'", (run3["id"],)))


@pytest.mark.asyncio
async def test_cancel_and_held_sends_and_worker_job(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    SentBox.sent.clear()
    root = tmp_path / "ws"
    db = MailDB(root / "data" / "mail.db"); await db.init()
    ma = await db.add_mailbox(address=A, provider="imap", creds=crypto.seal_json("s", {}), status="ok",
                              settings=MailboxSettings(signature_html="J", project_link="https://a.test", hours_start="09:00", hours_end="09:01").model_dump())
    ta = await _seed(db, ma, 3, "ann", "A")
    run = await runs.create_run(db, [await db.mailbox(ma)], ta, mode="auto")
    assert await runs.cancel_run(db, run["id"]) and not await runs.cancel_run(db, run["id"])
    assert (await db.one("SELECT COUNT(*) AS n FROM run_items WHERE run_id = ? AND status = 'skipped'", (run["id"],)))["n"] == 3
    # A cancelled run's job finds nothing queued and the run stays cancelled.
    brain = FakeBrain([GOOD] * 3)
    await runs.process_mailbox(db, "s", {}, brain, None, "", run["id"], ma)
    assert brain.prompts == [] and (await db.one("SELECT status FROM reply_runs WHERE id = ?", (run["id"],)))["status"] == "cancelled"

    # The send window refuses: drafts are held for review with the reason, through the real worker job.
    run2 = await runs.create_run(db, [await db.mailbox(ma)], ta, mode="auto")
    worker = Worker(root, "s", {})
    monkeypatch.setenv("BB_LLM_PROVIDER", "anthropic"); monkeypatch.setenv("BB_LLM_API_KEY", "k")
    monkeypatch.setattr("bb.mail.brain.MailBrain.complete", FakeBrain([GOOD] * 3).complete)
    job = await worker.jobs.claim()
    assert job["kind"] == "reply_run"
    out = await worker.handlers.run(job)
    assert out["needs_review"] == 3 and SentBox.sent == []
    held = await db.query("SELECT detail FROM run_items WHERE run_id = ?", (run2["id"],))
    assert all("send window" in h["detail"] for h in held)
    drafts = await db.query("SELECT status, error FROM drafts WHERE run_id = ?", (run2["id"],))
    assert all(d["status"] == "pending_review" and "send window" in d["error"] for d in drafts)
    await worker.jobs.complete(job["id"], **out)
    j = await db.one("SELECT progress_json FROM jobs WHERE id = ?", (job["id"],))
    assert json.loads(j["progress_json"])["done"] == 3


def test_pacing_respects_override_and_default_range(monkeypatch):
    monkeypatch.setenv("BB_MAIL_PACE_SECONDS", "0")
    assert runs.pace() == 0
    monkeypatch.delenv("BB_MAIL_PACE_SECONDS")
    assert 20 <= runs.pace() <= 60
    s = MailboxSettings(excluded="bob@x.example, .gov, corp.example")
    assert runs.is_excluded(s, "Bob@x.example") and runs.is_excluded(s, "x@tax.gov") and runs.is_excluded(s, "y@corp.example")
    assert not runs.is_excluded(s, "ann@x.example")


def test_run_pages_create_watch_and_cancel(mail_client, monkeypatch):
    client = mail_client
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: 1)   # no real worker process in this test
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "started"))
    client.post("/apikey", data={"api_key": "sk-ant-abcdefghijklmnop"})
    client.post("/mail/accounts/imap", data={"email": A, "password": "pw"})
    client.post("/mail/accounts/imap", data={"email": B, "password": "pw"})
    db = ws.mail_db()
    boxes = asyncio.run(db.mailboxes())
    ma, mb = boxes[0]["id"], boxes[1]["id"]
    ta = asyncio.run(_seed(db, ma, 2, "ann", "A"))
    tb = asyncio.run(_seed(db, mb, 1, "bob", "B"))

    page = client.get("/mail/runs").text
    assert "3 threads waiting for a reply" in page and page.count('name="thread"') == 3 and A in page and B in page
    assert "Draft and hold for review" in page and "Send the confident ones" in page and "Dry run" in page
    r = client.post("/mail/runs", data={"mode": "review"})
    assert "Tick%20at%20least%20one" in r.headers["location"]
    r = client.post("/mail/runs", data={"thread": ta + tb, "mode": "auto"})
    assert "None%20of%20the%20accounts%20can%20send" in r.headers["location"]   # no signature / link yet
    r = client.post("/mail/runs", data={"thread": ta + tb, "mode": "review", "instructions": "be brief", "reply_to": "not-an-email"})
    assert "Reply-To" in r.headers["location"]
    r = client.post("/mail/runs", data={"thread": ta + tb, "mode": "review", "instructions": "be brief"})
    assert r.headers["location"].startswith("/mail/runs/") and "Run%20started" in r.headers["location"]
    run_id = r.headers["location"].split("/mail/runs/")[1].split("?")[0]
    jobs = asyncio.run(db.query("SELECT mailbox_id FROM jobs WHERE kind = 'reply_run'"))
    assert {j["mailbox_id"] for j in jobs} == {ma, mb}

    page = client.get(f"/mail/runs/{run_id}").text
    assert "Replying 1 of 3" in page and "be brief" in page and "Cancel" in page and page.count("replies leave from this mailbox") == 2
    data = client.get(f"/mail/runs/{run_id}/items.json").json()
    assert data["total"] == 3 and data["done"] == 0 and len(data["items"]) == 3 and data["status"] == "queued"
    assert f'/mail/runs/{run_id}' in client.get("/mail").text and "Reply run in progress" in client.get("/mail").text
    assert "Reply runs" in client.get("/mail").text

    r = client.post(f"/mail/runs/{run_id}/cancel")
    assert "Cancelled" in r.headers["location"]
    data = client.get(f"/mail/runs/{run_id}/items.json").json()
    assert data["status"] == "cancelled" and data["summary"]["skipped"] == 3
    page = client.get("/mail/runs").text
    assert "Earlier runs" in page and "cancelled" in page
    assert client.get("/mail/runs/nope").status_code == 404
