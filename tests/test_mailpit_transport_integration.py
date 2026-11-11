"""Network-isolated smoke test for BB's existing SMTP adapter.

Runs only under the sender-evaluation GitHub Actions workflow; all recipient
addresses are reserved .test examples and Mailpit has no outbound relay.
"""

import asyncio
import os
import time
from pathlib import Path

import aiosmtplib
import httpx
import pytest

from bb.mail.providers.base import PermanentError
from bb.mail.providers.imap import ImapAccount

pytestmark = pytest.mark.skipif(
    os.getenv("BB_MAILPIT_TEST") != "1",
    reason="requires the isolated Mailpit service; disabled in normal test suite",
)


@pytest.fixture(autouse=True)
def _use_stable_loopback_ehlo_hostname(monkeypatch):
    """Avoid platform-dependent FQDN lookup during the local Mailpit handshake.

    Scope this override to the isolated integration tests. Production SMTP
    clients retain aiosmtplib's normal hostname selection and TLS behavior.
    """
    original_send = aiosmtplib.send

    async def local_send(*args, **kwargs):
        assert kwargs.get("hostname") == "127.0.0.1"
        kwargs.setdefault("local_hostname", "localhost")
        return await original_send(*args, **kwargs)

    monkeypatch.setattr(aiosmtplib, "send", local_send)


async def _wait_for_mailpit() -> None:
    async with httpx.AsyncClient(timeout=3) as client:
        for _ in range(40):
            try:
                response = await client.get("http://127.0.0.1:8025/api/v1/messages")
                response.raise_for_status()
                return
            except (httpx.HTTPError, OSError):
                await asyncio.sleep(0.25)
    pytest.fail("Mailpit was not ready on localhost:8025")


@pytest.mark.asyncio
async def test_bb_smtp_adapter_delivers_40_unique_messages_to_mailpit(tmp_path: Path):
    """Test real SMTP, MIME, authenticated submission, and parallel sends."""
    await _wait_for_mailpit()
    account = ImapAccount(
        {"address": "bb-sender@example.test", "provider": "imap"},
        {
            "smtp_host": "127.0.0.1", "smtp_port": 1025,
            "username": "bb-local-test", "password": "local-only-test-password",
        },
        {},
    )
    # Mailpit is an SMTP sink, not an IMAP server. Disable only the optional
    # best-effort copy into the sender's remote IMAP Sent folder.
    account._append_sent = lambda _data: None
    attachment = tmp_path / "bb-test.txt"
    attachment.write_text("local mail test attachment")

    start = time.monotonic()
    semaphore = asyncio.Semaphore(8)

    async def send_one(n: int):
        async with semaphore:
            result = await account.send(
                to=[f"recipient-{n}@example.test"],
                subject=f"BB Mailpit controlled test {n:03d}",
                body_text=f"Consent-based local test message {n}.",
                body_html=f"<p>Consent-based local test message {n}.</p>",
                from_name="BB Test Sender",
                reply_to="replies@example.test",
                attachments=[str(attachment)] if n == 0 else [],
            )
            assert len(result) == 3
            assert result[0].startswith("<") and result[0].endswith(">")
            return result[0]

    message_ids = await asyncio.gather(*(send_one(i) for i in range(40)))
    assert len(set(message_ids)) == 40

    async with httpx.AsyncClient(timeout=10) as client:
        results = []
        for _ in range(20):
            response = await client.get("http://127.0.0.1:8025/api/v1/messages", params={"limit": 100})
            response.raise_for_status()
            data = response.json()
            results = data.get("messages", [])
            seen = {m.get("Subject") for m in results}
            if all(f"BB Mailpit controlled test {i:03d}" in seen for i in range(40)):
                break
            await asyncio.sleep(0.2)

    assert len({m.get("Subject") for m in results if (m.get("Subject") or "").startswith("BB Mailpit controlled")}) == 40
    elapsed = time.monotonic() - start
    print(f"BB Mailpit 40-message local SMTP integration: {elapsed:.3f} seconds; 0 public sends")


@pytest.mark.asyncio
async def test_bb_smtp_rejected_recipient_is_not_marked_sent():
    """Mailpit rejects recipients outside the local reserved-domain allowlist."""
    await _wait_for_mailpit()
    account = ImapAccount(
        {"address": "bb-sender@example.test", "provider": "imap"},
        {"smtp_host": "127.0.0.1", "smtp_port": 1025, "username": "bb-local-test", "password": "local-only-test-password"},
        {},
    )
    account._append_sent = lambda _data: None
    with pytest.raises(PermanentError):
        await account.send(
            to=["outside@example.invalid"], subject="Should be rejected", body_text="Local-only negative test", body_html=""
        )
