"""KumoMTA process smoke test: localhost-only SMTP listener and null queue.

No messages are delivered to the internet. Enabled only by CI with BB_KUMO_TEST.
"""

import os
import smtplib
import socket
import time
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("BB_KUMO_TEST") != "1",
    reason="requires isolated KumoMTA null-queue sink",
)


def _ready() -> None:
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", 2025), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    pytest.fail("KumoMTA local sink did not start")


def test_kumomta_null_queue_accepts_12_local_smtp_messages():
    _ready()
    started = time.monotonic()
    mids = set()
    with smtplib.SMTP("127.0.0.1", 2025, timeout=10) as smtp:
        smtp.ehlo()
        for i in range(12):
            msg = EmailMessage()
            msg["From"] = "bb-test-sender@example.test"
            msg["To"] = f"recipient-{i}@example.test"
            msg["Subject"] = f"BB KumoMTA controlled sink test {i:03d}"
            msg["Message-ID"] = make_msgid(domain="example.test")
            msg.set_content(f"Only local KumoMTA SMTP sink test number {i}.")
            refused = smtp.send_message(msg)
            assert refused == {}
            mids.add(msg["Message-ID"])
    assert len(mids) == 12
    print(f"KumoMTA null-queue local smoke: 12 SMTP acknowledgements in {time.monotonic()-started:.3f}s; 0 public sends")
