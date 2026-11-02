"""End-to-end tests for user-generated BB Microsoft authorization links.

No Microsoft credentials or network access are required: only the official
OAuth authorization-code exchange is mocked.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from bb import microsoft_oauth
from bb.accounts import Accounts
from bb.app import create_app
from bb.mail import service
from bb.microsoft_authorization_links import (
    generate_link, get_link, link_for_token, revoke_link,
)
from bb.workspaces import Workspace
from tests.test_bb_web import ADMIN, USER, _dns_stub, _make_user, _settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "Started."))
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)
    settings = _settings(tmp_path, MS_CLIENT_ID="ms-test-app", MS_CLIENT_SECRET="test-secret")
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def _admin_ws(client):
    account = Accounts(client.settings.accounts_db).get_by_email(ADMIN["email"])
    return Workspace(client.settings, account).ensure()


def _generate(client):
    return client.post(
        "/links/microsoft/generate",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )


def _revoke(client):
    return client.post(
        "/links/microsoft/revoke",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )


def test_generate_show_copy_and_regenerate_link(client):
    client.post("/login", data=ADMIN)
    assert "Generate live link" in client.get("/links").text
    assert _generate(client).status_code == 303
    first = get_link(_admin_ws(client).user_data_dir)
    assert first and first["enabled"]
    page = client.get("/links")
    assert "http://testserver/l/" + first["token"] in page.text
    assert "Copy link" in page.text
    r = client.get("/l/" + first["token"])
    assert r.status_code == 200
    assert "Connect your Microsoft mailbox" in r.text
    assert 'action="/l/' + first["token"] + '/connect"' in r.text
    assert _generate(client).status_code == 303
    second = get_link(_admin_ws(client).user_data_dir)
    assert second["token"] != first["token"]
    assert client.get("/l/" + first["token"]).status_code == 404
    assert client.get("/l/" + second["token"]).status_code == 200


def test_owner_bound_login_and_official_microsoft_consent(client, monkeypatch):
    client.post("/login", data=ADMIN)
    _make_user(client)
    _generate(client)
    token = get_link(_admin_ws(client).user_data_dir)["token"]
    client.cookies.clear()
    public = client.get("/l/" + token)
    assert public.status_code == 200
    assert "Connect your Microsoft account to BB" in public.text
    assert 'href="/login?next=/l/' + token + '"' in public.text
    assert "Microsoft password" in public.text
    login_page = client.get("/login?next=/l/" + token)
    assert f'name="next" value="/l/{token}"' in login_page.text
    # Another BB user must not be able to redeem an administrator's link.
    as_other = client.post("/login", data={**USER, "next": "/l/" + token})
    assert as_other.headers["location"] == "/l/" + token
    assert client.get("/l/" + token).status_code == 404
    assert client.post("/l/" + token + "/connect").status_code == 404
    client.cookies.clear()
    client.post("/login", data=ADMIN)
    start = client.post("/l/" + token + "/connect")
    assert start.status_code == 303
    assert start.headers["location"].startswith("https://login.microsoftonline.com/")
    assert "offline_access" in start.headers["location"]
    state = start.headers["location"].split("state=")[1].split("&")[0]

    async def exchange(settings, code, redirect=""):
        assert code == "auth-code"
        assert not redirect
        return {"access_token": "access-test", "refresh_token": "refresh-test"}

    async def account_email(access_token):
        assert access_token == "access-test"
        return "owner@outlook.com"

    monkeypatch.setattr(microsoft_oauth, "exchange_code", exchange)
    monkeypatch.setattr(microsoft_oauth, "account_email", account_email)
    cb = client.get("/microsoft/callback", params={"code": "auth-code", "state": state})
    assert cb.status_code == 303
    assert "Microsoft%20connected%20as%20owner%40outlook.com" in cb.headers["location"]
    ws = _admin_ws(client)
    box = asyncio.run(ws.mail_db().mailbox_by_address("owner@outlook.com"))
    assert box["address"] == "owner@outlook.com"
    assert "refresh-test" not in box["creds"]
    assert service.open_creds(client.settings.secret_key(), box)["refresh_token"] == "refresh-test"
    assert not ws.microsoft_token_file.exists()


def test_revocation_expiry_and_csrf(client):
    client.post("/login", data=ADMIN)
    for origin in (None, "https://attacker.example"):
        assert client.post("/links/microsoft/generate", headers=(
            {"origin": origin} if origin else {}
        )).status_code == 403
    assert get_link(_admin_ws(client).user_data_dir) is None
    _generate(client)
    token = get_link(_admin_ws(client).user_data_dir)["token"]
    assert client.get("/l/" + token).status_code == 200
    assert client.post("/links/microsoft/revoke", headers={"origin": "https://attacker.example"}).status_code == 403
    assert client.get("/l/" + token).status_code == 200
    assert _revoke(client).status_code == 303
    assert client.get("/l/" + token).status_code == 404
    assert get_link(_admin_ws(client).user_data_dir) is None
    # Independent expiry check; the OAuth refresh token is not affected by link expiry.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = generate_link(_admin_ws(client).user_data_dir, now=now)
    assert link_for_token(_admin_ws(client).user_data_dir, record["token"], now=now + timedelta(days=29))
    assert not link_for_token(_admin_ws(client).user_data_dir, record["token"], now=now + timedelta(days=31))


def test_link_rejected_for_unconfigured_microsoft_and_unauthenticated_client(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    settings = _settings(tmp_path, MS_CLIENT_ID="", MS_CLIENT_SECRET="")
    with TestClient(create_app(settings), follow_redirects=False) as c:
        assert c.post("/links/microsoft/generate", headers={"origin": "http://testserver"}).status_code == 303
        c.post("/login", data=ADMIN)
        response = c.post("/links/microsoft/generate", headers={"origin": "http://testserver"})
        assert "Configure%20Microsoft%20sign-in" in response.headers["location"]
        assert get_link(Workspace(settings, Accounts(settings.accounts_db).get_by_email(ADMIN["email"])).ensure().user_data_dir) is None
