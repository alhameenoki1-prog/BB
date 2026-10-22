"""The rest of the deferred work: a changed goal rewrites what has not gone
out, one run at a time per set of threads, the Instantly path closes a no
like the native one, and a failing segment template costs one model call,
not one per recipient."""

import asyncio
from datetime import datetime

import pytest

from bb.accounts import Accounts
from bb.mail import runs
from bb.mail.db import MailDB
from bb.workspaces import Workspace
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from tests.test_bb_web import PROFILE, USER, _as_user, client, settings  # noqa: F401
from tests.test_no_and_reconnect import ME, _handler, _seed_history
from tests.test_outbox_native import FakeProvider, make_sender, state  # noqa: F401


# ── a changed goal rewrites the emails that have not gone out ──

def test_a_changed_goal_rewrites_unsent_first_emails_and_keeps_threads_going(client):  # noqa: F811
    _as_user(client)
    form = {**PROFILE, "campaign_goal": "Get a reply.", "rewrite_scheduled_form": "1", "rewrite_scheduled": "1"}
    assert client.post("/profile", data=form).status_code == 303
    ws = Workspace(client.settings, Accounts(client.settings.accounts_db).get_by_email(USER["email"]))

    async def seed():
        sm = ws.state()
        await sm.init_db()
        for pid, status in (("q0", "queued"), ("q1", "queued"), ("d0", "queued"), ("c0", "contacted")):
            await sm.add_prospect(Prospect(id=pid, first_name="Ann", email=f"{pid}@x{pid}.example", email_status="provided",
                                           status=status))
        active = Campaign(id="", name="seg", sequence=[EmailStep(step=1, subject="s", body="b"), EmailStep(step=2, subject="f", body="f")],
                          prospect_ids=["q0", "q1", "c0"], status="active")
        active.id = await sm.add_campaign(active)
        draft = Campaign(id="", name="seg2", sequence=[EmailStep(step=1, subject="s", body="b")], prospect_ids=["d0"], status="draft")
        draft.id = await sm.add_campaign(draft)
        when = datetime(2026, 1, 1).isoformat()
        for pid in ("q0", "q1"):
            for step in (1, 2):
                await sm.add_outbox_item(prospect_id=pid, campaign_id=active.id, step=step, to_email=f"{pid}@x{pid}.example",
                                         subject="s", body="b", send_at=when, status="approved")
        await sm.add_outbox_item(prospect_id="c0", campaign_id=active.id, step=1, to_email="c0@xc0.example", subject="s", body="b",
                                 send_at=when, status="sent")
        follow = await sm.add_outbox_item(prospect_id="c0", campaign_id=active.id, step=2, to_email="c0@xc0.example",
                                          subject="f", body="f", send_at=when, status="approved")
        return draft.id, follow

    draft_id, follow = asyncio.run(seed())

    # Saving with nothing that shapes the words changed: nothing is touched.
    r = client.post("/profile", data={**form, "max_daily_sends": "30"})
    assert "written again" not in r.headers["location"]

    async def snapshot():
        sm = ws.state()
        return ({p: (await sm.get_prospect(p)).status for p in ("q0", "q1", "d0", "c0")},
                {i["id"]: i["status"] for i in await sm.get_outbox()},
                draft_id in {c.id for c in await sm.get_campaigns_by_status("failed")})

    statuses, outbox, retired = asyncio.run(snapshot())
    assert statuses["q0"] == "queued" and list(outbox.values()).count("approved") == 5 and not retired

    # The goal changes but the box is unticked: the queued emails stay.
    r = client.post("/profile", data={**PROFILE, "campaign_goal": "Ask for the brief.", "rewrite_scheduled_form": "1"})
    assert "written again" not in r.headers["location"]
    assert asyncio.run(snapshot())[0]["q0"] == "queued"

    # The goal changes with the box ticked: first emails not sent yet are written again.
    r = client.post("/profile", data={**form, "campaign_goal": "Get permission to send the project brief."})
    loc = r.headers["location"]
    assert "4%20unsent%20email" in loc and "3%20recipient" in loc and "keep%20the%20wording" in loc
    statuses, outbox, retired = asyncio.run(snapshot())
    assert statuses == {"q0": "new", "q1": "new", "d0": "new", "c0": "contacted"}
    assert outbox[follow] == "approved"                 # the follow-up of a started thread goes on
    assert sorted(outbox.values()).count("cancelled") == 4 and retired
    cancelled = [i for i in asyncio.run(ws.state().get_outbox(status="cancelled"))]
    assert all("new campaign goal" in i["error"] for i in cancelled)
    page = client.get("/profile").text
    assert 'name="rewrite_scheduled" value="1" checked' in page


# ── one run at a time over the same threads or contacts ──

@pytest.mark.asyncio
async def test_a_double_submit_does_not_start_two_runs(tmp_path):
    from bb.mail.models import MailboxSettings
    db = MailDB(tmp_path / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds="", status="ok",
                               settings=MailboxSettings(signature_html="J", project_link="https://p.example").model_dump())
    await _seed_history(db, mid)
    boxes = [await db.mailbox(mid)]
    a, b = await asyncio.gather(runs.create_reconnect_run(db, boxes, months=6), runs.create_reconnect_run(db, boxes, months=6))
    made = [r for r in (a, b) if r.get("id")]
    assert len(made) == 1 and (a.get("busy") or b.get("busy")) == made[0]["id"]
    assert len(await db.query("SELECT * FROM reply_runs WHERE kind = 'reconnect'")) == 1
    # Once it is finished another may start (and finds nobody left to follow up).
    await db.execute("UPDATE reply_runs SET status = 'done'")
    again = await runs.create_reconnect_run(db, boxes, months=6)
    assert not again.get("busy")

    # A reply run: the same threads twice at once go into one run only.
    t = await db.one("SELECT id FROM threads WHERE mailbox_id = ? ORDER BY last_message_at DESC LIMIT 1", (mid,))
    await db.execute("UPDATE threads SET state = 'needs_reply' WHERE id = ?", (t["id"],))
    one, two = await asyncio.gather(runs.create_run(db, boxes, [t["id"]]), runs.create_run(db, boxes, [t["id"]]))
    assert sorted(bool(r) for r in (one, two)) == [False, True]


# ── the Instantly (legacy) path closes a no like the native one ──

class _FakeInstantly:
    def __init__(self):
        self.sent = []

    async def send_reply(self, uuid, text):
        self.sent.append((uuid, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_instantly_sends_the_closing_note_and_closes_the_conversation(state):  # noqa: F811
    for pid, email in (("i1", "dana@build.example"), ("i2", "sam@build.example")):
        await state.add_prospect(Prospect(id=pid, first_name="Dana", email=email, email_status="provided",
                                          industry="construction", company="Build Co", status="contacted"))
    for pid, email, intent, text in (("i1", "dana@build.example", "not_interested", "Thanks, but we're not taking on new work this year."),
                                     ("i2", "sam@build.example", "objection", "We can't take this on, our books are full.")):
        handler = await _handler(state, intent)
        handler.provider = None                 # no native mailbox: the Instantly path
        handler.config.channels.email.provider = "instantly"
        handler.instantly = _FakeInstantly()
        await handler._process_reply(email, text, reply_uuid=f"u-{pid}")
        assert len(handler.instantly.sent) == 1, intent
        uuid, note = handler.instantly.sent[0]
        assert uuid == f"u-{pid}" and note.count("https://docs.example/towson") == 1 and "?" not in note
        assert (await state.get_prospect(pid)).status == "lost"
        assert await state.get_outbox() == []     # nothing went through a native outbox
    closed = await state.get_conversations_by_status("closed")
    assert len(closed) == 2 and all(c.stage == "closed_lost" for c in closed)


# ── a failing template costs one model call, not one per recipient ──

SPAM = ("FREE guarantee!!! Click here now, limited time offer, act now, winner, 100% risk free, "
        "best-in-class solutions for {{company}}, boost your revenue, unlock exclusive discount, don't miss out!!!")
CLEAN = "I'm looking for a supplier for about 40 tons of rebar a month from spring. Could {{company}} supply that volume?\n\nJane"


class _CountingBrain:
    def __init__(self, template_reply, copy_reply=None):
        self.calls = []
        self.template_reply, self.copy_reply = template_reply, copy_reply

    async def think_json(self, prompt, **kw):
        self.calls.append(kw.get("task"))
        return self.template_reply if kw.get("task") == "fit_template" else self.copy_reply


async def _segment(state, n, body=SPAM, personal=()):
    ids = []
    for i in range(n):
        pid = f"s{i}"
        await state.add_prospect(Prospect(id=pid, first_name=f"Ann{i}", email=f"ann{i}@co{i}.example", company=f"Co{i}",
                                          email_status="provided", industry="manufacturing"))
        ids.append(pid)
    c = Campaign(id="", name="manufacturing · executive", channel="email", prospect_ids=ids, status="draft",
                 sequence=[EmailStep(step=1, subject="Exciting opportunity", body=body)])
    c.id = await state.add_campaign(c)
    for pid in personal:
        await state.add_outbox_item(prospect_id=pid, campaign_id=c.id, step=1, to_email=f"ann{pid[1:]}@co{pid[1:]}.example",
                                    subject="Rebar for Co", body="Hi Ann,\n\nA personal note.\n\nJane",
                                    send_at=datetime(2026, 1, 1).isoformat(), status="approved")
    return c


@pytest.mark.asyncio
async def test_one_rewrite_serves_every_copy_of_a_template(state):  # noqa: F811
    sender = make_sender(state, FakeProvider(), require_approval=False)
    sender.brain = _CountingBrain({"subject": "Rebar, about 40 tons a month", "body": CLEAN})
    c = await _segment(state, 6, personal=("s5",))
    await sender._stage_campaign_native(c)
    assert sender.brain.calls == ["fit_template"]          # one call for six people
    items = {i["prospect_id"]: i for i in await state.get_outbox()}
    assert len(items) == 6
    for i in range(5):
        it = items[f"s{i}"]
        assert it["status"] == "approved" and it["subject"] == "Rebar, about 40 tons a month"
        assert it["body"].startswith(f"Hi Ann{i},") and f"Could Co{i} supply" in it["body"] and "{{" not in it["body"]
    assert items["s5"]["body"] == "Hi Ann,\n\nA personal note.\n\nJane"     # the personal draft kept its slot


@pytest.mark.asyncio
async def test_a_template_rewrite_that_drops_merge_fields_is_not_used_and_copies_are_capped(state):  # noqa: F811
    from harvey.agents.sender import PER_COPY_REWRITES
    sender = make_sender(state, FakeProvider(), require_approval=False)
    # The template fix loses {{company}}, and the per-copy fixes fail too.
    sender.brain = _CountingBrain({"subject": "Rebar", "body": "No merge fields here at all, which is wrong."}, copy_reply=None)
    c = await _segment(state, 6)
    await sender._stage_campaign_native(c)
    assert sender.brain.calls.count("fit_template") == 1
    assert sender.brain.calls.count("fit_for_sending") == PER_COPY_REWRITES
    items = await state.get_outbox()
    assert len(items) == 6 and all(i["status"] == "pending_review" and i["error"].startswith("Needs review") for i in items)
