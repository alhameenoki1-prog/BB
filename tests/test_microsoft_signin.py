"""Microsoft sign-in (Outlook, Hotmail, Microsoft 365) through the site's
own app registration, on both the Mailbox page and the command center,
and the Reconnect path when a provider forces a new sign-in. The
browser's existing Microsoft session does the signing in; no password
reaches the site, and the stored refresh token outlives a password
change."""

import asyncio
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from bb import microsoft_oauth
from bb.accounts import Accounts
from bb.app import create_app
from bb.mail import service
from bb.mail.db import MailDB
from bb.mail.models import MailboxSettings
from bb.mail.providers import AuthError, TransientError
from bb.mail.providers.graph import GraphAccount
from bb.workspaces import Workspace
from tests.test_bb_web import PROFILE, USER, _as_user, _dns_stub, _login, _settings

MS = {"MS_CLIENT_ID": "ms-cid", "MS_CLIENT_SECRET": "ms-sec"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(Workspace, "start_mail_worker", lambda self: (True, "Started (pid 1)."))
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)
    settings = _settings(tmp_path, **MS)
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def _user_ws(client) -> Workspace:
    return Workspace(client.settings, Accounts(client.settings.accounts_db).get_by_email(USER["email"]))


# ── the OAuth helper ──

def test_state_is_signed_per_user_and_expires(tmp_path):
    settings = _settings(tmp_path, **MS)
    state = microsoft_oauth.sign_state("k", "u1", "nonce")
    assert microsoft_oauth.verify_state("k", "u1", "nonce", state)
    assert not microsoft_oauth.verify_state("k", "u2", "nonce", state)       # another user
    assert not microsoft_oauth.verify_state("k", "u1", "other", state)       # another browser
    assert not microsoft_oauth.verify_state("k", "u1", "nonce", "garbage")
    old = microsoft_oauth.sign_state("k", "u1", "nonce", now=1)
    assert not microsoft_oauth.verify_state("k", "u1", "nonce", old)          # ten minutes is the limit
    url = microsoft_oauth.auth_url(settings, state, login_hint="me@outlook.com")
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "client_id=ms-cid" in url and "redirect_uri=http%3A%2F%2Ftestserver%2Fmicrosoft%2Fcallback" in url
    assert "offline_access" in url and "Mail.Send" in url and "prompt=select_account" in url
    assert "login_hint=me%40outlook.com" in url
    other = microsoft_oauth.auth_url(settings, state, redirect="http://testserver/mail/microsoft/callback")
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fmail%2Fmicrosoft%2Fcallback" in other
    tenant = _settings(tmp_path, **MS, MS_TENANT="contoso.onmicrosoft.com")
    assert "/contoso.onmicrosoft.com/oauth2" in microsoft_oauth.auth_url(tenant, state)


# ── the Mailbox page ──

def test_microsoft_connect_round_trip(client, monkeypatch):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    r = client.post("/microsoft/connect")
    assert r.status_code == 303
    url = r.headers["location"]
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fmicrosoft%2Fcallback" in url
    assert "login_hint=jane%40acme-research.com" in url     # the profile address, as a hint only
    state = url.split("state=")[1].split("&")[0]

    async def fake_exchange(settings, code, redirect=""):
        assert code == "the-code" and not redirect
        return {"refresh_token": "rt-1", "access_token": "at-1", "scope": "Mail.Send"}

    seen_tokens = []

    async def fake_email(token):
        seen_tokens.append(token)
        return "jane@outlook.com"

    monkeypatch.setattr(microsoft_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(microsoft_oauth, "account_email", fake_email)
    bad = client.get("/microsoft/callback", params={"code": "the-code", "state": "1.deadbeef"})
    assert "did%20not%20match" in bad.headers["location"]
    denied = client.get("/microsoft/callback", params={"error": "access_denied", "error_description": "User declined"})
    assert "Microsoft%20said%3A%20User%20declined" in denied.headers["location"]
    ok = client.get("/microsoft/callback", params={"code": "the-code", "state": state})
    assert "Microsoft%20connected%20as%20jane%40outlook.com" in ok.headers["location"]

    ws = _user_ws(client)
    token = json.loads(ws.microsoft_token_file.read_text())
    assert token["refresh_token"] == "rt-1"       # the file harvey.integrations.microsoft reads
    assert ws.microsoft_account()["email"] == "jane@outlook.com"
    assert ws.mail_provider() == "microsoft" and ws.mailbox_connected() and ws.sending_address() == "jane@outlook.com"
    config = yaml.safe_load(ws.config_file.read_text())
    assert config["persona"]["email"] == "jane@outlook.com" and config["channels"]["email"]["provider"] == "microsoft"
    # The worker signs in through the site's app registration, as this user.
    env = ws.worker_env()
    assert env["M365_CLIENT_ID"] == "ms-cid" and env["M365_CLIENT_SECRET"] == "ms-sec" and env["M365_TENANT_ID"] == "common"
    assert "M365_MAILBOX" not in env
    assert ws.mailbox_tier()[0] == "consumer"     # a personal Outlook address is paced gently

    page = client.get("/mailbox")
    assert "Connected" in page.text and "jane@outlook.com" in page.text and 'action="/microsoft/disconnect"' in page.text
    assert 'value="microsoft" checked' in page.text

    # Test connection: a rotated refresh token is kept.
    async def fake_refresh(settings, refresh_token):
        assert refresh_token == "rt-1"
        return {"access_token": "at-2", "refresh_token": "rt-2"}

    monkeypatch.setattr(microsoft_oauth, "refresh_access_token", fake_refresh)
    r = client.post("/microsoft/test")
    assert "Connected%20as%20jane%40outlook.com" in r.headers["location"]
    assert seen_tokens == ["at-1", "at-2"]
    assert json.loads(ws.microsoft_token_file.read_text())["refresh_token"] == "rt-2"

    assert client.post("/microsoft/disconnect").status_code == 303
    assert not ws.microsoft_token_file.exists() and not ws.microsoft_connected()
    assert ws.mail_provider() != "microsoft"


def test_microsoft_connect_needs_the_site_app(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    settings = _settings(tmp_path)   # no MS_CLIENT_ID
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.settings = settings
        _as_user(client)
        assert "no%20Microsoft%20app%20registration" in client.post("/microsoft/connect").headers["location"]
        page = client.get("/mailbox")
        assert "not switched on" in page.text and 'action="/microsoft/connect"' not in page.text


def test_reconnect_is_offered_when_the_sign_in_is_refused(client, monkeypatch):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    ws = _user_ws(client)
    microsoft_oauth.save_token(ws, {"refresh_token": "rt"}, "jane@outlook.com")
    ws.set_mail_provider("microsoft")
    # Nothing wrong yet: no banner.
    page = client.get("/mailbox")
    assert "Reconnect your mailbox" not in page.text
    # The worker's login check failed because Microsoft revoked the token.
    failed = {"ok": False, "checked_at": "2026-10-22T10:00:00Z", "last_sent_at": "",
              "error": "token refresh rejected (400): AADSTS70000: invalid_grant. Re-run 'harvey microsoft auth'.",
              "alert": "Mailbox login FAILED at 10:00 UTC: token refresh rejected (400): invalid_grant"}

    async def health(self):
        return failed

    monkeypatch.setattr(Workspace, "mailbox_health", health)
    page = client.get("/mailbox")
    assert "Reconnect your mailbox" in page.text and 'action="/microsoft/connect"' in page.text
    assert "Changing your password does not do this on its own" in page.text
    home = client.get("/")
    assert "needs reconnecting" in home.text and 'href="/mailbox"' in home.text
    # A Gmail connection points at Google instead; SMTP at the password form.
    ws.set_mail_provider("gmail")
    assert 'action="/gmail/connect"' in client.get("/mailbox").text
    ws.set_mail_provider("smtp")
    assert 'href="#smtp"' in client.get("/mailbox").text


def test_needs_reconnect_reads_only_sign_in_failures(tmp_path):
    settings = _settings(tmp_path, **MS)
    acc = Accounts(settings.accounts_db); acc.create_user(USER["email"], USER["password"])
    ws = Workspace(settings, acc.get_by_email(USER["email"])).ensure()
    assert ws.needs_reconnect({"alert": "", "error": ""}) == ""
    assert ws.needs_reconnect({"alert": "Nothing delivered for 2 hours while 5 emails are due", "error": ""}) == ""
    assert "refused" in ws.needs_reconnect({"alert": "", "error": "SMTP server refused the login (535 5.7.8)"})
    assert "invalid_grant" in ws.needs_reconnect({"error": "Google no longer accepts this sign-in: invalid_grant"})
    assert ws.needs_reconnect({"error": "token refresh rejected (400): expired. Re-run 'harvey microsoft auth'."})


# ── the Graph adapter, signed in as the person ──

class _FakeHttp:
    """httpx.AsyncClient stand-in: records the token form, answers Graph."""
    posts: list = []
    token_answer = {"access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-2"}
    token_status = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, **k):
        _FakeHttp.posts.append((url, dict(data or {})))
        return _Resp(_FakeHttp.token_status, _FakeHttp.token_answer)

    async def request(self, method, url, params=None, json=None, headers=None):
        assert headers["Authorization"] == "Bearer at-1"
        if url.endswith("/me"):
            return _Resp(200, {"mail": "Me@Outlook.com", "userPrincipalName": "me@outlook.com"})
        if url.endswith("/me/mailFolders/inbox"):
            return _Resp(200, {"totalItemCount": 7})
        if url.endswith("/me/mailFolders/sentitems"):
            return _Resp(200, {"id": "sent-1"})
        return _Resp(404, {})


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_graph_signed_in_refreshes_through_the_site_app_and_keeps_the_rotated_token(monkeypatch):
    monkeypatch.setattr("bb.mail.providers.graph.httpx.AsyncClient", _FakeHttp)
    _FakeHttp.posts.clear(); _FakeHttp.token_status = 200
    env = {"MS_CLIENT_ID": "ms-cid", "MS_CLIENT_SECRET": "ms-sec", "MS_TENANT": "common"}
    acct = GraphAccount({"address": "me@outlook.com", "provider": "graph"}, {"refresh_token": "rt-1"}, env)
    assert acct.signed_in and acct._me == "/me"
    note = await acct.test()
    assert note == "Inbox reachable (7 messages)."
    url, form = _FakeHttp.posts[0]
    assert url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-1"
    assert form["client_id"] == "ms-cid" and form["client_secret"] == "ms-sec" and "offline_access" in form["scope"]
    assert acct.creds["refresh_token"] == "rt-2" and acct.creds_changed      # rotated: the caller stores it
    assert len(_FakeHttp.posts) == 1                                          # the access token is cached

    wrong = GraphAccount({"address": "other@outlook.com", "provider": "graph"}, {"refresh_token": "rt-1"}, env)
    with pytest.raises(AuthError, match="signed in as me@outlook.com, not other@outlook.com"):
        await wrong.test()

    _FakeHttp.token_status = 400
    _FakeHttp.token_answer = {"error": "invalid_grant", "error_description": "AADSTS70000: The token is expired or revoked."}
    revoked = GraphAccount({"address": "me@outlook.com", "provider": "graph"}, {"refresh_token": "rt-old"}, env)
    with pytest.raises(AuthError, match="no longer accepts this sign-in"):
        await revoked.test()
    _FakeHttp.token_answer = {"error": "temporarily_unavailable", "error_description": "try later"}
    with pytest.raises(TransientError):
        await GraphAccount({"address": "me@outlook.com", "provider": "graph"}, {"refresh_token": "rt"}, env).test()
    with pytest.raises(TransientError, match="no Microsoft app registration"):
        await GraphAccount({"address": "me@outlook.com", "provider": "graph"}, {"refresh_token": "rt"}, {}).test()

    # An admin app keeps its own path and grant.
    app = GraphAccount({"address": "box@contoso.com", "provider": "graph"},
                       {"tenant_id": "t", "client_id": "c", "client_secret": "s"}, env)
    assert not app.signed_in and app._me == "/users/box@contoso.com"


# ── the service: storing, rotating, migrating ──

class _FakeAccount:
    outcome = "ok"
    rotate = True

    def __init__(self, mailbox, creds, env):
        self.mailbox, self.creds, self.env = mailbox, creds, env
        self.creds_changed = False

    async def test(self):
        if _FakeAccount.outcome == "auth":
            raise AuthError("Microsoft no longer accepts this sign-in")
        if _FakeAccount.rotate and self.creds.get("refresh_token"):
            self.creds["refresh_token"] = self.creds["refresh_token"] + "+"
            self.creds_changed = True
        return "Inbox reachable (3 messages)."


@pytest.mark.asyncio
async def test_connect_microsoft_stores_the_rotated_token_and_check_keeps_rotating(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "account_for", lambda m, c, e: _FakeAccount(m, c, e))
    _FakeAccount.outcome, _FakeAccount.rotate = "ok", True
    db = MailDB(tmp_path / "mail.db"); await db.init()
    with pytest.raises(ValueError):
        await service.connect_microsoft(db, "sec", address="", refresh_token="rt", env={})
    mid, note = await service.connect_microsoft(db, "sec", address="Me@Outlook.com", refresh_token="rt", env={})
    m = await db.mailbox(mid)
    assert m["address"] == "me@outlook.com" and m["provider"] == "graph" and m["mode"] == "full" and m["status"] == "ok"
    assert service.open_creds("sec", m)["refresh_token"] == "rt+"      # the rotation during the probe was kept
    assert service.signed_in("sec", m) and "rt" not in m["creds"]
    assert await service.check_mailbox(db, "sec", m, {}) == "ok"
    assert service.open_creds("sec", await db.mailbox(mid))["refresh_token"] == "rt++"
    # Reconnecting the same address replaces the token in place.
    again, _ = await service.connect_microsoft(db, "sec", address="me@outlook.com", refresh_token="fresh", env={})
    assert again == mid and service.open_creds("sec", await db.mailbox(mid))["refresh_token"] == "fresh+"
    assert [r["text"] for r in await db.activity()][0] == "Reconnected me@outlook.com through Microsoft."
    _FakeAccount.outcome = "auth"
    await db.update_mailbox(mid, settings=MailboxSettings(signature_html="J", project_link="https://j.com").model_dump())
    assert service.ready_to_send(await db.mailbox(mid)) == (True, "")
    assert await service.check_mailbox(db, "sec", await db.mailbox(mid), {}) == "reconnect"
    assert service.ready_to_send(await db.mailbox(mid))[1] == "This mailbox needs reconnecting before it can send."
    # An admin-app mailbox is not a sign-in.
    _FakeAccount.outcome = "ok"
    gid, _ = await service.connect_graph(db, "sec", address="box@contoso.com", tenant_id="t", client_id="c",
                                         client_secret="s", env={})
    assert not service.signed_in("sec", await db.mailbox(gid))


@pytest.mark.asyncio
async def test_migration_brings_a_microsoft_sign_in_over(tmp_path):
    settings = _settings(tmp_path, **MS)
    acc = Accounts(settings.accounts_db); acc.create_user(USER["email"], USER["password"])
    ws = Workspace(settings, acc.get_by_email(USER["email"])).ensure()
    ws.write_profile(PROFILE)
    microsoft_oauth.save_token(ws, {"refresh_token": "rt-ms"}, "jane@outlook.com")
    ws.set_mail_provider("microsoft")
    db = ws.mail_db(); await db.init()
    mid = await service.migrate_from_workspace(db, "sec", ws)
    m = await db.mailbox(mid)
    assert m["address"] == "jane@outlook.com" and m["provider"] == "graph" and m["mode"] == "full"
    assert service.open_creds("sec", m) == {"refresh_token": "rt-ms"} and service.signed_in("sec", m)


# ── the command center pages ──

def test_mail_pages_connect_and_reconnect_with_microsoft(client, monkeypatch):
    monkeypatch.setattr(service, "account_for", lambda m, c, e: _FakeAccount(m, c, e))
    _FakeAccount.outcome, _FakeAccount.rotate = "ok", False
    _as_user(client)
    page = client.get("/mail/accounts")
    assert "Sign in with Microsoft" in page.text and 'action="/mail/microsoft/connect"' in page.text
    r = client.post("/mail/microsoft/connect", data={"email": "jane@outlook.com"})
    assert r.status_code == 303
    url = r.headers["location"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fmail%2Fmicrosoft%2Fcallback" in url and "login_hint=jane%40outlook.com" in url
    state = url.split("state=")[1].split("&")[0]

    async def fake_exchange(settings, code, redirect=""):
        assert redirect == "http://testserver/mail/microsoft/callback"
        return {"refresh_token": "rt-1", "access_token": "at-1"}

    async def fake_email(token):
        return "jane@outlook.com"

    monkeypatch.setattr(microsoft_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(microsoft_oauth, "account_email", fake_email)
    assert "did%20not%20match" in client.get("/mail/microsoft/callback", params={"code": "c", "state": "1.bad"}).headers["location"]
    ok = client.get("/mail/microsoft/callback", params={"code": "c", "state": state})
    assert ok.headers["location"].startswith("/mail/accounts/") and "through%20Microsoft" in ok.headers["location"]
    mid = ok.headers["location"].split("/mail/accounts/")[1].split("?")[0]
    page = client.get("/mail/accounts")
    assert "Microsoft sign-in" in page.text and "jane@outlook.com" in page.text
    account = client.get(f"/mail/accounts/{mid}")
    assert "Connected through Microsoft (full mode)" in account.text

    ws = _user_ws(client)
    db = ws.mail_db()
    asyncio.run(db.update_mailbox(mid, status="reconnect", status_detail="Microsoft no longer accepts this sign-in"))
    page = client.get("/mail/accounts")
    assert 'action="/mail/microsoft/connect"' in page.text and 'value="jane@outlook.com"' in page.text
    account = client.get(f"/mail/accounts/{mid}")
    assert "Reconnect with Microsoft" in account.text and "needs reconnecting" in account.text
    # Signing in again puts the same mailbox right, keeping its settings.
    asyncio.run(db.update_mailbox(mid, settings=MailboxSettings(signature_html="J", project_link="https://j.com").model_dump()))
    state = client.post("/mail/microsoft/connect", data={"email": "jane@outlook.com"}).headers["location"].split("state=")[1].split("&")[0]
    again = client.get("/mail/microsoft/callback", params={"code": "c", "state": state})
    assert again.headers["location"].startswith(f"/mail/accounts/{mid}")
    box = asyncio.run(db.mailbox(mid))
    assert box["status"] == "ok" and box["settings"]["project_link"] == "https://j.com"


def test_mail_microsoft_connect_needs_the_site_app(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(Workspace, "mail_worker_running", lambda self: None)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.settings = settings
        _as_user(client)
        assert "no%20Microsoft%20app%20registration" in client.post("/mail/microsoft/connect").headers["location"]
        assert "Not switched on yet" in client.get("/mail/accounts").text
