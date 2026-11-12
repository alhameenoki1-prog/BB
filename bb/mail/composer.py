"""Validation for user-written, review-before-send messages.

The existing send queue and MIME builder remain the only delivery path.
"""


def validate_manual_draft(subject: str, body: str) -> tuple[str, str]:
    """Reject empty, oversized, or header-injection-prone manual drafts."""
    subject = subject.strip()
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not subject or len(subject) > 200:
        raise ValueError("Subject must contain 1 to 200 characters.")
    if "\n" in subject or any(ord(ch) < 32 for ch in subject):
        raise ValueError("Subject cannot contain line breaks or control characters.")
    if not body or len(body) > 100_000:
        raise ValueError("Message must contain 1 to 100,000 characters.")
    if "\x00" in body:
        raise ValueError("Message cannot contain NUL characters.")
    return subject, body

import re

_TOKEN_RE = re.compile(r"\{\{\s*(email|name|first_name)\s*\}\}", re.IGNORECASE)
_NAME_PARTS = re.compile(r"[._+-]+")


def recipient_fields(recipient: dict) -> dict[str, str]:
    """Recipient-specific fields; never fetch or infer an unrelated identity.

    A saved contact name wins. Otherwise, derive a display name from the
    address local part only when it contains alphabetic characters.
    """
    address = str(recipient.get("email") or "").strip()
    saved = str(recipient.get("name") or "").strip()
    local = address.partition("@")[0]
    derived = " ".join(part.capitalize() for part in _NAME_PARTS.split(local)
                       if part and part.isalpha()) if not saved else ""
    name = saved or derived
    return {"email": address, "name": name, "first_name": name.split()[0] if name else ""}


def render_recipient_tokens(value: str, recipient: dict) -> str:
    """Resolve only explicit, allowlisted Studio tokens, case-insensitively.

    Unknown tokens are left unchanged rather than silently fabricated.
    """
    fields = recipient_fields(recipient)
    return _TOKEN_RE.sub(lambda match: fields[match.group(1).lower()], value)
