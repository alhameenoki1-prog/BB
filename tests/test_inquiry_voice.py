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
    assert ok and "Everything you'd need to judge it is in the brief" in body and body.endswith("Thursday?")
    body, ok = enquiry.resolve_scope_marker("Got it. It's all here:\n[SCOPE LINK]\n\nThursday?", url, True)
    assert not ok and url not in body and "here:" not in body and body == "Got it.\n\nThursday?"
    body, ok = enquiry.resolve_scope_marker("April start works.", url, False, wants=True, label="the Towson brief")
    assert ok and body.endswith(url) and "Everything you'd need to judge it is in the Towson brief" in body
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
        "Hi Dana,\n\nWe want to be on site in April and the drawings are finished. The budget I'll keep for the call. Everything a builder would ask before pricing it is in the brief:\n[SCOPE LINK]\n\nWould Thursday afternoon suit you?\n\nJane",
        "Hi Dana,\n\nMaybe. Splitting it in two would alter the financing, so I'd need to hear your sequence first; let's cover that on the call.\n[SCOPE LINK]\n\nJane",
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
    assert "before pricing it is in the brief:\nhttps://docs.example/towson\n\nWould Thursday" in text
    await handler._queue_native_reply(text, prospect, convo, {"subject": "Re: block"}, "question")

    # The second reply in the same conversation: the context says it went out, and the marker is dropped.
    text2 = await handler._generate_response("interested", "Got the brief. Open to a phased build?", prospect, convo)
    assert "has ALREADY been sent to them" in brain.prompts[-1]
    assert "docs.example" not in text2 and "[SCOPE LINK]" not in text2 and text2.endswith("on the call.\n\nJane")
    assert len(brain.prompts) == 2          # neither reply copied the examples, so no retry

    # A model that forgot the marker when they asked for the documents: the link goes in by code.
    brain.texts = ["Hi Sam,\n\nOf course, April start, drawings done.\n\nJane"]
    other = Prospect(id="c2", first_name="Sam", email="sam@steel.example", email_status="provided", industry="manufacturing")
    await state.add_prospect(other)
    convo2 = Conversation(id="", prospect_id="c2", channel="email", status="open")
    convo2.id = await state.add_conversation(convo2)
    text3 = await handler._generate_response("question", "Who are you exactly?", other, convo2)
    assert "docs.example" not in text3     # the rebar row has no link of its own and there is no general one
    cfg.outreach.details_url = "https://docs.example/general"
    text4 = await handler._generate_response("question", "Who are you exactly?", other, convo2)
    # Asked for or not, the first reply carries it, above the sign-off, which stays last.
    assert text4.rstrip().endswith("https://docs.example/general\n\nJane")
    assert "Have a look and tell me whether it's something you'd take on:" in text4
    # Someone who declined gets an answer and no link, whatever the model wrote.
    brain.texts = ["Hi Sam,\n\nUnderstood, thanks for letting me know. Everything is in the brief:\n[SCOPE LINK]\n\nJane"]
    text5 = await handler._generate_response("objection", "We're not taking new work this year.", other, convo2)
    assert "docs.example" not in text5 and "[SCOPE LINK]" not in text5 and text5.rstrip().endswith("Jane")
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
    assert "saying that a short call would be the easiest way to explain it" in p and "walk you through" in p
    # The ask bullet follows the theme: a visit is a visit, and an email reply adds no call at all.
    cfg.outreach.ask = "visit"
    assert "saying that coming to see it would be the easiest way" in writer._ask_block()
    cfg.outreach.ask = "email"
    block = writer._ask_block()
    assert "no call, meeting or visit is mentioned anywhere" in block and "easiest way" not in block
    cfg.outreach.ask = "zoom"
    assert "SALES SKILLS" not in p and "{{" not in p
    assert "Services for a 24-unit block" in writer._base_prompt("legal")          # uncovered sector: the general need
    seg = writer._segment_block([Prospect(industry="construction", title="CEO", email="x@y.example")])
    assert "a single reader, a construction company" in seg and "{{first_name}}" in seg and "will decide" in seg
    assert writer._style_block() == "" and writer._length_rule.startswith("Between 35 and 90")
    assert writer._forbidden_terms([Prospect(email="a@acme-build.example", company="Acme Build")]) >= {"acme-build.example", "Acme Build"}
    assert "Greystone" not in " ".join(writer._forbidden_terms([Prospect(email="a@acme-build.example")]))


@pytest.mark.asyncio
async def test_the_inbox_auto_reply_shares_the_matching_scope_on_its_own(state):  # noqa: F811
    from tests.test_inbox_autoreply import FakeProvider, _InboxBrain, _handler, _mail

    provider = FakeProvider()
    provider.inbound = [_mail(1, "dana@steel.example", "your note to my colleague",
                              "My colleague forwarded your email. We distribute rebar across the Mid-Atlantic. Can you send the specs?",
                              headers={"from": "Dana Whitfield <dana@steel.example>"})]
    brain = _InboxBrain([{"action": "reply", "reason": "a supplier offering to quote", "share": [],
                          "reply": "Hi Dana,\n\nGlad your colleague passed it on. It's about 40 tons of rebar a month for a year, "
                                   "delivered to our Baltimore site, and I'd like one supplier on a monthly schedule.\n\nJane"}])
    handler = _handler(state, provider, brain, mode="inquiry", intent="Services and materials for a 24-unit block in Towson.",
                       sender_line="I run Greystone Development in Baltimore.",
                       sector_needs=[{**NEEDS[0]}, {**NEEDS[1], "details_url": "https://docs.example/rebar",
                                                    "details_label": "the rebar spec"}])
    assert await handler._run_native() is True
    prompt = brain.prompts[0]
    assert "WHAT JANE DOE IS LOOKING FOR" in prompt and "[4] the rebar spec: share when they write about this (manufacturing)" in prompt
    assert "I run Greystone Development" in prompt
    items = await state.get_outbox()
    assert len(items) == 1
    body = items[0]["body"]
    assert body.count("https://docs.example/rebar") == 1 and "docs.example/towson" not in body
    assert "Everything you'd need to judge it is in the rebar spec" in body and body.rstrip().endswith("Jane")

    # A stranger who asked something else gets an answer, not the brief.
    provider.inbound = [_mail(2, "ray@steel.example", "your email", "Who is this and how did you get my address?",
                              headers={"from": "Ray <ray@steel.example>"})]
    brain.decisions = [{"action": "reply", "reason": "asks who we are", "share": [],
                        "reply": "Hi Ray,\n\nI run Greystone Development in Baltimore; your address came from the team page "
                                 "on your site. I wrote because we're buying rebar for a block in Towson this year.\n\nJane"}]
    assert await handler._run_native() is True
    body2 = sorted((await state.get_outbox()), key=lambda i: i["created_at"])[-1]["body"]
    assert "docs.example" not in body2 and body2.rstrip().endswith("Jane")

    # When the model chose the document itself, its own sentence introduces it: no second one is added.
    provider.inbound = [_mail(3, "kim@steel.example", "specs", "Happy to quote. Can you send the spec over?",
                              headers={"from": "Kim <kim@steel.example>"})]
    brain.decisions = [{"action": "reply", "reason": "wants the spec", "share": [4],
                        "reply": "Hi Kim,\n\nGladly. The spec below has the grades, the monthly tonnage and the delivery "
                                 "schedule, which is the quickest way to see whether it's your kind of order.\n\nJane"}]
    assert await handler._run_native() is True
    body3 = sorted((await state.get_outbox()), key=lambda i: i["created_at"])[-1]["body"]
    assert body3.count("https://docs.example/rebar") == 1 and "Everything you'd need to judge it" not in body3
    assert "the rebar spec: https://docs.example/rebar" in body3 and body3.rstrip().endswith("Jane")


def test_a_sentence_lifted_from_the_examples_is_caught_and_the_senders_own_facts_are_not():
    writer_examples = enquiry.prompt_examples("inquiry_writer")
    handler_examples = enquiry.prompt_examples("inquiry_handler")
    assert "Example 1" in writer_examples and "THE RULES" not in writer_examples and "They wrote:" in handler_examples
    # The closing question of the steel example, with one word changed, is still a copy.
    assert enquiry.copied_phrases("Can you supply that volume month to month, and who should I send the bar list to for pricing?",
                                  writer_examples) == ["and who should i send the bar list to for pricing"]
    # The sender's own line is theirs, however many words it shares with anything.
    mine = "I run Greystone Development, a residential developer in Baltimore."
    assert enquiry.copied_phrases(f"{mine} Can you take this on?", writer_examples, allowed=mine) == []
    # Same shape, own words: not a copy.
    assert enquiry.copied_phrases("Could you build it, and where should the drawings go?", writer_examples) == []
    assert enquiry.example_copying("the bar list for a 24-unit block", "", "inquiry_writer") == ["24-unit", "bar list"]
    # The handler example's opening, copied into a reply, is caught too.
    assert enquiry.copied_phrases("Start is April; the drawings and permits are done, so nothing is holding it up.",
                                  handler_examples) == ["start is april the drawings and permits are done so nothing"]


@pytest.mark.asyncio
async def test_a_reply_written_in_the_examples_words_is_asked_for_again(state):  # noqa: F811
    from harvey.agents.handler import Handler

    cfg = _config(mode="inquiry", intent="Services and materials for a 24-unit block in Towson.",
                  sender_line="I run Greystone Development, a residential developer in Baltimore.",
                  sector_needs=NEEDS, ask="zoom")
    copied = ("Hi Dana,\n\nStart is April; the drawings and permits are done, so nothing on our side is holding it up. "
              "The rest is in the brief:\n[SCOPE LINK]\n\nIs there an afternoon this week you could talk?\n\nJane")
    own = ("Hi Dana,\n\nApril is when we want to break ground, and the drawings are finished. What you'd need before "
           "quoting is in the brief:\n[SCOPE LINK]\n\nWhich afternoon this week could you talk?\n\nJane")
    brain = _ReplyBrain([copied, own])
    handler = Handler(brain, state, cfg, EnvConfig())
    prospect = Prospect(id="c9", first_name="Dana", email="dana@build.example", email_status="provided",
                        industry="construction", company="Build Co")
    await state.add_prospect(prospect)
    convo = Conversation(id="", prospect_id="c9", channel="email", status="open")
    convo.id = await state.add_conversation(convo)
    text = await handler._generate_response("question", "When do you want to start?", prospect, convo)
    assert len(brain.prompts) == 2 and "copied sentences from the examples" in brain.prompts[1]
    assert '"start is april the drawings and permits are done so nothing' in brain.prompts[1]
    assert text.startswith("Hi Dana,\n\nApril is when we want to break ground") and text.count("https://docs.example/towson") == 1


@pytest.mark.asyncio
async def test_a_first_email_in_the_examples_words_is_asked_for_again(state):  # noqa: F811
    from harvey.agents.writer import Writer

    drafts = iter([
        {"subject": "Rebar, 40 tons a month", "body": "Hi Luis,\n\nI need about 40 tons of rebar a month for a year, delivered to a Baltimore site. "
                                                      "I run Greystone Development here.\n\nCan you supply that volume on a monthly schedule, and who "
                                                      "should I send the bar list to for pricing?\n\nJane"},
        {"subject": "Rebar, 40 tons a month", "body": "Hi Luis,\n\nI need about 40 tons of rebar a month for a year, delivered to a Baltimore site. "
                                                      "I run Greystone Development here.\n\nCould you supply that much every month, and where should "
                                                      "the spec go for a price?\n\nJane"},
    ])

    class _CopyingBrain(_Brain):
        async def think_json(self, prompt, **kw):
            self.prompts.append(prompt)
            return next(drafts)

    cfg = _config(mode="inquiry", intent="Materials for a 24-unit block in Towson.",
                  sender_line="I run Greystone Development, a residential developer in Baltimore.", sector_needs=NEEDS)
    brain = _CopyingBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    writer.skills = ""
    p = Prospect(id="s1", first_name="Luis", email="luis@steel.example", email_status="provided", industry="manufacturing")
    await state.add_prospect(p)
    draft = await writer._write_personal_email(p)
    assert len(brain.prompts) == 2 and "copied from the examples" in brain.prompts[1]
    assert '"bar list"' in brain.prompts[1] and 'send the bar list to for pricing"' in brain.prompts[1]
    assert "bar list" not in draft["body"] and "where should the spec go" in draft["body"]

