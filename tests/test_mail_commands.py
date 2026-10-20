"""Phase 7: the command bar — tools on Anthropic, the JSON fallback on
other providers, confirmation before anything is sent, scope and
inline account override, and the page."""

import asyncio
import json

import pytest

from bb.mail import commands, crypto
from bb.mail.brain import MailBrain
from bb.mail.commands import CommandCenter
from bb.mail.db import MailDB
from bb.mail.models import MailboxSettings
import bb.mail.service as service

from tests.test_mail_runs import _seed, GOOD, A, B
from tests.test_mail_reply import FakeBrain
from tests.test_mail_foundation import _FakeAccount, mail_client  # noqa: F401
from tests.test_bb_web import USER, _as_user, _accept_key  # noqa: F401
from bb.accounts import Accounts
from bb.workspaces import Workspace


class ToolBrain(MailBrain):
    """Scripted tool turns: each entry is either a list of tool calls or a final text."""
    def __init__(self, turns, answers=()):
        super().__init__({"BB_LLM_PROVIDER": "anthropic", "BB_LLM_API_KEY": "k"})
        self.turns = list(turns); self.seen = []; self.answers = list(answers)
    async def tool_turn(self, system, messages, tools, max_tokens=1500):
        self.seen.append(messages[-1])
        t = self.turns.pop(0)
        if isinstance(t, str):
            return {"text": t, "tool_calls": [], "content": [{"type": "text", "text": t}], "stop_reason": "end_turn"}
        calls = [{"id": f"c{i}", "name": n, "input": a} for i, (n, a) in enumerate(t)]
        return {"text": "", "tool_calls": calls, "content": [{"type": "tool_use", **c} for c in calls], "stop_reason": "tool_use"}
    async def complete(self, system, user, max_tokens=1200):
        return json.dumps(self.answers.pop(0))


async def _setup(tmp_path):
    db = MailDB(tmp_path / "mail.db"); await db.init()
    ready = MailboxSettings(display_name="Jane", signature_html="Jane", project_link="https://acme.test").model_dump()
    ma = await db.add_mailbox(address=A, provider="imap", creds=crypto.seal_json("s", {}), status="ok", settings=ready)
    mb = await db.add_mailbox(address=B, provider="imap", creds=crypto.seal_json("s", {}), status="ok")
    ta = await _seed(db, ma, 2, "ann", "A")
    tb = await _seed(db, mb, 1, "bob", "B")
    boxes = await db.mailboxes()
    for m in boxes:
        m["ready"], m["ready_why"] = service.ready_to_send(m)
    return db, boxes, ma, mb, ta, tb


@pytest.mark.asyncio
async def test_tools_answer_questions_and_run_reviews_without_confirmation(tmp_path):
    db, boxes, ma, mb, ta, tb = await _setup(tmp_path)
    brain = ToolBrain([[("get_counts", {})], "3 threads need a reply: 2 in a@mine.example and 1 in b@mine.example."])
    center = CommandCenter(db, brain, boxes, boxes, user_email="me")
    out = await center.run("how many need a reply?")
    assert out["reply"].startswith("3 threads") and out["pending"] is None and out["steps"][0]["tool"] == "get_counts"
    assert out["steps"][0]["result"]["total"]["needs_reply"] == 3

    # Scope: only b@ selected; an inline override names a@.
    started = []
    brain = ToolBrain([[("list_threads", {"state": "needs_reply"})], [("start_reply_run", {"mode": "review", "accounts": ["a@mine.example"], "instructions": "be warm"})],
                       "Started a review run for the 2 threads in a@mine.example."])
    center = CommandCenter(db, brain, boxes, [boxes[1]], user_email="me", on_worker_needed=lambda: started.append(1))
    out = await center.run("reply to everything in a@ for review, be warm")
    assert out["pending"] is None and started == [1]
    listed = out["steps"][0]["result"]
    assert [t["account"] for t in listed] == [B]                       # scope = b@ only
    run = out["steps"][1]["result"]
    assert run["total"] == 2 and run["accounts"] == [{"account": A, "threads": 2, "can_send": True}] and run["mode"] == "review"
    r = await db.one("SELECT * FROM reply_runs WHERE id = ?", (run["run_id"],))
    assert r["instructions"] == "be warm" and r["created_by"] == "me"
    assert "Started a review run" in out["reply"]

    # A thread summary and a draft through the bar.
    brain = ToolBrain([[("summarize_thread", {"thread_id": tb[0]})], [("generate_draft", {"thread_id": tb[0], "instructions": "say yes"})], "Drafted; it waits on Review."])
    monkey = FakeBrain([GOOD])
    brain.complete = monkey.complete   # drafting goes through complete()
    center = CommandCenter(db, brain, boxes, boxes, user_email="me")
    out = await center.run("summarise the thread from Bob and draft a yes")
    summ = out["steps"][0]["result"]
    assert summ["subject"] == "Question 0" and summ["messages"][0]["who"] == "Bob 0"
    d = out["steps"][1]["result"]
    assert d["confidence"] == "high" and d["writing_check"] == "passed" and d["note"].startswith("waiting")
    assert (await db.one("SELECT status FROM drafts WHERE id = ?", (d["draft_id"],)))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_sends_wait_for_confirmation_and_confirm_executes(tmp_path):
    db, boxes, ma, mb, ta, tb = await _setup(tmp_path)
    # Auto run on b@ (cannot send) is refused with the reason; on a@ it becomes pending.
    brain = ToolBrain([[("start_reply_run", {"mode": "auto", "accounts": ["b@"]})], [("start_reply_run", {"mode": "auto", "accounts": ["a@mine.example"]})],
                       "Ready to send 2 replies from a@mine.example — waiting for your confirmation."])
    center = CommandCenter(db, brain, boxes, boxes, user_email="me")
    out = await center.run("reply to everything and send")
    assert "cannot send yet" in out["steps"][0]["result"]["error"]
    pend = out["pending"]
    assert pend and pend["kind"] == "start_reply_run" and pend["accounts"] == [{"account": A, "threads": 2, "can_send": True}]
    assert "send the confident ones" in pend["description"] and "waiting for your confirmation" in out["reply"]
    assert "pending the person's confirmation" in brain.seen[-1]["content"]
    assert not await db.query("SELECT id FROM reply_runs")                 # nothing started yet
    line = await commands.confirm(CommandCenter(db, brain, boxes, boxes, user_email="me"), pend)
    run = await db.one("SELECT * FROM reply_runs")
    assert run["mode"] == "auto" and run["total"] == 2 and line.startswith("Run started: 2 threads")

    # send_reply on a draft: pending, then confirm queues the send job.
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('d1', ?, ?, 'reply', ?, 'Re: Question 0', 'Tuesday works.', 'pending_review', ?, ?)",
                     (ma, ta[0], json.dumps([{"name": "Ann 0", "email": "ann0@x.example"}]), now, now))
    brain = ToolBrain([[("send_reply", {"draft_id": "d1"})], "Waiting for you to confirm."])
    center = CommandCenter(db, brain, boxes, boxes, user_email="me")
    out = await center.run("send the draft to Ann")
    assert out["pending"]["kind"] == "send_reply" and "Ann 0" in out["pending"]["description"]
    await commands.confirm(CommandCenter(db, brain, boxes, boxes, user_email="me"), out["pending"])
    assert (await db.one("SELECT status FROM drafts WHERE id = 'd1'"))["status"] == "approved"
    assert await db.one("SELECT id FROM jobs WHERE kind = 'send_draft'")
    # history + pending persistence
    await commands.remember(db, "send the draft to Ann", out["reply"], out["steps"])
    await commands.set_pending(db, out["pending"])
    assert (await commands.pending(db))["kind"] == "send_reply" and (await commands.history(db))[-1]["tools"] == ["send_reply"]
    await commands.set_pending(db, None)
    assert await commands.pending(db) is None


@pytest.mark.asyncio
async def test_fallback_protocol_on_other_providers_and_plain_refusal(tmp_path):
    db, boxes, ma, mb, ta, tb = await _setup(tmp_path)
    env = {"BB_LLM_PROVIDER": "groq", "BB_LLM_API_KEY": "g", "BB_LLM_BASE_URL": "https://groq.test/v1", "BB_LLM_MODEL": "llama"}
    brain = FakeBrain([{"action": "get_counts", "args": {}}, {"answer": "3 need a reply."}])
    brain.provider, brain.base_url, brain.model = "groq", env["BB_LLM_BASE_URL"], "llama"
    assert not brain.supports_tools and brain.configured
    center = CommandCenter(db, brain, boxes, boxes)
    out = await center.run("how many need a reply?")
    assert out["reply"] == "3 need a reply." and out["steps"][0]["tool"] == "get_counts"
    assert "ACTIONS:" in brain.prompts[0][0] and "ACTION get_counts ->" in brain.prompts[1][1]
    # The model rambles: the bar says it cannot, rather than guessing.
    brain.answers = ["Sure, I'd love to help with your inbox!"]
    out = await center.run("do the thing")
    assert out["reply"].startswith("I could not turn that into an action") and "Anthropic" in out["reply"]
    brain.answers = [{"action": "delete_everything", "args": {}}]
    assert (await center.run("wipe it"))["reply"].startswith("I could not")
    # An auto run through the fallback still ends in a pending action.
    brain.answers = [{"action": "start_reply_run", "args": {"mode": "auto", "accounts": ["a@mine.example"]}}]
    out = await center.run("send replies to everything in a@")
    assert out["pending"]["kind"] == "start_reply_run" and out["reply"].startswith("Waiting for your confirmation")
    # No provider at all.
    out = await CommandCenter(db, MailBrain({}), boxes, boxes).run("hi")
    assert "No AI provider" in out["reply"]


def test_command_bar_page_flow(mail_client, monkeypatch):
    client = mail_client
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: 1)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "started"))
    page = client.get("/mail").text
    assert "Set up an AI provider to use the command bar" in page
    client.post("/apikey", data={"api_key": "sk-ant-abcdefghijklmnop"})
    client.post("/mail/accounts/imap", data={"email": A, "password": "pw"})
    db = ws.mail_db()
    ma = asyncio.run(db.mailboxes())[0]["id"]
    asyncio.run(db.update_mailbox(ma, settings=MailboxSettings(signature_html="J", project_link="https://a.test").model_dump()))
    ta = asyncio.run(_seed(db, ma, 2, "ann", "A"))
    turns = [[("start_reply_run", {"mode": "auto"})], "Two replies from a@mine.example are ready to send once you confirm."]
    monkeypatch.setattr("bb.mail.brain.MailBrain.tool_turn", ToolBrain(turns).tool_turn)
    r = client.post("/mail/command", data={"q": "reply to everything and send the good ones"})
    assert r.headers["location"] == "/mail#command"
    page = client.get("/mail").text
    assert "Confirm before anything is sent." in page and f"{A}: 2 threads" in page and "ready to send once you confirm" in page
    assert "reply to everything and send the good ones" in page and "start reply run" in page
    r = client.post("/mail/command", data={"q": "and now?"})
    assert "pending%20action%20first" in r.headers["location"]
    r = client.post("/mail/command/confirm")
    assert "Confirmed" in r.headers["location"]
    run = asyncio.run(db.one("SELECT * FROM reply_runs"))
    assert run["mode"] == "auto" and run["total"] == 2
    page = client.get("/mail").text
    assert "Done: Reply to 2 threads" in page and "Confirm before anything is sent." not in page
    assert client.post("/mail/command/cancel").headers["location"].endswith("Nothing%20is%20waiting%20for%20confirmation.")
    client.post("/mail/command/clear")
    assert "Done: Reply to 2 threads" not in client.get("/mail").text
