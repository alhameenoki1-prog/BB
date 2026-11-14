# Microsoft calendar and Teams invitations in BB Studio

Open **BB Studio → Calendar invitations** and choose a connected organizer.
Use **Connect Microsoft calendar** for delegated sign-in and calendar consent.
An existing Microsoft 365 app connection can also create events after its
tenant grants the app `Calendars.ReadWrite`.

1. Enter the attendees, subject, local date and time, time zone, and description.
2. Paste the HTTPS URL into **Project scope link** to add a clickable link to
   the event description. Select **Include a Microsoft Teams meeting** if the
   organizer's calendar supports Teams.
3. Review the draft. When **Look up attendee companies** is selected, BB uses
   saved contact records and public company sites to show the matches for each
   attendee. The invitation body includes the company names and descriptions.
   Clear the option to omit the company details.
4. Select **Create event and invite attendees** to queue the approved draft.
   Microsoft Graph creates the calendar event and distributes invitations.

BB posts to `/me/calendar/events` for delegated sign-in and
`/users/{organizer}/calendar/events` for an app connection. BB records the
event ID and displays the Outlook event and Teams join links when Graph returns
them. The organizer can reschedule, cancel, or check RSVPs in Outlook.

Use `tests/test_studio_calendar.py` to verify the review, queue and outgoing
Graph payload for Outlook and Teams with test attendees. A production check
requires an authorized Microsoft 365 organizer and an approved test attendee.
Confirm the event opens in Outlook, the project-scope link appears in the
attendee's event, and the Teams join link opens for a Teams meeting.
