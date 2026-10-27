"""The hosted web app: admin-made accounts, per-user workspaces, the profile
that becomes harvey.yaml, recipient upload, outbox review, Gmail OAuth over
the web redirect flow, and the worker process (stubbed)."""

import asyncio
import json
import sqlite3
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

import bb.workspaces as ws_mod
from bb import gmail_oauth
from bb.accounts import Accounts, hash_password, verify_password
from bb.app import create_app
from bb.settings import Settings
from bb.workspaces import Workspace
from harvey.config import HarveyConfig
from harvey.state import StateManager

ADMIN = {"email": "admin@example.com", "password": "correct-horse-battery"}
USER = {"email": "jane@example.com", "password": "jane-password-123"}


def _settings(tmp_path, **extra) -> Settings:
    env = {
        "BB_HOME": str(tmp_path / "bb"), "BB_BASE_URL": "http://testserver",
        "BB_SECRET_KEY": "test-secret", "BB_ADMIN_EMAIL": ADMIN["email"],
        "BB_ADMIN_PASSWORD": ADMIN["password"],
        "GMAIL_CLIENT_ID": "cid.apps.googleusercontent.com", "GMAIL_CLIENT_SECRET": "csec",
        **extra,
    }
    return Settings(env=env)


@pytest.fixture
def settings(tmp_path):
    return _settings(tmp_path)


async def _dns_stub(domain):
    return {"domain": domain, "public": domain == "gmail.com", "score": 1, "checks": [
        {"name": "SPF", "ok": True, "detail": "v=spf1 ok", "fix": ""},
        {"name": "DKIM", "ok": False, "detail": "No DKIM key found.", "fix": "Turn on DKIM at your provider."},
    ]}


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)  # never touch real DNS in tests
    with TestClient(create_app(settings), follow_redirects=False) as c:
        c.settings = settings
        yield c


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password})
    return r


def _admin(client):
    assert _login(client, **ADMIN).status_code == 303
    return client


def _make_user(client):
    r = client.post("/admin/users", data={"email": USER["email"], "password": USER["password"]})
    assert r.status_code == 303 and "Created" in r.headers["location"]
    return client.settings and Accounts(client.settings.accounts_db).get_by_email(USER["email"])


def _as_user(client):
    _admin(client)
    _make_user(client)
    client.cookies.clear()
    assert _login(client, **USER).status_code == 303
    return client


PROFILE = {
    "mode": "inquiry", "intent": "find a financial advisor for a family portfolio",
    "reply_instructions": "Offer a call on Tuesday afternoons.", "attach_on": "first_reply",
    "name": "Jane Doe", "company": "Acme Research", "role": "Founder",
    "linkedin": "https://linkedin.com/in/janedoe", "email": "jane@acme-research.com",
    "tone": "friendly and direct", "offer_name": "Acme Insights",
    "offer_description": "A monthly report on logistics pricing.", "benefits": "Saves a day a month\nNo setup",
    "pricing": "", "goal": "get_reply", "booking_url": "", "meeting_duration": "15 minutes",
    "industries": "Logistics, Retail", "titles": "Head of Ops", "company_size": "10-200",
    "geography": "United Kingdom", "max_daily_sends": "25", "max_daily_cost_usd": "3",
    "quiet_start": "20:00", "quiet_end": "08:00", "timezone": "Europe/London",
}


# ── accounts ──


def test_password_hashing_roundtrip():
    stored = hash_password("s3cret-password")
    assert stored.startswith("scrypt$") and verify_password("s3cret-password", stored)
    assert not verify_password("wrong", stored) and not verify_password("x", "garbage")


def test_admin_is_bootstrapped_and_login_works(client):
    assert client.get("/").status_code == 303  # not signed in → /login
    assert client.get("/").headers["location"] == "/login"
    bad = _login(client, ADMIN["email"], "nope")
    assert bad.status_code == 200 and "Wrong email or password" in bad.text
    good = _login(client, **ADMIN)
    assert good.status_code == 303 and "bb_session" in good.cookies
    page = client.get("/")
    assert page.status_code == 200 and "Dashboard" in page.text and ADMIN["email"] in page.text
    assert client.get("/admin/users").status_code == 200
    # Second boot keeps the one admin (no duplicate bootstrap).
    with TestClient(create_app(client.settings)) as again:
        assert Accounts(client.settings.accounts_db).count() == 1


def test_login_is_throttled(client):
    for _ in range(10):
        _login(client, ADMIN["email"], "nope")
    r = _login(client, **ADMIN)
    assert r.status_code == 200 and "Too many attempts" in r.text


def test_admin_creates_users_and_users_are_not_admins(client):
    _admin(client)
    weak = client.post("/admin/users", data={"email": USER["email"], "password": "short"})
    assert "at%20least%2010" in weak.headers["location"]
    user = _make_user(client)
    assert user["role"] == "user" and not user["disabled"]
    dup = client.post("/admin/users", data={"email": USER["email"], "password": USER["password"]})
    assert "already%20exists" in dup.headers["location"]

    client.cookies.clear()
    assert _login(client, **USER).status_code == 303
    assert client.get("/admin/users").status_code == 403
    assert client.post("/admin/users", data={"email": "x@y.io", "password": "abcdefghijk"}).status_code == 403

    # Disabled users are signed out and can't sign in again.
    client.cookies.clear()
    _admin(client)
    assert client.post(f"/admin/users/{user['id']}/disable").status_code == 303
    client.cookies.clear()
    assert _login(client, **USER).status_code == 200
    _admin(client)
    client.post(f"/admin/users/{user['id']}/enable")
    client.post(f"/admin/users/{user['id']}/password", data={"password": "a-new-password-1"})
    client.cookies.clear()
    assert _login(client, USER["email"], USER["password"]).status_code == 200
    assert _login(client, USER["email"], "a-new-password-1").status_code == 303


def test_cross_site_posts_are_refused(client):
    _admin(client)
    r = client.post("/worker/start", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/sending/pause", headers={"Origin": "http://testserver"})
    assert r.status_code == 303


# ── workspace: profile → harvey.yaml ──


def test_profile_writes_a_valid_harvey_config(client):
    _as_user(client)
    r = client.post("/profile", data=PROFILE)
    assert r.status_code == 303, r.text
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    cfg = yaml.safe_load(ws.config_file.read_text())
    loaded = HarveyConfig(**cfg)
    assert loaded.persona.name == "Jane Doe"
    assert loaded.persona.email == "jane@acme-research.com"  # nothing connected yet: the typed one
    assert loaded.outreach.mode == "inquiry" and "family portfolio" in loaded.outreach.intent
    assert loaded.outreach.reply_instructions == "Offer a call on Tuesday afternoons."
    assert loaded.outreach.attach_on == "first_reply" and loaded.outreach.reply_attachments == []
    assert loaded.outreach.personalize_each is False  # web default: one email per segment
    client.post("/profile", data={**PROFILE, "writing": "person"})
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.personalize_each is True
    client.post("/profile", data=PROFILE)
    assert loaded.product.key_benefits == ["Saves a day a month", "No setup"]
    assert loaded.icp.auto_prospect is False  # unchecked box: only the uploaded list
    assert loaded.channels.email.provider == "gmail"
    assert loaded.channels.email.require_approval is False  # unchecked: fully automatic
    assert loaded.channels.email.max_daily_sends == 25
    assert loaded.usage.max_daily_cost_usd == 3.0
    assert loaded.usage.quiet_hours.timezone == "Europe/London"
    assert loaded.brain.provider == "claude"
    knowledge = (ws.skills_dir / "product_knowledge.md").read_text()
    assert "Acme Insights" in knowledge and "Saves a day a month" in knowledge
    assert "family portfolio" in knowledge and "nothing is being sold" in knowledge
    # The form round-trips.
    page = client.get("/profile")
    assert "Jane Doe" in page.text and "Acme Insights" in page.text

    # Checked boxes flip both flags; a bad value is reported, not written.
    r = client.post("/profile", data={**PROFILE, "auto_prospect": "on", "require_approval": "on"})
    loaded = HarveyConfig(**yaml.safe_load(ws.config_file.read_text()))
    assert loaded.icp.auto_prospect is True and loaded.channels.email.require_approval is True
    r = client.post("/profile", data={**PROFILE, "timezone": "Mars/Olympus"})
    assert r.status_code == 200 and "Could not save" in r.text and "timezone" in r.text
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).usage.quiet_hours.timezone == "Europe/London"
    r = client.post("/profile", data={**PROFILE, "name": ""})
    assert r.status_code == 200 and "required" in r.text
    r = client.post("/profile", data={**PROFILE, "intent": "work with you"})
    assert r.status_code == 200 and "Say what you want from the people you write to" in r.text


# ── recipients ──


CSV = """Email,Name,Company,Job Title,LinkedIn,Notes
jane@acme.com,Jane Doe,Acme,Head of Growth,https://linkedin.com/in/janedoe,Spoke at SaaStr
bob@gmail.com,Bob,,,,"Freelancer, met at a meetup"
JANE@acme.com,Jane Again,Acme,,,
not-an-email,Nobody,,,,
sam@beta.io,,Beta Industries,CTO,,
ada.okafor@gamma.ng,,Gamma,,,
pat@delta.co,,Delta,,https://linkedin.com/in/pat-lee-7a6b5c,
info@delta.co,,Delta,,,
lee@north-west-build.co.uk,,,,,
"""


def test_recipient_upload_creates_prospects_and_companies(client):
    _as_user(client)
    r = client.post("/recipients", files={"file": ("list.csv", CSV, "text/csv")})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "Added%207" in loc and "1%20already" in loc and "1%20skipped" in loc

    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    db = sqlite3.connect(str(ws.db_path))
    rows = db.execute(
        "SELECT email, first_name, last_name, email_status, status, company, title, linkedin_url, "
        "personalization_notes FROM prospects ORDER BY email").fetchall()
    by_email = {r[0]: r for r in rows}
    assert sorted(by_email) == ["ada.okafor@gamma.ng", "bob@gmail.com", "info@delta.co", "jane@acme.com",
                                "lee@north-west-build.co.uk", "pat@delta.co", "sam@beta.io"]
    # No company column: the domain becomes a readable name, never "north-west-build.co.uk".
    assert by_email["lee@north-west-build.co.uk"][5] == "North West Build"
    # Names the rows left blank are worked out where it is safe to.
    ada = by_email["ada.okafor@gamma.ng"]
    assert ada[1:3] == ("Ada", "Okafor") and "email address" in ada[8]
    pat = by_email["pat@delta.co"]
    assert pat[1:3] == ("Pat", "Lee") and "LinkedIn URL" in pat[8]
    shared = by_email["info@delta.co"]
    assert shared[1:3] == ("", "") and "Shared inbox" in shared[8]
    rows = [by_email["bob@gmail.com"], by_email["jane@acme.com"], by_email["sam@beta.io"]]
    jane = rows[1]
    assert jane[1:5] == ("Jane", "Doe", "provided", "new")
    assert jane[5] == "Acme" and jane[6] == "Head of Growth"
    assert "linkedin.com/in/janedoe" in jane[7] and "SaaStr" in jane[8]
    sam = rows[2]
    assert "No name was given" in sam[8] and sam[5] == "Beta Industries"
    # Uploading turns on the free signals, so the worker researches each company's site.
    confirmed = {r[0] for r in db.execute("SELECT code FROM signal_codes WHERE status = 'confirmed'")}
    assert {"TECH_STACK", "HIRING_ROLE", "CONTACT_FOUND"} <= confirmed
    assert not {"SERP_RANK", "EMAIL_STATUS"} & confirmed          # nothing that costs credits
    companies = {r[0]: r for r in db.execute("SELECT name, domain, website, source FROM companies")}
    assert companies["Acme"][1:] == ("acme.com", "https://acme.com", "upload")
    assert companies["Beta Industries"][1] == "beta.io"
    assert "gmail.com" not in {c[1] for c in companies.values()}  # public mailbox ≠ a company

    # Re-uploading the same file adds nobody new.
    r = client.post("/recipients", data={"pasted": CSV})
    assert "Added%200" in r.headers["location"]

    summary = client.get("/api/summary").json()
    assert summary["recipients"] == 7 and summary["waiting"] == 7 and summary["not_sent"] == 7
    page = client.get("/recipients")
    assert "jane@acme.com" in page.text and "Beta Industries" in page.text
    assert "waiting to be sorted" in page.text
    db.execute("UPDATE prospects SET industry = 'construction', seniority = 'executive' WHERE email = 'jane@acme.com'")
    db.execute("UPDATE prospects SET industry = 'construction', seniority = 'admin' WHERE email = 'sam@beta.io'")
    db.commit()
    page = client.get("/recipients")
    assert "Construction" in page.text and "1 executive" in page.text and "1 admin" in page.text
    assert "5 not sorted yet" in page.text


def test_pasted_addresses_without_a_header(client):
    _as_user(client)
    r = client.post("/recipients", data={"pasted": "a@one.com\nb@two.org\na@one.com\n"})
    assert "Added%202" in r.headers["location"]
    assert client.post("/recipients", data={"pasted": "  "}).headers["location"].startswith("/recipients?error=")


def test_upload_limit(client, monkeypatch):
    import bb.recipients as rec
    monkeypatch.setattr(rec, "MAX_ROWS", 2)
    _as_user(client)
    r = client.post("/recipients", data={"pasted": "a@x.com\nb@x.com\nc@x.com\n"})
    assert "Added%202" in r.headers["location"] and "1%20beyond" in r.headers["location"]


# ── outbox ──


def _seed_outbox(ws: Workspace, prefix: str = "o"):
    async def seed():
        state = ws.state()
        await state.init_db()
        async with state._connect() as db:
            for i, status in enumerate(["pending_review", "pending_review", "approved"]):
                await db.execute(
                    "INSERT INTO outbox (id, prospect_id, to_email, subject, body, status, kind, step) "
                    "VALUES (?, '', ?, ?, ?, ?, 'sequence', 1)",
                    (f"{prefix}{i}", f"p{i}@x.com", f"Hello {i}", "body", status))
            await db.commit()
    asyncio.run(seed())


def test_outbox_review(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    _seed_outbox(ws)
    page = client.get("/outbox")
    assert "Hello 0" in page.text and "Hello 1" in page.text and "Hello 2" not in page.text
    assert client.post("/outbox/o0/reject").status_code == 303
    assert client.post("/outbox/o1/approve").status_code == 303
    assert client.post("/outbox/missing/approve").status_code == 404
    db = sqlite3.connect(str(ws.db_path))
    assert dict(db.execute("SELECT id, status FROM outbox")) == {"o0": "rejected", "o1": "approved", "o2": "approved"}
    summary = client.get("/api/summary").json()
    assert summary["outbox_scheduled"] == 2 and summary["outbox_rejected"] == 1
    _seed_outbox(Workspace(client.settings, user), prefix="q")  # two more pending
    assert "Approved%202" in client.post("/outbox/approve-all").headers["location"]
    assert db.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'").fetchone()[0] == 0


# ── Gmail (web redirect flow) ──


def test_gmail_connect_round_trip(client, monkeypatch):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    r = client.post("/gmail/connect")
    assert r.status_code == 303
    url = r.headers["location"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fgmail%2Fcallback" in url
    assert "gmail.modify" in url and "access_type=offline" in url
    state = url.split("state=")[1].split("&")[0]

    async def fake_exchange(settings, code):
        assert code == "the-code"
        return {"refresh_token": "rt-1", "access_token": "at-1", "scope": "gmail.modify"}

    async def fake_email(token):
        assert token == "at-1"
        return "jane.sends@gmail.com"

    monkeypatch.setattr(gmail_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(gmail_oauth, "account_email", fake_email)
    bad = client.get("/gmail/callback", params={"code": "the-code", "state": "1.deadbeef"})
    assert "did%20not%20match" in bad.headers["location"]
    ok = client.get("/gmail/callback", params={"code": "the-code", "state": state})
    assert "connected%20as%20jane.sends%40gmail.com" in ok.headers["location"]
    assert ok.headers["location"].startswith("/mailbox")

    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    token = json.loads(ws.gmail_token_file.read_text())
    assert token["refresh_token"] == "rt-1"  # the format harvey.integrations.gmail reads
    assert ws.gmail_account()["email"] == "jane.sends@gmail.com"
    # The persona now sends from the connected mailbox.
    assert yaml.safe_load(ws.config_file.read_text())["persona"]["email"] == "jane.sends@gmail.com"
    assert client.get("/gmail").headers["location"] == "/mailbox"  # old address still works
    page = client.get("/mailbox")
    assert "Connected" in page.text and "jane.sends@gmail.com" in page.text
    summary = client.get("/api/summary").json()
    assert summary["gmail"] is True and summary["mailbox"] == "gmail"

    assert client.post("/gmail/disconnect").status_code == 303
    assert not ws.gmail_token_file.exists() and client.get("/api/summary").json()["gmail"] is False


def test_gmail_connect_needs_a_server_client(tmp_path):
    settings = _settings(tmp_path, GMAIL_CLIENT_ID="", GMAIL_CLIENT_SECRET="")
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _login(client, **ADMIN)
        assert "no%20Google%20OAuth%20client" in client.post("/gmail/connect").headers["location"]
        assert "Sign in with Google is off" in client.get("/").text  # admin sees the gap and where to fix it


# ── worker process ──


async def _accept_key(key, workspace_id=""):
    return (True, "Key accepted.") if key.startswith("sk-ant-") else (False, "Anthropic rejected that key.")


async def _smtp_ok(smtp):
    return (True, f"SMTP and IMAP OK for {smtp['smtp_username']}") if smtp["smtp_password"] != "bad" \
        else (False, "login failed")


async def _endpoint_ok(base_url, api_key, model):
    if api_key == "bad":
        return False, "That provider rejected the key."
    return True, "Connected."


def test_ai_provider_anthropic_is_checked_and_stored_per_user(client, monkeypatch):
    _as_user(client)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    page = client.get("/apikey")
    assert "Not set up yet" in page.text and "free tier" in page.text
    bad = client.post("/apikey", data={"provider": "anthropic", "api_key": "nope"})
    assert "rejected" in bad.headers["location"]
    ok = client.post("/apikey", data={"api_key": "sk-ant-abcdefghijklmnop"})  # provider inferred from the key
    assert "Anthropic%20Claude%20saved" in ok.headers["location"]
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert ws.anthropic_key() == "sk-ant-abcdefghijklmnop" and ws.llm()["provider"] == "anthropic"
    assert ws.llm()["workspace_id"] == ""

    # A user-scoped key with its workspace: the id is checked with the key and handed to the worker.
    seen = {}
    async def accept_with_workspace(key, workspace_id=""):
        seen["ws"] = workspace_id
        return True, "Key accepted."
    monkeypatch.setattr("bb.app.check_anthropic_key", accept_with_workspace)
    ok = client.post("/apikey", data={"provider": "anthropic", "api_key": "sk-ant-usr-xyz",
                                       "workspace_id": " wrkspc_01ABC "})
    assert "saved" in ok.headers["location"] and seen["ws"] == "wrkspc_01ABC"
    assert ws.llm()["workspace_id"] == "wrkspc_01ABC"
    assert ws.worker_env()["ANTHROPIC_WORKSPACE_ID"] == "wrkspc_01ABC"
    assert "wrkspc_01ABC" in client.get("/apikey").text
    client.post("/apikey", data={"provider": "anthropic", "api_key": "sk-ant-abcdefghijklmnop"})
    assert ws.llm()["workspace_id"] == "" and "ANTHROPIC_WORKSPACE_ID" not in ws.worker_env()
    assert oct(ws.secrets_file.stat().st_mode)[-3:] == "600"
    page = client.get("/apikey")
    assert "Anthropic Claude" in page.text and "sk-ant-abcdefghijklmnop" not in page.text  # masked
    assert client.get("/api/summary").json()["api_key"] is True
    assert ws.brain_section() == {"provider": "claude"}
    assert "removed" in client.post("/apikey/remove").headers["location"]
    assert ws.anthropic_key() == "" and client.get("/api/summary").json()["api_key"] is False


@pytest.mark.asyncio
async def test_anthropic_key_check_explains_what_anthropic_said(monkeypatch):
    import anthropic
    import httpx
    from bb.app import check_anthropic_key

    ok, detail = await check_anthropic_key("sk-ant-oat01-" + "x" * 40)
    assert not ok and "sign-in token" in detail

    def fake_client(status, body):
        class Models:
            async def list(self, **kw):
                request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
                response = httpx.Response(status, json=body, request=request)
                if status == 400:
                    raise anthropic.BadRequestError(body["error"]["message"], response=response, body=body)
                raise anthropic.InternalServerError("boom", response=response, body=body)
        class Client:
            def __init__(self, **kw):
                self.models = Models()
        return Client

    no_credit = {"type": "error", "error": {"type": "invalid_request_error",
                                             "message": "Your credit balance is too low to access the Anthropic API."}}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_client(400, no_credit))
    ok, detail = await check_anthropic_key("sk-ant-api03-" + "x" * 40)
    assert not ok and "no credit" in detail and "Billing" in detail and "Gemini" in detail

    other = {"type": "error", "error": {"type": "invalid_request_error", "message": "limit: must be <= 1000"}}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_client(400, other))
    ok, detail = await check_anthropic_key("sk-ant-api03-" + "x" * 40)
    assert not ok and detail == "Anthropic refused the request: limit: must be <= 1000"

    unscoped = {"type": "error", "error": {"type": "invalid_request_error", "message":
                "This API key is not scoped to a workspace, so this request must include the "
                "anthropic-workspace-id header with the ID of the workspace to use."}}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_client(400, unscoped))
    ok, detail = await check_anthropic_key("sk-ant-usr-" + "x" * 40)
    assert not ok and "Workspace ID box" in detail and "Settings → Workspaces" in detail

    outage = {"type": "error", "error": {"type": "api_error", "message": "Overloaded"}}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_client(500, outage))
    ok, detail = await check_anthropic_key("sk-ant-api03-" + "x" * 40)
    assert not ok and detail == "Anthropic answered 500: Overloaded"


def test_ai_provider_free_endpoint(client, monkeypatch):
    _as_user(client)
    monkeypatch.setattr("bb.app.check_openai_compatible", _endpoint_ok)
    client.post("/profile", data=PROFILE)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert "key%20first" in client.post("/apikey", data={"provider": "gemini"}).headers["location"]
    assert "rejected" in client.post("/apikey", data={"provider": "gemini", "api_key": "bad"}).headers["location"]
    r = client.post("/apikey", data={"provider": "gemini", "api_key": "AIza-free-key"})
    assert "Google%20Gemini%20saved" in r.headers["location"]
    llm = ws.llm()
    assert llm["provider"] == "gemini" and llm["model"] == "gemini-2.5-flash"
    assert llm["base_url"].startswith("https://generativelanguage.googleapis.com") and llm["free"]
    assert ws.llm_configured() and ws.anthropic_key() == ""
    # harvey.yaml now points the worker at the endpoint, and the env carries the key.
    cfg = HarveyConfig(**yaml.safe_load(ws.config_file.read_text()))
    assert cfg.brain.provider == "openai" and cfg.brain.model == "gemini-2.5-flash"
    assert cfg.brain.base_url == llm["base_url"]
    env = ws.worker_env()
    assert env["LLM_API_KEY"] == "AIza-free-key" and "ANTHROPIC_API_KEY" not in env
    # A blank key on re-save keeps the stored one; a custom model is kept.
    client.post("/apikey", data={"provider": "gemini", "api_key": "", "model": "gemini-2.5-pro"})
    assert ws.llm()["api_key"] == "AIza-free-key" and ws.llm()["model"] == "gemini-2.5-pro"
    # Ollama needs no key at all.
    r = client.post("/apikey", data={"provider": "ollama", "api_key": "", "model": "llama3.1"})
    assert "Ollama" in r.headers["location"] and ws.llm_configured() and ws.llm()["api_key"] == ""
    assert "LLM_API_KEY" not in ws.worker_env()
    assert "Set%20up%20your%20AI%20provider" not in client.post("/worker/start").headers["location"]


def test_mailbox_smtp_settings(client, monkeypatch):
    monkeypatch.setattr(ws_mod, "STOP_GRACE_SECONDS", 0.5)   # the fake worker never exits
    _as_user(client)
    monkeypatch.setattr("bb.app.test_smtp", _smtp_ok)
    client.post("/profile", data=PROFILE)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    page = client.get("/mailbox")
    assert "SMTP" in page.text and "Not set up" in page.text

    bad = client.post("/mailbox/smtp", data={"smtp_host": "smtp.example.com", "smtp_username": "me@example.com",
                                             "smtp_password": "bad"})
    assert "Not%20saved" in bad.headers["location"] and not ws.smtp_configured()
    ok = client.post("/mailbox/smtp", data={"smtp_host": "smtp.example.com", "smtp_port": "465",
                                            "smtp_username": "me@example.com", "smtp_password": "sekret-9f3q",
                                            "imap_host": "imap.example.com", "reply_to": "replies@example.com"})
    assert "Mailbox%20saved" in ok.headers["location"]
    assert ws.smtp_configured() and ws.mail_provider() == "smtp" and ws.sending_address() == "me@example.com"
    # harvey.yaml follows: provider smtp, persona sends from the mailbox.
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["channels"]["email"]["provider"] == "smtp" and cfg["persona"]["email"] == "me@example.com"
    page = client.get("/mailbox")
    assert "me@example.com" in page.text and "sekret-9f3q" not in page.text

    # The worker gets the mailbox credentials, and only this user's.
    seen = {}
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    client.post("/apikey", data={"api_key": "sk-ant-user-key-2"})
    monkeypatch.setattr(ws_mod.subprocess, "Popen", lambda cmd, **kw: seen.update(kw) or _FakeProc())
    monkeypatch.setattr(ws_mod.os, "kill", lambda pid, sig: None)
    assert "Started" in client.post("/worker/start").headers["location"]
    env = seen["env"]
    assert env["SMTP_HOST"] == "smtp.example.com" and env["SMTP_PORT"] == "465"
    assert env["IMAP_HOST"] == "imap.example.com" and env["IMAP_USERNAME"] == "me@example.com"
    assert env["IMAP_PASSWORD"] == "sekret-9f3q" and env["ANTHROPIC_API_KEY"] == "sk-ant-user-key-2"
    assert env["SMTP_REPLY_TO"] == "replies@example.com"
    assert "replies@example.com" in client.get("/mailbox").text

    # A blank password on re-save keeps the stored one; removing clears it.
    client.post("/mailbox/smtp", data={"smtp_host": "smtp.example.com", "smtp_username": "me@example.com",
                                       "smtp_password": "", "smtp_port": "587"})
    assert ws.smtp_settings()["smtp_password"] == "sekret-9f3q" and ws.smtp_settings()["smtp_port"] == "587"
    assert "removed" in client.post("/mailbox/smtp/remove").headers["location"]
    assert not ws.smtp_configured()


def test_attachments_upload_and_delete(client):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    r = client.post("/attachments", files=[("files", ("My Brief (v2).pdf", b"%PDF", "application/pdf")),
                                            ("files", ("../../evil.sh", b"#!/bin/sh", "text/plain"))])
    assert r.status_code == 303 and "Added" in r.headers["location"]
    names = [a["name"] for a in ws.attachments()]
    assert names == ["My Brief (v2).pdf", "evil.sh"] or set(names) == {"My Brief (v2).pdf", "evil.sh"}
    assert not (ws.root / "evil.sh").exists()  # stayed inside attachments/
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert set(cfg["outreach"]["reply_attachments"]) == {"My Brief (v2).pdf", "evil.sh"}
    page = client.get("/profile")
    assert "My Brief (v2).pdf" in page.text
    assert client.get("/api/summary").json()["attachments"] == 2

    client.post("/attachments/evil.sh/delete")
    assert [a["name"] for a in ws.attachments()] == ["My Brief (v2).pdf"]
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["reply_attachments"] == ["My Brief (v2).pdf"]
    # Saving the profile keeps the folder as the source of truth.
    client.post("/profile", data={**PROFILE, "attach_on": "every_reply"})
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["outreach"]["reply_attachments"] == ["My Brief (v2).pdf"] and cfg["outreach"]["attach_on"] == "every_reply"
    assert client.post("/attachments", files=[("files", ("", b"", "text/plain"))]).headers["location"].startswith("/profile?error")


class _FakeProc:
    def __init__(self):
        self.pid = 4242


def test_worker_start_stop(client, monkeypatch):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)

    assert "AI%20provider%20first" in client.post("/worker/start").headers["location"]
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    assert "saved%20and%20verified" in client.post("/apikey", data={"api_key": "sk-ant-user-key-1"}).headers["location"]
    assert "profile%20first" in client.post("/worker/start").headers["location"]
    client.post("/profile", data=PROFILE)
    assert "mailbox%20first" in client.post("/worker/start").headers["location"]
    gmail_oauth.save_token(ws, {"refresh_token": "rt"}, "jane@acme-research.com")
    ws.set_mail_provider("gmail")

    seen = {}

    def fake_popen(cmd, **kwargs):
        alive.add(4242)   # a spawned process is alive at once
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return _FakeProc()

    alive = {4242}
    real_kill = ws_mod.os.kill

    def fake_kill(pid, sig):
        if pid == 4242:
            if sig == 0 and 4242 in alive:
                return None
            if sig in (ws_mod.signal.SIGTERM, ws_mod.signal.SIGKILL):
                alive.discard(4242)
                return None
            raise ProcessLookupError
        return real_kill(pid, sig)

    monkeypatch.setattr(ws_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ws_mod.os, "kill", fake_kill)
    monkeypatch.setattr(ws_mod, "_is_harvey_process", lambda pid: True)   # 4242 is not a real process here

    r = client.post("/worker/start")
    assert "Started%20%28pid%204242%29" in r.headers["location"]
    assert seen["cmd"] == [sys.executable, "-m", "harvey.main"]
    env = seen["kwargs"]["env"]
    assert env["HARVEY_HOME"] == str(ws.root)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-user-key-1"  # the user's, never the site's
    assert env["GMAIL_CLIENT_ID"].startswith("cid") and "SMTP_HOST" not in env
    assert not any(k.startswith("BB_") for k in env)
    assert seen["kwargs"]["start_new_session"] is True
    assert ws.pid_file.read_text() == "4242" and ws.autostart_file.exists()
    assert client.get("/api/summary").json()["worker_running"] is True
    assert "Already%20running" in client.post("/worker/start").headers["location"]

    r = client.post("/worker/stop")
    assert "Stopped" in r.headers["location"]
    assert not ws.pid_file.exists() and not ws.autostart_file.exists()
    assert client.get("/api/summary").json()["worker_running"] is False
    # The worker's log says who stopped it, so a user reading the tail is not left guessing.
    assert "BB is stopping the worker: Stop pressed on the dashboard." in ws.log_file.read_text()

    # A worker that dies on its own (never stopped from here) is started again
    # the next time the dashboard looks, and the page says so.
    alive.add(4242)
    client.post("/worker/start")
    assert ws.autostart_file.exists()
    alive.discard(4242)                      # the process vanished (crash, reboot)
    assert ws.worker_running() is None
    client.get("/api/summary")               # the next look revives it (fake_popen answers pid 4242)
    alive.add(4242)
    assert ws.worker_running() == 4242 and ws.autostart_file.exists()
    log = ws.log_file.read_text()
    assert "was not running although it was never stopped from here" in log
    # Stopped on purpose: nothing revives it.
    client.post("/worker/stop")
    alive.discard(4242)
    client.get("/")
    assert ws.worker_running() is None

    # Pause for edits: stopped, remembered as paused, not revived; Resume starts it again.
    client.post("/worker/start"); alive.add(4242)
    r = client.post("/worker/pause")
    alive.discard(4242)
    assert "Make%20your%20edits" in r.headers["location"]
    assert ws.worker_paused() and client.get("/api/summary").json()["worker_paused"] is True
    page = client.get("/").text
    assert "Paused for edits" in page and 'action="/worker/resume"' in page
    assert ws.worker_running() is None                       # revive_worker left it alone
    assert "paused for edits from the dashboard" in ws.log_file.read_text()
    r = client.post("/worker/resume"); alive.add(4242)
    assert "Resumed" in r.headers["location"] and not ws.worker_paused() and ws.worker_running() == 4242

    # Saving the profile restarts a running worker so the change applies.
    stops_before = ws.log_file.read_text().count("restarting to apply the new profile")
    r = client.post("/profile", data={**PROFILE, "style": "concise"})
    alive.add(4242)                          # fake_popen handed back the same pid
    assert "restarted" in r.headers["location"]
    assert ws.log_file.read_text().count("restarting to apply the new profile") == stops_before + 1
    assert ws.worker_running() == 4242 and ws.pid_file.exists()

    # Start over: worker stopped, data gone, settings kept.
    client.post("/sending/pause")            # creates the database with a setting in it
    assert ws.db_path.exists()
    r = client.post("/worker/reset")
    alive.discard(4242)
    assert "Everything%20was%20cleared" in r.headers["location"]
    assert not ws.db_path.exists() and not ws.autostart_file.exists()
    assert ws.is_configured() and ws.llm_configured() and ws.mailbox_connected()
    assert client.get("/api/summary").json()["recipients"] == 0
    assert "Start over" in client.get("/").text


def test_workers_autostart_on_boot(settings, monkeypatch):
    started = []
    monkeypatch.setattr(Workspace, "start_worker", lambda self: (started.append(self.user["email"]) or (True, "ok")))
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _login(client, **ADMIN)
        admin = Accounts(settings.accounts_db).get_by_email(ADMIN["email"])
        ws = Workspace(settings, admin).ensure()
        ws.autostart_file.write_text("1")
    assert started == []
    with TestClient(create_app(settings), follow_redirects=False):
        pass
    assert started == [ADMIN["email"]]


def test_sending_pause_resume(client):
    _as_user(client)
    assert client.post("/sending/pause").status_code == 303
    s = client.get("/api/summary").json()
    assert s["sending_paused"] == "paused from the web app"
    assert client.post("/sending/resume").status_code == 303
    assert client.get("/api/summary").json()["sending_paused"] == ""


# ── settings ──


def test_secret_key_is_generated_once(tmp_path):
    s = _settings(tmp_path, BB_SECRET_KEY="")
    first = s.secret_key()
    assert len(first) > 40 and (s.home / "secret.key").read_text() == first
    assert _settings(tmp_path, BB_SECRET_KEY="").secret_key() == first


# ── terminal account management ──


def test_cli_creates_and_resets_accounts(tmp_path, monkeypatch, capsys):
    import bb.__main__ as cli

    monkeypatch.setenv("BB_HOME", str(tmp_path / "bbcli"))
    monkeypatch.setenv("BB_BASE_URL", "http://localhost:8000")
    for k in ("BB_ADMIN_EMAIL", "BB_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    cli.main(["list-users"])
    assert "No accounts yet" in capsys.readouterr().out
    cli.main(["create-user", "Boss@Example.com", "--admin", "--password", "a-long-password-1"])
    out = capsys.readouterr().out
    assert "Created admin boss@example.com" in out and "localhost:8000" in out
    with pytest.raises(SystemExit):
        cli.main(["create-user", "boss@example.com", "--password", "a-long-password-1"])
    assert "already exists" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["create-user", "weak@example.com", "--password", "short"])
    assert "at least 10" in capsys.readouterr().out

    cli.main(["reset-password", "boss@example.com", "--password", "another-long-one-2"])
    assert "Password changed" in capsys.readouterr().out
    accounts = Accounts(Settings().accounts_db)
    assert accounts.authenticate("boss@example.com", "another-long-one-2")
    assert not accounts.authenticate("boss@example.com", "a-long-password-1")
    accounts.set_disabled(accounts.get_by_email("boss@example.com")["id"], True)
    cli.main(["reset-password", "boss@example.com", "--password", "third-long-one-33"])
    assert "enabled again" in capsys.readouterr().out
    cli.main(["list-users"])
    assert "boss@example.com" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["reset-password", "nobody@example.com", "--password", "whatever-long-1"])

    # The site itself sees the same account and doesn't re-bootstrap.
    with TestClient(create_app(Settings()), follow_redirects=False) as client:
        assert client.post("/login", data={"email": "boss@example.com", "password": "third-long-one-33"}).status_code == 303


@pytest.mark.asyncio
async def test_model_check_accepts_gemini_style_prefixed_ids(monkeypatch):
    import httpx

    from bb.app import check_openai_compatible

    listing = {"data": [{"id": "models/gemini-2.5-flash"}, {"id": "models/gemini-2.5-pro"},
                        {"id": "models/text-embedding-004"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            assert url.endswith("/models") and headers == {"Authorization": "Bearer k"}
            return httpx.Response(200, json=listing, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    base = "https://generativelanguage.googleapis.com/v1beta/openai"
    assert (await check_openai_compatible(base, "k", "gemini-2.5-flash")) == (True, "Connected.")
    assert (await check_openai_compatible(base, "k", "models/gemini-2.5-pro")) == (True, "Connected.")
    ok, detail = await check_openai_compatible(base, "k", "gemini-9")
    assert not ok and "gemini-2.5-flash" in detail and "embedding" not in detail


# ── speed, style, signature, Microsoft 365 admin app, deliverability ──


def test_profile_speed_style_and_signature(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    page = client.get("/profile").text
    assert "Writing style" in page and "Engaging" in page and "Email signature" in page
    assert "Gentle" in page and "Fast" in page and "Warm this mailbox up" in page

    r = client.post("/profile", data={**PROFILE, "speed": "fast", "style": "engaging", "warmup_ramp": "on",
                                       "signature_html": 'Jane<br><a href="https://acme.test">acme.test</a>{{logo}}'})
    assert r.status_code == 303
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["usage"]["heartbeat_interval_minutes"] == 1
    assert cfg["channels"]["email"]["sends_per_cycle"] == 25
    assert cfg["channels"]["email"]["warmup_ramp"] is True
    assert cfg["channels"]["email"]["list_unsubscribe"] is False   # a personal enquiry carries no bulk headers
    assert cfg["outreach"]["style"] == "engaging"
    assert cfg["outreach"]["signature_html"].startswith("Jane<br>")
    assert HarveyConfig(**cfg).outreach.style == "engaging"
    prof = ws.profile()
    assert prof["speed"] == "fast" and prof["style"] == "engaging" and prof["warmup_ramp"]
    assert "acme.test" in client.get("/profile").text

    # Unchecked warm-up on a save turns it off; a speed the form doesn't know falls back to normal.
    client.post("/profile", data={**PROFILE, "speed": "warp", "style": "concise"})
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["channels"]["email"]["warmup_ramp"] is False
    assert cfg["usage"]["heartbeat_interval_minutes"] == 2 and cfg["outreach"]["style"] == "concise"

    # Signature image: wrong type refused, good one stored and served, then removed.
    bad = client.post("/signature/image", files={"image": ("logo.svg", b"<svg/>", "image/svg+xml")})
    assert "PNG" in bad.headers["location"]
    assert client.get("/signature/image").status_code == 404
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    ok = client.post("/signature/image", files={"image": ("My Logo.png", png, "image/png")})
    assert "saved" in ok.headers["location"]
    assert ws.signature_image() == "My_Logo.png" or ws.signature_image().endswith(".png")
    served = client.get("/signature/image")
    assert served.status_code == 200 and served.content == png
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["signature_image"] == ws.signature_image()
    assert "embedded inline" in client.get("/profile").text
    too_big = client.post("/signature/image", files={"image": ("big.png", b"0" * (ws_mod.MAX_SIGNATURE_IMAGE + 1), "image/png")})
    assert "1%20MB" in too_big.headers["location"]
    assert "removed" in client.post("/signature/image/delete").headers["location"]
    assert ws.signature_image() == "" and client.get("/signature/image").status_code == 404
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["signature_image"] == ""


async def _m365_ok(settings_dict):
    return True, f"Connected as {settings_dict['m365_mailbox']} (admin app)"


async def _m365_bad(settings_dict):
    return False, "Graph 403: grant admin consent"


def test_mailbox_microsoft_admin_app(client, monkeypatch):
    monkeypatch.setattr(ws_mod, "STOP_GRACE_SECONDS", 0.5)   # the fake worker never exits
    _as_user(client)
    client.post("/profile", data=PROFILE)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    page = client.get("/mailbox").text
    assert "Option C" in page and "Microsoft 365 admin app" in page and "Grant admin consent" in page
    assert "Deliverability" in page and "Connect a mailbox above" in page

    form = {"m365_tenant_id": "tenant-1", "m365_client_id": "client-1",
            "m365_client_secret": "secret-1", "m365_mailbox": "Outreach@Corp.Example"}
    missing = client.post("/mailbox/m365", data={**form, "m365_mailbox": ""})
    assert "required" in missing.headers["location"] and not ws.m365_configured()
    monkeypatch.setattr("bb.app.test_m365", _m365_bad)
    bad = client.post("/mailbox/m365", data=form)
    assert "Not%20saved" in bad.headers["location"] and not ws.m365_configured()

    monkeypatch.setattr("bb.app.test_m365", _m365_ok)
    ok = client.post("/mailbox/m365", data=form)
    assert "Microsoft%20365%20saved" in ok.headers["location"]
    assert ws.m365_configured() and ws.mail_provider() == "microsoft_app"
    assert ws.sending_address() == "outreach@corp.example" and ws.mailbox_connected()
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["channels"]["email"]["provider"] == "microsoft_app"
    assert cfg["persona"]["email"] == "outreach@corp.example"
    page = client.get("/mailbox").text
    assert "outreach@corp.example" in page and "secret-1" not in page and "tenant-1" in page
    # Deliverability card renders the (stubbed) DNS report for the sending domain.
    assert "corp.example" in page and "No DKIM key found." in page and "Turn on DKIM" in page

    # The worker gets the app credentials; nothing from SMTP/Gmail.
    seen = {}
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    client.post("/apikey", data={"api_key": "sk-ant-user-key-3"})
    monkeypatch.setattr(ws_mod.subprocess, "Popen", lambda cmd, **kw: seen.update(kw) or _FakeProc())
    monkeypatch.setattr(ws_mod.os, "kill", lambda pid, sig: None)
    assert "Started" in client.post("/worker/start").headers["location"]
    env = seen["env"]
    assert env["M365_TENANT_ID"] == "tenant-1" and env["M365_CLIENT_ID"] == "client-1"
    assert env["M365_CLIENT_SECRET"] == "secret-1" and env["M365_MAILBOX"] == "outreach@corp.example"
    assert "SMTP_HOST" not in env

    # Blank secret keeps the saved one; test and remove work.
    client.post("/mailbox/m365", data={**form, "m365_client_secret": ""})
    assert ws.m365_settings()["m365_client_secret"] == "secret-1"
    assert "Connected" in client.post("/mailbox/m365/test").headers["location"]
    assert "removed" in client.post("/mailbox/m365/remove").headers["location"]
    assert not ws.m365_configured() and not ws.mailbox_connected()


@pytest.mark.asyncio
async def test_m365_check_uses_the_app_provider(monkeypatch):
    from bb.app import test_m365
    import harvey.integrations.microsoft as ms

    class Fake:
        def __init__(self, config, env, transport=None):
            Fake.env = env
        async def test_connection(self):
            return True, "Connected as outreach@corp.example (admin app)"

    monkeypatch.setattr(ms, "MicrosoftAppProvider", Fake)
    ok, detail = await test_m365({"m365_tenant_id": "t", "m365_client_id": "c",
                                  "m365_client_secret": "s", "m365_mailbox": "outreach@corp.example"})
    assert ok and "outreach@corp.example" in detail and Fake.env.m365_client_secret == "s"


def test_outbox_bounced_view(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)

    async def seed():
        state = ws.state()
        await state.init_db()
        from harvey.models.prospect import Prospect
        await state.add_prospect(Prospect(id="b1", email="x@gone.example", first_name="Pat", company="Gone Ltd",
                                          email_status="invalid"))
        sent = await state.add_outbox_item(prospect_id="b1", to_email="x@gone.example", subject="Roof quote",
                                           body="Hi Pat,\n\nCould you quote?", send_at="2026-01-01T00:00:00",
                                           status="approved")
        await state.update_outbox_item(sent, status="sent", sent_at="2026-01-01T00:00:00",
                                       error="Bounced: 550 5.1.1 no such user. The address does not exist.")
        ok = await state.add_outbox_item(prospect_id="b1", to_email="y@fine.example", subject="s", body="b",
                                         send_at="2026-01-01T00:00:00", status="approved")
        await state.update_outbox_item(ok, status="sent", sent_at="2026-01-01T00:00:00")
    asyncio.run(seed())

    page = client.get("/outbox?status=bounced").text
    assert "Bounced: 550 5.1.1 no such user" in page and "y@fine.example" not in page
    assert "Emails the receiving server sent back" in page
    assert "x@gone.example" in client.get("/outbox?status=sent").text
    assert 'href="/outbox?status=bounced"' in client.get("/").text
    assert client.get("/outbox?status=nonsense").status_code == 200


def test_profile_own_message_option(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert "Use my own message" in client.get("/profile").text

    r = client.post("/profile", data={**PROFILE, "writing": "template", "template_body": "short"})
    assert r.status_code == 200 and "Write the message you want sent" in r.text

    body = ("We are planning a 40 square metre kitchen extension in Lekki and want the work to start in March. "
            "Do you take on work like this, and how do you usually start?\n\nJane")
    r = client.post("/profile", data={**PROFILE, "writing": "template", "intent": "",
                                       "template_subject": "Kitchen extension in Lekki, looking for quotes",
                                       "template_body": body, "template_followup_1": "Hi {{first_name}},\n\nFollowing my note about the kitchen extension: could you take it on?"})
    assert r.status_code == 303 and "error=" not in r.headers["location"]   # the request sentence is not needed here
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["outreach"]["use_template"] is True and cfg["outreach"]["template_body"] == body
    assert cfg["outreach"]["template_subject"].startswith("Kitchen extension")
    assert cfg["outreach"]["template_followup_1"].startswith("Hi {{first_name}}")
    assert HarveyConfig(**cfg).outreach.use_template
    prof = ws.profile()
    assert prof["writing"] == "template" and body.split("\n")[0] in client.get("/profile").text

    # Marketing words in the operator's own message get a warning, but are saved.
    spammy = "Dear Sir/Madam, I hope this email finds you well. We offer the best solutions! Click here for a FREE quote. 100% guaranteed."
    r = client.post("/profile", data={**PROFILE, "writing": "template", "template_subject": "PARTNERSHIP OPPORTUNITY",
                                       "template_body": spammy})
    assert "land%20in%20junk%20folders" in r.headers["location"] and "Profile%20saved" in r.headers["location"]
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["template_body"] == spammy

    # Back to AI writing: template kept on disk but off.
    client.post("/profile", data={**PROFILE, "writing": "segment"})
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["outreach"]["use_template"] is False and ws.profile()["writing"] == "segment"


def test_profile_sector_versions_of_own_message(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    body = "We are planning a kitchen extension in Lekki and want the work to start in March. Do you take on work like this?\n\nJane"
    data = {**PROFILE, "writing": "template", "template_subject": "A request for your team", "template_body": body,
            "sm_sectors_0": ["construction", "real_estate"],
            "sm_subject_0": "Kitchen extension in Lekki, looking for quotes",
            "sm_body_0": "The builders' version of the message, long enough to count as a message.",
            "sm_followup_1_0": "Following my note about the kitchen extension.",
            "sm_sectors_1": ["financial_advisory"], "sm_body_1": "",                   # empty: dropped
            "sm_sectors_2": ["nonsense"], "sm_body_2": "A body with no valid sector is dropped too."}
    r = client.post("/profile", data=data)
    assert r.status_code == 303, r.text
    cfg = yaml.safe_load(ws.config_file.read_text())
    versions = cfg["outreach"]["sector_messages"]
    assert len(versions) == 1 and versions[0]["sectors"] == ["construction", "real_estate"]
    assert versions[0]["subject"].startswith("Kitchen") and versions[0]["followup_1"].startswith("Following")
    assert HarveyConfig(**cfg).outreach.sector_messages[0].sectors == ["construction", "real_estate"]
    page = client.get("/profile").text
    assert "Different versions for particular sectors" in page
    assert 'name="sm_sectors_0"' in page and "builders&#39; version" in page and "Add a version for other sectors" in page
    assert 'value="construction" selected' in page and 'value="real_estate" selected' in page
    assert ws.profile()["sector_messages"][0]["sectors"] == ["construction", "real_estate"]


def test_inbox_page_saves_settings_and_keeps_them_through_profile_saves(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert "profile" in client.post("/inbox", data={"inbox_form": "1", "inbox_autoreply": "on"}).headers["location"].lower() \
        or "Fill%20in%20your" in client.post("/inbox", data={"inbox_form": "1"}).headers["location"]
    client.post("/profile", data=PROFILE)
    page = client.get("/inbox").text
    assert "Inbox auto-reply" in page and "Documents it can share" in page and 'name="link_url_0"' in page

    r = client.post("/inbox", data={"inbox_form": "1", "inbox_autoreply": "on", "inbox_attach_files": "on",
                                     "inbox_instructions": "Send the brochure when they ask what we do.",
                                     "link_label_0": "Brochure", "link_url_0": "drive.google.com/brochure",
                                     "link_when_0": "they ask what we do",
                                     "link_label_1": "", "link_url_1": "", "link_when_1": "",
                                     "link_label_2": "Booking", "link_url_2": "https://cal.com/jane", "link_when_2": "they want a call"})
    assert "auto-reply%20is%20on" in r.headers["location"]
    cfg = yaml.safe_load(ws.config_file.read_text())["outreach"]
    assert cfg["inbox_autoreply"] is True and cfg["inbox_attach_files"] is True
    assert cfg["inbox_instructions"].startswith("Send the brochure")
    assert [l["label"] for l in cfg["inbox_links"]] == ["Brochure", "Booking"]
    assert HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.inbox_links[0].url == "https://drive.google.com/brochure"
    page = client.get("/inbox").text
    assert 'value="Brochure"' in page and 'value="https://cal.com/jane"' in page and "checked" in page

    # Saving the profile again does not lose the inbox settings.
    client.post("/profile", data={**PROFILE, "style": "concise"})
    cfg = yaml.safe_load(ws.config_file.read_text())["outreach"]
    assert cfg["inbox_autoreply"] is True and len(cfg["inbox_links"]) == 2

    # Switching it off keeps the instructions and links for later.
    r = client.post("/inbox", data={"inbox_form": "1", "inbox_instructions": "Send the brochure when they ask what we do.",
                                     "link_label_0": "Brochure", "link_url_0": "https://drive.google.com/brochure"})
    assert "auto-reply%20is%20off" in r.headers["location"]
    cfg = yaml.safe_load(ws.config_file.read_text())["outreach"]
    assert cfg["inbox_autoreply"] is False and cfg["inbox_links"][0]["label"] == "Brochure"

    # Inbox replies are listed on the page, and inbox contacts are not counted as recipients.
    async def seed():
        state = ws.state(); await state.init_db()
        from harvey.models.prospect import Prospect
        await state.add_prospect(Prospect(id="ib1", email="sam@new.example", first_name="Sam", source="inbox",
                                          status="replied", email_status="provided"))
        await state.add_outbox_item(prospect_id="ib1", to_email="sam@new.example", subject="Re: What do you do?",
                                    body="Hi Sam,\n\nHere is the brochure.\n\nBrochure: https://x", send_at="2026-01-01T00:00:00",
                                    status="pending_review", kind="inbox_reply")
    asyncio.run(seed())
    page = client.get("/inbox").text
    assert "sam@new.example" in page and "Here is the brochure" in page and 'action="/outbox/' in page
    assert client.get("/api/summary").json()["recipients"] == 0
    assert "Inbox" in client.get("/").text


def test_mailbox_email_login_works_out_the_servers(client, monkeypatch):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert "Log in with your email" in client.get("/mailbox").text

    seen = {}
    async def smtp_ok(smtp):
        seen.update(smtp)
        return (True, "Connected.") if smtp["smtp_password"] == "app-pass" else (False, "535 bad credentials")
    monkeypatch.setattr("bb.app.test_smtp", smtp_ok)
    monkeypatch.setattr("bb.mail_autoconfig._mx_hosts", lambda d: ["aspmx.l.google.com"] if d == "acme.test" else [])

    r = client.post("/mailbox/login", data={"email": "Me@Gmail.com", "password": "wrong"})
    assert "app%20password" in r.headers["location"] and not ws.smtp_configured()
    r = client.post("/mailbox/login", data={"email": "me@gmail.com", "password": "app-pass"})
    assert "Signed%20in%20to%20me%40gmail.com" in r.headers["location"]
    assert seen["smtp_host"] == "smtp.gmail.com" and seen["imap_host"] == "imap.gmail.com" and seen["smtp_port"] == "587"
    assert ws.smtp_configured() and ws.mail_provider() == "smtp" and ws.sending_address() == "me@gmail.com"

    # A custom domain hosted on Google Workspace is found through its mail records.
    client.post("/mailbox/login", data={"email": "jane@acme.test", "password": "app-pass"})
    assert seen["smtp_host"] == "smtp.gmail.com" and seen["smtp_username"] == "jane@acme.test"
    # An unknown host gets the conventional names.
    client.post("/mailbox/login", data={"email": "jane@unknown-host.example", "password": "app-pass"})
    assert seen["smtp_host"] == "smtp.unknown-host.example" and seen["imap_host"] == "imap.unknown-host.example"
    r = client.post("/mailbox/login", data={"email": "x@proton.me", "password": "app-pass"})
    assert "no%20plain%20SMTP" in r.headers["location"]


def test_inbox_backlog_option(client):
    _as_user(client)
    client.post("/profile", data=PROFILE)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    page = client.get("/inbox").text
    assert "Also unanswered mail from the last 30 days" in page
    client.post("/inbox", data={"inbox_form": "1", "inbox_autoreply": "on", "inbox_backlog_days": "30",
                                "inbox_instructions": "Answer briefly."})
    cfg = yaml.safe_load(ws.config_file.read_text())["outreach"]
    assert cfg["inbox_backlog_days"] == 30 and HarveyConfig(**yaml.safe_load(ws.config_file.read_text())).outreach.inbox_backlog_days == 30
    assert 'value="30" selected' in client.get("/inbox").text
    client.post("/profile", data={**PROFILE, "style": "concise"})
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["inbox_backlog_days"] == 30
    client.post("/inbox", data={"inbox_form": "1", "inbox_autoreply": "on", "inbox_backlog_days": "abc"})
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["inbox_backlog_days"] == 0


def test_profile_ask_theme_roundtrip_and_dashboard_activity_feed(client, monkeypatch):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    page = client.get("/profile").text
    assert "What you'd like to happen next" in page and "A video call" in page and "Just a reply" in page
    r = client.post("/profile", data={**PROFILE, "ask": "coffee", "ask_detail": "Tuesdays after 2pm"})
    assert r.status_code == 303
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["outreach"]["ask"] == "coffee" and cfg["outreach"]["ask_detail"] == "Tuesdays after 2pm"
    assert HarveyConfig(**cfg).outreach.ask == "coffee"
    assert ws.profile()["ask"] == "coffee" and 'value="coffee" checked' in client.get("/profile").text
    client.post("/profile", data={**PROFILE, "ask": "nonsense"})
    assert yaml.safe_load(ws.config_file.read_text())["outreach"]["ask"] == ""

    # The dashboard feed: parsed, de-noised, folded; the raw log is still there behind a toggle.
    ws.ensure()
    ws.log_file.parent.mkdir(parents=True, exist_ok=True)
    import time as _t
    today = _t.strftime("%Y-%m-%d")
    ws.log_file.write_text("\n".join([
        f"{today} 09:00:01,100 [harvey.main] INFO: Checking pipeline state...",
        f"{today} 09:00:01,200 [harvey.main] INFO: Decision: send_campaigns (3 due)",
        f"{today} 09:00:05,100 [harvey.agents.sender] INFO: Sender: 3 email(s) sent this cycle (12 today).",
        f"{today} 09:15:01,100 [harvey.agents.handler] INFO: Handler: No new replies.",
        f"{today} 09:30:01,100 [harvey.agents.handler] INFO: Handler: Reply from ann@x.example",
        f"{today} 09:30:01,100 [harvey.agents.handler] INFO: Handler: Reply from ann@x.example",
        f"{today} 09:31:00 - bb - INFO - BB is stopping the worker: paused for edits.",
    ]) + "\n")
    feed = ws.recent_activity()
    assert [a["text"] for a in feed] == ["BB is stopping the worker: paused for edits.", "Reply from ann@x.example",
                                         "3 email(s) sent this cycle (12 today)."]
    assert feed[1]["count"] == 2 and feed[1]["agent"] == "Handler" and feed[1]["kind"] == "reply" and feed[1]["when"] == "09:30"
    assert feed[2]["kind"] == "sent" and feed[0]["kind"] == "control"
    page = client.get("/").text
    assert "Reply from ann@x.example" in page and "×2" in page and "Checking pipeline state" not in page.split("Show the raw log")[0]
    assert "Show the raw log" in page and "Checking pipeline state" in page


def test_dashboard_explains_the_warm_up_cap(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    client.post("/profile", data={**PROFILE, "speed": "fast", "max_daily_sends": "500", "warmup_ramp": "on"})
    import asyncio as _a
    s = _a.run(ws.summary())
    assert s["allowance"]["warming"] and s["allowance"]["allowed"] == 20 and s["allowance"]["day"] == 1
    page = client.get("/").text
    assert "Warm-up day 1: 0 of 20 first emails and follow-ups sent today" in page and "Warm this mailbox up" in page
    client.post("/profile", data={**PROFILE, "speed": "fast", "max_daily_sends": "500"})   # ramp off
    s = _a.run(ws.summary())
    assert not s["allowance"]["warming"] and s["allowance"]["allowed"] == 500
    page = client.get("/").text
    assert "0 of 500 first emails and follow-ups sent today (your daily limit)" in page
    # A limit far above a safe cold-email pace: the card says what happens at that volume.
    assert "gets it blocked" in page and "30 to 50 a day per mailbox" in page
    client.post("/profile", data={**PROFILE, "speed": "gentle", "max_daily_sends": "40"})
    assert "gets it blocked" not in client.get("/").text


def test_cli_make_admin(tmp_path, monkeypatch, capsys):
    from bb import __main__ as cli
    monkeypatch.setenv("BB_HOME", str(tmp_path)); monkeypatch.setenv("BB_BASE_URL", "http://x.test")
    monkeypatch.setenv("BB_ADMIN_EMAIL", "a@x.test"); monkeypatch.setenv("BB_ADMIN_PASSWORD", "p" * 12)
    cli.main(["create-user", "u@x.test", "--password", "s3cret-password"])
    settings, accounts = cli._accounts()
    assert accounts.get_by_email("u@x.test")["role"] == "user"
    cli.main(["make-admin", "u@x.test"])
    assert accounts.get_by_email("u@x.test")["role"] == "admin" and "/admin/users" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["make-admin", "nobody@x.test"])


def test_cli_status_and_env_file(tmp_path, monkeypatch, capsys):
    from bb import __main__ as cli
    for k in ("BB_BASE_URL", "BB_ADMIN_EMAIL", "BB_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BB_HOME", str(tmp_path / "home"))
    env_file = tmp_path / "bb.env"
    env_file.write_text("BB_BASE_URL=https://site.test\nBB_ADMIN_EMAIL=boss@x.test\nBB_ADMIN_PASSWORD=pw-pw-pw-pw-pw\n")
    monkeypatch.setattr(cli, "ENV_FILES", (env_file,))
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "https://site.test" in out and "boss@x.test will be created" in out and str(env_file) in out
    cli.main(["create-user", "u@x.test", "--password", "s3cret-password"])
    cli.main(["status"])
    assert "No admin account" in capsys.readouterr().out
    cli.main(["make-admin", "u@x.test"]); cli.main(["status"])
    assert "Admins (they see Users at https://site.test/admin/users): u@x.test" in capsys.readouterr().out


def test_admin_link_sits_near_the_top_of_the_menu(client):
    """The menu grew past a laptop screen once; the Admin entry must not
    depend on scrolling, and the menu itself must scroll."""
    _admin(client)
    page = client.get("/").text
    menu = page.split('<nav class="nav">')[1].split("</nav>")[0]
    assert menu.index('href="/admin/users"') < menu.index('href="/mail"')
    assert ".nav { flex:1 1 auto; min-height:0; overflow-y:auto;" in page
    _make_user(client)
    client.cookies.clear()
    _login(client, **USER)
    assert 'href="/admin/users"' not in client.get("/").text


def test_quiet_hours_can_be_switched_off(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    assert ws.profile()["quiet_enabled"] is False            # a new profile sends at any hour
    page = client.get("/profile").text
    assert "Only send during these hours" in page and 'name="quiet_enabled"' in page
    client.post("/profile", data={**PROFILE, "quiet_form": "1"})   # box unticked
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["usage"]["quiet_hours"]["enabled"] is False and cfg["usage"]["quiet_hours"]["start"] == "20:00"
    from harvey.main import in_quiet_hours
    assert in_quiet_hours(HarveyConfig(**cfg)) is False
    client.post("/profile", data={**PROFILE, "quiet_form": "1", "quiet_enabled": "on", "quiet_start": "00:00", "quiet_end": "23:59"})
    cfg = yaml.safe_load(ws.config_file.read_text())
    assert cfg["usage"]["quiet_hours"]["enabled"] is True and ws.profile()["quiet_enabled"] is True
    assert in_quiet_hours(HarveyConfig(**cfg)) is True
    # An older page without the checkbox keeps quiet hours on.
    client.post("/profile", data=PROFILE)
    assert yaml.safe_load(ws.config_file.read_text())["usage"]["quiet_hours"]["enabled"] is True


def test_speed_is_held_on_personal_mailboxes_and_dashboard_says_why_not_sending(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    # No mailbox yet: the chosen pace is written as is.
    client.post("/profile", data={**PROFILE, "speed": "relay", "max_daily_sends": "0", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert email["send_every_seconds"] == 30 and email["send_concurrency"] == 8 and email["sends_per_cycle"] == 100
    assert ws.profile()["speed"] == "relay"
    # A Gmail SMTP mailbox: Relay is held to Gentle's pace, the radio stays on Relay, the page says so.
    ws.save_secrets(smtp_host="smtp.gmail.com", smtp_port=587, smtp_username="me@gmail.com", smtp_password="app-pw")
    ws.set_mail_provider("smtp")
    client.post("/profile", data={**PROFILE, "speed": "relay", "max_daily_sends": "0", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert email["send_concurrency"] == 1 and email["sends_per_cycle"] == 8 and email["send_every_seconds"] == 300
    assert email["plain_first_email"] is False          # the form was posted without the (default-on) checkbox
    client.post("/profile", data={**PROFILE, "speed": "relay", "max_daily_sends": "0", "quiet_form": "1", "plain_first_email": "on"})
    assert yaml.safe_load(ws.config_file.read_text())["channels"]["email"]["plain_first_email"] is True
    assert ws.profile()["plain_first_email"] is True and ws.rewrite_config()["channels"]["email"]["plain_first_email"] is True
    assert ws.profile()["speed"] == "relay"
    page = client.get("/profile").text
    assert "Right now: a personal Gmail address throttles or locks" in page
    # The dashboard explains the current state in one line.
    s = asyncio.run(ws.summary())
    assert s["sending"]["state"] == "stuck" and "not running" in s["sending"]["text"]
    page = client.get("/").text
    assert "Not sending." in page and "The worker is not running" in page


def test_dashboard_says_first_emails_go_while_replies_wait_for_the_model(client):
    """The sender's skip reason "…; follow-ups wait, first emails still go out"
    is a note on what the sender is really doing, not a verdict of its own:
    sending when approved first emails can leave, the day's limit when it is
    used up, waiting when nothing is approved; the cause says the AI or the
    inbox, and links the page that fixes it."""
    from datetime import datetime, timezone
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    client.post("/profile", data={**PROFILE, "quiet_form": "1"})          # quiet hours off
    sm = StateManager(str(ws.db_path))
    asyncio.run(sm.init_db())
    base = {"worker_running": True, "worker_paused": False, "worker_doing": {}, "sending_paused": "",
            "allowance": {"reached": False, "text": ""}, "outbox_pending": 0, "outbox_scheduled": 0,
            "recipients": 3, "waiting": 0, "sent_today": 2}
    ai = ("replies couldn't all be read and handled (the AI can't answer right now: LLM API usage limit "
          "reached for this plan); follow-ups wait, first emails still go out")
    asyncio.run(sm.set_setting("sender_skip_reason", ai))
    # Nothing approved to go: waiting, not "sending", and not "stuck" either.
    s = asyncio.run(ws._sending_status(dict(base)))
    assert s["state"] == "waiting" and s["link"] == "/apikey"
    assert "No first emails are waiting to go right now." in s["text"]
    assert "Replies and follow-ups wait until the AI can answer again" in s["text"]
    assert "usage limit reached for this plan" in s["text"] and "follow-ups wait, first emails" not in s["text"]
    # A held follow-up is not something that can leave; an approved first email is.
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    asyncio.run(sm.add_outbox_item(prospect_id="p1", to_email="a@x.example", subject="s", body="b",
                                   send_at=now, status="approved", campaign_id="c1", step=2))
    assert asyncio.run(ws._sending_status(dict(base, outbox_scheduled=1)))["state"] == "waiting"
    asyncio.run(sm.add_outbox_item(prospect_id="p2", to_email="b@x.example", subject="s", body="b",
                                   send_at=now, status="approved", campaign_id="c1", step=1))
    s = asyncio.run(ws._sending_status(dict(base, outbox_scheduled=2)))
    assert s["state"] == "sending" and s["text"].startswith("Sending first emails: 1 are approved")
    assert "2 went out today" in s["text"] and s["link"] == "/apikey"
    # The day's limit used up: that is what stops them, said with the note.
    s = asyncio.run(ws._sending_status(dict(base, outbox_scheduled=2,
                                            allowance={"reached": True, "text": "20 of 20 sent today."})))
    assert s["state"] == "stuck" and s["text"].startswith("20 of 20 sent today.") and "until the AI can answer" in s["text"]
    # The inbox as the cause: the mailbox page, and the AI is not blamed.
    asyncio.run(sm.set_setting("sender_skip_reason", "replies couldn't all be read and handled (the mailbox could not "
                               "be read: IMAP login refused); follow-ups wait, first emails still go out"))
    s = asyncio.run(ws._sending_status(dict(base, outbox_scheduled=2)))
    assert s["state"] == "sending" and s["link"] == "/mailbox" and "IMAP login refused" in s["text"]
    assert "until the inbox can be read again" in s["text"] and "the AI" not in s["text"]
    # A backlog in the inbox being worked through: an inbox cause, not the AI.
    asyncio.run(sm.set_setting("sender_skip_reason", "replies couldn't all be read and handled (the inbox has more new "
                               "mail than one pass reads; catching up pass by pass); follow-ups wait, first emails still go out"))
    s = asyncio.run(ws._sending_status(dict(base, outbox_scheduled=2)))
    assert "catches up on a backlog in the inbox" in s["text"] and "the AI" not in s["text"]
    asyncio.run(sm.set_setting("sender_skip_reason", ai))
    # Replies read from the mailbox but not yet answered: said on the card.
    asyncio.run(sm.set_setting("replies_waiting", "3"))
    s = asyncio.run(ws._sending_status(dict(base, outbox_scheduled=2)))
    assert "3 replies came in and wait for the AI to answer" in s["text"]
    asyncio.run(sm.set_setting("replies_waiting", ""))
    # The daily spend cap: the profile, where the cap is set.
    asyncio.run(sm.set_setting("sender_skip_reason", "the daily AI spend cap is reached; replies wait for tomorrow; "
                               "follow-ups wait, first emails still go out"))
    assert asyncio.run(ws._sending_status(dict(base, outbox_scheduled=2)))["link"] == "/profile"
    # Any other skip reason is still a stop.
    asyncio.run(sm.set_setting("sender_skip_reason", "the mailbox refused the login: 535 bad credentials"))
    s = asyncio.run(ws._sending_status(dict(base)))
    assert s["state"] == "stuck" and "skipping its passes" in s["text"]


def test_resume_sending_forgets_old_bounce_reports(client):
    """Pressing Resume records when, clears the burst window and the bounce
    count, so a report about an email sent before the unblock cannot stop
    sending again."""
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    sm = StateManager(str(ws.db_path))
    asyncio.run(sm.init_db())
    asyncio.run(sm.set_setting("sending_paused", "your mailbox is blocked from sending (...)"))
    asyncio.run(sm.set_setting("bounce_burst", '["2026-10-27T09:00:00"]'))
    asyncio.run(sm.set_setting("bounce_count", "40"))
    asyncio.run(ws.set_sending_paused(False))
    assert asyncio.run(sm.get_setting("sending_paused")) == "" and asyncio.run(sm.get_setting("bounce_burst")) == ""
    assert asyncio.run(sm.get_setting("bounce_count")) == "0"
    assert asyncio.run(sm.get_setting("sending_resumed_at")).startswith("20")


def test_gmail_mailboxes_get_their_own_daily_ceiling(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    from bb.workspaces import is_personal_smtp_host
    assert is_personal_smtp_host("smtp.gmail.com") and is_personal_smtp_host("smtp.office365.com") and is_personal_smtp_host("mail.gmail.com")
    assert not is_personal_smtp_host("smtp-relay.gmail.com") and not is_personal_smtp_host("email-smtp.eu-west-1.amazonaws.com")
    ws.save_secrets(smtp_host="smtp.gmail.com", smtp_port=587, smtp_username="someone@gmail.com", smtp_password="app-pw")
    ws.set_mail_provider("smtp")
    assert ws.mailbox_daily_ceiling() == 450 and ws.mailbox_tier() == ("consumer", "a personal Gmail address")
    client.post("/profile", data={**PROFILE, "speed": "relay", "max_daily_sends": "0", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert email["max_daily_sends"] == 0 and email["mailbox_daily_ceiling"] == 450     # the user's number stays theirs
    assert asyncio.run(ws.summary())["allowance"]["allowed"] == 450
    client.post("/profile", data={**PROFILE, "max_daily_sends": "100", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert email["max_daily_sends"] == 100 and email["mailbox_daily_ceiling"] == 450
    # Google Workspace on a company domain: the business tier, 20 a minute two at a time, 1,800 a day.
    ws.save_secrets(smtp_username="ops@acme-workspace.com")
    assert ws.mailbox_tier() == ("business", "Google Workspace") and ws.mailbox_daily_ceiling() == 1800
    client.post("/profile", data={**PROFILE, "speed": "relay", "max_daily_sends": "0", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert (email["sends_per_cycle"], email["send_every_seconds"], email["send_concurrency"]) == (20, 60, 2)
    assert "Google Workspace allows about 20 emails a minute" in client.get("/profile").text
    # Microsoft 365 on a company domain is business too; a sending service is unheld.
    ws.save_secrets(smtp_host="smtp.office365.com", smtp_username="ops@acme-workspace.com")
    assert ws.mailbox_tier() == ("business", "Microsoft 365") and ws.mailbox_daily_ceiling() == 9000
    ws.save_secrets(smtp_host="email-smtp.eu-west-1.amazonaws.com")
    assert ws.mailbox_tier() == ("", "") and ws.mailbox_daily_ceiling() == 0
    client.post("/profile", data={**PROFILE, "speed": "relay", "max_daily_sends": "0", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert email["max_daily_sends"] == 0 and email["mailbox_daily_ceiling"] == 0 and email["send_concurrency"] == 8
    # Custom: the user's own numbers, within bounds.
    client.post("/profile", data={**PROFILE, "speed": "custom", "custom_per_batch": "40", "custom_every": "45",
                                   "custom_delay": "1.5", "custom_lanes": "4", "max_daily_sends": "0", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert (email["sends_per_cycle"], email["send_every_seconds"], email["send_jitter_seconds"], email["send_concurrency"]) == (40, 45, 1.5, 4)
    prof = ws.profile()
    assert prof["speed"] == "custom" and prof["custom_per_batch"] == 40 and prof["custom_lanes"] == 4
    assert 'value="custom" checked' in client.get("/profile").text and "Emails per batch" in client.get("/profile").text
    client.post("/profile", data={**PROFILE, "speed": "custom", "custom_per_batch": "9999", "custom_every": "1", "custom_lanes": "99", "quiet_form": "1"})
    email = yaml.safe_load(ws.config_file.read_text())["channels"]["email"]
    assert (email["sends_per_cycle"], email["send_every_seconds"], email["send_concurrency"]) == (100, 10, 16)


def test_workers_on_old_code_are_restarted_when_the_site_starts(client, monkeypatch):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    monkeypatch.setattr("bb.app.check_anthropic_key", _accept_key)
    client.post("/apikey", data={"api_key": "sk-ant-user-key-2"})
    client.post("/profile", data=PROFILE)
    ws.save_secrets(smtp_host="smtp.example.com", smtp_port=587, smtp_username="me@example.com", smtp_password="pw")
    ws.set_mail_provider("smtp")
    starts = []
    monkeypatch.setattr(ws_mod.subprocess, "Popen", lambda cmd, **kw: starts.append(cmd) or _FakeProc())
    monkeypatch.setattr(ws_mod.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(ws_mod, "STOP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(ws_mod, "code_version", lambda: "v1")
    assert "Started" in client.post("/worker/start").headers["location"]
    assert ws.pid_file.with_suffix(".version").read_text() == "v1" and len(starts) == 1
    assert asyncio.run(ws.restart_workers_on_old_code()) == []           # same code: left alone
    monkeypatch.setattr(ws_mod, "code_version", lambda: "v2")
    assert asyncio.run(ws.restart_workers_on_old_code()) == ["worker"]   # new code: restarted
    assert len(starts) == 2 and ws.pid_file.with_suffix(".version").read_text() == "v2"


def test_recipients_verify_and_clean_and_restore(client, monkeypatch):
    """The free Verify & Clean sweep moves provably-dead addresses to the
    Excluded list (reversibly) and the page offers Restore."""
    _as_user(client)
    # good · role (kept) · dead domain (invalid) · disposable (invalid)
    client.post("/recipients", data={"pasted":
        "jane@acme.com\ninfo@acme.com\nghost@dead.tld\nburner@mailinator.com\n"})

    async def fake_lookup(domains):
        table = {"acme.com": ("mx", "mail.acme.com"), "dead.tld": ("none", None),
                 "mailinator.com": ("mx", "m.mailinator.com")}
        return {d: table.get(d, ("error", None)) for d in dict.fromkeys(domains)}
    monkeypatch.setattr("harvey.listclean._lookup_many", fake_lookup)

    r = client.post("/recipients/clean")
    assert r.status_code == 303

    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    db = sqlite3.connect(str(ws.db_path))
    excluded = [row[0] for row in db.execute(
        "SELECT email FROM prospects WHERE COALESCE(excluded, 0) = 1").fetchall()]
    assert set(excluded) == {"ghost@dead.tld", "burner@mailinator.com"}

    # The page shows the Excluded card; the dead one is not in "Recently added".
    page = client.get("/recipients").text
    assert "Excluded" in page and "ghost@dead.tld" in page

    # Restore returns it to the sending pool.
    pid = db.execute("SELECT id FROM prospects WHERE email = 'ghost@dead.tld'").fetchone()[0]
    assert client.post(f"/recipients/{pid}/restore").status_code == 303
    val = sqlite3.connect(str(ws.db_path)).execute(
        "SELECT COALESCE(excluded, 0) FROM prospects WHERE email = 'ghost@dead.tld'").fetchone()[0]
    assert val == 0


def test_outbox_reject_all(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    _seed_outbox(ws)   # o0, o1 pending_review; o2 approved
    r = client.post("/outbox/reject-all", data={"status": "pending_review"})
    assert r.status_code == 303 and "Rejected%202" in r.headers["location"]
    db = sqlite3.connect(str(ws.db_path))
    assert dict(db.execute("SELECT id, status FROM outbox")) == {"o0": "rejected", "o1": "rejected", "o2": "approved"}
    # The scheduled (approved) one can be cancelled in bulk too.
    r = client.post("/outbox/reject-all", data={"status": "approved"})
    assert "Cancelled%201" in r.headers["location"]
    assert db.execute("SELECT status FROM outbox WHERE id = 'o2'").fetchone()[0] == "rejected"
