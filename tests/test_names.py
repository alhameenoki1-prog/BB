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
    assert names.greeting_for("", "info@acme.com", "Acme") == "Hello Acme team,"
    assert names.greeting_for("", "info@acme.com", "Acme", word="Dear") == "Dear Acme team,"
    assert names.greeting_for("", "info@acme.com", "") == "Hello,"
    assert names.greeting_for("", "jdoe@acme.com") == "Hi there,"
    assert names.greeting_for("", "jdoe@acme.com", word="Good day") == "Good day there,"
    assert names.greeting_for("", "jdoe@acme.com", word="Dear") == "Hello,"
    assert names.greeting_for("Jane", word="Yo") == "Hi Jane,"   # unknown word falls back


def test_ensure_greeting_replaces_whatever_the_model_wrote():
    body = "Hi Jane,\n\nSaw your roofing work in Leeds.\n\nWould a quote be possible?"
    assert names.ensure_greeting(body, "Jane") == body
    assert names.ensure_greeting(body, "", "jdoe@x.com") == body.replace("Hi Jane,", "Hi there,")
    assert names.ensure_greeting("Hi ,\n\nSaw your work.", "Jane") == "Hi Jane,\n\nSaw your work."
    assert names.ensure_greeting("Hello there — saw your work in Leeds.\nMore.", "Jane") == \
        "Hi Jane,\n\nSaw your work in Leeds.\nMore."
    assert names.ensure_greeting("Dear Sir or Madam,\n\nSaw your work.", "", "info@acme.com", "Acme") == \
        "Hello Acme team,\n\nSaw your work."
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
    assert draft["body"].startswith("Hello Fast team,\n\n")
    assert "shared inbox" in writer.brain.prompts[-1]
    draft = await writer._write_personal_email(await state.get_prospect("b"))
    assert draft["body"].startswith("Hi there,\n\n") and "do not guess" in writer.brain.prompts[-1]


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
    assert sender._render(body, nameless, body=True).startswith("Hello there,\n\nSaw Acme's work.")
    shared = Prospect(id="3", email="info@acme.com", company="Acme", title="")
    assert sender._render(body, shared, body=True).startswith("Hello Acme team,\n\nSaw Acme's work.")
    # Model forgot the greeting line: it is added. Subjects are untouched.
    assert sender._render("Saw {{company}}.", named, body=True) == "Hello Jane,\n\nSaw Acme."
    assert sender._render("{{first_name}}, quick question", named) == "Jane, quick question"
    assert sender._render("{{first_name}}, quick question", nameless) == "there, quick question"
