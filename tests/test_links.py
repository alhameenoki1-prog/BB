"""Links are clicked, never pasted: every web address in an outgoing HTML
part is a link on its own words."""
import json

import pytest

from bb.mail.models import MailboxSettings
from harvey.integrations.mail_provider import body_to_html, link_labels, render_parts


class _Outreach:
    signature_html = ""
    signature_text = ""
    signature_image = ""
    inbox_links = []
    sector_needs = []
    details_url = "https://docs.example/brief"
    details_label = "the project brief"
    intent = "x"


def test_outgoing_html_turns_addresses_into_links_on_their_own_words():
    body = ("Hi Dana,\n\nEverything is in the brief:\nhttps://docs.example/brief\n\nthe rebar spec: https://docs.example/rebar\n\n"
            "See https://greystone.example/about for the rest.\n\nJane")
    plain, html, _img = render_parts(body, _Outreach())
    assert html, "a body with an address gets an HTML part even without a signature"
    assert '<a href="https://docs.example/brief" style="color:#0a60c2;font-weight:600">View the project brief</a>' in html
    assert '<a href="https://docs.example/rebar" style="color:#0a60c2;font-weight:600">View the rebar spec</a>' in html
    assert 'See <a href="https://greystone.example/about" style="color:#0a60c2">here</a> for the rest.' in html
    assert "https://docs.example/brief</a>" not in html and ">https://" not in html      # never the address as the text
    assert "https://docs.example/brief" in plain                                        # the text part keeps it (nothing else to click)
    # A plain first email stays text only.
    assert render_parts(body, _Outreach(), plain=True)[1] == ""
    assert render_parts("Hi Dana,\n\nNo links here.\n\nJane", _Outreach())[1] == ""
    assert link_labels(_Outreach()) == {"https://docs.example/brief": "the project brief"}
    assert body_to_html("Look: https://x.example/a, then https://x.example/b.", {"https://x.example/a": "the plan"}) == \
        '<p style="margin:0 0 14px">Look: <a href="https://x.example/a" style="color:#0a60c2">the plan</a>, then <a href="https://x.example/b" style="color:#0a60c2">here</a>.</p>'
    assert body_to_html("<script>x</script> & https://x.example/") == \
        '<p style="margin:0 0 14px">&lt;script&gt;x&lt;/script&gt; &amp; <a href="https://x.example/" style="color:#0a60c2">here</a></p>'


def test_the_command_center_finalizes_with_clickable_links():
    from bb.mail.drafting import finalize
    settings = MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://greystone.example")
    plain, html = finalize("Gladly. The spec has the grades and the schedule:\nhttps://docs.example/rebar", settings,
                           greeting_name="Dana", link_labels={"https://docs.example/rebar": "the rebar spec"})
    assert '<a href="https://docs.example/rebar" style="color:#0a60c2;font-weight:600">View the rebar spec</a>' in html
    assert '<a href="https://greystone.example">View the project</a>' in html and ">https://" not in html
    assert plain.endswith("Jane\nhttps://greystone.example") and "https://docs.example/rebar" in plain


@pytest.mark.asyncio
async def test_send_draft_names_the_links_from_the_profile(tmp_path, monkeypatch):
    import yaml

    from bb.mail import crypto, sending
    from tests.test_mail_reply import ME, MailDB, SentBox, _seed_thread, service

    monkeypatch.setattr("bb.mail.sending.account_for", lambda m, c, e: SentBox(m, c, e))
    root = tmp_path / "ws"
    root.mkdir()
    (root / "harvey.yaml").write_text(yaml.safe_dump({"outreach": {"mode": "inquiry", "intent": "x", "sector_needs": [
        {"sectors": ["manufacturing"], "need": "rebar", "details_url": "https://docs.example/rebar", "details_label": "the rebar spec"}]},
        "persona": {"name": "Jane"}}))
    db = MailDB(root / "data" / "mail.db"); await db.init()
    mid = await db.add_mailbox(address=ME, provider="imap", creds=crypto.seal_json("s", {}), status="ok",
                               settings=MailboxSettings(display_name="Jane Doe", signature_html="Jane", project_link="https://acme.test").model_dump())
    tid = await _seed_thread(db, mid)
    now = service.now_iso()
    await db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                     "VALUES ('l1', ?, ?, 'reply', ?, 'Re: Site visit', 'Tuesday works. The spec is here:\nhttps://docs.example/rebar', 'approved', ?, ?)",
                     (mid, tid, json.dumps([{"name": "Ann", "email": "ann@x.example"}]), now, now))
    draft = await db.one("SELECT * FROM drafts WHERE id = 'l1'")
    assert (await sending.send_draft(db, "s", {"HARVEY_HOME": str(root)}, draft))["status"] == "sent"
    html = SentBox.sent[-1]["body_html"]
    assert ">View the rebar spec</a>" in html and ">View the project</a>" in html and ">https://" not in html
