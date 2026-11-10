"""Offline tests: no AWS credentials and no network calls."""
from unittest.mock import Mock

import pytest

from bb.mail.ses_transport import SESAPITransport
from bb.mail.providers.base import AuthError, TransientError


class Session:
    def __init__(self, client):
        self.client_obj = client

    def client(self, name, region_name):
        assert name == "sesv2"
        assert region_name == "us-east-1"
        return self.client_obj


@pytest.mark.asyncio
async def test_capacity_is_region_specific():
    client = Mock()
    client.get_account.return_value = {
        "ProductionAccessEnabled": True,
        "SendQuota": {"Max24HourSend": 10000, "SentLast24Hours": 2000, "MaxSendRate": 5},
    }
    result = await SESAPITransport(region="us-east-1", session=Session(client)).capacity()
    assert result.remaining == 8000
    assert result.production_access is True
    assert result.max_send_rate == 5


@pytest.mark.asyncio
async def test_send_raw_returns_ses_message_id():
    client = Mock()
    client.send_email.return_value = {"MessageId": "ses-id"}
    result = await SESAPITransport(region="us-east-1", session=Session(client)).send_raw(
        from_address="sender@example.test",
        recipients=["recipient@example.test"],
        raw_message=b"From: sender@example.test\r\nTo: recipient@example.test\r\n\r\nTest",
        configuration_set="bb-feedback",
    )
    assert result == "ses-id"
    assert client.send_email.call_args.kwargs["ConfigurationSetName"] == "bb-feedback"


@pytest.mark.asyncio
async def test_rejects_oversized_recipient_batch_before_network():
    client = Mock()
    transport = SESAPITransport(region="us-east-1", session=Session(client))
    with pytest.raises(ValueError):
        await transport.send_raw(from_address="sender@example.test",
                                 recipients=["x@example.test"] * 51, raw_message=b"test")
    client.send_email.assert_not_called()


def test_auth_failure_is_classified():
    class AWSFailure(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    client = Mock()
    client.get_account.side_effect = AWSFailure()
    with pytest.raises(AuthError):
        SESAPITransport._call(client, "get_account")


def test_throttle_is_classified():
    class AWSFailure(Exception):
        response = {"Error": {"Code": "TooManyRequestsException"}}

    client = Mock()
    client.get_account.side_effect = AWSFailure()
    with pytest.raises(TransientError):
        SESAPITransport._call(client, "get_account")
