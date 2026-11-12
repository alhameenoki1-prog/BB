"""Validation for user-written, review-before-send messages.

The existing send queue and MIME builder remain the only delivery path.
"""

import re
from email.errors import HeaderParseError
from email.headerregistry import Address


def validate_manual_draft(subject: str, body: str, *, allow_empty: bool = False) -> tuple[str, str]:
    """Reject empty, oversized, or header-injection-prone manual drafts."""
    subject = subject.strip()
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not subject or len(subject) > 200:
        raise ValueError("Subject must contain 1 to 200 characters.")
    if "\n" in subject or any(ord(ch) < 32 for ch in subject):
        raise ValueError("Subject cannot contain line breaks or control characters.")
    if (not body and not allow_empty) or len(body) > 100_000:
        raise ValueError("Message must contain 1 to 100,000 characters.")
    if "\x00" in body:
        raise ValueError("Message cannot contain NUL characters.")
    return subject, body

_TOKEN_RE = re.compile(r"\{\{\s*(email|name|first_name|company)\s*\}\}", re.IGNORECASE)
_NAME_PARTS = re.compile(r"[._-]+")


def recipient_fields(recipient: dict) -> dict[str, str]:
    """Recipient-specific fields; never fetch or infer an unrelated identity.

    A saved contact name wins. Otherwise, derive a display name from the
    address local part only when it contains alphabetic characters.
    """
    address = str(recipient.get("email") or "").strip()
    saved = str(recipient.get("name") or "").strip()
    local = address.partition("@")[0].partition("+")[0]
    derived = " ".join(part.capitalize() for part in _NAME_PARTS.split(local)
                       if part and part.isalpha()) if not saved else ""
    name = saved or derived
    fields = {"email": address, "name": name, "first_name": name.split()[0] if name else ""}
    if recipient.get("company"):
        fields["company"] = str(recipient["company"]).strip()
    return fields


def has_recipient_tokens(*values: str) -> bool:
    return any(_TOKEN_RE.search(value) for value in values)


def render_recipient_tokens(value: str, recipient: dict, *, html: bool = False) -> str:
    """Resolve only explicit, allowlisted Studio tokens, case-insensitively.

    Unknown tokens are left unchanged rather than silently fabricated.
    """
    fields = recipient_fields(recipient)
    if html:
        from html import escape
        fields = {key: escape(text, quote=True) for key, text in fields.items()}
    return _TOKEN_RE.sub(lambda match: fields.get(match.group(1).lower(), match.group(0)), value)


def needs_company(*values: str) -> bool:
    return any(match.group(1).lower() == "company" for value in values for match in _TOKEN_RE.finditer(value))


def parse_copy_recipients(raw: str, *, field_name: str, limit: int = 20) -> list[str]:
    """Parse a comma/semicolon separated Cc/Bcc list without accepting headers.

    Addresses are returned in original order and normalized for comparison.
    """
    raw = raw or ""
    if len(raw) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(f"{field_name} contains an invalid address list.")
    raw = raw.strip()
    if not raw:
        return []
    tokens = [part.strip() for part in raw.replace(";", ",").split(",")]
    if any(not part for part in tokens) or len(tokens) > limit:
        raise ValueError(f"Enter up to {limit} email addresses in {field_name}, separated by commas.")
    addresses = []
    for token in tokens:
        try:
            address = Address(addr_spec=token)
        except (HeaderParseError, ValueError) as exc:
            raise ValueError(f"Enter a valid email address in {field_name}.") from exc
        if (address.addr_spec != token or len(token) > 254 or not address.username or
                "." not in address.domain or address.domain.startswith("[") or
                any(ch.isspace() for ch in token)):
            raise ValueError(f"Enter a valid email address in {field_name}.")
        normalized = token.lower()
        if normalized not in addresses:
            addresses.append(normalized)
    return addresses
