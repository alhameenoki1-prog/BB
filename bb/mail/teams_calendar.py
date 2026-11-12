"""Validate and render Outlook and Teams calendar invitations."""
from datetime import datetime
from html import escape
import json

import pytz

from bb.mail.composer import parse_copy_recipients
from bb.mail.studio import web_url


def build_teams_event(*, subject: str, start: str, end: str, timezone: str,
                      attendees: list[str], description: str = "",
                      project_url: str = "", teams: bool = True) -> dict:
    """Return the official Graph event JSON body; callers must authorize creation."""
    if not subject.strip() or len(subject) > 255 or any(ord(ch) < 32 for ch in subject):
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
    addresses = parse_copy_recipients(", ".join(attendees), field_name="Attendees", limit=50)
    clean = [{"emailAddress": {"address": address}, "type": "required"} for address in addresses]
    link = project_url.strip()
    if link and (not link.lower().startswith("https://") or not web_url(link) or len(link) > 2048):
        raise ValueError("Project link must be an HTTPS URL without embedded credentials.")
    body = escape(description).replace("\n", "<br>")
    if link:
        safe_link = escape(link, quote=True)
        body += f'<p>Project scope: <a href="{safe_link}">{safe_link}</a></p>'
    event = {
        "subject": subject.strip(),
        "start": {"dateTime": start, "timeZone": timezone.strip()},
        "end": {"dateTime": end, "timeZone": timezone.strip()},
        "body": {"contentType": "HTML", "content": body},
        "attendees": clean,
        "allowNewTimeProposals": True,
    }
    if teams:
        event.update(isOnlineMeeting=True, onlineMeetingProvider="teamsForBusiness")
    return event


def studio_invitation(form) -> tuple[dict, dict]:
    """Convert the organizer's wall-clock times to unambiguous UTC for Graph."""
    start = str(form.get("start", ""))
    end = str(form.get("end", ""))
    timezone = str(form.get("timezone", "")).strip()
    subject = str(form.get("subject", "")).strip()
    description = str(form.get("description", "")).strip()
    project_url = str(form.get("project_url", "")).strip()
    if len(description) > 10_000 or len(timezone) > 80:
        raise ValueError("Use a valid time zone and a description of up to 10,000 characters.")
    try:
        zone = pytz.timezone(timezone)
        start_utc, end_utc = [
            zone.localize(datetime.fromisoformat(value), is_dst=None).astimezone(pytz.UTC).replace(tzinfo=None).isoformat()
            for value in (start, end)
        ]
    except (ValueError, pytz.UnknownTimeZoneError, pytz.InvalidTimeError) as exc:
        raise ValueError("Choose valid local start and end times and an IANA time zone, such as Asia/Dubai.") from exc
    attendees = parse_copy_recipients(str(form.get("attendees", "")), field_name="Attendees", limit=50)
    event = build_teams_event(subject=subject, start=start_utc, end=end_utc, timezone="UTC",
                             attendees=attendees, description=description, project_url=project_url,
                             teams=form.get("teams") == "1")
    display = {"start": start.replace("T", " "), "end": end.replace("T", " "), "timezone": timezone,
               "description": description, "project_url": project_url}
    return event, display


def calendar_connection(mailbox: dict, creds: dict) -> bool:
    if mailbox.get("provider") != "graph":
        return False
    if creds.get("refresh_token"):
        return bool(creds.get("calendar"))
    return all(creds.get(field) for field in ("tenant_id", "client_id", "client_secret"))


async def send_invitation(db, secret: str, env: dict, invite_id: str, job_id: str) -> dict:
    from bb.mail import service
    from bb.mail.providers import AuthError, account_for
    from bb.mail.providers.base import PermanentError
    from bb.mail.runs import is_excluded, ledger_for

    invite = await db.one("SELECT * FROM calendar_invites WHERE id = ? AND job_id = ? AND status = 'queued'",
                          (invite_id, job_id))
    if invite is None:
        return {"status": "skipped"}
    event = json.loads(invite["event_json"])
    event["transactionId"] = invite_id
    box = await db.mailbox(invite["mailbox_id"])
    creds = service.open_creds(secret, box) if box else {}

    async def failed(reason):
        await db.execute("UPDATE calendar_invites SET status = 'failed', error = ?, updated_at = ? "
                         "WHERE id = ? AND job_id = ?", (reason, service.now_iso(), invite_id, job_id))
        return {"status": "failed"}

    if not box or not service.ready_to_send(box, manual=True)[0] or not calendar_connection(box, creds):
        return await failed("The organizer's Microsoft connection needs calendar access.")
    settings = service.settings_of(box)
    ledger = ledger_for(env)
    for attendee in event["attendees"]:
        address = attendee["emailAddress"]["address"]
        if is_excluded(settings, address) or (ledger and ledger.is_suppressed(address)):
            return await failed(f"{address} is on an exclusion list.")
    account = account_for(box, creds, env)
    try:
        created = await account.create_calendar_event(event)
    except (AuthError, PermanentError) as exc:
        return await failed(str(exc))
    finally:
        await service.persist_creds(db, secret, box, account)
    links = {
        "outlook": str(created.get("webLink") or ""),
        "teams": str((created.get("onlineMeeting") or {}).get("joinUrl") or ""),
    }
    result = {"id": created["id"], **{key: url for key, url in links.items()
                                      if url.startswith("https://") and web_url(url)}}
    await db.execute("UPDATE calendar_invites SET status = 'sent', result_json = ?, updated_at = ? "
                     "WHERE id = ? AND job_id = ?", (json.dumps(result), service.now_iso(), invite_id, job_id))
    await db.log("sent", f"Microsoft created calendar invitation {event['subject']!r}.", box["id"], invite_id=invite_id)
    return {"status": "sent", "event_id": created["id"]}
