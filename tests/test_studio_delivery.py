"""Exercise Studio from HTTP draft creation through the worker and mail adapter."""

import asyncio
import base64
import json
import os
from email import policy
from email.parser import BytesParser

import aiosmtplib
import httpx
import pytest

from bb.accounts import Accounts
from bb.mail import sending, service
from bb.mail.jobs import Jobs
from bb.mail.providers.imap import ImapAccount
from bb.mail.worker import Handlers
from bb.workspaces import Workspace
from tests.test_bb_web import USER, _as_user
from tests.test_mail_foundation import mail_client  # noqa: F401
from tests.test_mailpit_transport_integration import _wait_for_mailpit

ADDRESS = "studio-sender@example.test"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/Jx8AAAAASUVORK5CYII=")


@pytest.fixture
def studio_client(mail_client, monkeypatch):
    _as_user(mail_client)
    user = Accounts(mail_client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(mail_client.settings, user)
    db = ws.mail_db()
    secret = mail_client.settings.secret_key()
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: 1)
    monkeypatch.setattr(ImapAccount, "_append_sent", lambda *_: None)
    monkeypatch.setattr(sending, "account_for", ImapAccount)
    asyncio.run(db.init())
    mailbox = asyncio.run(db.add_mailbox(
        address=ADDRESS, provider="imap", mode="limited",
        creds=service.crypto.seal_json(secret, {"smtp_host": "127.0.0.1", "smtp_port": 1025,
                                               "username": "bb-test", "password": "local-test"}),
        settings={"display_name": "BB, Studio", "signature_html": "<b>Studio Team</b>"}, status="ok",
    ))
    return mail_client, db, secret, mailbox, ws


def _create(client, mailbox, *, files=None, **values):
    return client.post("/mail/compose", files=files, data={
        "compose_mode": "manual", "mailbox": mailbox, "message_format": "html",
        "to": "john.smith@example.test", "subject": "Project scope", "body": "<p>Review the brief.</p>",
        **values,
    })


async def _work(db, secret):
    queue = Jobs(db)
    job = await queue.claim()
    assert job["kind"] == "send_draft"
    outcome = await Handlers(db, queue, secret, {}).send_draft(job)
    await queue.complete(job["id"], **outcome)
    return outcome


def test_studio_uploads_and_saved_files_survive_the_review_queue(studio_client, monkeypatch):
    client, db, secret, mailbox, ws = studio_client
    ws.add_attachment("saved.txt", b"Original saved file")
    response = _create(
        client, mailbox, files=[("files", ("uploaded.pdf", b"Test PDF bytes", "application/pdf"))],
        to="john.smith@example.test, ann.lee@example.test", cc="cc@example.test", bcc="bcc@example.test",
        saved_files="saved.txt", body='<p style="color: red" onclick="unsafe()">Hi team</p><script>bad()</script>',
    )
    url = response.headers["location"]
    assert "/mail/compose?draft=" in url
    draft = asyncio.run(db.one("SELECT * FROM drafts"))
    assert draft["status"] == "pending_review"
    page = client.get(url).text
    assert "Gmail-style" in page and "Outlook-style" in page
    assert all(text in page for text in ("ann.lee@example.test", "cc@example.test", "bcc@example.test", "uploaded.pdf", "saved.txt"))
    assert asyncio.run(Jobs(db).list()) == []
    ws.add_attachment("saved.txt", b"Changed after review")

    sent = []

    async def smtp(message, **options):
        sent.append((message.as_bytes(), options["recipients"]))
        return {}, "accepted"

    monkeypatch.setattr(aiosmtplib, "send", smtp)
    action = f"/mail/drafts/{draft['id']}/send"
    assert "Sending" in client.post(action).headers["location"]
    assert "already%20queued" in client.post(action).headers["location"]
    assert len(asyncio.run(Jobs(db).list())) == 1
    assert asyncio.run(_work(db, secret))["status"] == "sent"
    assert len(sent) == 1
    raw, envelope = sent[0]
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert envelope == ["john.smith@example.test", "ann.lee@example.test", "cc@example.test", "bcc@example.test"]
    assert message["From"].addresses[0].display_name == "BB, Studio"
    assert "bcc@example.test" not in raw.decode() and message["Cc"] == "cc@example.test"
    html = message.get_body(("html",)).get_content()
    assert "Hi team" in html and "onclick" not in html and "bad()" not in html
    assert {part.get_filename(): part.get_payload(decode=True) for part in message.iter_attachments()} == {
        "uploaded.pdf": b"Test PDF bytes", "saved.txt": b"Original saved file",
    }
    assert all(part["Content-Transfer-Encoding"] == "base64" for part in message.iter_attachments())


def test_personalized_drafts_use_private_addresses_and_matching_preview(studio_client):
    client, db, _, mailbox, _ = studio_client
    response = _create(client, mailbox, to="john.smith@example.test, ann.lee@example.test",
                       subject="Hello {{first_name}}", body="<p>{{name}} · {{email}}</p>")
    assert response.headers["location"].startswith("/mail/review")
    drafts = asyncio.run(db.query("SELECT * FROM drafts"))
    assert len(drafts) == 2
    for draft in drafts:
        contact, = json.loads(draft["to_json"])
        expected = {"john.smith@example.test": "Hello John", "ann.lee@example.test": "Hello Ann"}[contact["email"]]
        preview = client.post(f"/mail/drafts/{draft['id']}/preview", data={"client": "outlook"}).json()["doc"]
        assert expected in preview and contact["email"] in preview
        assert "Segoe UI" in preview and "mailto:" not in preview
        other = {"john.smith@example.test", "ann.lee@example.test"} - {contact["email"]}
        assert other.pop() not in preview
    response = _create(client, mailbox, to="john.smith@example.test, ann.lee@example.test",
                       body="{{name}}", bcc="bcc@example.test")
    assert "Leave%20Cc%20and%20Bcc%20empty" in response.headers["location"]
    assert not asyncio.run(Jobs(db).list())


def test_excluded_copy_recipient_holds_message_before_submission(studio_client, monkeypatch):
    client, db, secret, mailbox, _ = studio_client
    asyncio.run(db.update_mailbox(mailbox, settings={"excluded": ["private@example.test"]}))
    response = _create(client, mailbox, bcc="private@example.test")
    assert "draft=" in response.headers["location"]
    draft = asyncio.run(db.one("SELECT * FROM drafts"))
    monkeypatch.setattr(aiosmtplib, "send", lambda *_args, **_kw: pytest.fail("Must not send an excluded copy"))
    client.post(f"/mail/drafts/{draft['id']}/send")
    assert asyncio.run(_work(db, secret))["status"] == "held"


def test_template_and_workspace_contact_data_are_available_in_studio(studio_client, monkeypatch):
    client, db, _, mailbox, _ = studio_client
    response = client.post("/mail/studio/templates/save", data={
        "template_name": "Project check-in", "message_format": "text",
        "subject": "Question for {{company}}", "body": "Hi {{first_name}}, could we review the scope?",
    })
    assert "Saved%20template" in response.headers["location"]
    assert "Project check-in" in client.get("/mail/compose").text

    async def contacts(_self, addresses):
        assert addresses == ["john.smith@example.test"]
        return {addresses[0]: {"first_name": "Jonathan", "last_name": "Smith", "company": "Example Engineering"}}

    monkeypatch.setattr(Workspace, "studio_contacts", contacts)
    response = _create(client, mailbox, message_format="text", subject="Question for {{company}}", body="Hi {{first_name}}.")
    assert "draft=" in response.headers["location"]
    draft = asyncio.run(db.one("SELECT * FROM drafts"))
    preview = client.post(f"/mail/drafts/{draft['id']}/preview").json()["doc"]
    assert "Question for Example Engineering" in preview and "Hi Jonathan." in preview
    assert "Template%20deleted" in client.post("/mail/studio/templates/delete",
                                               data={"template_name": "Project check-in"}).headers["location"]


@pytest.mark.skipif(os.getenv("BB_MAILPIT_TEST") != "1", reason="requires isolated Mailpit")
def test_studio_inline_image_reaches_local_smtp_after_review(studio_client, monkeypatch):
    client, db, secret, mailbox, _ = studio_client
    asyncio.run(_wait_for_mailpit())
    original_send = aiosmtplib.send

    async def loopback_send(*args, **options):
        assert options["hostname"] == "127.0.0.1"
        options["local_hostname"] = "localhost"
        return await original_send(*args, **options)

    monkeypatch.setattr(aiosmtplib, "send", loopback_send)
    response = _create(client, mailbox, files={"inline_image": ("invite.png", PNG, "image/png")},
                       message_format="image", subject="BB Studio Mailpit image",
                       body="", image_link="https://example.test/project")
    assert "draft=" in response.headers["location"]
    draft = asyncio.run(db.one("SELECT * FROM drafts"))
    preview = client.post(f"/mail/drafts/{draft['id']}/preview").json()["doc"]
    assert "data:image/png;base64," in preview and 'href="https://example.test/project"' in preview
    client.post(f"/mail/drafts/{draft['id']}/send")
    assert asyncio.run(_work(db, secret))["status"] == "sent"
    with httpx.Client(base_url="http://127.0.0.1:8025", trust_env=False) as mailpit:
        messages = mailpit.get("/api/v1/messages").raise_for_status().json()["messages"]
        received = next(row for row in messages if row["Subject"] == "BB Studio Mailpit image")
        raw = mailpit.get(f"/api/v1/message/{received['ID']}/raw").raise_for_status().content
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert 'href="https://example.test/project"' in message.get_body(("html",)).get_content()
    image = next(part for part in message.walk() if part.get_content_type() == "image/png")
    assert image["Content-ID"] == "<studio-image>" and image.get_content_disposition() == "inline"
    assert image.get_payload(decode=True) == PNG
