"""Go-live hardening: the writer keeps going after the first reply, mail
connections verify certificates, the login brake cannot be moved by a forged
header, and every response carries the browser-side locks."""
import imaplib

import pytest
from fastapi.testclient import TestClient

from bb.app import create_app
from bb.settings import Settings


def _settings(tmp_path, **extra) -> Settings:
    env = {
        "BB_HOME": str(tmp_path / "bb"), "BB_BASE_URL": "http://testserver",
        "BB_SECRET_KEY": "test-secret", "BB_ADMIN_EMAIL": "admin@example.com",
        "BB_ADMIN_PASSWORD": "correct-horse-battery", **extra,
    }
    return Settings(env=env)


@pytest.mark.asyncio
async def test_an_open_conversation_does_not_stop_the_writer_on_native_mail():
    import harvey.main as main_mod

    class _Cfg:
        class icp:
            auto_prospect = False

        class channels:
            class email:
                provider = "gmail"

    summary = {"prospects": {"new": 40}, "draft_campaigns": 0, "open_conversations": 3}
    assert await main_mod.decide_next_action(None, None, _Cfg(), summary=summary) == "write_campaign"
    # Instantly has no per-cycle mailbox read, so there the handler still leads.
    _Cfg.channels.email.provider = "instantly"
    assert await main_mod.decide_next_action(None, None, _Cfg(), summary=summary) == "handle_replies"


def test_imap_connections_verify_the_certificate(monkeypatch):
    from harvey.integrations import smtp_mail

    seen = {}

    class Fake:
        def __init__(self, host, port, timeout=None, ssl_context=None):
            seen.update(host=host, port=port, ctx=ssl_context)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Fake)
    smtp_mail.imap_connect("imap.example.com", 993)
    assert seen["host"] == "imap.example.com" and seen["port"] == 993
    assert seen["ctx"] is not None and seen["ctx"].check_hostname is True


def test_bind_host_and_trusted_proxies(tmp_path):
    s = _settings(tmp_path)
    assert s.host == "0.0.0.0" and s.trusted_proxies == "127.0.0.1"
    tunnelled = _settings(tmp_path, BB_TUNNEL="cloudflare")
    assert tunnelled.host == "127.0.0.1"
    assert _settings(tmp_path, BB_TUNNEL="cloudflare", BB_HOST="0.0.0.0").host == "0.0.0.0"
    assert _settings(tmp_path, BB_TRUSTED_PROXIES="10.0.0.5").trusted_proxies == "10.0.0.5"


def test_every_response_carries_the_browser_locks(tmp_path):
    with TestClient(create_app(_settings(tmp_path)), follow_redirects=False) as c:
        r = c.get("/login")
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "referrer-policy" in r.headers
        assert "strict-transport-security" not in r.headers  # plain http
    with TestClient(create_app(_settings(tmp_path, BB_BASE_URL="https://outreach.example.com")),
                    follow_redirects=False) as c:
        r = c.get("/login")
        assert r.headers["strict-transport-security"].startswith("max-age=")
