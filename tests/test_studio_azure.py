"""Azure SMTP setup and Studio delivery with an isolated SMTP sink."""

import asyncio
import json
import os
import smtplib
import sqlite3
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit

import aiosmtplib
import httpx
import pytest
from bs4 import BeautifulSoup

from bb.mail import sending, service
from bb.mail.db import MIGRATIONS, MailDB
from bb.mail.jobs import Jobs
from bb.mail.providers import account_for
from bb.mail.providers.smtp import AZURE_SMTP_HOST, AZURE_SMTP_PORT, AzureSmtpAccount
from tests.test_studio_delivery import _create, _work, mail_client, studio_client  # noqa: F401
from tests.test_mailpit_transport_integration import _wait_for_mailpit

SENDER = "studio-sender@example.test"
AZURE_FORM = {"email": SENDER, "username": "bb-local-test", "client_secret": "local-only-test-password"}


@pytest.fixture
def azure_studio(studio_client, monkeypatch):
    monkeypatch.setattr(service, "account_for", account_for)
    monkeypatch.setattr(sending, "account_for", account_for)
    return studio_client


def _connect(client, **values):
    response = client.post("/mail/accounts/azure-smtp", data={**AZURE_FORM, **values})
    address = urlsplit(response.headers["location"])
    assert address.path.startswith("/mail/accounts/"), parse_qs(address.query)
    return address.path.rsplit("/", 1)[-1]


def test_azure_smtp_coexists_with_inbox_and_retains_reviewed_message(azure_studio, monkeypatch):
    client, db, secret, inbox, _ = azure_studio
    checks = []

    class MockSmtp:
        def __init__(self, host, port, *, timeout):
            checks.append(("connect", host, port))

        def starttls(self, *, context):
            assert context.check_hostname
            checks.append(("tls",))

        def login(self, username, password):
            checks.append(("login", username, password))

        def close(self):
            checks.append(("close",))

    monkeypatch.setattr(smtplib, "SMTP", MockSmtp)
    mailbox = _connect(client)
    assert mailbox != inbox
    assert asyncio.run(db.mailbox_by_address(SENDER))["id"] == inbox
    row = asyncio.run(db.mailbox(mailbox))
    assert row["mode"] == "send_only"
    assert AZURE_FORM["client_secret"] not in row["creds"]
    assert service.open_creds(secret, row)["password"] == AZURE_FORM["client_secret"]
    assert checks == [
        ("connect", AZURE_SMTP_HOST, AZURE_SMTP_PORT), ("tls",),
        ("login", AZURE_FORM["username"], AZURE_FORM["client_secret"]), ("close",),
    ]
    assert service.ready_to_send(row, manual=True)[0] and not service.ready_to_send(row)[0]
    assert AZURE_FORM["client_secret"] not in client.get(f"/mail/accounts/{mailbox}").text
    assert "no inbox to sync" in parse_qs(urlsplit(client.post(f"/mail/accounts/{mailbox}/sync")
                                                  .headers["location"]).query)["error"][0]
    assert not asyncio.run(Jobs(db).list())

    page = BeautifulSoup(client.get("/mail/compose").text, "html.parser")
    assert "Azure Email SMTP" in page.select_one(f'#manual-mailbox option[value="{mailbox}"]').text
    assert page.find("label", attrs={"for": "manual-reply-to"})
    response = _create(
        client, mailbox, subject="Original subject", reply_to="replies@example.test",
        cc="cc@example.test", bcc="private@example.test",
        files={"files": ("brief.txt", b"Test attachment", "text/plain")},
    )
    assert "draft=" in response.headers["location"]
    draft = asyncio.run(db.one("SELECT * FROM drafts"))
    assert draft["reply_to"] == "replies@example.test"

    calls = []

    async def send(message, **options):
        calls.append((message.as_bytes(), options))
        return {}, "250 accepted by fixture"

    monkeypatch.setattr(aiosmtplib, "send", send)
    action = f"/mail/drafts/{draft['id']}/send"
    assert "Sending" in client.post(action, data={"subject": "Edited subject"}).headers["location"]
    assert asyncio.run(_work(db, secret))["status"] == "sent"
    (raw, options), = calls
    assert options["hostname"] == AZURE_SMTP_HOST and options["port"] == AZURE_SMTP_PORT
    assert options["start_tls"] and not options["use_tls"]
    assert options["recipients"] == ["john.smith@example.test", "cc@example.test", "private@example.test"]
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(message["From"]) == SENDER and str(message["Reply-To"]) == "replies@example.test"
    assert message["Subject"] == "Edited subject" and message["Cc"] == "cc@example.test"
    assert message["Bcc"] is None and b"private@example.test" not in raw
    assert next(message.iter_attachments()).get_payload(decode=True) == b"Test attachment"

    asyncio.run(db.update_mailbox(mailbox, enabled=0, settings={"display_name": "Project updates"}))
    assert _connect(client, client_secret="test-rotated-secret") == mailbox
    row = asyncio.run(db.mailbox(mailbox))
    assert row["enabled"] and row["settings"]["display_name"] == "Project updates"
    assert service.open_creds(secret, row)["password"] == "test-rotated-secret"


def test_azure_personalized_drafts_require_review_and_apply_recipient_limit(azure_studio, monkeypatch):
    client, db, _, _, _ = azure_studio
    monkeypatch.setattr(AzureSmtpAccount, "_smtp_check", lambda self: None)
    mailbox = _connect(client)
    to = [f"contact{i}@example.test" for i in range(50)]
    response = _create(client, mailbox, to=", ".join(to), bcc="extra@example.test")
    assert "50 recipients" in parse_qs(urlsplit(response.headers["location"]).query)["error"][0]
    response = _create(client, mailbox, to=", ".join(to[:2]), subject="Update for {{first_name}}",
                       reply_to="team@example.test")
    assert "/mail/review" in response.headers["location"]
    drafts = asyncio.run(db.query("SELECT to_json, reply_to, status FROM drafts"))
    assert {json.loads(row["to_json"])[0]["email"] for row in drafts} == set(to[:2])
    assert all(row["status"] == "pending_review" and row["reply_to"] == "team@example.test" for row in drafts)
    assert not asyncio.run(Jobs(db).list())


@pytest.mark.asyncio
async def test_migration_preserves_deployed_microsoft_metadata_and_adds_studio(tmp_path):
    db = MailDB(tmp_path / "mail.db")
    with sqlite3.connect(db.path) as connection:
        connection.executescript("".join(MIGRATIONS[:4]))
        connection.executescript(
            "ALTER TABLE mailboxes ADD COLUMN granted_scopes TEXT NOT NULL DEFAULT '';"
            "ALTER TABLE mailboxes ADD COLUMN token_expires_at TEXT NOT NULL DEFAULT '';"
            "ALTER TABLE mailboxes ADD COLUMN last_refresh_at TEXT NOT NULL DEFAULT '';"
            "PRAGMA user_version=5;"
        )
        connection.execute(
            "INSERT INTO mailboxes(id, address, provider, creds, granted_scopes, created_at, updated_at) "
            "VALUES ('inbox', ?, 'graph', 'sealed-test-value', 'Mail.ReadWrite', '', '')", (SENDER,),
        )

    await asyncio.gather(db.init(), db.init())
    inbox = await db.mailbox_by_address(SENDER)
    assert inbox["creds"] == "sealed-test-value" and inbox["granted_scopes"] == "Mail.ReadWrite"
    mailbox = await db.save_azure_smtp(address=SENDER, creds="sealed-azure", status_detail="test", settings={})
    assert mailbox != inbox["id"]
    assert len(await db.save_studio_drafts(
        mailbox, [[{"email": "contact@example.test"}]], reply_to="", subject="Test", body="Test", options={}, files=[],
    )) == 1
    assert not await db.query("SELECT * FROM calendar_invites")


@pytest.mark.skipif(os.getenv("BB_MAILPIT_TEST") != "1", reason="requires isolated Mailpit")
def test_azure_studio_submission_reaches_local_mailpit(azure_studio, monkeypatch):
    client, db, secret, _, _ = azure_studio
    asyncio.run(_wait_for_mailpit())
    smtp = smtplib.SMTP
    send = aiosmtplib.send

    def local_check(host, port, **kwargs):
        assert (host, port) == (AZURE_SMTP_HOST, AZURE_SMTP_PORT)
        return smtp("127.0.0.1", 1025, local_hostname="localhost", **kwargs)

    async def local_send(message, **options):
        assert (options["hostname"], options["port"]) == (AZURE_SMTP_HOST, AZURE_SMTP_PORT)
        assert options["start_tls"]
        options.update(hostname="127.0.0.1", port=1025, local_hostname="localhost")
        return await send(message, **options)

    monkeypatch.setattr(smtplib, "SMTP", local_check)
    monkeypatch.setattr(aiosmtplib, "send", local_send)
    mailbox = _connect(client)
    title = "Isolated Azure SMTP delivery"
    response = _create(client, mailbox, subject=title, reply_to="qa-replies@example.test")
    draft_id = parse_qs(urlsplit(response.headers["location"]).query)["draft"][0]
    client.post(f"/mail/drafts/{draft_id}/send")
    assert asyncio.run(_work(db, secret))["status"] == "sent"
    with httpx.Client(base_url="http://127.0.0.1:8025", trust_env=False) as mailpit:
        messages = mailpit.get("/api/v1/messages?limit=100").raise_for_status().json()["messages"]
        captured, = (message for message in messages if message["Subject"] == title)
        raw = mailpit.get(f"/api/v1/message/{captured['ID']}/raw").raise_for_status().content
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(message["From"]) == SENDER and str(message["Reply-To"]) == "qa-replies@example.test"
