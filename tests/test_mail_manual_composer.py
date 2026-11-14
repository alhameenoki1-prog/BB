"""Local-only tests for AI-free manual compose; no mail or network."""

import pytest

from bb.mail.composer import validate_manual_draft


def test_manual_draft_keeps_content_and_normalizes_newlines():
    assert validate_manual_draft(" Meeting ", "Hello\r\nPlease book: https://calendar.example.test/meet\r\n") == (
        "Meeting", "Hello\nPlease book: https://calendar.example.test/meet"
    )


@pytest.mark.parametrize("subject, body", [
    ("", "Body"),
    ("Subject", ""),
    ("A" * 201, "Body"),
    ("Subject\r\nBcc: attacker@example.test", "Body"),
    ("Subject\x00", "Body"),
    ("Subject", "a" * 100001),
    ("Subject", "NUL\x00inside"),
], ids=["empty-subject", "empty-body", "long-subject", "header-injection", "control-subject", "oversized-body", "nul-body"])
def test_invalid_manual_drafts_are_rejected(subject, body):
    with pytest.raises(ValueError):
        validate_manual_draft(subject, body)
