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
