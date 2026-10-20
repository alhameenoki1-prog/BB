"""The site-run cloudflared tunnel: the public link is read from its
output, shown on the Users page, and the process is restarted or
reported missing. A fake cloudflared stands in for the real one."""

import asyncio
import os
import stat
import time

import pytest
from starlette.testclient import TestClient

from bb.app import create_app
from bb.tunnel import Tunnel
from tests.test_bb_web import ADMIN, _admin, _settings

FAKE = """#!/bin/sh
# prints what cloudflared prints, then stays up until told to stop
echo "INF Requesting new quick Tunnel on trycloudflare.com..." >&2
sleep 0.2
echo "INF +--------------------------------------------------------------------------------------------+" >&2
echo "INF |  https://sunny-ridge-example.trycloudflare.com                                             |" >&2
trap 'exit 0' TERM
while true; do sleep 0.2; done
"""


def _fake(tmp_path, body=FAKE):
    path = tmp_path / "cloudflared"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.asyncio
async def test_quick_tunnel_reports_its_address_and_restarts(tmp_path):
    seen = []
    t = Tunnel("quick", 8000, binary=_fake(tmp_path), on_url=seen.append)
    assert t.command()[-2:] == ["--url", "http://localhost:8000"]
    await t.start()
    for _ in range(50):
        await asyncio.sleep(0.1)
        if t.status == "up":
            break
    assert t.url == "https://sunny-ridge-example.trycloudflare.com" and seen == [t.url]
    assert t.describe()["changes"] is True
    # The process dies: the tunnel notes it and comes back with the backoff.
    t._proc.kill()
    for _ in range(30):
        await asyncio.sleep(0.1)
        if t.status == "down":
            break
    assert t.status == "down" and "starting it again" in t.detail
    await t.stop()
    assert t.status == "off" and t._proc.returncode is not None

    missing = Tunnel("quick", 8000, binary=str(tmp_path / "nope"))
    await missing.start()
    assert missing.status == "missing" and "not installed" in missing.detail
    named = Tunnel("named", 8000, token="tok", public_url="https://out.example", binary=_fake(tmp_path))
    assert named.url == "https://out.example" and named.command()[-2:] == ["--token", "tok"] and not named.describe()["changes"]
    assert not Tunnel("", 8000).enabled


def test_users_page_shows_the_link_and_the_site_follows_it(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", lambda d: asyncio.sleep(0, result=None))
    settings = _settings(tmp_path, BB_BASE_URL="http://localhost:8000", BB_TUNNEL="cloudflare", BB_CLOUDFLARED=_fake(tmp_path))
    assert settings.tunnel_mode == "quick" and not any("BB_TUNNEL" in p for p in settings.problems())
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _admin(client)
        for _ in range(50):
            if client.app.state.tunnel.status == "up":
                break
            time.sleep(0.1)
        page = client.get("/admin/users").text
        assert "The link your users open" in page and "https://sunny-ridge-example.trycloudflare.com" in page
        assert "new address every time the site starts" in page and "tunnel up" in page
        assert settings.base_url == "https://sunny-ridge-example.trycloudflare.com" and settings.secure_cookies
        assert not any("Only this computer" in p for p in settings.problems())
    assert client.app.state.tunnel.status == "off"

    # No tunnel configured: the page says what to add.
    plain = _settings(tmp_path, BB_BASE_URL="http://localhost:8000")
    assert any("BB_TUNNEL=cloudflare" in p for p in plain.problems())
    with TestClient(create_app(plain), follow_redirects=False) as client:
        _admin(client)
        page = client.get("/admin/users").text
        assert "only works on this computer" in page and "no tunnel" in page
