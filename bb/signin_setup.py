"""The site's Google and Microsoft sign-in, switched on from the web.

"Sign in with Google" and "Sign in with Microsoft" connect a mailbox with
the account the person's browser is already signed in to: no password
reaches this site. Google and Microsoft only allow that through an app the
site owner registers once (a client id and secret). An admin can paste
those on the Sign-in setup page instead of editing the server's settings
and restarting: they are kept here, in ``BB_HOME/signin.json`` (the
secrets sealed with the site's key), and take effect at once.

Values set in the server's environment (``GMAIL_CLIENT_ID`` and so on, or
bb.env) win over the page, which then shows them as set on the server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FILE = "signin.json"
# field on Settings → its environment variable
FIELDS = {
    "gmail_client_id": "GMAIL_CLIENT_ID",
    "gmail_client_secret": "GMAIL_CLIENT_SECRET",
    "ms_client_id": "MS_CLIENT_ID",
    "ms_client_secret": "MS_CLIENT_SECRET",
    "ms_tenant": "MS_TENANT",
}
SECRETS = ("gmail_client_secret", "ms_client_secret")


def _path(settings) -> Path:
    return Path(settings.home) / FILE


def stored(settings) -> dict:
    """What the page saved: {field: value}, secrets opened."""
    try:
        raw = json.loads(_path(settings).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for field in FIELDS:
        value = raw.get(field) or ""
        if field in SECRETS and value:
            try:
                from bb.mail import crypto
                value = (crypto.open_json(settings.secret_key(), value) or {}).get("v", "")
            except Exception:
                value = ""
        out[field] = str(value or "").strip()
    return out


def from_server(settings) -> set[str]:
    """The fields the server's own settings fix (the page cannot change them)."""
    return {field for field, key in FIELDS.items() if (settings.env_at_start.get(key) or "").strip()}


def apply(settings):
    """Put the saved values into the running site, where the server's own
    settings leave a field empty. Workers started from now on get them too."""
    fixed = from_server(settings)
    saved = stored(settings)
    for field, key in FIELDS.items():
        if field in fixed:
            continue
        value = saved.get(field, "")
        if field == "ms_tenant":
            value = value or "common"
        setattr(settings, field, value)
        if value and not (field == "ms_tenant" and value == "common" and not saved.get(field)):
            settings.env[key] = value
        else:
            settings.env.pop(key, None)


def save(settings, values: dict) -> list[str]:
    """Store what the admin typed (a blank secret keeps the stored one;
    ``clear`` lists the providers to switch off) and apply it. Returns
    problems with what was typed, in words; nothing is stored then."""
    current = stored(settings)
    fixed = from_server(settings)
    new = dict(current)
    for field in FIELDS:
        if field in fixed or field not in values:
            continue
        value = str(values.get(field) or "").strip()
        if field in SECRETS and not value:
            continue                       # blank: keep what is there
        new[field] = value
    for provider in values.get("clear") or []:
        prefix = "gmail_" if provider == "google" else "ms_"
        for field in FIELDS:
            if field.startswith(prefix) and field not in fixed:
                new[field] = ""
    problems = []
    gid = new.get("gmail_client_id", "")
    if gid and not gid.endswith(".apps.googleusercontent.com"):
        problems.append("A Google client ID ends in .apps.googleusercontent.com: copy it from Google Cloud → Credentials.")
    if gid and not new.get("gmail_client_secret") and "gmail_client_secret" not in fixed:
        problems.append("Google needs the client secret too (shown when the client was created; Credentials → the client → Client secrets).")
    mid = new.get("ms_client_id", "")
    import re
    if mid and not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", mid):
        problems.append("A Microsoft application (client) ID looks like 1a2b3c4d-…: copy it from the app's Overview page in Entra.")
    tenant = new.get("ms_tenant", "")
    if tenant and not re.fullmatch(r"[A-Za-z0-9.\-]{2,64}", tenant):
        problems.append("The Microsoft tenant is 'common', 'consumers', 'organizations', a tenant ID or a domain.")
    if problems:
        return problems
    from bb.mail import crypto
    out = {}
    for field in FIELDS:
        value = new.get(field, "")
        if field in SECRETS and value:
            value = crypto.seal_json(settings.secret_key(), {"v": value})
        out[field] = value
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    apply(settings)
    return []


def status(settings) -> dict:
    """For the page: what is on and where each value comes from."""
    fixed = from_server(settings)
    saved = stored(settings)
    return {
        "google_on": bool(settings.gmail_client_id and settings.gmail_client_secret),
        "microsoft_on": bool(settings.ms_client_id),
        "fixed": sorted(fixed),
        "gmail_client_id": settings.gmail_client_id,
        "gmail_secret_set": bool(settings.gmail_client_secret),
        "ms_client_id": settings.ms_client_id,
        "ms_secret_set": bool(settings.ms_client_secret),
        "ms_tenant": settings.ms_tenant or "common",
        "saved": {k: bool(v) for k, v in saved.items()},
    }
