"""Controlled SOCKS5 endpoint selection with health-based failover.

This selects among operator-authorized proxies for network resilience. It does
not change sender identity, bypass destination limits, or perform mail delivery.
"""
from dataclasses import dataclass
from typing import Sequence

from bb.mail.socks5_config import Socks5Endpoint


@dataclass(frozen=True)
class ProxyHealth:
    available: bool = True
    consecutive_failures: int = 0
    disabled: bool = False


def select_proxy(
    endpoints: Sequence[Socks5Endpoint],
    health: dict[tuple[str, int, str], ProxyHealth] | None = None,
    *,
    preferred_index: int = 0,
    max_failures: int = 3,
) -> Socks5Endpoint:
    """Choose a usable endpoint, preserving operator order and failing closed.

    The preferred endpoint is tried first; other entries are only selected
    when that endpoint is unavailable or disabled. Never returns a direct
    network connection or silently falls back to one.
    """
    if not endpoints:
        raise RuntimeError("No SOCKS5 endpoints configured; direct routing is disabled")
    if not 0 <= preferred_index < len(endpoints):
        raise ValueError("Preferred proxy index is out of range")
    health = health or {}
    for offset in range(len(endpoints)):
        endpoint = endpoints[(preferred_index + offset) % len(endpoints)]
        state = health.get((endpoint.host.lower(), endpoint.port, endpoint.username), ProxyHealth())
        if state.available and not state.disabled and state.consecutive_failures < max_failures:
            return endpoint
    raise RuntimeError("No healthy SOCKS5 endpoints; refusing direct connection")
