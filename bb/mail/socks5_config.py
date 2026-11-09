"""SOCKS5 endpoint import and validation for BB.

This module handles proxy configuration only. It does not send mail, rotate
identities, or open network connections. Secrets must be encrypted by the
application's credential store before persistence.
"""
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit, unquote


@dataclass(frozen=True)
class Socks5Endpoint:
    host: str
    port: int
    username: str = ""
    password: str = ""
    remote_dns: bool = True

    def public_view(self) -> dict:
        """Safe-to-display representation; never expose the password."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "authenticated": bool(self.username),
            "remote_dns": self.remote_dns,
        }


def parse_socks5_url(value: str) -> Socks5Endpoint:
    """Accept socks5h://user:pass@host:port or socks5://host:port.

    socks5h requests remote DNS resolution. Reject embedded query options,
    fragments, invalid ports, and local/loopback targets to reduce SSRF risk.
    """
    raw = value.strip()
    try:
        url = urlsplit(raw)
        host = url.hostname or ""
        port = url.port
    except ValueError as exc:
        raise ValueError("Invalid SOCKS5 URL or port") from exc
    if url.scheme not in ("socks5", "socks5h"):
        raise ValueError("Only socks5:// and socks5h:// URLs are supported")
    if not host or port is None or not 1 <= port <= 65535:
        raise ValueError("Proxy hostname and valid port are required")
    if url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("Proxy URL must not contain paths, queries or fragments")
    if any(c.isspace() for c in host):
        raise ValueError("Invalid proxy hostname")
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        raise ValueError("Local proxy addresses are not permitted")
    try:
        ip = ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError("Proxy address must be globally routable")
    username = unquote(url.username or "")
    password = unquote(url.password or "")
    if password and not username:
        raise ValueError("Proxy password requires a username")
    return Socks5Endpoint(host=host, port=port, username=username,
                          password=password, remote_dns=url.scheme == "socks5h")


def import_socks5_lines(data: str, *, limit: int = 100) -> list[Socks5Endpoint]:
    """Parse newline-separated purchased proxy URLs, without network access."""
    endpoints = []
    seen = set()
    for line_number, raw in enumerate(data.splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        if len(endpoints) >= limit:
            raise ValueError(f"Import exceeds {limit} proxy entries")
        try:
            item = parse_socks5_url(raw)
        except ValueError as exc:
            raise ValueError(f"Proxy line {line_number}: {exc}") from exc
        key = (item.host.lower(), item.port, item.username)
        if key not in seen:
            endpoints.append(item)
            seen.add(key)
    return endpoints
