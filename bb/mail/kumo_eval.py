"""Offline-only KumoMTA HTTP transport experiment for BB.

This is NOT registered as a production mailbox provider or exposed in routes.
Both the endpoint and recipients are restricted to loopback/reserved .test
addresses, so benchmarking cannot send to arbitrary public mailboxes.
"""

import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import urlsplit

import httpx

from bb.mail.providers.base import AuthError, PermanentError, TransientError

_TEST_ADDRESS = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.-]+\.test$")


@dataclass(frozen=True)
class SinkReceipt:
    message_id: str
    accepted_recipients: int


class KumoLocalSink:
    """Test BB's MIME/HTTP submission against a local Kumo null-queue server."""

    def __init__(self, endpoint: str = "http://127.0.0.1:8005/api/inject/v1"):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.port != 8005 or parsed.path != "/api/inject/v1"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("The evaluation transport accepts only the local KumoMTA test endpoint")
        self.endpoint = endpoint

    async def submit(self, *, sender: str, recipient: str, subject: str,
                     body_text: str, body_html: str = "") -> SinkReceipt:
        if not _TEST_ADDRESS.fullmatch(sender) or not _TEST_ADDRESS.fullmatch(recipient):
            raise ValueError("The evaluation sink accepts only reserved .test addresses")
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        from email.utils import make_msgid
        msg["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        payload = {
            "envelope_sender": sender,
            "content": msg.as_string(policy=SMTP),
            "recipients": [{"email": recipient}],
        }
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.post(self.endpoint, json=payload)
        except httpx.RequestError as exc:
            raise TransientError("The local KumoMTA test service could not be reached") from exc
        if 300 <= response.status_code < 400:
            raise PermanentError("KumoMTA test endpoint redirected; redirects are not permitted")
        if response.status_code in (401, 403):
            raise AuthError("The KumoMTA test listener rejected this client")
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(f"KumoMTA local service returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PermanentError(f"KumoMTA rejected this test message: HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise TransientError("KumoMTA returned a non-JSON acknowledgement") from exc
        if not isinstance(result, dict):
            raise TransientError("KumoMTA returned an invalid acknowledgement object")
        if result.get("success_count") != 1 or result.get("fail_count") != 0:
            raise PermanentError("KumoMTA did not confirm acceptance of the test recipient")
        return SinkReceipt(message_id=msg["Message-ID"], accepted_recipients=1)
