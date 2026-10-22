"""The profile collects the need per kind of business with its full-scope
link and the line about the sender; the config carries them, the Inbox
page lists them, and a mailbox change does not lose them."""
import yaml

from bb.accounts import Accounts
from bb.workspaces import Workspace
from harvey.config import HarveyConfig

from tests.test_bb_web import PROFILE, USER, _as_user, client, settings  # noqa: F401

NEEDS_FORM = {
    **PROFILE,
    "intent": "Services and materials for a 24-unit residential block in Towson, Maryland.",
    "sender_line": "I run Greystone Development, a residential developer in Baltimore.",
    "details_url": "docs.example/general-brief", "details_label": "the project brief",
    "sn_sectors_0": ["construction", "architecture_engineering"],
    "sn_need_0": "A builder for a 24-unit residential block in Towson, drawings done, start on site in April.",
    "sn_question_0": "Could you take on a job like this, and can I send over the drawings?",
    "sn_url_0": "https://docs.example/towson", "sn_label_0": "the Towson brief",
    "sn_sectors_1": "manufacturing", "sn_need_1": "About 40 tons of rebar a month for a year, delivered to a Baltimore site.",
    "sn_sectors_2": "legal", "sn_need_2": "",     # an empty row is dropped
}


def test_profile_round_trips_needs_and_the_scope_links(client):  # noqa: F811
    _as_user(client)
    r = client.post("/profile", data=NEEDS_FORM)
    assert r.status_code == 303, r.text
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    cfg = HarveyConfig(**yaml.safe_load(ws.config_file.read_text()))
    o = cfg.outreach
    assert o.sender_line.startswith("I run Greystone") and o.details_url == "https://docs.example/general-brief"
    assert o.details_label == "the project brief"
    assert [n.sectors for n in o.sector_needs] == [["construction", "architecture_engineering"], ["manufacturing"]]
    assert o.sector_needs[0].details_url == "https://docs.example/towson" and o.sector_needs[0].question.startswith("Could you")
    assert o.sector_needs[1].details_url == ""
    knowledge = (ws.skills_dir / "product_knowledge.md").read_text()
    assert "I run Greystone Development" in knowledge and "40 tons of rebar" in knowledge
    assert "https://docs.example/towson" in knowledge and "nothing is being sold" in knowledge

    # The form shows what was saved; the Inbox page lists the documents.
    page = client.get("/profile").text
    assert "the Towson brief" in page and "About 40 tons of rebar" in page and "I run Greystone Development" in page
    inbox = client.get("/inbox").text
    assert "Documents it already knows" in inbox and "https://docs.example/towson" in inbox and "Construction" in inbox
    assert [l["url"] for l in ws.scope_links()] == ["https://docs.example/towson", "https://docs.example/general-brief"]

    # A mailbox change rewrites harvey.yaml from the stored profile without losing the rows.
    assert ws.rewrite_config() is not None
    again = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach
    assert [n.need for n in again.sector_needs] == [n.need for n in o.sector_needs]
    assert again.sector_needs[0].details_url == "https://docs.example/towson" and again.sender_line == o.sender_line

    # Saving the Inbox page rewrites harvey.yaml too, and keeps the rows and links.
    r = client.post("/inbox", data={"inbox_form": "1", "inbox_autoreply": "on",
                                    "inbox_instructions": "Send the brief when they ask what we need."})
    assert r.status_code == 303, r.text
    after_inbox = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach
    assert after_inbox.inbox_autoreply and [n.need for n in after_inbox.sector_needs] == [n.need for n in o.sector_needs]
    assert after_inbox.sector_needs[0].details_url == "https://docs.example/towson" and after_inbox.details_url == o.details_url

    # A need nobody could repeat back is refused with the reason, and nothing is written.
    r = client.post("/profile", data={**NEEDS_FORM, "intent": "I would like to start working with your team"})
    assert r.status_code == 200 and "says nothing a builder, adviser or supplier can answer" in r.text
    # The page shows the rows as typed, not the stored ones, so nothing is lost on the retry.
    r = client.post("/profile", data={**NEEDS_FORM, "sn_need_1": "working together on steel",
                                      "sn_need_0": "A builder for a 36-unit block in Dundalk, start in June."})
    assert r.status_code == 200 and "Manufacturing: that says nothing" in r.text
    assert "36-unit block in Dundalk" in r.text and "working together on steel" in r.text
    assert "block in Towson, drawings done" not in r.text      # the stored row, not shown over the typed one
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.intent.startswith("Services and")
    r = client.post("/profile", data={**NEEDS_FORM, "sn_need_1": "working together on steel"})
    assert r.status_code == 200 and "Manufacturing: that says nothing" in r.text
    r = client.post("/profile", data={**NEEDS_FORM, "sender_line": "See https://greystone.example"})
    assert r.status_code == 200 and "Leave web addresses out" in r.text
    # The template mode keeps its own sector versions and ignores the needs' strictness.
    r = client.post("/profile", data={**NEEDS_FORM, "writing": "template", "template_subject": "Hello",
                                      "template_body": "Hi {{first_name}},\n\nMy own words here, long enough to pass."})
    assert r.status_code == 303, r.text


def test_dashboard_explains_why_emails_bounce(client):  # noqa: F811
    import asyncio
    from datetime import datetime

    from harvey.models.prospect import Prospect

    _as_user(client)
    assert client.post("/profile", data=NEEDS_FORM).status_code == 303
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)

    async def seed():
        sm = ws.state()
        await sm.init_db()
        pid = await sm.add_prospect(Prospect(first_name="Ann", email="ann@gone.example", email_status="provided"))
        for n, error in enumerate([
            "Bounced: 550 5.1.1 The email account that you tried to reach does not exist. The address does not exist. "
            "Nothing to fix on your side; it has been marked invalid.",
            "Bounced: 550 5.1.1 User unknown. The address does not exist. Nothing to fix on your side; it has been marked invalid.",
            "Bounced: 421-4.7.0 Our system has detected an unusual rate of unsolicited mail. Your mailbox provider throttled you. "
            "Switch the profile to the Gentle speed, keep the warm-up ramp on, and lower the daily maximum (30 to 50 a day "
            "for a mailbox under a month old).",
        ]):
            item = await sm.add_outbox_item(prospect_id=pid, conversation_id="", kind="sequence", to_email=f"a{n}@gone.example",
                                            subject="s", body="b", send_at=datetime(2026, 1, 1).isoformat(),
                                            status="sent", provider="gmail")
            await sm.update_outbox_item(item, error=error)
        return await ws.bounce_summary()

    groups = asyncio.run(seed())
    assert [(g["what"], g["count"]) for g in groups] == [("no such address", 2), ("sending too fast or too much", 1)]
    assert groups[0]["example"].startswith("550 5.1.1") and "Gentle speed" in groups[1]["hint"]
    page = client.get("/").text
    assert "Why emails bounce" in page and "no such address" in page and "sending too fast or too much" in page


def test_queued_emails_written_with_the_old_brief_can_be_rewritten(client):  # noqa: F811
    import asyncio
    from datetime import datetime

    from harvey.models.campaign import Campaign, EmailStep
    from harvey.models.prospect import Prospect

    _as_user(client)
    assert client.post("/profile", data=NEEDS_FORM).status_code == 303
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    OLD = ("Manufacturing supply for our current needs",
           "Good day there,\n\nFinding a manufacturer for our current needs is the next step for us, and we would like to "
           "start the conversation with your team. Before going into detail, it would help to understand how you usually "
           "take on new clients.\n\nThanks,\nThomas")
    NEW = ("Rebar, around 40 tons a month from spring",
           "Hi Ann,\n\nI run Greystone Development in Baltimore and we have about a year of work starting in spring that "
           "will need roughly 40 tons of rebar a month.\n\nCan you supply that volume month to month?\n\nThomas")

    async def seed():
        sm = ws.state()
        await sm.init_db()
        for i in range(3):
            await sm.add_prospect(Prospect(id=f"q{i}", first_name="Ann", email=f"ann{i}@steel{i}.example", email_status="provided", status="queued"))
        await sm.add_prospect(Prospect(id="s0", first_name="Sam", email="sam@sent.example", email_status="provided", status="contacted"))
        c = Campaign(id="", name="manufacturing · executive", sequence=[EmailStep(step=1, subject=OLD[0], body=OLD[1])],
                     prospect_ids=["q0", "q1", "q2"], status="active")
        c.id = await sm.add_campaign(c)
        for i, (subject, body) in enumerate([OLD, OLD, NEW]):
            await sm.add_outbox_item(prospect_id=f"q{i}", campaign_id=c.id, step=1, kind="sequence", to_email=f"ann{i}@steel{i}.example",
                                     subject=subject, body=body, send_at=datetime(2026, 1, 1).isoformat(),
                                     status="approved" if i else "pending_review", provider="gmail")
        item = await sm.add_outbox_item(prospect_id="s0", campaign_id=c.id, step=1, kind="sequence", to_email="sam@sent.example",
                                        subject=OLD[0], body=OLD[1], send_at=datetime(2026, 1, 1).isoformat(), status="sent", provider="gmail")
        return c.id, item

    campaign_id, sent_item = asyncio.run(seed())
    assert asyncio.run(ws.stale_queued()) == 2
    page = client.get("/").text
    assert "2 queued first emails still read like a campaign" in page and "Rewrite the unsent emails" in page

    r = client.post("/outbox/rewrite-unsent")
    assert r.status_code == 303 and "Cancelled%203%20unsent" in r.headers["location"]

    async def after():
        sm = ws.state()
        return ({p.id: p.status for p in await sm.get_prospects_by_status("new")},
                [i["status"] for i in await sm.get_outbox(status="approved")] + [i["status"] for i in await sm.get_outbox(status="pending_review")],
                (await sm.get_outbox_item(sent_item))["status"],
                campaign_id in {c.id for c in await sm.get_campaigns_by_status("failed")},
                (await sm.get_prospect("s0")).status)
    new, unsent, sent_status, retired, sam = asyncio.run(after())
    assert set(new) == {"q0", "q1", "q2"} and unsent == [] and sent_status == "sent" and retired and sam == "contacted"
    assert asyncio.run(ws.stale_queued()) == 0 and "still read like a campaign" not in client.get("/").text


def test_the_campaign_goal_is_saved_and_every_queued_email_can_be_rewritten(client, monkeypatch):  # noqa: F811
    import asyncio

    _as_user(client)
    goal = ("The goal of the first email is to get permission to send the project brief. Every first email ends with "
            "an open question offering it.")
    r = client.post("/profile", data={**NEEDS_FORM, "campaign_goal": goal})
    assert r.status_code == 303, r.text
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.goal == goal
    assert goal in client.get("/profile").text
    assert ws.rewrite_config() is not None and yaml.safe_load(ws.config_file.read_text())["outreach"]["goal"] == goal

    async def seed():
        state = ws.state()
        await state.init_db()
        from harvey.models.prospect import Prospect
        await state.add_prospect(Prospect(id="q1", email="dana@build.example", first_name="Dana", company="Build Co",
                                          email_status="provided", industry="construction"))
        a = await state.add_outbox_item(prospect_id="q1", to_email="dana@build.example", subject="Quick question",
                                        body="Hi Dana,\n\nI need a builder for a 24-unit block in Towson, drawings done, start in April. "
                                             "Can you take it on?\n\nJane", send_at="2030-01-01T00:00:00", status="approved")
        h = await state.add_outbox_item(prospect_id="q1", to_email="dana@build.example", subject="Towson block, 24 units",
                                        body="Hi Dana,\n\nFREE!!! Act now.\n\nJane", send_at="2030-01-01T00:00:00",
                                        status="pending_review")
        return a, h
    approved_id, held_id = asyncio.run(seed())

    page = client.get("/outbox?status=approved").text
    assert ">Rewrite<" in page and f'id="rw-{approved_id}"' in page and "Rewrite with AI" in page and "Or edit it yourself" in page
    assert "Needs review" in page and "Awaiting approval" not in page
    # Edit by hand: saved, still scheduled.
    r = client.post(f"/outbox/{approved_id}/edit", data={"subject": "Builder for a 24-unit block in Towson", "body": "Hi Dana,\n\nNew words.\n\nJane"})
    assert r.status_code == 303 and "Saved" in r.headers["location"]

    async def read(item_id):
        state = ws.state()
        await state.init_db()
        return await state.get_outbox_item(item_id)
    item = asyncio.run(read(approved_id))
    assert item["subject"] == "Builder for a 24-unit block in Towson" and item["body"].startswith("Hi Dana,\n\nNew words.") and item["status"] == "approved"
    # Save and approve a held one.
    r = client.post(f"/outbox/{held_id}/edit", data={"subject": "Towson block, 24 units", "body": "Hi Dana,\n\nPlain words about the block.\n\nJane", "approve": "1"})
    assert r.status_code == 303 and asyncio.run(read(held_id))["status"] == "approved"

    # Rewrite with the user's own AI: the goal is in front of the model, the greeting line is kept, the checks run.
    seen = {}

    class _FakeBrain:
        configured = True

        def __init__(self, env):
            seen["env"] = env

        async def complete_json(self, system, user, **_kw):
            seen["system"], seen["user"] = system, user
            return {"subject": "Quick question", "body": "Hi Dana,\n\nShorter: a 24-unit block in Towson, drawings done, start in April. "
                                                        "Want me to send over the project brief so you have the full scope?\n\nJane"}

    monkeypatch.setattr("bb.mail.brain.MailBrain", _FakeBrain)
    r = client.post(f"/outbox/{approved_id}/rewrite", data={"instruction": "shorter, end by offering the brief"})
    assert r.status_code == 303 and "Rewritten" in r.headers["location"], r.headers.get("location")
    assert "CAMPAIGN GOAL" in seen["system"] and goal in seen["system"] and "INSTRUCTION FROM Jane Doe: shorter" in seen["user"]
    item = asyncio.run(read(approved_id))
    assert "Want me to send over the project brief" in item["body"] and item["body"].startswith("Hi Dana,") and item["status"] == "approved"
    assert item["subject"] != "Quick question" and "24-unit" in item["subject"]      # boilerplate swapped for the need

    class _NoBrain(_FakeBrain):
        configured = False
    monkeypatch.setattr("bb.mail.brain.MailBrain", _NoBrain)
    r = client.post(f"/outbox/{approved_id}/rewrite", data={"instruction": "x"})
    assert "no%20AI%20provider" in r.headers["location"] or "no AI provider" in r.headers["location"]


def test_the_dashboard_and_mailbox_page_show_the_health_alert(client):  # noqa: F811
    import asyncio

    _as_user(client)
    client.post("/profile", data=NEEDS_FORM)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)

    async def seed():
        state = ws.state()
        await state.init_db()
        await state.set_setting("mailbox_check_at", "2026-10-22T06:00:00")
        await state.set_setting("mailbox_check_ok", "0")
        await state.set_setting("mailbox_check_error", "SMTP login failed: 535 authentication failed")
        await state.set_setting("mailbox_alert", "The mailbox check at 06:00 UTC on 22 Oct failed: SMTP login failed: 535 authentication failed. Nothing is sent until it works again.")
    asyncio.run(seed())
    assert "The mailbox check at 06:00 UTC on 22 Oct failed" in client.get("/").text
    page = client.get("/mailbox").text
    assert "Is it still sending?" in page and "FAILED: SMTP login failed: 535" in page and "2026-10-22 06:00 UTC" in page

