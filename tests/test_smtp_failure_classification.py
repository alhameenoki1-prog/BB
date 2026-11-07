"""SMTP failure-classification contract; all deliveries are mocked and offline."""

import aiosmtplib
import pytest

from bb.mail.providers.base import AuthError, PermanentError, TransientError
from bb.mail.providers.imap import ImapAccount


def _account():
    account = ImapAccount(
        {"address": "sender@example.test", "provider": "imap"},
        {"smtp_host": "localhost", "smtp_port": 1025, "username": "test", "password": "test"},
        {},
    )
    account._append_sent = lambda _: None
    return account


async def _send(account):
    return await account.send(
        to=["recipient@example.test"],
        subject="BB offline error-classification test",
        body_text="This is not sent to the public internet.",
        body_html="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "error_class"),
    [
        (421, TransientError),
        (451, TransientError),
        (550, PermanentError),
        (554, PermanentError),
    ],
)
async def test_smtp_recipient_failure_classification(monkeypatch, code, error_class):
    async def refused(*args, **kwargs):
        raise aiosmtplib.SMTPRecipientsRefused([
            aiosmtplib.SMTPRecipientRefused(code, "test recipient rejected", "recipient@example.test")
        ])

    monkeypatch.setattr(aiosmtplib, "send", refused)
    with pytest.raises(error_class):
        await _send(_account())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "error_class"),
    [(450, TransientError), (550, PermanentError)],
)
async def test_smtp_response_failure_classification(monkeypatch, code, error_class):
    async def refused(*args, **kwargs):
        raise aiosmtplib.SMTPDataError(code, "test data rejected")

    monkeypatch.setattr(aiosmtplib, "send", refused)
    with pytest.raises(error_class):
        await _send(_account())


@pytest.mark.asyncio
async def test_smtp_auth_is_reconnect_not_delivery_retry(monkeypatch):
    async def denied(*args, **kwargs):
        raise aiosmtplib.SMTPAuthenticationError(535, "wrong password")

    monkeypatch.setattr(aiosmtplib, "send", denied)
    with pytest.raises(AuthError):
        await _send(_account())


@pytest.mark.asyncio
async def test_smtp_partial_acceptance_is_never_retried_blindly(monkeypatch):
    async def partial(*args, **kwargs):
        return {"recipient@example.test": (550, "refused")}, "250 partly accepted"

    monkeypatch.setattr(aiosmtplib, "send", partial)
    with pytest.raises(PermanentError, match="partially|some recipients"):
        await _send(_account())


@pytest.mark.asyncio
async def test_smtp_local_success_has_message_id(monkeypatch):
    async def accepted(*args, **kwargs):
        return {}, "250 accepted locally"

    monkeypatch.setattr(aiosmtplib, "send", accepted)
    msg_id, provider_id, thread = await _send(_account())
    assert msg_id.startswith("<") and msg_id.endswith(">")
    assert provider_id == thread == ""
