"""KumoMTA process smoke test: localhost-only SMTP listener and null queue.

No messages are delivered to the internet. Enabled only by CI with BB_KUMO_TEST.
"""

import asyncio
import os
import smtplib
import socket
import time
from email.message import EmailMessage
from email.utils import make_msgid

import pytest
import httpx

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


@pytest.mark.asyncio
async def test_kumomta_local_http_injection_to_null_queue():
    """Test documented REST injection; all recipients are reserved .test addresses."""
    _ready()
    endpoint = "http://127.0.0.1:8005/api/inject/v1"
    async with httpx.AsyncClient(timeout=5.0) as client:
        # Account for the Docker test process starting HTTP slightly after SMTP.
        for attempt in range(30):
            try:
                response = await client.get("http://127.0.0.1:8005/api/check-liveness/v1")
                if response.status_code < 500:
                    break
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.25)

        started = time.monotonic()
        for i in range(10):
            body = (
                f"From: BB Test <bb-sender@example.test>\\r\\n"
                f"To: recipient-{i}@example.test\\r\\n"
                f"Subject: BB Kumo HTTP null-queue smoke {i:03d}\\r\\n"
                "MIME-Version: 1.0\\r\\n"
                "Content-Type: text/plain; charset=utf-8\\r\\n"
                "\\r\\nThis stays inside the isolated test sink."
            ).replace("\\r", "\r").replace("\\n", "\n")
            payload = {
                "envelope_sender": "bb-sender@example.test",
                "content": body,
                "recipients": [{"email": f"recipient-{i}@example.test"}],
            }
            response = await client.post(endpoint, json=payload)
            assert response.status_code == 200, response.text[:300]
            accepted = response.json()
            assert accepted.get("success_count") == 1, accepted
            assert accepted.get("fail_count") == 0, accepted
    elapsed = time.monotonic() - started
    print(f"KumoMTA HTTP injection: 10 locally accepted, {elapsed:.3f}s; 0 public sends")

@pytest.mark.asyncio
async def test_kumomta_http_injection_concurrent_local_load():
    """Bounded local HTTP test; never routes a message beyond the null queue."""
    _ready()
    total = 200
    latency = []
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=15.0, limits=httpx.Limits(max_connections=12)) as client:
        async def one(index: int):
            async with sem:
                from email.message import EmailMessage
                m = EmailMessage()
                m["From"] = "bb-sender@example.test"
                m["To"] = f"recipient-{index}@example.test"
                m["Subject"] = f"BB Kumo HTTP concurrent sink {index:03d}"
                m.set_content("Test-only message, never delivered to public inboxes.")
                started = time.monotonic()
                response = await client.post(
                    "http://127.0.0.1:8005/api/inject/v1",
                    json={
                        "envelope_sender": "bb-sender@example.test",
                        "content": m.as_string(),
                        "recipients": [{"email": f"recipient-{index}@example.test"}],
                    },
                )
                latency.append(time.monotonic() - started)
                assert response.status_code == 200, response.text[:240]
                result = response.json()
                assert result.get("success_count") == 1 and result.get("fail_count") == 0

        await asyncio.gather(*(one(i) for i in range(total)))
    assert len(latency) == total
    sorted_latency = sorted(latency)
    p95 = sorted_latency[int(total * 0.95) - 1] * 1000
    print(f"KumoMTA HTTP local load: {total}/{total} accepted; p95 {p95:.1f}ms; 0 public sends")

@pytest.mark.asyncio
async def test_bb_kumo_http_adapter_against_real_local_sink():
    """Exercise BB's candidate adapter end-to-end, not a bare HTTP request."""
    _ready()
    from bb.mail.kumo_eval import KumoLocalSink
    receipt = await KumoLocalSink().submit(
        sender="verified-sender@example.test",
        recipient="consenting-recipient@example.test",
        subject="BB candidate HTTP delivery adapter",
        body_text="Local test; no public delivery",
        body_html="<p>Local test; no public delivery</p>",
    )
    assert receipt.accepted_recipients == 1
    assert receipt.message_id.startswith("<")
