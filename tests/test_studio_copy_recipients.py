"""Studio Cc/Bcc stays private across MIME construction and SMTP envelopes."""
from email.parser import BytesParser
from email import policy

import pytest

from bb.mail.composer import parse_copy_recipients
from bb.mail.providers.base import MailAccount


@pytest.mark.parametrize("raw,expected", [
    ("a@example.test, B@example.test", ["a@example.test", "b@example.test"]),
    ("a@example.test; b@example.test", ["a@example.test", "b@example.test"]),
    ("a@example.test, a@example.test", ["a@example.test"]),
    ("", []),
])
def test_copy_recipient_parser(raw, expected):
    assert parse_copy_recipients(raw, field_name="Cc") == expected


@pytest.mark.parametrize("invalid", [
    "bad", "a@example.test\r\nBcc: victim@example.test", "a@example.test,",
    "a@example.test, not-an-address", "a@example.test, " * 25,
])
def test_copy_recipient_rejects_invalid_input(invalid):
    with pytest.raises(ValueError):
        parse_copy_recipients(invalid, field_name="Bcc")


def test_mime_builds_cc_and_bcc_for_provider_specific_delivery():
    account = MailAccount({"address": "sender@example.test"}, {}, {})
    msg = account.build_mime(
        to=["to@example.test"], cc=["copy@example.test"], bcc=["hidden@example.test"],
        subject="Meeting", body_text="Hello", body_html="<p>Hello</p>",
    )
    parsed = BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
    assert parsed["To"] == "to@example.test"
    assert parsed["Cc"] == "copy@example.test"
    assert parsed["Bcc"] == "hidden@example.test"
    assert parsed.get_body(preferencelist=("html",)) is not None


@pytest.mark.asyncio
async def test_smtp_envelope_includes_bcc_but_transmitted_message_does_not(monkeypatch):
    from bb.mail.providers.imap import ImapAccount
    import aiosmtplib

    observed = {}
    async def fake_send(msg, **kwargs):
        observed["recipients"] = kwargs["recipients"]
        observed["headers"] = msg.as_string()
        return {}, "ok"

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    account = ImapAccount(
        {"address": "sender@example.test"},
        {"smtp_host": "127.0.0.1", "smtp_port": 1025,
         "username": "sender@example.test", "password": "fake-test-only"}, {},
    )
    await account.send(
        to=["to@example.test"], cc=["copy@example.test"], bcc=["hidden@example.test"],
        subject="Meeting", body_text="Hi", body_html="<p>Hi</p>",
    )
    assert observed["recipients"] == ["to@example.test", "copy@example.test", "hidden@example.test"]
    assert "Cc: copy@example.test" in observed["headers"]
    assert "Bcc:" not in observed["headers"]
    assert "hidden@example.test" not in observed["headers"]
