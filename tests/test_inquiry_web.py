"""The profile collects the need per kind of business with its full-scope
link and the line about the sender; the config carries them, the Inbox
page lists them, and a mailbox change does not lose them."""
import yaml

from bb.accounts import Accounts
from bb.mail.brain import MailBrain as _RealMailBrain  # the class itself, before a test swaps it
from bb.workspaces import AUTO_SIG_MARKER, Workspace, build_auto_signature
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

    # A model that answers with its own thinking instead of the email: the
    # approved email is left exactly as it was (never sent as the rewrite).
    class _Rambling(_RealMailBrain):
        async def complete(self, system, user, max_tokens=1200):
            return "Okay, the user wants me to rewrite this email so it is shorter. Let me think about the subject first."
    before = asyncio.run(read(approved_id))
    monkeypatch.setattr("bb.mail.brain.MailBrain", lambda env: _Rambling({"BB_LLM_PROVIDER": "anthropic", "BB_LLM_API_KEY": "k"}))
    r = client.post(f"/outbox/{approved_id}/rewrite", data={"instruction": "shorter"})
    assert "Could%20not%20rewrite" in r.headers["location"] and "JSON" in r.headers["location"]
    after = asyncio.run(read(approved_id))
    assert after["body"] == before["body"] and after["subject"] == before["subject"] and after["status"] == "approved"


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




def test_build_auto_signature_unit():
    sig = build_auto_signature("Ada Okafor", "Founder", "Okafor & Co", "linkedin.com/in/ada")
    assert sig.startswith(AUTO_SIG_MARKER)
    assert "Ada Okafor" in sig and "Founder · Okafor &amp; Co" in sig
    assert 'href="https://linkedin.com/in/ada"' in sig and "LinkedIn</a>" in sig
    assert build_auto_signature("", "", "", "") == AUTO_SIG_MARKER   # nothing to show but the (invisible) marker


def test_signature_auto_builds_when_blank(client):  # noqa: F811
    _as_user(client)
    assert client.post("/profile", data=NEEDS_FORM).status_code == 303   # leaves signature_html blank
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    sig = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.signature_html
    assert sig.startswith(AUTO_SIG_MARKER)
    assert "Jane Doe" in sig and "Founder · Acme Research" in sig
    assert "linkedin.com/in/janedoe" in sig and "LinkedIn</a>" in sig
    # The form shows the box blank, so later saves keep rebuilding it from the fields.
    assert "bb-auto-signature" not in client.get("/profile").text
    # A mailbox-change rewrite keeps it auto and fresh.
    ws.rewrite_config()
    sig2 = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.signature_html
    assert sig2.startswith(AUTO_SIG_MARKER) and "Jane Doe" in sig2


def test_custom_signature_is_kept(client):  # noqa: F811
    _as_user(client)
    assert client.post("/profile", data={**NEEDS_FORM, "signature_html": "<b>My own sig</b>"}).status_code == 303
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.signature_html == "<b>My own sig</b>"
    assert "My own sig" in client.get("/profile").text   # shown back for editing


WORDINGS = [
    {"subject": "Builder wanted for 24 units in Towson",
     "body": "Hi {{first_name}},\n\nWe need a builder for a 24-unit residential block in Towson, starting in April. "
             "Is that the kind of job you take on?\n\nJane"},
    {"subject": "",
     "body": "Hi {{first_name}},\n\nA 24-unit block in Towson, work from April: could your firm build it?\n\nJane"},
    {"subject": "ACT NOW!!! FREE $$$ GUARANTEED", "body": "Hi {{first_name}},\n\nBUY NOW!!! 100% FREE, CLICK HERE, RISK FREE, ACT NOW!!!\n\nJane"},
    {"subject": "Dup", "body": "Hi {{first_name}},\n\nA 24-unit block in Towson, work from April: could your firm build it?\n\nJane"},
    {"subject": "Short", "body": "Hi"},
]


def test_general_message_is_written_by_the_ai_and_becomes_the_fallback(client, monkeypatch):  # noqa: F811
    class _Fake:
        calls = []

        def __init__(self, env):
            self.configured = True

        async def complete_json(self, system, user, **kw):
            _Fake.calls.append(system)
            if "other wordings" in system:
                assert "THE ORIGINAL" in user and "24-unit" in user
                return {"versions": WORDINGS}
            assert "every recipient" in system and "Towson" in user       # built from the profile
            return {"subject": "A builder for a 24-unit block in Towson",
                    "body": "Hi {{first_name}},\n\nWe're building a 24-unit residential block in Towson and need a "
                            "builder to start in April. Could you take on a job like this?\n\nJane"}
    monkeypatch.setattr("bb.mail.brain.MailBrain", _Fake)
    _as_user(client)
    r = client.post("/profile/general-message", data={**NEEDS_FORM, "writing": "segment"})
    assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers["location"]
    assert "2+other+wordings" in r.headers["location"] or "2%20other%20wordings" in r.headers["location"]

    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    o = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach
    assert o.template_subject == "A builder for a 24-unit block in Towson" and "24-unit" in o.template_body
    assert o.template_fallback is True and o.use_template is False       # an AI mode keeps it as the fallback
    # The other wordings: the spammy one, the repeat and the stub are dropped.
    assert [v.subject for v in o.template_variants] == ["Builder wanted for 24 units in Towson", ""]
    assert all("24-unit" in v.body and v.body.startswith("Hi {{first_name}},") for v in o.template_variants)
    page = client.get("/profile").text
    assert "A builder for a 24-unit block in Towson" in page
    assert 'name="template_fallback" value="1" id="template_fallback" checked' in page
    assert 'name="tv_subject_0" value="Builder wanted for 24 units in Towson"' in page and "tv_body_1" in page
    assert "Write 5 other wordings of my message" in page
    # The rows round-trip through a profile save (edited, one removed), and reach the worker's config.
    form = {**NEEDS_FORM, "writing": "segment", "template_fallback": "1", "template_subject": o.template_subject,
            "template_body": o.template_body, "tv_subject_0": "Edited subject", "tv_body_0": o.template_variants[0].body}
    assert client.post("/profile", data=form).status_code == 303
    o2 = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach
    assert [v.subject for v in o2.template_variants] == ["Edited subject"]
    ws.rewrite_config()
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.template_variants == o2.template_variants
    # The wordings of a hand-written message, on their own.
    r = client.post("/profile/general-message/variants", data={**NEEDS_FORM, "writing": "template",
                                                                 "template_subject": "My own subject",
                                                                 "template_body": "Hi {{first_name}},\n\nMy own message about the 24-unit block, long enough.\n\nJane"})
    assert r.status_code == 303 and "error=" not in r.headers["location"], r.headers["location"]
    o3 = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach
    assert o3.use_template and len(o3.template_variants) == 2 and o3.template_subject == "My own subject"
    # No message yet: a plain refusal, nothing saved.
    r = client.post("/profile/general-message/variants", data={**NEEDS_FORM, "writing": "template", "template_body": ""})
    assert "error=" in r.headers["location"]
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.template_variants == o3.template_variants

    # Saving again keeps it, and so does the rewrite a mailbox change triggers.
    r = client.post("/profile", data={**NEEDS_FORM, "writing": "segment", "template_fallback": "1",
                                      "template_subject": o.template_subject, "template_body": o.template_body})
    assert r.status_code == 303, r.text
    ws.rewrite_config()
    again = HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach
    assert again.template_fallback is True and again.template_subject == o.template_subject



def test_typed_wordings_get_the_same_checks_as_the_message(client, monkeypatch):  # noqa: F811
    """A wording typed on the page is sent in place of the message to some
    recipients: a stub is refused, a spammy one is warned about, and a
    rejected save keeps what was typed."""
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    body = "Hi {{first_name}},\n\nWe are planning a 24-unit block in Towson and need a builder from April.\n\nJane"
    base = {**NEEDS_FORM, "writing": "template", "template_subject": "A builder for Towson", "template_body": body}
    r = client.post("/profile", data={**base, "tv_subject_0": "", "tv_body_0": "todo"})
    assert r.status_code == 200 and "Wording 1 of your message is too short" in r.text
    assert 'name="tv_body_0" rows="5">todo</textarea>' in r.text           # what was typed is still there
    assert not HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.template_variants if ws.config_file.exists() else True
    spammy = "Hi {{first_name}},\n\nFREE!!! ACT NOW!!! 100% GUARANTEED, BUY TODAY, LIMITED TIME OFFER!!!\n\nJane"
    r = client.post("/profile", data={**base, "tv_subject_0": "", "tv_body_0": spammy})
    assert r.status_code == 303
    assert ws.template_warning().startswith("Wording 1 of your message may land in junk folders")


def test_new_wordings_rewrite_the_first_emails_already_written(client, monkeypatch):  # noqa: F811
    """Writing new wordings changes the words of the emails: with "rewrite
    scheduled" ticked, first emails written before and not sent yet are
    written again, so the backlog goes out rotated too."""
    class _Fake:
        def __init__(self, env):
            self.configured = True

        async def complete_json(self, system, user, **kw):
            return {"versions": WORDINGS[:2]}
    monkeypatch.setattr("bb.mail.brain.MailBrain", _Fake)
    calls = []
    async def fake_rewrite(self, why=""):
        calls.append(why); return {"cancelled": 4, "prospects": 4}, True
    monkeypatch.setattr(Workspace, "rewrite_unsent_now", fake_rewrite)
    _as_user(client)
    body = "Hi {{first_name}},\n\nMy own message about the 24-unit block in Towson, long enough.\n\nJane"
    form = {**NEEDS_FORM, "writing": "template", "template_subject": "My own subject", "template_body": body}
    assert client.post("/profile", data=form).status_code == 303
    r = client.post("/profile/general-message/variants", data={**form, "rewrite_scheduled": "on"})
    assert r.status_code == 303 and "4+unsent+email" in r.headers["location"].replace("%20", "+") and calls
    calls.clear()
    r = client.post("/profile/general-message/variants", data=form)               # box unticked: no rewrite
    assert r.status_code == 303 and not calls
