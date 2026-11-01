"""Owner-bound, revocable URLs that launch BB's official Microsoft OAuth flow.

A generated URL is a locator, NOT an OAuth token or a grant of mailbox access.
The visitor must authenticate to the BB account that generated the URL, and
Microsoft handles its own authorization, MFA and consent.
"""

import hmac
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bb.workspace_file_lock import workspace_file_lock

_FILENAME = "microsoft_authorization_link.json"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LIFETIME = timedelta(days=30)


def _path(user_data_dir):
    return Path(user_data_dir).resolve() / _FILENAME


def _read(user_data_dir):
    try:
        record = json.loads(_path(user_data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _valid(record, *, now=None):
    if not record or not record.get("enabled") or not _TOKEN_PATTERN.fullmatch(str(record.get("token", ""))):
        return False
    try:
        expires = datetime.fromisoformat(record["expires_at"])
    except (ValueError, KeyError, TypeError):
        return False
    if expires.tzinfo is None:
        return False
    return expires > (now or datetime.now(timezone.utc))


def get_link(user_data_dir, *, now=None):
    """Read the owner's active link; expired or revoked links are hidden."""
    record = _read(user_data_dir)
    return dict(record) if _valid(record, now=now) else None


def link_for_token(user_data_dir, token, *, now=None):
    """Do not permit another BB user's link to authorize this workspace."""
    if not _TOKEN_PATTERN.fullmatch(str(token or "")):
        return None
    record = get_link(user_data_dir, now=now)
    if not record or not hmac.compare_digest(record["token"], token):
        return None
    return record


def _write(user_data_dir, record):
    path = _path(user_data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".microsoft-link-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def generate_link(user_data_dir, *, now=None):
    """Generate a new URL token; any previous generated URL stops working."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Timezone-aware timestamp is required.")
    token = secrets.token_urlsafe(32)
    record = {
        "token": token,
        "created_at": current.isoformat(),
        "expires_at": (current + _LIFETIME).isoformat(),
        "enabled": True,
    }
    with workspace_file_lock(user_data_dir, _FILENAME):
        _write(user_data_dir, record)
    return dict(record)


def revoke_link(user_data_dir):
    """Revoke the current generated URL, without disconnecting Microsoft."""
    with workspace_file_lock(user_data_dir, _FILENAME):
        record = _read(user_data_dir)
        if record:
            record["enabled"] = False
            _write(user_data_dir, record)
    return True
