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
