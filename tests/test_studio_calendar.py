"""Outlook and Teams invitations through Studio with Graph intercepted."""

import asyncio
import json

import pytest

from bb.mail import service
from bb.mail.jobs import Jobs
from bb.mail.providers.graph import GraphAccount
from bb.mail.worker import Handlers
from bb.microsoft_oauth import CALENDAR_PERMISSION
from tests.test_studio_delivery import studio_client, mail_client  # noqa: F401


def test_teams_invitation_is_reviewed_and_created_as_the_connected_organizer(studio_client, monkeypatch):
    client, db, secret, _, _ = studio_client
    mailbox = asyncio.run(db.add_mailbox(
        address="organizer@example.test", provider="graph", status="ok",
        creds=service.crypto.seal_json(secret, {"refresh_token": "test-only", "calendar": True}),
    ))
    calls = []

    async def graph(self, method, path, *, params=None, json=None, scope=""):
        calls.append((method, path, json, scope))
        assert self.address == "organizer@example.test" and scope == CALENDAR_PERMISSION
        if method == "GET":
            return {"allowedOnlineMeetingProviders": ["teamsForBusiness"]}
        return {"id": "event-1", "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/meet/example"},
                "webLink": "https://outlook.office.com/calendar/item/example"}

    monkeypatch.setattr(GraphAccount, "_call", graph)
    response = client.post("/mail/studio/calendar", data={
        "mailbox": mailbox, "attendees": "john@example.test, ann@example.test", "subject": "Project scope",
        "start": "2026-12-01T13:00", "end": "2026-12-01T13:30", "timezone": "Asia/Dubai",
        "description": "Bring the first draft.", "project_url": "https://example.test/brief", "teams": "1",
    })
    preview = response.headers["location"]
    assert preview.startswith("/mail/studio/calendar?draft=")
    assert "2026-12-01 13:00" in client.get(preview).text and "ann@example.test" in client.get(preview).text
    assert not calls and not asyncio.run(Jobs(db).list())
    draft = asyncio.run(db.one("SELECT * FROM calendar_invites"))
    action = f"/mail/studio/calendar/{draft['id']}/send"
    assert "Invitation%20queued" in client.post(action).headers["location"]
    assert "already%20queued" in client.post(action).headers["location"]

    async def work():
        queue = Jobs(db)
        job = await queue.claim()
        outcome = await Handlers(db, queue, secret, {}).run(job)
        await queue.complete(job["id"], **outcome)
        return outcome

    assert asyncio.run(work()) == {"status": "sent", "event_id": "event-1"}
    assert [call[:2] for call in calls] == [("GET", "/me/calendar"), ("POST", "/me/calendar/events")]
    payload = calls[-1][2]
    assert payload["transactionId"] == draft["id"]
    assert payload["start"] == {"dateTime": "2026-12-01T09:00:00", "timeZone": "UTC"}
    assert payload["onlineMeetingProvider"] == "teamsForBusiness"
    assert "https://example.test/brief" in payload["body"]["content"]
    page = client.get(preview).text
    assert "Created in Outlook" in page and "Open Teams meeting" in page


@pytest.mark.asyncio
async def test_teams_creation_stops_when_the_organizer_does_not_offer_teams(monkeypatch):
    from bb.mail.providers.base import PermanentError

    async def graph(_self, method, path, **kwargs):
        assert (method, path) == ("GET", "/users/sender@example.test/calendar")
        return {"allowedOnlineMeetingProviders": []}

    monkeypatch.setattr(GraphAccount, "_call", graph)
    account = GraphAccount({"address": "sender@example.test"}, {}, {})
    with pytest.raises(PermanentError, match="does not support"):
        await account.create_calendar_event({"isOnlineMeeting": True})


def test_uncertain_deliveries_require_individual_review_to_retry(studio_client):
    client, db, _, mailbox, _ = studio_client

    async def seed():
        queue = Jobs(db)
        invite = await queue.enqueue("calendar_invite", mailbox, {"invite_id": "test"})
        assert (await queue.claim())["id"] == invite
        await queue.release(invite)
        send = await queue.enqueue("send_draft", mailbox, {"draft_id": "test"})
        assert (await queue.claim())["id"] == send
        await queue.release(send)
        return invite, send

    invite, send = asyncio.run(seed())
    client.post("/mail/jobs/retry-all")
    assert asyncio.run(Jobs(db).counts())["dead"] == 2
    response = client.post(f"/mail/jobs/{send}/retry")
    assert "cannot%20be%20changed" in response.headers["location"]
    response = client.post(f"/mail/jobs/{invite}/retry", data={"delivery_reviewed": "1"})
    assert "Queued%20again" in response.headers["location"]
    assert asyncio.run(Jobs(db).counts())["queued"] == 1
