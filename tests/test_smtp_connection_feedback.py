"""The legacy SMTP form should return promptly and identify relay failures."""

import asyncio

from bb.app import test_smtp as probe_smtp


SETTINGS = {"smtp_host": "smtp.sendgrid.net", "smtp_port": "587", "smtp_username": "apikey",
            "smtp_password": "test-key", "imap_host": "", "imap_port": "993",
            "imap_username": "", "imap_password": ""}


def test_relay_disconnect_during_login_has_actionable_error(monkeypatch):
    class DisconnectingSMTP:
        is_connected = False

        def __init__(self, **kwargs):
            pass

        async def connect(self):
            self.is_connected = True

        async def login(self, *_):
            self.is_connected = False
            raise ConnectionError("Connection lost")

    monkeypatch.setattr("harvey.integrations.smtp_mail.aiosmtplib.SMTP", DisconnectingSMTP)
    ok, detail = asyncio.run(probe_smtp(SETTINGS))
    assert not ok
    assert "server closed the connection" in detail
    assert "smtp.sendgrid.net:587" in detail


def test_relay_quit_failure_after_login_does_not_reject_credentials(monkeypatch):
    class DisconnectingOnQuit:
        is_connected = False

        def __init__(self, **kwargs):
            pass

        async def connect(self):
            self.is_connected = True

        async def login(self, *_):
            pass

        async def quit(self):
            self.is_connected = False
            raise ConnectionError("Connection lost")

        def close(self):
            self.is_connected = False

    monkeypatch.setattr("harvey.integrations.smtp_mail.aiosmtplib.SMTP", DisconnectingOnQuit)
    ok, detail = asyncio.run(probe_smtp(SETTINGS))
    assert ok and "SMTP connected" in detail
