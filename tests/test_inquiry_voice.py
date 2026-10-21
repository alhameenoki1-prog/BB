"""Inquiry mode reads as one person writing to another with a real need:
the need per kind of business, the vagueness gate, and replies that stay
on the need and send the full scope once."""
import pytest

from harvey import enquiry
from harvey.config import EnvConfig, HarveyConfig, OutreachConfig
from harvey.models.conversation import Conversation
from harvey.models.prospect import Prospect
from harvey.spamcheck import REVIEW, vague_risk

from tests.test_outreach import BASE, _Brain, _config, state  # noqa: F401

REJECTED = ("Looking to start work with your team",
            "Good day there,\n\nOur team has a current need we would like to talk through with you. Before going into "
            "detail, it would help to understand how you usually take on new clients, what the first steps look like "
            "and what you would need from us to begin.\n\nWe are ready to share the specifics and answer anything you "
            "need to judge whether it is a good fit on your side.\n\nWould a short Zoom or Google Meet call at a time "
            "that suits you work, or, if you would rather skip the call, is a one-line reply on how you normally start "
            "easier?\n\nThanks,\nThomas")
REAL = ("Rebar, around 40 tons a month from spring",
        "Hi Luis,\n\nI run Greystone Development in Baltimore and we have about a year of work starting in spring that "
        "will need roughly 40 tons of rebar a month, cut and delivered to site.\n\nWe've been buying job by job and I'd "
        "rather have one supplier on a monthly schedule.\n\nCan you supply that volume month to month, and who should I "
        "send the bar list to for pricing?\n\nThomas")
NEEDS = [
    {"sectors": ["construction"], "need": "A builder for a 24-unit residential block in Towson, drawings done, start in April.",
     "details_url": "https://docs.example/towson", "details_label": "the Towson brief"},
    {"sectors": ["manufacturing"], "need": "About 40 tons of rebar a month for a year, delivered to a Baltimore site."},
]


def test_the_need_follows_the_recipients_sector_and_the_link_falls_back():
    o = OutreachConfig(mode="inquiry", intent="Services and materials for a 24-unit block in Towson.",
                       details_url="docs.example/general", details_label="the project brief", sector_needs=NEEDS)
    assert o.details_url == "docs.example/general"            # the profile normalises; config keeps what it is given
    c = enquiry.need_for(o, "construction")
    assert c["need"].startswith("A builder") and c["url"] == "https://docs.example/towson" and c["label"] == "the Towson brief"
    m = enquiry.need_for(o, "manufacturing")
    assert m["need"].startswith("About 40 tons") and m["url"] == "docs.example/general"   # no link of its own
    assert enquiry.need_for(o, "legal")["need"].startswith("Services and") and enquiry.need_for(o, "")["sectors"] == []
    links = enquiry.scope_links(o)
    assert [l["url"] for l in links] == ["https://docs.example/towson", "docs.example/general"]
    assert enquiry.sector_ask("financial_advisory").startswith("being taken on as a client")
    assert enquiry.sector_noun("manufacturing") == "a manufacturer or materials supplier"
    assert enquiry.clean_notes("No name was given; open without one.\nPrefers mornings.") == "Prefers mornings."


def test_the_campaign_email_is_held_and_a_real_enquiry_passes():
    score, reasons = vague_risk(*REJECTED, need="an advisor for about three million of family savings")
    assert score >= REVIEW and any("a need instead of the thing" in r for r in reasons)
    assert any("asks about their process" in r for r in reasons) and "no first person anywhere" in reasons
    assert vague_risk(*REAL, need="About 40 tons of rebar a month") == (0, [])
    assert enquiry.need_is_concrete("about 40 tons of rebar a month") and enquiry.need_is_concrete("start on site in spring")
    assert not enquiry.need_is_concrete("start working with your team")
    assert enquiry.GENERIC_NEED_RE.search("I want to start work with your team")
    assert enquiry.wants_details("Sure, send me the specs and the schedule") and not enquiry.wants_details("Who is this?")
    assert enquiry.example_leaks("We sold a house in Scarsdale") == ["scarsdale"] and enquiry.example_leaks("Tulsa site", "our Tulsa yard") == []


def test_the_scope_marker_becomes_the_link_exactly_once():
    url = "https://docs.example/towson"
    body, ok = enquiry.resolve_scope_marker("Happy to. The drawings and schedule are here:\n[SCOPE LINK]\n\nThursday?", url, False)
    assert ok and body.count(url) == 1 and "[SCOPE LINK]" not in body and "here:\n" + url in body
    body, ok = enquiry.resolve_scope_marker("Happy to.\n[SCOPE LINK 1]\n\nThursday?", url, False, label="the brief")
    assert ok and "The full scope is in the brief" in body and body.endswith("Thursday?")
    body, ok = enquiry.resolve_scope_marker("Got it. It's all here:\n[SCOPE LINK]\n\nThursday?", url, True)
    assert not ok and url not in body and "here:" not in body and body == "Got it.\n\nThursday?"
    body, ok = enquiry.resolve_scope_marker("April start works.", url, False, wants=True, label="the Towson brief")
    assert ok and body.endswith(url) and "so you can see what's involved" in body
    assert enquiry.resolve_scope_marker("April start works.", "", False, wants=True) == ("April start works.", False)
    assert enquiry.already_shared(url, ["earlier reply with " + url + "/ in it"]) and not enquiry.already_shared(url, ["nothing"])


class _ReplyBrain(_Brain):
    """The reply model: writes the marker where the link belongs."""

    def __init__(self, texts):
        super().__init__()
        self.texts = list(texts)

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.texts.pop(0) if self.texts else "Hi Dana,\n\nSure.\n\nJane"


@pytest.mark.asyncio
async def test_a_reply_answers_on_the_need_and_sends_the_scope_once(state):  # noqa: F811
    from harvey.agents.handler import Handler

    cfg = _config(mode="inquiry", intent="Services and materials for a 24-unit block in Towson.",
                  sender_line="I run Greystone Development, a residential developer in Baltimore.",
                  sector_needs=NEEDS, ask="zoom", reply_instructions="Never discuss the budget by email.")
    brain = _ReplyBrain([
        "Hi Dana,\n\nStart is April; the drawings and permits are done. Everything a builder wants first is in the brief:\n[SCOPE LINK]\n\nWould Thursday afternoon suit you?\n\nJane",
        "Hi Dana,\n\nPossibly. Two phases of twelve would change the financing, so let's go through it on the call.\n[SCOPE LINK]\n\nJane",
    ])
    handler = Handler(brain, state, cfg, EnvConfig())
    prospect = Prospect(id="c1", first_name="Dana", email="dana@build.example", email_status="provided",
                        industry="construction", company="Build Co")
    await state.add_prospect(prospect)
    convo = Conversation(id="", prospect_id="c1", channel="email", status="open")
    convo.id = await state.add_conversation(convo)

    text = await handler._generate_response("question", "What is the budget and when do you want to start?", prospect, convo)
    prompt = brain.prompts[-1]
    assert "[inquiry_handler prompt]" in prompt and "[handler prompt]" not in prompt      # its own brief, no sales skills
    assert "A builder for a 24-unit residential block in Towson" in prompt and "Greystone Development" in prompt
    assert "THE FULL SCOPE of what Jane needs is written up in the Towson brief" in prompt and "[SCOPE LINK]" in prompt
    assert "WHAT JANE WANTS NEXT: a short video call" in prompt and "Never discuss the budget" in prompt
    assert text.count("https://docs.example/towson") == 1 and "[SCOPE LINK]" not in text
    assert "in the brief:\nhttps://docs.example/towson\n\nWould Thursday" in text
    await handler._queue_native_reply(text, prospect, convo, {"subject": "Re: block"}, "question")

    # The second reply in the same conversation: the context says it went out, and the marker is dropped.
    text2 = await handler._generate_response("interested", "Got the brief. Open to a phased build?", prospect, convo)
    assert "has ALREADY been sent to them" in brain.prompts[-1]
    assert "docs.example" not in text2 and "[SCOPE LINK]" not in text2 and text2.endswith("on the call.\n\nJane")

    # A model that forgot the marker when they asked for the documents: the link goes in by code.
    brain.texts = ["Hi Sam,\n\nOf course, April start, drawings done.\n\nJane"]
    other = Prospect(id="c2", first_name="Sam", email="sam@steel.example", email_status="provided", industry="manufacturing")
    await state.add_prospect(other)
    convo2 = Conversation(id="", prospect_id="c2", channel="email", status="open")
    convo2.id = await state.add_conversation(convo2)
    text3 = await handler._generate_response("question", "Can you send over the specs?", other, convo2)
    assert text3.endswith("https://docs.example/towson") is False     # the rebar row has no link of its own and there is no general one
    cfg.outreach.details_url = "https://docs.example/general"
    text4 = await handler._generate_response("question", "Can you send over the specs?", other, convo2)
    assert text4.rstrip().endswith("https://docs.example/general") and "The full scope is in the brief" in text4
    # The classifier speaks the customer's language in inquiry mode.
    assert "asks for details, specs, scope" in (await _classifier_prompt(handler, other))


async def _classifier_prompt(handler, prospect):
    handler.brain.prompts.clear()
    handler.brain.texts = ["question"]
    await handler._classify_intent("Can you send the specs?", prospect)
    return handler.brain.prompts[-1]


@pytest.mark.asyncio
async def test_writer_prompts_carry_the_sender_line_and_the_sector_need(state):  # noqa: F811
    from harvey.agents.writer import Writer
    from harvey.brain import Brain

    cfg = HarveyConfig(**{**BASE, "outreach": {"mode": "inquiry", "intent": "Services for a 24-unit block in Towson.",
                                               "sender_line": "I run Greystone Development in Baltimore.",
                                               "sector_needs": NEEDS, "ask": "zoom"}})
    writer = Writer(Brain(state, cfg.brain), state, cfg, EnvConfig())
    writer.skills = "SALES SKILLS"
    p = writer._base_prompt("construction")
    assert "I run Greystone Development in Baltimore." in p and "A builder for a 24-unit residential block" in p
    assert enquiry.SECTOR_ASKS["construction"] in p and "FOUR ENQUIRIES WRITTEN THE RIGHT WAY" in p
    assert "THE ASK: what" in p and "a short video call" in p and "never an easy out" in p and "Booking link" not in p
    assert "SALES SKILLS" not in p and "{{" not in p
    assert "Services for a 24-unit block" in writer._base_prompt("legal")          # uncovered sector: the general need
    seg = writer._segment_block([Prospect(industry="construction", title="CEO", email="x@y.example")])
    assert "a single reader, a construction company" in seg and "{{first_name}}" in seg and "will decide" in seg
    assert writer._style_block() == "" and writer._length_rule.startswith("Between 35 and 90")
    assert writer._forbidden_terms([Prospect(email="a@acme-build.example", company="Acme Build")]) >= {"acme-build.example", "Acme Build"}
    assert "Greystone" not in " ".join(writer._forbidden_terms([Prospect(email="a@acme-build.example")]))
