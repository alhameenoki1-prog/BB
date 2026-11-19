"""SendGrid belongs to Studio's sender records, not legacy mailbox settings."""

import asyncio
import smtplib

from bb.mail import service
from tests.test_studio_delivery import _create, mail_client, studio_client  # noqa: F401


def test_sendgrid_sender_appears_in_studio_only_after_authentication(studio_client, monkeypatch):
    client, db, secret, _, _ = studio_client

    class SMTP:
        def __init__(self, host, port, *, timeout):
            assert (host, port) == ("smtp.sendgrid.net", 587)

        def starttls(self, *, context):
            assert context.check_hostname

        def login(self, username, password):
            assert (username, password) == ("apikey", "SG.local-test-key")

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    response = client.post("/mail/accounts/sendgrid", data={"email": "updates@example.test",
                                                        "api_key": "SG.local-test-key"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/mail/accounts/")
    mailbox = asyncio.run(db.mailbox_by_address("updates@example.test"))
    assert mailbox["provider"] == "sendgrid" and mailbox["mode"] == "send_only"
    assert service.open_creds(secret, mailbox)["password"] == "SG.local-test-key"
    assert "updates@example.test" in client.get("/mail/compose").text
    draft_response = _create(client, mailbox["id"])
    assert "/mail/compose?draft=" in draft_response.headers["location"]
