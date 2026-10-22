"""The spam-risk second opinion, the writer's rewrite-or-hold, the sender's
hold on autopilot, and the operator's own message instead of AI writing."""

import pytest

import harvey.agents.sender as sender_mod
from harvey.agents.writer import Writer
from harvey.config import EnvConfig, HarveyConfig
from harvey.models.prospect import Prospect
from harvey.spamcheck import REVIEW, describe, spam_risk

from tests.test_outreach import BASE, _Brain, state  # noqa: F401

GOOD = ("Kitchen extension in Lekki, looking for quotes",
        "Hi Jane,\n\nWe are planning a 40 square metre kitchen extension on a two-storey house in Lekki "
        "and want the work to start in March. Your team came up when I looked for builders who handle "
        "extensions of this size.\n\nDo you take on work like this, and how do you usually start, with a "
        "site visit or drawings first?\n\nA line back either way is plenty.\n\nAvrum")
BAD = ("PARTNERSHIP OPPORTUNITY!!!",
       "Dear Sir/Madam, I hope this email finds you well. We offer the best solutions. "
       "Click here: http://x.com. 100% guaranteed!")
MEH = ("right person at 5b4me.com",
       "Hi there,\n\nShared inboxes like this one tend to be where every request lands first, so you probably "
       "know better than anyone who at 5b4me.com picks up new work.\n\nWho's the right person for a request "
       "like this, and could you pass this note along to them? A one-line reply with a name is plenty.\n\nAvrum")


def test_spam_risk_scores_what_filters_react_to():
    assert spam_risk(*GOOD) == (0, [])
    score, reasons = spam_risk(*BAD)
    assert score > 20 and "classic spam words" in reasons and "exclamation marks (2)" in reasons
    assert "a web link" in " ".join(reasons) and "a marketing-style subject (3)" in reasons
    score, reasons = spam_risk(*MEH)
    assert score >= REVIEW and "template openers and clichés (2)" in reasons and "a terse all-lowercase subject" in reasons
    # Case matters only where it should: capitals are shouting, ordinary words are not.
    assert spam_risk("Roof repair on a two-storey house", "Hi Sam,\n\n" + "plain words about roofs " * 8 + "\n\nDo you take this on?\n\nAvrum")[0] == 0
    assert "words in capitals" in " ".join(spam_risk("Roof repair", "Hi Sam,\n\nTHIS IS URGENT ROOF WORK " + "words " * 30)[1])
    assert "a fake reply or forward subject" in spam_risk("Re: our call", "Hi,\n\n" + "words " * 40)[1]
    assert "very short body (reads like phishing)" in spam_risk("Roof repair", "Hi Sam,\n\nCan you call me?")[1]
    assert describe(["a", "b"]) == "a; b" and describe([f"r{i}" for i in range(8)]).endswith(" …")


@pytest.mark.asyncio
async def test_writer_rewrites_a_spammy_draft_once_then_holds(state):  # noqa: F811
    drafts = iter([
        {"subject": BAD[0], "body": BAD[1]},
        {"subject": GOOD[0], "body": GOOD[1]},
        # second prospect: spammy twice → held for review
        {"subject": BAD[0], "body": BAD[1]},
        {"subject": "URGENT PROPOSAL!!!", "body": "Click here now. Free offer. Guaranteed!"},
    ])

    class _SpammyBrain(_Brain):
        async def think_json(self, prompt, **kw):
            self.prompts.append(prompt)
            return next(drafts)

    cfg = HarveyConfig(**{**BASE, "outreach": {"mode": "inquiry", "intent": "a builder for a kitchen extension"},
                          "channels": {"email": {**BASE["channels"]["email"], "require_approval": False}}})
    brain = _SpammyBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    writer.skills = ""
    p1 = Prospect(id="s1", email="jane@buildco.example", first_name="Jane", email_status="provided", company="Buildco")
    p2 = Prospect(id="s2", email="sam@roofco.example", first_name="Sam", email_status="provided", company="Roofco")
    for p in (p1, p2):
        await state.add_prospect(p)

    draft = await writer._write_personal_email(p1)
    assert draft["subject"] == GOOD[0] and draft["spam_reasons"] == []
    assert "would land in the junk folder or read as a campaign" in brain.prompts[1] and "classic spam words" in brain.prompts[1]

    draft2 = await writer._write_personal_email(p2)
    assert draft2["spam_reasons"], "still spammy after the rewrite: reasons are kept"

    # Through the campaign path: the clean one is approved, the spammy one is held with a note.
    drafts = iter([{"subject": GOOD[0], "body": GOOD[1]},
                   {"subject": BAD[0], "body": BAD[1]}, {"subject": BAD[0], "body": BAD[1]}])
    campaign_id = await state.add_campaign(__import__("harvey.models.campaign", fromlist=["Campaign"]).Campaign(
        id="", name="x", channel="email", sequence=[], prospect_ids=["s1", "s2"], status="draft"))
    await writer._personalize_first_emails(campaign_id, [p1, p2])
    items = {i["prospect_id"]: i for i in await state.get_outbox()}
    assert items["s1"]["status"] == "approved" and not items["s1"]["error"]
    assert items["s2"]["status"] == "pending_review" and items["s2"]["error"].startswith("Needs review")


@pytest.mark.asyncio
async def test_sender_holds_spammy_sequence_emails_on_autopilot(state):  # noqa: F811
    from harvey.models.campaign import Campaign, EmailStep

    cfg = HarveyConfig(**{**BASE, "channels": {"email": {**BASE["channels"]["email"], "require_approval": False}}})
    sender = sender_mod.Sender(_Brain(), state, cfg, EnvConfig())
    await state.add_prospect(Prospect(id="a", email="a@x.example", first_name="Ann", email_status="provided", status="queued"))
    await state.add_prospect(Prospect(id="b", email="b@y.example", first_name="Bo", email_status="provided", status="queued"))
    good = Campaign(id="", name="construction · manager", channel="email", prospect_ids=["a"], status="draft",
                    sequence=[EmailStep(step=1, subject=GOOD[0], body=GOOD[1].replace("Jane", "{{first_name}}"), delay_days=0)])
    bad = Campaign(id="", name="your message", channel="email", prospect_ids=["b"], status="draft",
                   sequence=[EmailStep(step=1, subject=BAD[0], body=BAD[1], delay_days=0)])
    spam = Campaign(id="", name="other · admin", channel="email", prospect_ids=["a"], status="draft",
                    sequence=[EmailStep(step=1, subject=BAD[0], body=BAD[1], delay_days=0)])
    for c in (good, bad, spam):
        c.id = await state.add_campaign(c)
        await sender._stage_campaign_native(c)
    items = await state.get_outbox()
    by_campaign = {i["campaign_id"]: i for i in items}
    assert by_campaign[good.id]["status"] == "approved"
    assert by_campaign[bad.id]["status"] == "approved"          # the operator's own words go out
    assert by_campaign[spam.id]["status"] == "pending_review"   # AI text that reads like spam waits
    assert "Needs review" in by_campaign[spam.id]["error"]


@pytest.mark.asyncio
async def test_own_message_skips_the_model_and_fills_the_name(state):  # noqa: F811
    cfg = HarveyConfig(**{**BASE, "outreach": {
        "mode": "inquiry", "intent": "x", "use_template": True, "personalize_each": False,
        "template_subject": "Kitchen extension in Lekki, looking for quotes",
        "template_body": "We are planning a kitchen extension in Lekki starting in March. Do you take on work like this?\n\nAvrum",
        "template_followup_1": "Hi {{first_name}},\n\nFollowing my note about the kitchen extension: is this something you could take on?",
    }, "channels": {"email": {**BASE["channels"]["email"], "require_approval": False, "max_daily_sends": 0}}})
    brain = _Brain()
    writer = Writer(brain, state, cfg, EnvConfig())
    assert writer.use_template
    steps = writer.template_steps()
    assert [s.delay_days for s in steps] == [0, 3] and steps[1].subject == steps[0].subject
    await state.add_prospect(Prospect(id="t1", email="jane@buildco.example", first_name="Jane", email_status="provided"))
    await state.add_prospect(Prospect(id="t2", email="info@roofco.example", email_status="provided"))
    await writer.run()
    assert brain.prompts == []                                  # no model call at all
    campaigns = await state.get_campaigns_by_status("draft")
    assert [c.name for c in campaigns] == ["your message"] and len(campaigns[0].sequence) == 2

    sender = sender_mod.Sender(brain, state, cfg, EnvConfig())
    await sender._stage_campaign_native(campaigns[0])
    items = sorted(await state.get_outbox(), key=lambda i: (i["to_email"], i["step"]))
    assert len(items) == 4 and all(i["status"] == "approved" for i in items)
    jane1, jane2, info1, _ = items[2], items[3], items[0], items[1]
    assert jane1["body"].startswith("Hi Jane,\n\nWe are planning") and jane2["body"].startswith("Hi Jane,\n\nFollowing")
    assert info1["body"].startswith("Hello team,\n\nWe are planning")
    assert jane1["subject"] == "Kitchen extension in Lekki, looking for quotes"
    assert "{{" not in jane2["body"]


@pytest.mark.asyncio
async def test_own_message_has_versions_per_sector(state):  # noqa: F811
    import json

    class _SortingBrain(_Brain):
        async def think_json(self, prompt, **kw):
            self.prompts.append(prompt)
            if "Sort these contacts" in prompt:
                rows = json.loads(prompt.split("CONTACTS (JSON): ", 1)[1].split("\n\nReturn ONLY", 1)[0])
                sector = {"bigbuild.example": "construction", "truewealth.example": "financial_advisory",
                          "bank.example": "banking"}
                return [{"id": r["id"], "sector": sector.get(r["domain"], "other"), "tier": "manager",
                         "department": "operations"} for r in rows]
            raise AssertionError("no writing call expected")

    cfg = HarveyConfig(**{**BASE, "outreach": {
        "mode": "inquiry", "intent": "x", "use_template": True, "personalize_each": False,
        "template_subject": "A request for your team",
        "template_body": "Hi {{first_name}},\n\nThe default version of my message, for everyone not covered below.\n\nAvrum",
        "sector_messages": [
            {"sectors": ["construction", "real_estate"], "subject": "Kitchen extension in Lekki, looking for quotes",
             "body": "We are planning a kitchen extension in Lekki starting in March. Do you take on work like this?",
             "followup_1": "Following my note about the kitchen extension: could you take it on?"},
            {"sectors": ["financial_advisory"], "body": "My family is looking for an adviser to take over a portfolio. Do you take on new families?"},
            {"sectors": ["banking"], "body": ""},   # blank body: ignored
        ],
    }, "channels": {"email": {**BASE["channels"]["email"], "require_approval": False, "max_daily_sends": 0}}})
    brain = _SortingBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    for pid, email in (("c1", "ann@bigbuild.example"), ("c2", "bo@bigbuild.example"),
                       ("f1", "cy@truewealth.example"), ("b1", "di@bank.example"), ("o1", "ed@shop.example")):
        await state.add_prospect(Prospect(id=pid, email=email, first_name=pid.upper(), email_status="provided"))
    await writer.run()
    assert len([p for p in brain.prompts if "Sort these contacts" in p]) == 1   # sorting only, no writing
    campaigns = {c.name: c for c in await state.get_campaigns_by_status("draft")}
    assert set(campaigns) == {"your message · construction / real_estate", "your message · financial_advisory", "your message"}
    assert sorted(campaigns["your message · construction / real_estate"].prospect_ids) == ["c1", "c2"]
    assert campaigns["your message · financial_advisory"].prospect_ids == ["f1"]
    assert sorted(campaigns["your message"].prospect_ids) == ["b1", "o1"]   # blank banking version → default
    build = campaigns["your message · construction / real_estate"].sequence
    assert build[0].subject.startswith("Kitchen extension") and len(build) == 2 and build[1].delay_days == 3
    wealth = campaigns["your message · financial_advisory"].sequence
    assert wealth[0].subject == "A request for your team" and len(wealth) == 1   # subject falls back to the default

    # The sender treats every version as the operator's own words: approved, name filled, nothing held.
    sender = sender_mod.Sender(brain, state, cfg, EnvConfig())
    for c in campaigns.values():
        await sender._stage_campaign_native(c)
    items = {i["to_email"]: i for i in await state.get_outbox() if i["step"] == 1}
    assert items["ann@bigbuild.example"]["body"].startswith("Hi C1,\n\nWe are planning a kitchen extension")
    assert items["cy@truewealth.example"]["body"].startswith("Hi F1,\n\nMy family is looking")
    assert items["ed@shop.example"]["body"].startswith("Hi O1,\n\nThe default version")
    assert all(i["status"] == "approved" for i in items.values())
