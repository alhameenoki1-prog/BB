"""Mailbox problems reach the user away from the site: once by email or
webhook, again a day later if still there, and a note when it clears."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from bb import alerts
from bb.accounts import Accounts
from bb.app import create_app
from bb.workspaces import Workspace
from tests.test_bb_web import USER, _as_user, _dns_stub, _settings

T0 = datetime(2026, 10, 22, 3, 0, 0)


def _ws(tmp_path, **extra):
    settings = _settings(tmp_path, **extra)
    acc = Accounts(settings.accounts_db)
    acc.create_user(USER["email"], USER["password"])
    return settings, Workspace(settings, acc.get_by_email(USER["email"])).ensure()


def test_webhook_addresses_must_be_public_https(monkeypatch):
    monkeypatch.setattr(alerts, "_addresses", lambda host, port: {"ntfy.sh": ["159.203.148.75"], "intranet.local": ["10.0.0.5"],
                                                                  "meta.example": ["169.254.169.254"], "v6.example": ["::1"]}[host])
    assert alerts.webhook_problem("") == ""
    assert alerts.webhook_problem("https://ntfy.sh/bb-alerts") == ""
    assert "https://" in alerts.webhook_problem("http://ntfy.sh/bb-alerts")
    assert "private network" in alerts.webhook_problem("https://intranet.local/hook")
    assert "private network" in alerts.webhook_problem("https://meta.example/latest")   # cloud metadata
    assert "private network" in alerts.webhook_problem("https://v6.example/x")

    def gone(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(alerts, "_addresses", gone)
    assert "could not be found" in alerts.webhook_problem("https://nowhere.example/x")


@pytest.mark.asyncio
async def test_each_problem_is_sent_once_reminded_daily_and_cleared(tmp_path, monkeypatch):
    settings, ws = _ws(tmp_path)
    now = {"alerts": {"mailbox": "The mailbox check at 03:00 failed: 535 authentication failed."}}
    sent = []

    async def fake_current(_ws):
        return dict(now["alerts"])

    async def fake_deliver(_settings, _ws, title, text, ok=False):
        sent.append((title, text, ok))
        return (["email to jane@example.com"], [])

    monkeypatch.setattr(alerts, "current_alerts", fake_current)
    monkeypatch.setattr(alerts, "deliver", fake_deliver)
    assert await alerts.check_workspace(settings, ws, T0) == ["mailbox: email to jane@example.com"]
    assert sent[-1][0] == "Your mailbox needs attention" and "535" in sent[-1][1] and not sent[-1][2]
    # The same problem with a newer timestamp in its text: not news.
    now["alerts"] = {"mailbox": "The mailbox check at 03:30 failed: 535 authentication failed."}
    assert await alerts.check_workspace(settings, ws, T0 + timedelta(minutes=30)) == []
    # Still there a day later: sent again.
    assert await alerts.check_workspace(settings, ws, T0 + timedelta(hours=24, minutes=5))
    assert sent[-1][0] == "Still not working: your mailbox needs attention"
    assert ws.alert_state()["incidents"]["mailbox"]["reminders"] == 1
    # It clears: one note, and the incident is gone.
    now["alerts"] = {}
    assert await alerts.check_workspace(settings, ws, T0 + timedelta(hours=25)) == ["mailbox cleared: email to jane@example.com"]
    assert sent[-1][0] == "Your mailbox is working again" and sent[-1][2] and "535" in sent[-1][1]
    assert ws.alert_state()["incidents"] == {}
    assert len(sent) == 3

    # Nothing could be reached: the problem is tried again on the next pass, not marked as said.
    async def nowhere(_settings, _ws, title, text, ok=False):
        return ([], ["the webhook answered 500"])

    monkeypatch.setattr(alerts, "deliver", nowhere)
    now["alerts"] = {"mail:abc": "jane@outlook.com needs reconnecting in the mail command center: revoked"}
    assert await alerts.check_workspace(settings, ws, T0 + timedelta(hours=26)) == []
    assert ws.alert_state()["incidents"]["mail:abc"]["notified_at"] == ""
    assert "500" in ws.alert_state()["last_problem"]
    monkeypatch.setattr(alerts, "deliver", fake_deliver)
    assert await alerts.check_workspace(settings, ws, T0 + timedelta(hours=26, minutes=5)) == ["mail:abc: email to jane@example.com"]
    assert sent[-1][0] == "A mailbox needs reconnecting"


@pytest.mark.asyncio
async def test_what_counts_as_a_problem(tmp_path, monkeypatch):
    settings, ws = _ws(tmp_path)

    async def health(self):
        return {"alert": "Nothing has been delivered since 01:00 although 5 approved emails are due.", "error": ""}

    monkeypatch.setattr(Workspace, "mailbox_health", health)
    # A worker the user stopped: its last alert is history, not news.
    assert await alerts.current_alerts(ws) == {}
    ws.autostart_file.write_text("1")
    got = await alerts.current_alerts(ws)
    assert list(got) == ["mailbox"] and "Nothing has been delivered" in got["mailbox"] and "Reconnect" not in got["mailbox"]

    async def refused(self):
        return {"alert": "The mailbox check failed: SMTP server refused the login (535).", "error": "refused the login"}

    monkeypatch.setattr(Workspace, "mailbox_health", refused)
    assert "press Reconnect" in (await alerts.current_alerts(ws))["mailbox"]

    # A command-center mailbox that needs signing in again counts while the mail worker is meant to run.
    db = ws.mail_db()
    await db.init()
    mid = await db.add_mailbox(address="jane@outlook.com", provider="graph", creds="", status="reconnect")
    await db.update_mailbox(mid, status_detail="Microsoft no longer accepts this sign-in")
    assert f"mail:{mid}" not in await alerts.current_alerts(ws)
    ws.mail_autostart_file.write_text("1")
    got = await alerts.current_alerts(ws)
    assert "jane@outlook.com needs reconnecting" in got[f"mail:{mid}"] and "no longer accepts" in got[f"mail:{mid}"]


@pytest.mark.asyncio
async def test_delivery_by_email_and_by_webhook(tmp_path, monkeypatch):
    import aiosmtplib

    settings, ws = _ws(tmp_path)
    # No site alert server, no webhook: email is on by default but cannot go.
    reached, problems = await alerts.deliver(settings, ws, "T", "text")
    assert reached == [] and "no alert email server" in problems[0]

    settings, ws = _ws(tmp_path / "two", BB_ALERT_SMTP_HOST="smtp.alerts.example", BB_ALERT_SMTP_USERNAME="alerts@site.example",
                       BB_ALERT_SMTP_PASSWORD="pw")
    mails, posts = [], []

    async def fake_send(msg, **kw):
        mails.append((msg, kw))

    class FakeClient:
        def __init__(self, *a, **k):
            assert k.get("follow_redirects") is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            posts.append((url, kw))
            return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    monkeypatch.setattr(alerts.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(alerts, "_addresses", lambda host, port: ["104.18.1.1"])
    ws.save_alert_settings(email="Jane.Alerts@Example.com", email_on=True, webhook="https://ntfy.sh/bb-jane")
    reached, problems = await alerts.deliver(settings, ws, "Your mailbox needs attention", "535 refused")
    assert problems == [] and reached == ["email to jane.alerts@example.com", "the webhook"]
    msg, kw = mails[0]
    assert msg["To"] == "jane.alerts@example.com" and msg["From"] == "alerts@site.example"
    assert msg["Subject"] == "Your mailbox needs attention" and "535 refused" in msg.get_content()
    assert "http://testserver/mailbox#health" in msg.get_content()
    assert kw["hostname"] == "smtp.alerts.example" and kw["port"] == 587 and kw["start_tls"]
    url, kw = posts[0]
    assert url == "https://ntfy.sh/bb-jane" and kw["content"] == b"535 refused" and kw["headers"]["Priority"] == "high"
    # Slack and Discord get their own shapes; anything else plain JSON.
    assert "text" in alerts._webhook_request("https://hooks.slack.com/services/x", "T", "x", "l", False, "a")["json"]
    assert "content" in alerts._webhook_request("https://discord.com/api/webhooks/1/x", "T", "x", "l", False, "a")["json"]
    assert alerts._webhook_request("https://example.com/hook", "T", "x", "l", True, "a")["json"]["status"] == "ok"
    # Email off: only the webhook.
    ws.save_alert_settings(email="jane@example.com", email_on=False, webhook="https://ntfy.sh/bb-jane")
    assert (await alerts.deliver(settings, ws, "T", "x"))[0] == ["the webhook"]
    with pytest.raises(ValueError):
        ws.save_alert_settings(email="not an address", email_on=True, webhook="")


def test_alert_settings_on_the_mailbox_page(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)
    monkeypatch.setattr(alerts, "_addresses", lambda host, port: ["10.1.2.3"] if host == "inside.example" else ["104.18.1.1"])
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.settings = settings
        _as_user(client)
        page = client.get("/mailbox").text
        assert "Tell me when it stops" in page and 'value="jane@example.com"' in page and "ntfy" in page
        assert "no alert email server yet" in page
        bad = client.post("/mailbox/alerts", data={"alert_email": "jane@example.com", "alert_email_on": "1",
                                                   "alert_webhook": "https://inside.example/hook"})
        assert "private%20network" in bad.headers["location"]
        ok = client.post("/mailbox/alerts", data={"alert_email": "me@else.example", "alert_webhook": "https://ntfy.sh/bb-x"})
        assert "Alert%20settings%20saved" in ok.headers["location"]
        ws = Workspace(settings, Accounts(settings.accounts_db).get_by_email(USER["email"]))
        assert ws.alert_settings() == {"email": "me@else.example", "email_on": False, "webhook": "https://ntfy.sh/bb-x"}

        async def fake_deliver(_settings, _ws, title, text, ok=False):
            assert title == "Test alert from BB" and ok
            return (["the webhook"], [])

        monkeypatch.setattr(alerts, "deliver", fake_deliver)
        r = client.post("/mailbox/alerts/test")
        assert "Test%20alert%20sent%20to%20the%20webhook" in r.headers["location"]
