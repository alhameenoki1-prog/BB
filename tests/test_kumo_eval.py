"""Offline KumoMTA evaluation transport contract tests; never contact the internet."""

import httpx
import pytest

from bb.mail import kumo_eval
from bb.mail.providers.base import AuthError, PermanentError, TransientError


def _fake_client(monkeypatch, callback):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(callback), **kwargs)

    monkeypatch.setattr(kumo_eval.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_kumo_eval_builds_mime_and_requires_api_acceptance(monkeypatch):
    def respond(request):
        data = __import__("json").loads(request.content)
        assert data["envelope_sender"] == "sender@example.test"
        assert data["recipients"] == [{"email": "recipient@example.test"}]
        assert "Subject: Local test" in data["content"]
        assert "Message-ID:" in data["content"]
        assert "multipart/alternative" in data["content"]
        return httpx.Response(200, json={"success_count": 1, "fail_count": 0})

    _fake_client(monkeypatch, respond)
    receipt = await kumo_eval.KumoLocalSink().submit(
        sender="sender@example.test", recipient="recipient@example.test",
        subject="Local test", body_text="Plain text", body_html="<p>HTML</p>",
    )
    assert receipt.accepted_recipients == 1 and receipt.message_id.startswith("<")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthError), (403, AuthError), (408, TransientError),
     (425, TransientError), (429, TransientError),
     (503, TransientError), (400, PermanentError)],
)
async def test_kumo_eval_classifies_http_failures(monkeypatch, status, error):
    _fake_client(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(error):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="recipient@example.test",
            subject="Negative test", body_text="local",
        )


@pytest.mark.asyncio
async def test_kumo_eval_requires_unambiguous_acceptance(monkeypatch):
    _fake_client(monkeypatch, lambda request: httpx.Response(
        200, json={"success_count": 0, "fail_count": 1}
    ))
    with pytest.raises(PermanentError):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="recipient@example.test",
            subject="Negative test", body_text="local",
        )


@pytest.mark.asyncio
async def test_kumo_eval_rejects_public_destinations_and_external_urls():
    with pytest.raises(ValueError):
        kumo_eval.KumoLocalSink("https://example.com/api/inject/v1")
    with pytest.raises(ValueError):
        kumo_eval.KumoLocalSink("http://127.0.0.1:8006/api/inject/v1")
    with pytest.raises(ValueError):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="user@gmail.com",
            subject="Never transmitted", body_text="blocked before network",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_kumo_eval_rejects_redirects_without_following(monkeypatch, status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://external.example/"})
    _fake_client(monkeypatch, respond)
    with pytest.raises(PermanentError, match="redirected"):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="recipient@example.test",
            subject="Redirect test", body_text="local",
        )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], None, "accepted", 42])
async def test_kumo_eval_rejects_non_object_acknowledgements(monkeypatch, body):
    _fake_client(monkeypatch, lambda request: httpx.Response(200, content=__import__("json").dumps(body).encode("utf-8"), headers={"content-type": "application/json"}))
    with pytest.raises(TransientError, match="invalid acknowledgement"):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="recipient@example.test",
            subject="Malformed acknowledgement", body_text="local",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [
    httpx.ConnectError("local sink unavailable"),
    httpx.ReadTimeout("local sink timed out"),
    httpx.RemoteProtocolError("local sink closed without acknowledgement"),
])
async def test_kumo_eval_ambiguous_transport_failures_are_not_accepted(monkeypatch, exception):
    """A failed/ambiguous HTTP exchange must never return a delivery receipt."""
    calls = []

    def respond(request):
        calls.append(request)
        raise exception

    _fake_client(monkeypatch, respond)
    with pytest.raises(TransientError, match="could not be reached"):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="recipient@example.test",
            subject="Transport interruption", body_text="local-only",
        )
    assert len(calls) == 1  # No implicit replay after ambiguous acceptance.


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [
    {"success_count": True, "fail_count": False},
    {"success_count": "1", "fail_count": "0"},
    {"success_count": 1.0, "fail_count": 0.0},
])
async def test_kumo_eval_requires_integer_acknowledgement_counts(monkeypatch, ack):
    """Bool and float equality with integers must not be interpreted as receipts."""
    _fake_client(monkeypatch, lambda request: httpx.Response(200, json=ack))
    with pytest.raises(TransientError, match="invalid acknowledgement"):
        await kumo_eval.KumoLocalSink().submit(
            sender="sender@example.test", recipient="recipient@example.test",
            subject="Invalid count types", body_text="local-only",
        )
