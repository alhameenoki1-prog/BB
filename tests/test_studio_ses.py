"""SES setup, reviewed Studio send and feedback reconciliation with mocked AWS."""

import asyncio
import json
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest

from bb.mail import sending, ses
from bb.mail.jobs import Jobs
from bb.mail.providers import account_for
from bb.mail.ses_auth import AssumedRole
from tests.test_studio_delivery import ADDRESS, _create, _work, studio_client, mail_client  # noqa: F401

ACCOUNT = "123456789012"
REGION = "us-east-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/StudioSender"
QUEUE = f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/StudioFeedback"


@pytest.fixture
def aws(studio_client, monkeypatch):
    client, _, _, _, _ = studio_client
    client.settings.ses_principal_arn = "arn:aws:iam::111122223333:role/BB"
    monkeypatch.setattr(ses, "available", lambda _settings: True)
    monkeypatch.setattr(sending, "account_for", account_for)
    mock_ses, mock_sqs = Mock(), Mock()
    mock_ses.get_account.return_value = {
        "SendingEnabled": True, "ProductionAccessEnabled": True,
        "SendQuota": {"Max24HourSend": 8000, "SentLast24Hours": 250, "MaxSendRate": 10},
    }
    mock_ses.get_email_identity.return_value = {"VerifiedForSendingStatus": True}
    mock_ses.send_email.return_value = {"MessageId": "provider-1"}
    mock_sqs.get_queue_attributes.return_value = {
        "Attributes": {"QueueArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:StudioFeedback"},
    }
    session = Mock()
    session.client.side_effect = lambda kind, **kwargs: {"sesv2": mock_ses, "sqs": mock_sqs}[kind]
    assumed_ids = []

    async def assume(*, role_arn, external_id, account_id, region):
        assert (role_arn, account_id, region) == (ROLE, ACCOUNT, REGION)
        assert len(external_id) >= 24
        assumed_ids.append(external_id)
        return AssumedRole(account_id, role_arn, region, session)

    monkeypatch.setattr(ses, "assume_ses_role_async", assume)
    return mock_ses, mock_sqs, assumed_ids


def test_ses_coexists_with_inbox_sends_reviewed_mime_and_matches_events(studio_client, aws):
    client, db, secret, original, _ = studio_client
    mock_ses, mock_sqs, ids = aws
    setup = client.get("/mail/ses")
    assert "IAM trust policy" in setup.text and "111122223333" in setup.text
    response = client.post("/mail/ses", data={
        "address": ADDRESS, "account_id": ACCOUNT, "role_arn": ROLE, "region": REGION,
        "configuration_set": "studio-events", "queue_url": QUEUE,
    })
    assert response.headers["location"].startswith("/mail/ses?mailbox=")
    mailbox = asyncio.run(db.mailbox_by_address(ADDRESS, ses=True))
    assert mailbox["id"] != original and mailbox["mode"] == "send_only"
    assert (asyncio.run(db.mailbox_by_address(ADDRESS)))["id"] == original
    assert "7,750 of 8,000" in client.get(response.headers["location"]).text
    mock_ses.send_email.assert_not_called()

    response = _create(client, mailbox["id"], cc="cc@example.test", bcc="bcc@example.test", subject="SES test",
                       files={"files": ("brief.txt", b"Project scope", "text/plain")})
    assert "draft=" in response.headers["location"]
    draft = asyncio.run(db.one("SELECT * FROM drafts"))
    client.post(f"/mail/drafts/{draft['id']}/send")
    assert asyncio.run(_work(db, secret))["status"] == "sent"
    payload = mock_ses.send_email.call_args.kwargs
    assert payload["Destination"] == {"ToAddresses": ["john.smith@example.test"], "CcAddresses": ["cc@example.test"],
                                      "BccAddresses": ["bcc@example.test"]}
    assert payload["ConfigurationSetName"] == "studio-events"
    mime = BytesParser(policy=policy.default).parsebytes(payload["Content"]["Raw"]["Data"])
    assert mime["From"] == ADDRESS and mime["Bcc"] is None
    assert next(mime.iter_attachments()).get_payload(decode=True) == b"Project scope"

    def feedback(message_id, ses_id):
        return {"MessageId": message_id, "ReceiptHandle": f"receipt-{message_id}", "Body": json.dumps({
            "eventType": "Bounce", "mail": {"messageId": ses_id, "source": ADDRESS, "sendingAccountId": ACCOUNT},
            "bounce": {"bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": "john.smith@example.test"}]},
        })}

    known, unknown = feedback("known", "provider-1"), feedback("unknown", "unrelated")
    mock_sqs.receive_message.side_effect = [{"Messages": [known, unknown]}, {"Messages": [known]}, {}]
    assert asyncio.run(ses.poll_feedback(db, secret, {}, mailbox)) == {"events": 1}
    assert [c.kwargs["ReceiptHandle"] for c in mock_sqs.delete_message.call_args_list] == ["receipt-known"] * 2
    assert len(set(ids)) == 1
    assert "hard bounce" in client.get("/mail/ses").text

    assert "draft=" in _create(client, mailbox["id"], subject="Blocked bounce").headers["location"]
    held = asyncio.run(db.one("SELECT id FROM drafts WHERE subject = 'Blocked bounce'"))
    client.post(f"/mail/drafts/{held['id']}/send")
    assert asyncio.run(_work(db, secret))["status"] == "held"
    assert mock_ses.send_email.call_count == 1


def test_ses_never_assumes_a_role_for_another_account_or_uses_an_unrelated_queue(studio_client, aws):
    client, db, _, _, _ = studio_client
    _, _, ids = aws
    form = {"address": ADDRESS, "account_id": ACCOUNT, "role_arn": ROLE, "region": REGION,
            "configuration_set": "studio-events", "queue_url": QUEUE}
    result = client.post("/mail/ses", data={**form, "account_id": "000000000000"})
    assert "role%20ARN%20must%20belong" in result.headers["location"]
    result = client.post("/mail/ses", data={**form, "queue_url": QUEUE.replace(ACCOUNT, "000000000000")})
    assert "standard%20SQS%20queue%20URL" in result.headers["location"]
    assert not ids and not asyncio.run(Jobs(db).list())


@pytest.mark.parametrize("error_code", ["NotFoundException", "AccessDeniedException"])
def test_ses_accepts_sender_under_verified_parent_domain(studio_client, aws, error_code):
    client, db, _, _, _ = studio_client
    mock_ses, _, _ = aws
    address = "ann@news.example.test"

    class IdentityUnavailable(Exception):
        response = {"Error": {"Code": error_code}}

    def identity(*, EmailIdentity):
        if EmailIdentity == "example.test":
            return {"VerifiedForSendingStatus": True}
        raise IdentityUnavailable()

    mock_ses.get_email_identity.side_effect = identity
    response = client.post("/mail/ses", data={
        "address": address, "account_id": ACCOUNT, "role_arn": ROLE, "region": REGION,
    })
    assert response.headers["location"].startswith("/mail/ses?mailbox=")
    assert asyncio.run(db.mailbox_by_address(address, ses=True))
    assert [call.kwargs["EmailIdentity"] for call in mock_ses.get_email_identity.call_args_list] == [
        address, "news.example.test", "example.test",
    ]
