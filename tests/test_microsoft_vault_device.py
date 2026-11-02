"""Device-code authorization and encrypted multi-mailbox Graph vault, offline tests."""
import asyncio
import json
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from bb import microsoft_device, microsoft_oauth, totp
from bb.accounts import Accounts
from bb.app import create_app
from bb.mail import crypto, service
from bb.mail.providers.graph import GraphAccount
from bb.microsoft_authorization_links import get_link
from bb.workspaces import Workspace
from tests.test_bb_web import ADMIN, _dns_stub, _settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "Started."))
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)
    settings = _settings(tmp_path, MS_CLIENT_ID="ms-test", MS_CLIENT_SECRET="test-secret",
                         MS_DEVICE_CLIENT_ID="public-device-test")
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def _owner(client):
    return Accounts(client.settings.accounts_db).get_by_email(ADMIN["email"])


def _workspace(client):
    return Workspace(client.settings, _owner(client)).ensure()


def _setup_mfa(client):
    client.post("/login", data=ADMIN)
    setup = client.get("/security/mfa")
    assert "authenticator setup key" in setup.text.lower()
    accounts = Accounts(client.settings.accounts_db)
    secret = accounts.totp_secret(_owner(client)["id"], client.settings.secret_key(),
                                 confirmed_only=False)
    assert secret
    result = client.post("/security/mfa/confirm", data={"code": totp.current_code(secret)})
    assert result.status_code == 303
    assert accounts.totp_enabled(_owner(client)["id"])
    return secret


def test_totp_enrollment_enforced_on_login_and_vault(client):
    client.post("/login", data=ADMIN)
    before = client.get("/vault")
    assert before.status_code == 303
    assert before.headers["location"] == "/security/mfa"
    secret = _setup_mfa(client)
    assert client.get("/vault").status_code == 200
    client.cookies.clear()
    login = client.post("/login", data=ADMIN)
    assert login.status_code == 303 and login.headers["location"].startswith("/login/totp")
    assert not client.cookies.get("bb_session")
    bad = client.post("/login/totp", data={"code": "000000"})
    assert bad.status_code == 200 and "Incorrect" in bad.text
    ok = client.post("/login/totp", data={"code": totp.current_code(secret)})
    assert ok.status_code == 303
    assert client.cookies.get("bb_session")
    assert client.get("/vault").status_code == 200


def test_microsoft_device_link_to_encrypted_vault_and_independent_mailboxes(client, monkeypatch):
    _setup_mfa(client)
    origin = {"origin": "http://testserver"}
    assert client.post("/links/microsoft/generate", headers=origin).status_code == 303
    link = get_link(_workspace(client).user_data_dir)
    token = link["token"]

    async def start(settings):
        return {"device_code": "secret-device-credential", "user_code": "ABCDEFGH",
                "verification_uri": "https://www.microsoft.com/devicelogin",
                "expires_in": 900, "interval": 5, "client_id": "public-device-test",
                "scopes": "offline_access Mail.ReadWrite Files.ReadWrite"}

    polls = []

    async def poll(settings, device_code, client_id):
        assert device_code == "secret-device-credential"
        assert client_id == "public-device-test"
        polls.append(1)
        return "connected", {
            "access_token": "access-secret-1", "refresh_token": "refresh-secret-1",
            "scope": "offline_access Mail.ReadWrite Files.ReadWrite",
            "expires_in": 3600,
        }

    async def email(access_token):
        assert access_token == "access-secret-1"
        return "my-test@outlook.com"

    monkeypatch.setattr(microsoft_device, "start", start)
    monkeypatch.setattr(microsoft_device, "poll", poll)
    monkeypatch.setattr(microsoft_oauth, "account_email", email)
    r = client.post("/l/" + token + "/device", headers=origin)
    assert r.status_code == 303
    path = r.headers["location"]
    assert path.startswith("/vault/device/")
    device_page = client.get(path)
    assert "ABCDEFGH" in device_page.text
    assert "https://www.microsoft.com/devicelogin" in device_page.text
    assert "secret-device-credential" not in device_page.text

    flow_id = path.rsplit("/", 1)[1]
    # The server enforces a polling interval; override only its timestamp in this offline test.
    from bb import microsoft_device_store
    with microsoft_device_store._db(_workspace(client).user_data_dir) as db:
        db.execute("UPDATE device_flows SET last_poll_at=0 WHERE id=?", (flow_id,))
    out = client.post(path + "/poll", headers=origin)
    assert out.status_code == 200
    assert out.json()["status"] == "connected"
    mid = out.json()["url"].split("mailbox=")[1]
    assert len(polls) == 1

    ws = _workspace(client)
    db = ws.mail_db()
    box = asyncio.run(db.mailbox(mid))
    assert box["address"] == "my-test@outlook.com"
    assert box["granted_scopes"].startswith("offline_access")
    assert box["token_expires_at"]
    assert "refresh-secret-1" not in box["creds"]
    assert "access-secret-1" not in box["creds"]
    stored = service.open_creds(client.settings.secret_key(), box)
    assert stored["refresh_token"] == "refresh-secret-1"
    assert stored["access_token"] == "access-secret-1"
    assert stored["public_client"] is True
    assert stored["client_id"] == "public-device-test"
    assert not ws.microsoft_token_file.exists()

    # A second independent mailbox must not overwrite the first token set.
    other, _ = asyncio.run(service.connect_microsoft(
        db, client.settings.secret_key(), address="second@outlook.com",
        refresh_token="refresh-secret-2", env=client.settings.worker_env(), verify=False,
        scopes="offline_access Mail.ReadWrite",
    ))
    assert other != mid
    assert service.open_creds(client.settings.secret_key(), asyncio.run(db.mailbox(mid)))["refresh_token"] == "refresh-secret-1"

    recorded = []
    async def graph_stub(self, method, path, **kwargs):
        recorded.append((self.address, method, path, kwargs))
        if path == "/me/messages":
            return {"value": [{"id": "msg-1", "subject": "Research", "bodyPreview": "Hello",
                               "from": {"emailAddress": {"address": "friend@example.com"}},
                               "receivedDateTime": "2026-01-01T12:00:00Z"}]}
        if path == "/me/drive/root/children":
            return {"value": [{"id": "drive-1", "name": "Report.txt", "size": 12, "file": {}}]}
        if path == "/me/messages/msg-1":
            return {"id": "msg-1", "subject": "Research", "from": {"emailAddress": {"address": "friend@example.com"}},
                    "body": {"contentType": "text", "content": "Research message"},
                    "receivedDateTime": "2026-01-01T12:00:00Z"}
        if path == "/organization":
            return {"value": [{"id": "org-1", "displayName": "Research Organization",
                               "verifiedDomains": [{"name": "research.example"}]}]}
        return {}

    monkeypatch.setattr(GraphAccount, "_call", graph_stub)
    page = client.get("/vault?mailbox=" + mid)
    assert page.status_code == 200
    assert "my-test@outlook.com" in page.text and "second@outlook.com" in page.text
    assert "Research" in page.text and "Report.txt" in page.text
    assert "refresh-secret-1" not in page.text
    assert "access-secret-1" not in page.text
    page2 = client.get("/vault?mailbox=" + other)
    assert page2.status_code == 200
    assert recorded[-1][0] == "second@outlook.com"

    read = client.get("/vault/mailbox/" + mid + "/message/msg-1")
    assert read.status_code == 200 and "Research message" in read.text
    send = client.post("/vault/mailbox/" + mid + "/send", data={
        "to": "research@example.com", "subject": "Test", "body": "A test sent through Graph",
    }, headers=origin)
    assert send.status_code == 303
    assert any(m == "POST" and p == "/me/sendMail" for _, m, p, _ in recorded)
    reply = client.post("/vault/mailbox/" + mid + "/message/msg-1/reply",
                        data={"body": "Reply through Graph"}, headers=origin)
    assert reply.status_code == 303
    assert any(p == "/me/messages/msg-1/reply" for _, _, p, _ in recorded)
    org = client.get("/vault/mailbox/" + mid + "/organization")
    assert "Research Organization" in org.text
    assert not client.get("/vault/mailbox/" + mid + "/message/../").status_code == 200


def test_rfc6238_known_vector():
    # RFC 6238 SHA-1 vector, last 6 digits of the eight-digit TOTP.
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.current_code(secret, timestamp=59) == "287082"
    assert totp.verify(secret, "287082", timestamp=59)
    assert not totp.verify(secret, "123456", timestamp=59)
