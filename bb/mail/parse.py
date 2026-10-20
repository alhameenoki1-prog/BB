"""RFC 822 → RawMessage, shared by the IMAP adapter and tests."""

import email
import email.policy
import re
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from bb.mail.providers.base import RawMessage


def _addresses(msg, header: str) -> list[dict]:
    out = []
    for name, addr in getaddresses([str(v) for v in msg.get_all(header, [])]):
        if addr:
            out.append({"name": name, "email": addr.lower()})
    return out


def body_text(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_rfc822(raw: bytes, *, provider_id: str, folder: str = "", outbound: bool | None = None) -> RawMessage | None:
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return None
    from_name, from_email = parseaddr(str(msg.get("From", "")))
    from_email = (from_email or "").lower()
    sent_at = ""
    try:
        dt = parsedate_to_datetime(str(msg.get("Date", "")))
        if dt is not None:
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            sent_at = dt.astimezone(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        sent_at = ""
    headers = {}
    for key in ("message-id", "in-reply-to", "references", "list-unsubscribe", "list-id", "precedence",
                "auto-submitted", "x-autoreply", "x-auto-response-suppress", "reply-to", "from", "to", "cc", "date"):
        value = msg.get(key)
        if value:
            headers[key] = str(value)[:500]
    text = body_text(msg)
    return RawMessage(
        provider_id=provider_id,
        thread_key="",
        message_id=str(msg.get("Message-ID", "")).strip(),
        in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
        references=str(msg.get("References", "")).strip(),
        from_email=from_email, from_name=from_name or "",
        to=_addresses(msg, "To"), cc=_addresses(msg, "Cc"),
        subject=str(msg.get("Subject", "")), body=text, snippet=re.sub(r"\s+", " ", text)[:160],
        headers=headers, folder=folder, sent_at=sent_at,
        outbound=bool(outbound) if outbound is not None else False,
    )
