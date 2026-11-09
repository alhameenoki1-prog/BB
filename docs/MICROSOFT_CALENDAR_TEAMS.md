# Native Microsoft calendar and Teams integration — implementation gate

BB must create **real Microsoft calendar events** with Teams meetings, not
messages that imitate Outlook or Teams invitations.

## Official API contract

- Use Microsoft Graph `POST /me/events` with the organizer's **delegated**
  Microsoft 365 OAuth token, `Calendars.ReadWrite` permission and explicit
  calendar consent. For Teams, set `isOnlineMeeting: true` and
  `onlineMeetingProvider: teamsForBusiness`.
- A supported Microsoft 365 account with Teams capability is required; personal
  Outlook accounts or tenants without Teams capability may not support this.
- Microsoft creates and distributes the calendar invitations. Only use the
  `onlineMeeting.joinUrl` returned by Graph. Never manufacture join URLs or
  impersonate Microsoft mail templates.
- A real event is only confirmed when Graph returns success and an event ID.
  Store the organizer's event ID, Graph request ID and response status; keep
  secrets and access tokens out of logs.
- Use event updates and cancellations via the same authorized organizer
  calendar; preserve meeting identity. Reconcile attendee response status from
  Graph rather than guessing.

## Current implementation

`bb/mail/teams_calendar.py` builds and validates an official Graph event
payload with Teams enabled, attendee addresses, meeting time, HTML-escaped
description and a verified HTTPS project-scope link. Offline tests are in
`tests/test_teams_calendar_payload.py`.

**This does not yet create, schedule or send meetings.** Existing Microsoft
OAuth scopes are for mail and must not silently be expanded. The next slice is
a separate, explicit calendar consent flow, an authenticated meeting editor,
server-side review and recipient-consent checks, then a Graph event service
with mocked HTTP tests and explicit duplicate-event protections.

Do not use company research to send unsolicited meeting invitations or
automatically add unconsenting leads to calendars. Research summaries and
personalization require source verification and human approval.

## Verification before release

1. Offline payload tests and existing mail regression tests pass.
2. Separate Microsoft calendar consent is requested only when used.
3. Test with an owned Microsoft 365 organizer and consenting .test-style mock
   attendees offline; use real consenting seed accounts only with approval.
4. Confirm Graph returns event ID, `onlineMeeting.joinUrl`, and accurate
   attendee status, then test reschedule and cancellation.
5. UI makes clear whether an event is draft, pending or created; never mark
   sent on local payload construction alone.
6. Keep the production site unchanged until separately approved for deployment.
