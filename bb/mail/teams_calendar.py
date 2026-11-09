"""Build genuine Microsoft Graph calendar-event requests, without sending them.

POST /me/events with isOnlineMeeting=True and onlineMeetingProvider='teamsForBusiness'
creates a real Outlook event with Teams join details when the signed-in organizer
has appropriate Microsoft 365 licensing and Calendars.ReadWrite delegated consent.
This module deliberately does not perform network calls or create invitations.
"""
from datetime import datetime
from html import escape
from urllib.parse import urlparse


def build_teams_event(*, subject: str, start: str, end: str, timezone: str,
                      attendees: list[str], description: str = "",
                      project_url: str = "") -> dict:
    """Return the official Graph event JSON body; callers must authorize creation."""
    if not subject.strip() or len(subject) > 255:
        raise ValueError("A meeting subject is required (maximum 255 characters).")
    if not timezone.strip():
        raise ValueError("A Microsoft-compatible time zone is required.")
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("Use ISO-8601 local meeting date-times.") from exc
    if start_dt.tzinfo or end_dt.tzinfo or end_dt <= start_dt:
        raise ValueError("Provide increasing local date-times without offsets; timezone is separate.")
    if not attendees or len(attendees) > 50:
        raise ValueError("Provide 1 to 50 invited attendees.")
    clean = []
    seen = set()
    for address in attendees:
        addr = address.strip().lower()
        if "@" not in addr or any(c.isspace() for c in addr) or addr.startswith("@") or addr.endswith("@"):
            raise ValueError("Every attendee must have a valid email address.")
        if addr not in seen:
            seen.add(addr)
            clean.append({"emailAddress": {"address": addr}, "type": "required"})
    link = project_url.strip()
    if link:
        parsed = urlparse(link)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Project link must be a public HTTPS URL without embedded credentials.")
    body = escape(description).replace("\n", "<br>")
    if link:
        safe_link = escape(link, quote=True)
        body += f'<p>Project scope: <a href="{safe_link}">{safe_link}</a></p>'
    return {
        "subject": subject.strip(),
        "start": {"dateTime": start, "timeZone": timezone.strip()},
        "end": {"dateTime": end, "timeZone": timezone.strip()},
        "body": {"contentType": "HTML", "content": body},
        "attendees": clean,
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "allowNewTimeProposals": True,
    }
