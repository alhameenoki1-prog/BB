# BB workspace UI

The redesign started from GitHub's [sender review branch](https://github.com/alhameenoki1-prog/BB/pull/3) at `0058594`. It uses the existing FastAPI routes, Jinja templates, and per-user SQLite stores. The hosted app runs from `bb/`. The `harvey/` engine still runs campaigns and has its own local CLI and dashboard. Docker, launchd, and Cloudflare continue to serve the same BB app.

GitHub `main` was at `2dcf9fc` when this was reviewed. The Mac's last reported health revision, `bac68b7`, matches a separate branch with its own Microsoft mailbox browser and link management pages. The preview uses an isolated checkout and data directory because those revisions have different features and storage histories.

## Where things live

| Section | What it contains | Scope |
| --- | --- | --- |
| Overview | New email and invitation shortcuts, pending Studio and reply drafts, threads needing replies, failed jobs, campaign setup and worker controls | Mail counts use the current mailbox filter for drafts and threads. Jobs are per user. Campaign performance uses the selected campaign. |
| Studio | Manual and assisted drafts, rich text, plain text, HTML, linked inline images, attachments, saved templates, addressing and personalization, Gmail-style and Outlook-style previews, dry runs and review | The connected From account controls sender identity. The review queue follows the mailbox filter. |
| Campaigns | Campaign list, campaign selection, recipients and exclusions, campaign outbox and delivery states | Recipient and outbox pages follow the selected campaign. |
| Inbox | Connected-mailbox threads, reply runs, campaign replies, auto-reply settings | Threads and reply runs use the mail workspace. Campaign replies and auto-reply settings use the selected campaign. |
| Calendar | Outlook events, Teams invitations, project-scope links, attendee details, draft review and event history | Uses connected Microsoft calendar organizers. |
| Delivery | Mail status, worker activity, synchronization, queued tasks, audit history, and delivery review | Uses the mail workspace. Audit filters apply to mailbox activity. |
| Settings | Gmail and Microsoft connections, Microsoft 365 app connections, SMTP/IMAP, Azure Email SMTP, and Amazon SES; plus campaign mailbox, profile, AI provider, and two-factor authentication | Sender accounts and identity settings are owned by the signed-in user. Campaign mailbox and profile follow the selected campaign. |
| Administration | Users, sign-in setup, and Microsoft connection diagnostics | Admin only. |

## Preview on the Mac

Run this in Terminal on the Mac that runs BB. This uses the existing Python environment, a fresh worktree, and a local address on port 8010.

```bash
cd ~/BB
git fetch origin feature/bb-workspace-ui
git worktree add --detach ~/BB-ui-preview FETCH_HEAD
cd ~/BB-ui-preview
export BB_HOME="$(mktemp -d "${TMPDIR:-/tmp}/bb-ui-preview.XXXXXX")"
export BB_HOST=127.0.0.1 BB_PORT=8010 BB_BASE_URL=http://127.0.0.1:8010
export BB_TUNNEL= BB_TUNNEL_TOKEN=
~/BB/.venv/bin/python -m bb create-user preview@example.test --admin
~/BB/.venv/bin/python -m bb
```

Use the password you just set at [http://127.0.0.1:8010](http://127.0.0.1:8010). Choose **Light**, **Dark**, or **System** in the header. Shrink the window to check the **Menu** and section tabs.

With an authorized test sender connected, open **Studio → New email**. Select From, enter the test address and subject, add an attachment, and set Reply-To in **Addressing & Reply-To**. Save, switch preview layouts, then use **Dry run**. Review the job in **Delivery → Jobs**. For Calendar, connect an authorized Microsoft test calendar, then enter attendees, dates, and a project-scope URL. Use **Review invitation** to check the link and the Teams option.

The repository also contains tests with simulated providers, which can run without provider accounts when the development dependencies are installed.

```bash
~/BB/.venv/bin/python -m pytest -q tests/test_studio_delivery.py tests/test_studio_calendar.py
```

Press **Control+C** in the preview Terminal to stop it.
