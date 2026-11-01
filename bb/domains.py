"""Domain ownership utilities for BB.

This module does not modify DNS records or configure public routing.
"""

import re
import secrets
from urllib.parse import urlparse


DOMAIN_RE = re.compile('^(?=.{1,253}$)(?:a-z0-9?\\.)+[a-z]{2,63}$')


def normalize_domain(value: str) -> str:
    """Return a normalized DNS hostname or raise ValueError."""
    value = (value or "").strip().lower().rstrip(".")

    if not value or "://" in value or "/" in value or ":" in value:
        raise ValueError("Enter a domain name, not a URL.")

    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Invalid internationalized domain name.") from exc


    labels = value.split(".")
    if len(labels) < 2 or len(value) > 253:
        raise ValueError("Invalid domain name.")

    for label in labels:
        if not 1 <= len(label) <= 63:
            raise ValueError("Invalid domain label length.")
        if not label[0].isalnum() or not label[-1].isalnum():
            raise ValueError("Domain labels cannot start or end with a hyphen.")
        if not all(char.isascii() and (char.isalnum() or char == "-") for char in label):
            raise ValueError("Invalid character in domain.")

    if not labels[-1].isalpha() or not labels[-1].isascii() or len(labels[-1]) < 2:
        raise ValueError("Invalid top-level domain.")

    return value


def verification_record(domain: str, challenge: str) -> dict:
    """Describe the TXT record required to prove domain ownership."""
    domain = normalize_domain(domain)

    if not re.fullmatch(r"[a-f0-9]{64}", challenge or ""):
        raise ValueError("Invalid verification challenge.")

    return {
        "type": "TXT",
        "name": f"_bb-verify.{domain}",
        "value": f"bb-domain-verification={challenge}",
        "domain": domain,
    }


def new_challenge() -> str:
    """Generate a cryptographically random ownership challenge."""
    return secrets.token_hex(32)


def default_domain(base_url: str) -> str:
    """Extract BB's configured public hostname."""
    parsed = urlparse(base_url or "")
    hostname = parsed.hostname or ""

    if hostname in ("localhost", "127.0.0.1", "::1"):
        return ""

    try:
        return normalize_domain(hostname)
    except ValueError:
        return ""
