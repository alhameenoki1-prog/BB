"""The mail command center drafts from the profile's needs: the scope
context in the prompt, the right full-scope link once per thread, and the
quality check that does not count that link as a stray web link."""
import pytest

from bb.mail import scope as scope_mod
from bb.mail.drafting import draft_reply, finalize
from bb.mail.models import MailboxSettings

from tests.test_mail_reply import ME, FakeBrain

CFG = {"persona": {"name": "Thomas Ryan", "role": "CEO", "company": "Greystone Development"},
       "outreach": {"mode": "inquiry", "intent": "Services and materials for a 24-unit block in Towson.",
                    "sender_line": "I run Greystone Development, a developer in Baltimore.",
                    "sector_needs": [
                        {"sectors": ["construction"], "need": "A builder for a 24-unit block in Towson, drawings done, start in April.",
                         "details_url": "https://docs.example/towson", "details_label": "the Towson brief"},
                        {"sectors": ["manufacturing"], "need": "About 40 tons of rebar a month for a year, delivered to a Baltimore site.",
                         "details_url": "https://docs.example/rebar", "details_label": "the rebar spec"}],
                    "ask": "zoom", "reply_instructions": "Never discuss the budget by email."}}


def test_scope_from_the_profile_and_link_picking():
    scope = scope_mod.from_config(CFG)
    assert scope["mode"] == "inquiry" and scope["first"] == "Thomas" and [l["url"] for l in scope["links"]] == [
        "https://docs.example/towson", "https://docs.example/rebar"]
    block = scope_mod.context_block(scope)
    assert "YOU ARE THE CUSTOMER HERE" in block and "[1] the Towson brief for construction" in block and "[SCOPE LINK n]" in block
    assert "a short video call" in block and "Never discuss the budget" in block and "I run Greystone Development" in block
    assert scope_mod.context_block(scope_mod.from_config({"outreach": {"mode": "offer"}})) == ""
    assert scope_mod.from_config(None)["links"] == [] and scope_mod.load("/nonexistent/folder")["mode"] == "offer"
    assert scope_mod.pick_link(scope, "x [SCOPE LINK 2] y")["url"] == "https://docs.example/rebar"
    assert scope_mod.pick_link(scope, "no marker", "we distribute rebar and steel")["url"] == "https://docs.example/rebar"
    assert scope_mod.pick_link(scope, "no marker", "our building site and the drawings")["url"] == "https://docs.example/towson"
    body, url = scope_mod.apply_scope_link("Here it is:\n[SCOPE LINK 1]", scope, [], inbound_text="send specs")
    assert url == "https://docs.example/towson" and body == "Here it is:\nhttps://docs.example/towson"
    body, url = scope_mod.apply_scope_link("Here it is:\n[SCOPE LINK 1]", scope, ["sent before https://docs.example/towson"])
    assert url == "" and "docs.example" not in body


@pytest.mark.asyncio
async def test_a_reply_run_draft_carries_the_scope_link_once():
    scope = scope_mod.from_config(CFG)
    settings = MailboxSettings(display_name="Thomas Ryan", signature_html="Thomas", project_link="https://greystone.example")
    mailbox, thread = {"address": ME}, {"subject": "Rebar supply for Baltimore"}
    messages = [{"direction": "inbound", "from_name": "Dana", "from_email": "dana@steel.example",
                 "body_text": "Sure, send over what you have and I will price it.", "body": "...", "sent_at": "2026-10-21T10:00:00"}]
    brain = FakeBrain([{"body": "Hi Dana,\n\nGladly. It's about 40 tons of rebar a month for a year, delivered to our Baltimore "
                                "site. The grades, lengths and the monthly schedule are all in the spec, so you can price it "
                                "properly:\n[SCOPE LINK 2]\n\nIf it fits, a short call this week would be the easiest way to go "
                                "through delivery.\n\nThomas", "confidence": "high", "confidence_reason": "clear ask"}])
    result = await draft_reply(brain, settings, mailbox, thread, messages,
                               instructions="send the scope link when they ask", scope=scope)
    system, user = brain.prompts[-1]
    assert "YOU ARE THE CUSTOMER HERE" in system and "[2] the rebar spec for manufacturing" in system
    assert "send the scope link" in user
    assert result["scope_link"] == "https://docs.example/rebar" and result["body"].count("https://docs.example/rebar") == 1
    assert "[SCOPE LINK" not in result["body"] and result["qa"]["ok"], result["qa"]

    # Already sent in this thread: the marker and its lead-in go, nothing else changes.
    messages.append({"direction": "outbound", "from_email": ME, "body_text": result["body"], "body": result["body"],
                     "sent_at": "2026-10-21T11:00:00"})
    messages.append({"direction": "inbound", "from_email": "dana@steel.example", "body_text": "Got the spec. Two deliveries a month?",
                     "body": "x", "sent_at": "2026-10-21T12:00:00"})
    brain.answers = [{"body": "Hi Dana,\n\nTwo a month works for us, the site can take a delivery on the first and the "
                              "fifteenth. It's all in the spec:\n[SCOPE LINK 2]\n\nThomas",
                      "confidence": "high", "confidence_reason": ""}]
    result2 = await draft_reply(brain, settings, mailbox, thread, messages, scope=scope)
    assert result2["scope_link"] == "" and "docs.example" not in result2["body"]
    assert result2["body"] == "Two a month works for us, the site can take a delivery on the first and the fifteenth."

    # An offer-mode workspace is untouched: no scope block, no marker handling.
    brain.answers = [{"body": "Hi Dana,\n\nTuesday works, I'll bring the drawings and the schedule for the whole site.\n\nThomas",
                      "confidence": "high", "confidence_reason": ""}]
    plain = await draft_reply(brain, settings, mailbox, thread, messages, scope=scope_mod.from_config({"outreach": {"mode": "offer"}}))
    assert "YOU ARE THE CUSTOMER" not in brain.prompts[-1][0] and plain["scope_link"] == ""

    # finalize never doubles a link the body already carries.
    text, html = finalize("Everything is at https://greystone.example already, have a look.", settings, greeting_name="Dana")
    assert text.count("https://greystone.example") == 1
