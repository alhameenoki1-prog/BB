"""Live Microsoft authorization entry point: each BB user connects their own mailbox."""
import json

import pytest
from fastapi.testclient import TestClient

from bb import microsoft_oauth
from bb.accounts import Accounts
from bb.app import create_app
from bb.workspaces import Workspace
from tests.test_bb_web import ADMIN, USER, _dns_stub, _make_user, _settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "Started (pid 1)."))
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)
    settings = _settings(tmp_path, MS_CLIENT_ID="test-microsoft-client", MS_CLIENT_SECRET="secret")
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def test_public_link_requires_bb_login_and_returns_to_authorization(client):
    entry = client.get("/microsoft/authorize")
    assert entry.status_code == 303
    assert entry.headers["location"] == "/login?next=/microsoft/authorize"
    login_form = client.get("/login?next=/microsoft/authorize")
    assert 'name="next" value="/microsoft/authorize"' in login_form.text
    signed_in = client.post("/login", data={**ADMIN, "next": "/microsoft/authorize"})
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/microsoft/authorize"
    page = client.get("/microsoft/authorize")
    assert page.status_code == 200
    assert "Continue to Microsoft" in page.text
    assert 'action="/microsoft/connect"' in page.text
    assert "http://testserver/microsoft/authorize" in page.text
    assert "your own BB workspace" in page.text
    links_page = client.get("/links")
    assert "Microsoft mailbox authorization link" in links_page.text
    assert 'href="/microsoft/authorize"' in links_page.text


def test_login_continuation_does_not_allow_open_redirect(client):
    attempted = client.post("/login", data={**ADMIN, "next": "https://evil.example/steal"})
    assert attempted.status_code == 303
    assert attempted.headers["location"] == "/"
    assert client.get("/login?next=//evil.example").headers["location"] == "/"


def test_live_link_uses_official_microsoft_oauth_and_is_user_bound(client, monkeypatch):
    client.post("/login", data=ADMIN)
    _make_user(client)

    async def fake_exchange(settings, code, redirect=""):
        assert not redirect
        return {"refresh_token": "refresh-" + code, "access_token": "access-" + code}

    async def fake_email(access_token):
        return "admin@outlook.com" if access_token == "access-admin" else "user@outlook.com"

    monkeypatch.setattr(microsoft_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(microsoft_oauth, "account_email", fake_email)

    def connect(code):
        link = client.get("/microsoft/authorize")
        assert "Continue to Microsoft" in link.text or "Reconnect with Microsoft" in link.text
        start = client.post("/microsoft/connect")
        assert start.status_code == 303
        assert start.headers["location"].startswith("https://login.microsoftonline.com/")
        assert "offline_access" in start.headers["location"]
        state = start.headers["location"].split("state=")[1].split("&")[0]
        return client.get("/microsoft/callback", params={"code": code, "state": state})

    admin_connected = connect("admin")
    assert "admin%40outlook.com" in admin_connected.headers["location"]
    admin = Accounts(client.settings.accounts_db).get_by_email(ADMIN["email"])
    admin_ws = Workspace(client.settings, admin).ensure()
    assert json.loads(admin_ws.microsoft_token_file.read_text())["refresh_token"] == "refresh-admin"

    client.cookies.clear()
    login = client.post("/login", data={**USER, "next": "/microsoft/authorize"})
    assert login.headers["location"] == "/microsoft/authorize"
    user_page = client.get("/microsoft/authorize")
    assert "No Microsoft mailbox is connected" in user_page.text
    user_connected = connect("user")
    assert "user%40outlook.com" in user_connected.headers["location"]
    owner = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    user_ws = Workspace(client.settings, owner).ensure()
    assert json.loads(user_ws.microsoft_token_file.read_text())["refresh_token"] == "refresh-user"
    assert json.loads(admin_ws.microsoft_token_file.read_text())["refresh_token"] == "refresh-admin"
    assert "refresh-admin" not in user_page.text


def test_missing_microsoft_configuration_disables_authorize_button(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    settings = _settings(tmp_path, MS_CLIENT_ID="", MS_CLIENT_SECRET="")
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.post("/login", data=ADMIN)
        page = client.get("/microsoft/authorize")
        assert page.status_code == 200
        assert "Microsoft sign-in is not configured" in page.text
        assert 'action="/microsoft/connect"' not in page.text
