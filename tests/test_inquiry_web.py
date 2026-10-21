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

    # A need nobody could repeat back is refused with the reason, and nothing is written.
    r = client.post("/profile", data={**NEEDS_FORM, "intent": "I would like to start working with your team"})
    assert r.status_code == 200 and "says nothing a builder, adviser or supplier can answer" in r.text
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
