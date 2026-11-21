"""Company research uses public pages and keeps contact matches separate."""

import httpx
import pytest

from bb import alerts
from bb.mail import company_lookup


@pytest.mark.asyncio
async def test_lookup_uses_saved_domain_and_shares_a_single_public_fetch(monkeypatch):
    requests = []
    resolved = []

    def addresses(host, port):
        resolved.append((host, port))
        return ["93.184.216.34"]

    def serve(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, stream=httpx.ByteStream(
            b'<meta property="og:site_name" content="Example Engineering">'
            b'<meta name="description" content="Builds equipment for research teams.">'
        ))

    client = httpx.AsyncClient
    def isolated_client(**kwargs):
        assert kwargs["trust_env"] is False  # Ignore HTTP(S)_PROXY and ALL_PROXY.
        return client(transport=httpx.MockTransport(serve), **kwargs)

    monkeypatch.setattr(alerts, "_addresses", addresses)
    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    matches = await company_lookup.lookup_companies(
        ["ann@example.com", "john@gmail.com", "unknown@gmail.com"],
        {"john@gmail.com": {"company_domain": "example.com"}},
    )
    assert matches == {
        address: {"name": "Example Engineering", "description": "Builds equipment for research teams.",
                  "website": "https://example.com/"}
        for address in ("ann@example.com", "john@gmail.com")
    }
    assert resolved == [("example.com", 443)]
    request, = requests
    assert request.url.host == "93.184.216.34"
    assert request.headers["Host"] == request.extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
async def test_lookup_stops_at_nonpublic_addresses_and_outside_redirects(monkeypatch):
    resolved = []

    def addresses(host, _port):
        resolved.append(host)
        return ["10.0.0.1"] if host.removeprefix("www.").startswith("inside.") else ["93.184.216.34"]

    def serve(request):
        assert request.headers["Host"] in {"example.com", "www.example.com"}
        return httpx.Response(302, headers={"Location": "https://outside.example.com/"})

    client = httpx.AsyncClient
    monkeypatch.setattr(alerts, "_addresses", addresses)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(serve), **kwargs))
    matches = await company_lookup.lookup_companies(
        ["ann@inside.example.com", "john@example.com"], {"john@example.com": {"company": "Saved company"}}
    )
    assert matches == {"john@example.com": {"name": "Saved company"}}
    assert "outside.example.com" not in resolved
