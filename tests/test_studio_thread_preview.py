"""Fictional Studio threads must never become outbound messages."""
from datetime import date

import pytest

from bb.mail.thread_preview import fictional_conversation


def test_fictional_thread_is_visual_only_and_clearly_marked():
    page = fictional_conversation(
        recipient={"email": "john.smith@example.test", "name": "John Smith"},
        sender_name="Project Team", subject="Project scope",
        topic="a calendar meeting", today=date(2026, 11, 12),
    )
    assert page.count("SIMULATED — NOT SENT") == 4
    assert "NO PRIOR EMAILS EXIST" in page
    assert "2026-11-09" in page and "2026-11-12" in page
    assert "John Smith" in page and "a calendar meeting" in page
    assert "Message-ID" not in page and "In-Reply-To" not in page


def test_fictional_thread_escapes_untrusted_fields():
    page = fictional_conversation(
        recipient={"email": "john@example.test", "name": "<script>alert(1)</script>"},
        sender_name="<img src=x>", subject="<svg onload=alert(1)>",
        topic="<script>evil()</script>", today=date(2026, 11, 12),
    )
    assert "<script>" not in page and "<svg onload" not in page
    assert "<img src=x>" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.parametrize("field", ["subject", "topic", "sender_name"])
def test_preview_rejects_oversized_fields(field):
    kwargs = dict(recipient={"email": "john@example.test"}, sender_name="Team",
                  subject="Scope", topic="timeline")
    kwargs[field] = "X" * 2001
    with pytest.raises(ValueError):
        fictional_conversation(**kwargs)
