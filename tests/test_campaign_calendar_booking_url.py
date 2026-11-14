"""Isolated calendar URL validation tests: no network or email delivery."""

import pytest

from bb.campaign_routes import validated_booking_url


@pytest.mark.parametrize("value, expected", [
    ("", ""),
    ("  ", ""),
    ("https://calendar.example.test/book/meeting", "https://calendar.example.test/book/meeting"),
    (" https://bookings.example.test/meet?duration=30 ", "https://bookings.example.test/meet?duration=30"),
    ("https://cal.example.test:443/book", "https://cal.example.test:443/book"),
])
def test_valid_calendar_booking_links(value, expected):
    assert validated_booking_url(value) == expected


@pytest.mark.parametrize("value", [
    "http://calendar.example.test/book",
    "javascript:alert(1)",
    "data:text/html,hello",
    "//calendar.example.test/book",
    "https://user:password@calendar.example.test/book",
    "https://calendar.example.test/book#fragment",
    "https://calendar.example.test:8080/book",
    "https://localhost/book",
    "https://calendar.example.test/book\nInjected: header",
    "https://calendar.example.test/" + "a" * 2100,
    "https://[::1]/book",
])
def test_unsafe_calendar_booking_links_rejected(value):
    with pytest.raises(ValueError, match="Calendar link|HTTPS booking link"):
        validated_booking_url(value)
