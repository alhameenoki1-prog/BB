import pytest

from bb.mail.socks5_config import import_socks5_lines, parse_socks5_url


def test_authenticated_remote_dns_proxy():
    endpoint = parse_socks5_url("socks5h://alice:secret@proxy.example.com:1080")
    assert endpoint.host == "proxy.example.com"
    assert endpoint.port == 1080
    assert endpoint.remote_dns is True
    assert endpoint.username == "alice"
    assert endpoint.password == "secret"
    assert "secret" not in str(endpoint.public_view())


def test_import_deduplicates_and_skips_comments():
    endpoints = import_socks5_lines(
        "# purchased proxies\nsocks5://proxy.example.com:1080\n"
        "socks5://proxy.example.com:1080\n"
        "socks5h://bob:pw@other.example.com:1081"
    )
    assert len(endpoints) == 2


@pytest.mark.parametrize("url", [
    "http://proxy.example.com:8080",
    "socks5://localhost:1080",
    "socks5://127.0.0.1:1080",
    "socks5://10.0.0.5:1080",
    "socks5://proxy.example.com:0",
    "socks5://proxy.example.com:99999",
    "socks5://proxy.example.com:1080/?token=secret",
    "socks5://:pass@proxy.example.com:1080",
])
def test_rejects_invalid_or_private_proxy_urls(url):
    with pytest.raises(ValueError):
        parse_socks5_url(url)


def test_import_reports_line_number():
    with pytest.raises(ValueError, match="line 2"):
        import_socks5_lines("socks5://proxy.example.com:1080\ninvalid")
