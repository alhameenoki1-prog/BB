import pytest

from bb.mail.socks5_config import Socks5Endpoint
from bb.mail.socks5_selection import ProxyHealth, select_proxy


FIRST = Socks5Endpoint("first.example.com", 1080, "alice", "secret")
SECOND = Socks5Endpoint("second.example.com", 1080, "bob", "other")


def test_prefers_selected_healthy_endpoint():
    assert select_proxy([FIRST, SECOND], preferred_index=1) == SECOND


def test_fails_over_to_next_healthy_proxy():
    health = {("first.example.com", 1080, "alice"): ProxyHealth(available=False)}
    assert select_proxy([FIRST, SECOND], health) == SECOND


def test_disables_proxy_after_failure_threshold():
    health = {("first.example.com", 1080, "alice"): ProxyHealth(consecutive_failures=3)}
    assert select_proxy([FIRST, SECOND], health) == SECOND


def test_fails_closed_without_proxies():
    with pytest.raises(RuntimeError, match="direct routing is disabled"):
        select_proxy([])


def test_fails_closed_if_all_proxies_unhealthy():
    health = {
        ("first.example.com", 1080, "alice"): ProxyHealth(disabled=True),
        ("second.example.com", 1080, "bob"): ProxyHealth(available=False),
    }
    with pytest.raises(RuntimeError, match="refusing direct connection"):
        select_proxy([FIRST, SECOND], health)


def test_invalid_preferred_index():
    with pytest.raises(ValueError):
        select_proxy([FIRST], preferred_index=3)
