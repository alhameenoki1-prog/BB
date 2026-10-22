"""Links are clicked, never pasted: every web address in an outgoing HTML
part is a link on its own words."""
import json

import pytest

from bb.mail.models import MailboxSettings
from harvey.integrations.mail_provider import body_to_html, link_labels, render_parts
from tests.test_bb_web import client, settings  # noqa: F401
from tests.test_mail_foundation import mail_client  # noqa: F401


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
    # The address under "Everything is in the brief:" goes into that sentence, on "the brief".
    assert 'Everything is in <a href="https://docs.example/brief" style="color:#0a60c2">the brief</a>.' in html
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
    assert 'Gladly. <a href="https://docs.example/rebar" style="color:#0a60c2">The spec</a> has the grades and the schedule.' in html
    plain, html = finalize("Gladly, it has the grades.\n\nhttps://docs.example/rebar", settings,
                           greeting_name="Dana", link_labels={"https://docs.example/rebar": "the rebar spec"})
    assert '<a href="https://docs.example/rebar" style="color:#0a60c2;font-weight:600">View the rebar spec</a>' in html   # nothing names it
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
    assert 'The spec is <a href="https://docs.example/rebar" style="color:#0a60c2">here</a>' not in html
    assert ">The spec</a> is here." in html and ">View the project</a>" in html and ">https://" not in html



def test_the_link_goes_into_the_sentence_that_names_the_document():
    """The draft a person saw: the brief's address on a line of its own
    after "here's the project brief". It goes out as a link on those words."""
    body = ("Thanks, David, and thanks for sending over the labor rates and the scope you currently cover.\n\n"
            "I'll call the office to get the account set up. So you have the service description ahead of that call, "
            "here's the project brief with the full scope of what we need. It's the quickest way for you to check it "
            "against what United California covers and see whether it's work you'd take on.\n\n"
            "https://glaze-icing-26700938.figma.site/\n\n"
            "If you get a chance to look through it before we talk, scheduling should be much simpler.")
    html = body_to_html(body, {"https://glaze-icing-26700938.figma.site/": "the full scope"})
    assert ("here&#x27;s <a href=\"https://glaze-icing-26700938.figma.site/\" style=\"color:#0a60c2\">the project brief</a> "
            "with the full scope of what we need.") in html
    assert "figma.site/</a>" not in html and html.count("<a ") == 1 and html.count("<p ") == 3     # the address's line is gone
    # Other ways of putting it.
    assert body_to_html("Here's the project brief: https://d.example/b") == \
        '<p style="margin:0 0 14px">Here&#x27;s <a href="https://d.example/b" style="color:#0a60c2">the project brief</a>.</p>'
    assert "If you&#x27;d like <a" in body_to_html("If you'd like the details, here they are:\nhttps://d.example/d")
    # Their document is not ours: "your proposal" is never the link.
    assert ">View here</a>" in body_to_html("Thanks for your proposal.\n\nhttps://d.example/x")
    # Two addresses in a row, a blank line between: nothing lost, nothing doubled.
    two = body_to_html("Here is the brief.\n\nhttps://d.example/a\nhttps://d.example/b")
    assert two.count("<a ") == 2 and ">the brief</a>" in two
    # The text part keeps the address: plain text has nothing else to click.
    from bb.mail.drafting import finalize
    plain, _ = finalize(body, MailboxSettings(display_name="Jane", signature_html="Jane"), greeting_name="David")
    assert "https://glaze-icing-26700938.figma.site/" in plain


# ── the editors show it as it will be sent ──

def test_the_thread_and_review_pages_preview_the_draft_as_sent(mail_client, monkeypatch):
    import asyncio

    from markupsafe import escape as _esc

    from bb.accounts import Accounts
    from bb.workspaces import Workspace
    from tests.test_bb_web import USER, _accept_key, _as_user
    from tests.test_mail_reply import ME, _seed_thread

    client = mail_client
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    client.post("/apikey", data={"api_key": "sk-ant-abcdefghijklmnop"})
    client.post("/mail/accounts/imap", data={"email": ME, "password": "pw"})
    db = ws.mail_db()
    mid = asyncio.run(db.mailboxes())[0]["id"]
    client.post(f"/mail/accounts/{mid}", data={"display_name": "Jane", "project_link": "https://acme.test", "signature_html": "Jane"})
    tid = asyncio.run(_seed_thread(db, mid))
    body = "Here's the project brief with the full scope of what we need.\n\nhttps://glaze-icing-26700938.figma.site/"
    asyncio.run(db.execute("INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, subject, body, status, created_at, updated_at) "
                           "VALUES ('p1', ?, ?, 'reply', ?, 'Re: Service Inquiry', ?, 'pending_review', '2026-01-01T00:00:00Z', "
                           "'2026-01-01T00:00:00Z')", (mid, tid, json.dumps([{"name": "David Kimel", "email": "davidk@ucgd.example"}]), body)))
    anchor = '<a href="https://glaze-icing-26700938.figma.site/" style="color:#0a60c2">the project brief</a>'
    page = client.get(f"/mail/threads/{tid}").text
    assert "How it looks when sent" in page and 'class="as-sent-frame"' in page and 'sandbox="allow-same-origin allow-popups' in page
    assert str(_esc(anchor)) in page and str(_esc("Hi David,")) in page        # the preview is the email as it goes out
    assert 'data-preview-url="/mail/drafts/p1/preview"' in page and 'data-source="draft-body-p1"' in page
    # Typing in the editor asks for the preview again.
    doc = client.post("/mail/drafts/p1/preview", data={"body": "All in the brief:\nhttps://glaze-icing-26700938.figma.site/"}).json()["doc"]
    assert 'All in <a href="https://glaze-icing-26700938.figma.site/" style="color:#0a60c2">the brief</a>.' in doc
    assert doc.startswith("<!doctype html>") and '<base target="_blank">' in doc
    assert client.post("/mail/drafts/missing/preview", data={"body": "x"}).status_code == 404
    # The Review page shows it too.
    assert str(_esc(anchor)) in client.get("/mail/review").text


def test_the_outbox_shows_each_email_as_it_goes_out(client, monkeypatch):
    import sqlite3

    from markupsafe import escape as _esc

    from bb.accounts import Accounts
    from bb.workspaces import Workspace
    from tests.test_bb_web import PROFILE, USER, _as_user

    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    client.post("/profile", data=PROFILE)
    state = ws.state()
    import asyncio
    asyncio.run(state.init_db())
    con = sqlite3.connect(str(ws.db_path))
    body = "Hi Dana,\n\nThe full scope is in the project brief:\nhttps://docs.example/brief\n\nJane"
    for oid, step in (("f1", 1), ("f2", 2)):
        con.execute("INSERT INTO outbox (id, prospect_id, to_email, subject, body, status, kind, step) "
                    "VALUES (?, '', 'dana@x.example', 'Towson', ?, 'pending_review', 'sequence', ?)", (oid, body, step))
    con.commit()
    page = client.get("/outbox").text
    anchor = '<a href="https://docs.example/brief" style="color:#0a60c2">the project brief</a>'
    assert str(_esc(anchor)) in page                                     # the follow-up, as it goes out
    assert "A first email goes as plain text" in page                       # the first one says why it shows the address
    doc = client.post("/outbox/f2/preview", data={"body": "Hi Dana,\n\nHere is the brief.\n\nhttps://docs.example/brief"}).json()["doc"]
    assert ">the brief</a>." in doc
    assert client.post("/outbox/f1/preview", data={"body": body}).json()["plain"] is True
