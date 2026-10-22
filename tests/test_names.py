"""Finding who an address belongs to, and the greeting line every email opens with."""

import pytest

from harvey import names
from harvey.agents.writer import Writer
from harvey.config import EnvConfig, HarveyConfig, OutreachConfig
from harvey.models.prospect import Prospect

from tests.test_outreach import BASE, _Brain, state  # noqa: F401


def test_role_addresses_are_recognised():
    for addr in ("info@acme.com", "Sales@acme.com", "office.manager@acme.com", "no-reply@x.io",
                 "hello2@acme.com", "customer-service@acme.com"):
        assert names.is_role_address(addr), addr
    for addr in ("jane@acme.com", "jane.doe@acme.com", "jdoe@acme.com", "sales.jane@acme.com"):
        assert not names.is_role_address(addr), addr


def test_name_from_email_pattern():
    assert names.name_from_email("jane.doe@acme.com") == ("Jane", "Doe")
    assert names.name_from_email("Jane_Doe99@acme.com") == ("Jane", "Doe")
    assert names.name_from_email("ada-okafor@acme.com") == ("Ada", "Okafor")
    assert names.name_from_email("john.mcdonald@acme.com") == ("John", "McDonald")
    assert names.name_from_email("liam.o'brien@acme.com") == ("Liam", "O'Brien")
    assert names.name_from_email("maria.de.souza@acme.com") == ("Maria", "De Souza")
    # Not enough to greet someone by: single word, initial, role word, too many parts.
    for addr in ("jane@acme.com", "j.doe@acme.com", "jdoe@acme.com", "info@acme.com",
                 "sales.team@acme.com", "a.b.c.d@acme.com", "jane.doe2024x1@x.com"):
        assert names.name_from_email(addr) == ("", ""), addr
    assert names.name_from_email("jane.doe2024x1@x.com") == ("", "")  # digits mid-token


def test_name_from_linkedin_slug():
    assert names.name_from_linkedin("https://www.linkedin.com/in/jane-doe-1a2b3c4/") == ("Jane", "Doe")
    assert names.name_from_linkedin("linkedin.com/in/ada-okafor") == ("Ada", "Okafor")
    assert names.name_from_linkedin("https://uk.linkedin.com/in/liam-o-brien-ba6a7b12?x=1") == ("Liam", "O Brien")
    assert names.name_from_linkedin("https://linkedin.com/in/jdoe99") == ("", "")
    assert names.name_from_linkedin("https://linkedin.com/company/acme") == ("", "")
    assert names.name_from_linkedin("") == ("", "")


def test_match_team_member_against_the_address():
    team = [{"first_name": "Jane", "last_name": "Doe", "title": "CEO"},
            {"first_name": "Bob", "last_name": "Smithson", "title": "Ops"},
            {"first_name": "Dana", "last_name": "Doe", "title": "CFO"}]
    assert names.match_team_member("bsmithson@acme.com", team)["first_name"] == "Bob"
    assert names.match_team_member("bob@acme.com", team)["first_name"] == "Bob"
    assert names.match_team_member("smithson.b@acme.com", team)["first_name"] == "Bob"
    assert names.match_team_member("bob.smithson@acme.com", team)["first_name"] == "Bob"
    assert names.match_team_member("jane.doe@acme.com", team)["first_name"] == "Jane"
    assert names.match_team_member("doe@acme.com", team) is None       # two Does: ambiguous
    assert names.match_team_member("info@acme.com", team) is None
    assert names.match_team_member("zed@acme.com", team) is None
    assert names.match_team_member("bob@acme.com", []) is None


def test_resolve_name_order_of_trust():
    assert names.resolve_name("Jane", "", "x.y@a.com", "linkedin.com/in/bob-ng")["source"] == "given"
    r = names.resolve_name("", "", "jane.doe@acme.com", "https://linkedin.com/in/ada-okafor-1234")
    assert (r["first_name"], r["last_name"], r["source"]) == ("Ada", "Okafor", "linkedin")
    r = names.resolve_name("", "", "jane.doe@acme.com", "")
    assert (r["first_name"], r["source"]) == ("Jane", "email")
    assert names.resolve_name("", "", "info@acme.com", "linkedin.com/in/ada-okafor")["source"] == "role"
    assert names.resolve_name("", "", "jdoe@acme.com", "")["source"] == ""


def test_greeting_line_fits_what_is_known():
    assert names.greeting_for("Jane") == "Hi Jane,"
    assert names.greeting_for("Jane Marie", word="Hello") == "Hello Jane,"
    assert names.greeting_for("", "info@acme.com", "Acme") == "Hello team,"
    assert names.greeting_for("", "info@acme.com", "Acme", word="Dear") == "Dear team,"
    assert names.greeting_for("", "info@acme.com", "") == "Hello team,"
    assert names.greeting_for("", "jdoe@acme.com") == "Hi,"
    assert names.greeting_for("", "jdoe@acme.com", word="Good day") == "Good day,"
    assert names.greeting_for("", "jdoe@acme.com", word="Dear") == "Hello,"
    assert names.greeting_for("Jane", word="Yo") == "Hi Jane,"   # unknown word falls back


def test_ensure_greeting_replaces_whatever_the_model_wrote():
    body = "Hi Jane,\n\nSaw your roofing work in Leeds.\n\nWould a quote be possible?"
    assert names.ensure_greeting(body, "Jane") == body
    assert names.ensure_greeting(body, "", "jdoe@x.com") == body.replace("Hi Jane,", "Hi,")
    assert names.ensure_greeting("Hi ,\n\nSaw your work.", "Jane") == "Hi Jane,\n\nSaw your work."
    assert names.ensure_greeting("Hello there — saw your work in Leeds.\nMore.", "Jane") == \
        "Hi Jane,\n\nSaw your work in Leeds.\nMore."
    assert names.ensure_greeting("Dear Sir or Madam,\n\nSaw your work.", "", "info@acme.com", "Acme") == \
        "Hello team,\n\nSaw your work."
    assert names.ensure_greeting("Saw your work.", "Sam", word="Hey") == "Hey Sam,\n\nSaw your work."
    assert names.ensure_greeting("", "Sam") == "Hi Sam,"
    # A first line that merely starts with "Hi" as a word in a sentence is kept.
    kept = "High season must be busy for you.\n\nQuick one."
    assert names.ensure_greeting(kept, "Sam") == "Hi Sam,\n\n" + kept


def test_greeting_config_word():
    assert OutreachConfig().greeting == "Hi"
    assert OutreachConfig(greeting="hello").greeting == "Hello"
    with pytest.raises(ValueError):
        OutreachConfig(greeting="Yo")


@pytest.mark.asyncio
async def test_writer_finds_missing_names(state, monkeypatch):  # noqa: F811
    import harvey.agents.scout as scout_mod

    team = {"acme.com": [{"first_name": "Jane", "last_name": "Doe", "title": "Managing Director"}]}
    fetched = []

    async def fake_team_page(self, domain):
        fetched.append(domain)
        return team.get(domain, [])

    monkeypatch.setattr(scout_mod.Scout, "_scrape_team_page", fake_team_page)
    rows = [
        Prospect(id="a", email="jdoe@acme.com", email_status="provided", company="Acme"),
        Prospect(id="b", email="zed@acme.com", email_status="provided", company="Acme"),          # no match
        Prospect(id="c", email="bo.ng@fast.co", email_status="provided", company="Fast"),         # pattern
        Prospect(id="d", email="x@slow.co", email_status="provided", company="Slow",
                 linkedin_url="https://linkedin.com/in/ada-okafor-9f8e7d"),                     # linkedin
        Prospect(id="e", email="info@fast.co", email_status="provided", company="Fast"),         # role inbox
        Prospect(id="f", email="sam@gmail.com", email_status="provided"),                        # personal mail
        Prospect(id="g", email="kim@slow.co", email_status="provided", first_name="Kim"),        # already named
    ]
    for p in rows:
        await state.add_prospect(p)
    class _DraftBrain(_Brain):
        async def think_json(self, prompt, **kw):
            self.prompts.append(prompt)
            return {"subject": "quick one",
                    "body": "Hello there — saw your roofing work.\n\nOpen to a quote?"}

    writer = Writer(_DraftBrain(), state, HarveyConfig(**BASE), EnvConfig())
    writer.skills = ""
    assert await writer._find_missing_names(rows) == 3
    assert fetched == ["acme.com"]   # one fetch per domain; the pattern and LinkedIn cases needed none
    a = await state.get_prospect("a")
    assert (a.first_name, a.last_name, a.title) == ("Jane", "Doe", "Managing Director")
    assert "team page" in a.personalization_notes
    assert (await state.get_prospect("b")).first_name == ""
    c = await state.get_prospect("c")
    assert (c.first_name, c.last_name) == ("Bo", "Ng") and "email address" in c.personalization_notes
    d = await state.get_prospect("d")
    assert (d.first_name, d.last_name) == ("Ada", "Okafor") and "LinkedIn" in d.personalization_notes
    assert (await state.get_prospect("e")).first_name == ""
    assert (await state.get_prospect("f")).first_name == ""

    # The per-person draft opens with the right greeting whatever the model wrote.
    draft = await writer._write_personal_email(a)
    assert draft["body"] == "Hi Jane,\n\nSaw your roofing work.\n\nOpen to a quote?"
    assert '"Hi Jane,"' in writer.brain.prompts[-1] and "- Name: Jane Doe" in writer.brain.prompts[-1]
    draft = await writer._write_personal_email(await state.get_prospect("e"))
    assert draft["body"].startswith("Hello team,\n\n")
    assert "shared inbox" in writer.brain.prompts[-1] and "write to the team" in writer.brain.prompts[-1]
    draft = await writer._write_personal_email(await state.get_prospect("b"))
    assert draft["body"].startswith("Hi,\n\n") and "do not guess" in writer.brain.prompts[-1]


@pytest.mark.asyncio
async def test_sender_renders_greeting_for_sequences(state):  # noqa: F811
    import harvey.agents.sender as sender_mod

    cfg = HarveyConfig(**{**BASE, "outreach": {"greeting": "Hello"}})
    sender = sender_mod.Sender(_Brain(), state, cfg, EnvConfig())
    body = "Hello {{first_name}},\n\nSaw {{company}}'s work. Does {{title}} cover roofing?"
    named = Prospect(id="1", email="jane@acme.com", first_name="Jane", company="Acme", title="CEO")
    assert sender._render(body, named, body=True) == \
        "Hello Jane,\n\nSaw Acme's work. Does CEO cover roofing?"
    nameless = Prospect(id="2", email="x@acme.com", company="Acme", title="CEO")
    assert sender._render(body, nameless, body=True).startswith("Hello,\n\nSaw Acme's work.")
    shared = Prospect(id="3", email="info@acme.com", company="Acme", title="")
    assert sender._render(body, shared, body=True).startswith("Hello team,\n\nSaw Acme's work.")
    # Model forgot the greeting line: it is added. Subjects are untouched.
    assert sender._render("Saw {{company}}.", named, body=True) == "Hello Jane,\n\nSaw Acme."
    assert sender._render("{{first_name}}, quick question", named) == "Jane, quick question"
    assert sender._render("{{first_name}}, quick question", nameless) == "there, quick question"


def test_company_display_name_never_shows_a_domain():
    assert names.looks_like_domain("5b4me.com") and not names.looks_like_domain("Acme Ltd")
    assert names.company_display_name("Acme Ltd", "acme.com") == "Acme Ltd"
    assert names.company_display_name("5b4me.com", "5b4me.com") == "5b4me"
    assert names.company_display_name("", "north-west-build.co.uk") == "North West Build"
    assert names.company_display_name("", "www.okafor-and-co.ng") == "Okafor And Co"
    assert names.company_display_name("", "https://acme.com/") == "Acme"
    assert names.company_display_name("", "") == ""


def test_site_name_and_description_from_html():
    html = ('<html><head><title>Home | 5B4ME Digital Agency</title>'
            '<meta property="og:site_name" content="5B4ME">'
            '<meta name="description" content="A small digital agency in Lagos building sites and brands for local businesses.">'
            '</head><body></body></html>')
    assert names.site_name_from_html(html, "5b4me.com") == "5B4ME"
    assert names.meta_description_from_html(html).startswith("A small digital agency in Lagos")
    title_only = "<html><head><title>Welcome - North West Build Ltd - Builders in Leeds</title></head></html>"
    assert names.site_name_from_html(title_only, "north-west-build.co.uk") == "North West Build Ltd"
    assert names.site_name_from_html("<title>Home</title>", "x.com") == ""
    assert names.site_name_from_html("<title>https://x.com</title>", "x.com") == ""
    assert names.site_name_from_html("", "x.com") == "" and names.meta_description_from_html("") == ""


@pytest.mark.asyncio
async def test_real_inquiry_prompt_renders_the_request(state):  # noqa: F811
    from harvey.brain import Brain

    cfg = HarveyConfig(**{**BASE, "outreach": {"mode": "inquiry",
                                               "intent": "quotes for a two-storey extension in Lagos"}})
    writer = Writer(Brain(state, cfg.brain), state, cfg, EnvConfig())
    writer.skills = "SALES SKILL TEXT THAT MUST NOT APPEAR"
    prompt = writer._base_prompt()
    assert "prospective CUSTOMER or PARTNER" in prompt and "quotes for a two-storey extension in Lagos" in prompt
    assert "FOUR ENQUIRIES WRITTEN THE RIGHT WAY" in prompt and "SUBJECT LINE" in prompt and "swap test" in prompt
    assert "Our team has a current need" in prompt           # the rejected email, labelled wrong
    from harvey import enquiry
    assert enquiry.sender_line(cfg) in prompt               # who is writing, built from the persona
    assert "SALES SKILL TEXT" not in prompt and "STRICT EMAIL RULES" not in prompt and "{{" not in prompt
    assert writer._length_rule == "Between 35 and 90 words, as short as the need allows"
    assert "sentence case" in writer._subject_rule

    offer = Writer(Brain(state, HarveyConfig(**BASE).brain), state, HarveyConfig(**BASE), EnvConfig())
    offer.skills = "SALES SKILL TEXT"
    prompt = offer._base_prompt()
    assert "STRICT EMAIL RULES" in prompt and "SALES SKILL TEXT" in prompt and "CUSTOMER or PARTNER" not in prompt
    assert "lowercase, 2-5 words" in offer._subject_rule


@pytest.mark.asyncio
async def test_sender_never_renders_a_domain_as_the_company(state):  # noqa: F811
    import harvey.agents.sender as sender_mod

    sender = sender_mod.Sender(_Brain(), state, HarveyConfig(**BASE), EnvConfig())
    p = Prospect(id="1", email="x@5b4me.com", first_name="Ada", company="5b4me.com")
    assert sender._render("Saw {{company}}'s work.", p) == "Saw 5b4me's work."
    p2 = Prospect(id="2", email="x@north-west-build.co.uk", first_name="Ada", company="")
    assert sender._render("{{company}}", p2) == "North West Build"


def test_scrub_removes_company_names_without_wrecking_the_sentence():
    from harvey.agents.writer import _mentions, _scrub

    terms = {"5b4me.com", "5b4me", "Wandlp", "Acme Roofing", "Acme"}
    assert _mentions("working with 5B4ME on this", terms) and not _mentions("working with you", terms)
    assert _scrub("Avrum here from Wandlp. I'm looking into working with 5b4me.com on a job.", terms) == \
        "Avrum here. I'm looking into working with you on a job."
    assert _scrub("Acme Roofing's team at Acme does roofs; see https://acme.com/x for more.", terms) == \
        "your team does roofs; see your site for more."
    assert _scrub("Roof repair quote for Acme", terms) == "Roof repair quote for you"
    assert _scrub("Nothing to change here.", terms) == "Nothing to change here."
    assert _scrub("", terms) == "" and _scrub("Keep Acme", set()) == "Keep Acme"
    # A company whose name is an ordinary word is caught only as the name is
    # written: "Build" the company, not "build" or "builder" the words.
    plain = {"Build", "build.example"}
    assert not _mentions("I need a builder, and I want to build in April.", plain)
    assert _mentions("I hear Build does blocks this size.", plain) and _mentions("see build.example", plain)
    assert _scrub("I need a builder to build it; a quote for Build would help.", plain) == \
        "I need a builder to build it; a quote for you would help."
    # A long distinctive name is caught however it is cased.
    assert _mentions("the fryreglet people", {"Fryreglet"}) and not _mentions("the fry people", {"Fryreglet"})
    # However the upload cased the company, it is the written name that counts: "BUILD" and
    # "Build" are the company, "build" is still the verb.
    assert _mentions("I saw that BUILD finished the Towson job.", {"build"}) and not _mentions("we build in April", {"build"})
    assert _scrub("I saw that BUILD finished the job; we build in April.", {"build"}) == "I saw that you finished the job; we build in April."
    assert _mentions("ZENITH's crews were recommended to me", {"Zenith"}) and _scrub("ZENITH's crews", {"zenith"}) == "your crews"


@pytest.mark.asyncio
async def test_inquiry_drafts_never_name_the_recipients_company(state):  # noqa: F811
    drafts = iter([
        {"subject": "Working with 5b4me.com", "body": "Hi there,\n\nAvrum here from Wandlp. Could 5b4me take on a small build?"},
        {"subject": "Small build, looking for a quote", "body": "Hi,\n\nI run Wandlp and I'm after a builder for a small extension next spring. Could you take it on?\n\nThanks,\nJane"},
    ])

    class _LeakyBrain(_Brain):
        async def think_json(self, prompt, **kw):
            self.prompts.append(prompt)
            return next(drafts)

    cfg = HarveyConfig(**{**BASE, "persona": {**BASE["persona"], "company": "Wandlp"},
                          "outreach": {"mode": "inquiry", "intent": "a builder for a small extension"}})
    brain = _LeakyBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    writer.skills = ""
    p = Prospect(id="z", email="hello@5b4me.com", email_status="provided", company="5b4me.com", first_name="")
    draft = await writer._write_personal_email(p)
    assert len(brain.prompts) == 2 and "named a company or web address" in brain.prompts[1]
    assert "5b4me" not in draft["subject"].lower() and "5b4me" not in draft["body"].lower()
    assert "wandlp" in draft["body"].lower()                 # the sender may say who they are
    assert draft["body"].startswith("Hello team,\n\n") and draft["body"].endswith("Could you take it on?\n\nJane")
    assert "(not to be named in the email" in brain.prompts[0] and "5b4me" not in brain.prompts[0].split("FACTS")[1].split("Requirements")[0].replace("hello@5b4me.com", "")


def test_bounce_reasons_and_hints():
    from harvey.integrations.mail_provider import bounce_reason, looks_like_bounce

    assert looks_like_bounce("mailer-daemon@googlemail.com", "Message blocked")
    reason, hint = bounce_reason(
        "Your message to info@x.com has been blocked. See technical details below for more information.\n\n"
        "The response was:\n550-5.7.1 [1.2.3.4] Our system has detected that this message is likely unsolicited mail.")
    assert reason.startswith("550-5.7.1") and "Gentle speed" in hint
    reason, hint = bounce_reason("Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.")
    assert "does not exist" in reason and "marked invalid" in hint
    reason, hint = bounce_reason("Remote server returned '550 5.7.26 This mail is unauthenticated, which poses a security risk to the sender and Gmail users (DMARC)'")
    assert "SPF, DKIM and DMARC" in hint
    reason, hint = bounce_reason("421-4.7.0 Our system has detected an unusual rate of unsolicited mail originating from your IP address.")
    assert "too fast" in reason or "throttled" in hint
    reason, hint = bounce_reason("something odd")
    assert "no reason given" in reason and "exact wording" in hint
