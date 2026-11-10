"""Local-only contract tests for existing authenticated Gmail and Graph send paths.

All API calls are intercepted in memory; no credentials, network or live delivery.
"""
import base64
from email import message_from_bytes

import httpx
import pytest

from bb.mail.providers.gmail import GmailAccount
from bb.mail.providers.graph import GraphAccount
from bb.mail.providers.base import AuthError, TransientError


@pytest.mark.asyncio
async def test_gmail_send_builds_authenticated_mime_and_returns_provider_ids(monkeypatch):
    captured = []

    async def token(_self):
        return "local-test-token"

    def handler(request):
        captured.append(request)
        assert request.url.path.endswith("/messages/send")
        assert request.headers["authorization"] == "Bearer local-test-token"
        import json
        raw = json.loads(request.content)["raw"]
        msg = message_from_bytes(base64.urlsafe_b64decode(raw))
        assert msg["From"] == "BB Test <sender@example.test>"
        assert msg["To"] == "recipient@example.test"
        assert msg["Reply-To"] == "reply@example.test"
        assert msg["Subject"] == "Controlled local test"
        return httpx.Response(200, json={"id": "mock-gmail-id", "threadId": "mock-thread"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("bb.mail.providers.gmail.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=transport, **kwargs))
    monkeypatch.setattr(GmailAccount, "_token", token)
    account = GmailAccount({"address": "sender@example.test"}, {"refresh_token": "unused"}, {})
    message_id, provider_id, thread = await account.send(
        to=["recipient@example.test"], subject="Controlled local test",
        body_text="Approved test", body_html="<p>Approved test</p>",
        from_name="BB Test", reply_to="reply@example.test",
    )
    assert message_id.startswith("<") and message_id.endswith(">")
    assert (provider_id, thread) == ("mock-gmail-id", "mock-thread")
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_gmail_auth_failure_is_not_reported_as_success(monkeypatch):
    async def token(_self):
        return "local-test-token"

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "unauthorized"}))
    monkeypatch.setattr("bb.mail.providers.gmail.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=transport, **kwargs))
    monkeypatch.setattr(GmailAccount, "_token", token)
    account = GmailAccount({"address": "sender@example.test"}, {}, {})
    with pytest.raises(AuthError):
        await account.send(to=["recipient@example.test"], subject="Not sent",
                           body_text="test", body_html="")


@pytest.mark.asyncio
async def test_graph_draft_send_uses_delegated_mailbox_and_preserves_reply_to(monkeypatch):
    calls = []

    async def fake_call(self, method, path, *, params=None, json=None, absolute=False):
        calls.append((method, path, json))
        if method == "POST" and path == "/me/messages":
            return {"id": "mock-draft", "conversationId": "mock-conversation",
                    "internetMessageId": "<mock@example.test>"}
        if method == "POST" and path == "/me/messages/mock-draft/send":
            return {}
        raise AssertionError(f"Unexpected Graph call: {method} {path}")

    monkeypatch.setattr(GraphAccount, "_call", fake_call)
    account = GraphAccount({"address": "sender@example.test"}, {"refresh_token": "unused"}, {})
    result = await account.send(
        to=["recipient@example.test"], subject="Controlled local test",
        body_text="Approved test", body_html="<p>Approved test</p>",
        reply_to="reply@example.test",
    )
    assert result == ("<mock@example.test>", "", "mock-conversation")
    assert calls[0][0:2] == ("POST", "/me/messages")
    assert calls[0][2]["toRecipients"] == [
        {"emailAddress": {"address": "recipient@example.test"}}
    ]
    assert calls[0][2]["replyTo"] == [
        {"emailAddress": {"address": "reply@example.test"}}
    ]
    assert calls[1] == ("POST", "/me/messages/mock-draft/send", None)
    assert len(calls) == 2
