"""Validation and lifecycle helpers for authorized training links."""

from datetime import datetime, timezone
from urllib.parse import urlparse
import re
import secrets


MAX_NAME_LENGTH = 120
MAX_PATH_LENGTH = 100

PATH_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_-]*$")


def new_link_id():
    """Generate an unpredictable identifier."""
    return secrets.token_urlsafe(18)


def normalize_path(value):
    """Validate a relative training-link path."""
    value = str(value or "").strip().strip("/")

    if not value:
        raise ValueError("A link path is required.")

    if len(value) > MAX_PATH_LENGTH:
        raise ValueError("Link path is too long.")

    if not PATH_PATTERN.fullmatch(value):
        raise ValueError("Link path contains unsupported characters.")

    if "//" in value or ".." in value:
        raise ValueError("Invalid link path.")

    return value


def validate_destination(value):
    """Require an absolute HTTPS destination without embedded credentials."""
    value = str(value or "").strip()

    if not value or len(value) > 2048:
        raise ValueError("A valid destination URL is required.")

    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid destination URL.") from None

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or any(char.isspace() for char in value)
    ):
        raise ValueError("Destination must be a standard HTTPS URL.")

    return value


def validate_name(value):
    value = str(value or "").strip()

    if not value or len(value) > MAX_NAME_LENGTH:
        raise ValueError("Link name must be between 1 and 120 characters.")

    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def is_expired(expires_at, now=None):
    if not expires_at:
        return False

    try:
        expires = datetime.fromisoformat(
            str(expires_at).replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError("Invalid expiration timestamp.") from None

    if expires.tzinfo is None:
        raise ValueError("Expiration timestamp must include a timezone.")

    current = now or datetime.now(timezone.utc)

    return expires <= current
