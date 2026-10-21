# What to build next

A review of the whole system (the worker, the web app, the mail command
center, deployment) produced this list. It is ordered by how much each item
protects or speeds up a site with real users on it, not by how hard it is.
Items marked **done** shipped in the same change as this file; the rest are
open. Each entry says what is wrong today, what to build, and where.

## Now: things that lose mail or trust

1. **A suppression list that survives "Start over".** Today an opt-out
   closes the conversation and marks the prospect, but the dashboard's
   Start over wipes the workspace database, and a fresh upload of the same
   list would email the person again. Keep a small `suppressions` table
   (address, reason, date) outside the data that Start over deletes, check
   it at upload *and* at send time, and add a one-click unsubscribe link
   (`List-Unsubscribe-Post`) next to the mailto header Harvey already
   sends. Files: `harvey/state.py`, `bb/workspaces.py` (`reset_data`),
   `bb/recipients.py`, `harvey/agents/sender.py`,
   `harvey/integrations/mail_provider.py`.

2. **Read the inbox by UID cursor, not UNSEEN.** `smtp_mail.get_replies`
   searches `UNSEEN` and relies on the fetch marking mail as read. Anything
   a person opens on their phone first is never seen by Harvey, and a
   crash between fetch and store loses the batch. Keep the last UID per
   folder in settings, fetch `UID n+1:*` with `BODY.PEEK[]` so the user's
   read state is untouched, and page Gmail history the same way with
   dedup on Message-ID. Files: `harvey/integrations/smtp_mail.py`,
   `harvey/integrations/gmail.py`. (The mail command center's `ingest.py`
   already works this way; the worker should match it.)

3. **The first reply must not stop the writer.** **Done.** On native
   providers the handler now runs alongside the writer instead of
   replacing it (`harvey/main.py`, `decide_next_action`).

4. **Verify TLS certificates on IMAP and SMTP.** **Done.** The standard
   library's default context verified nothing; `imap_connect` and the
   mailbox check now use `ssl.create_default_context()`.

5. **Login hardening for a public site.** **Done.** `BB_TRUSTED_PROXIES`
   (default loopback) decides who may set `X-Forwarded-For`; with a tunnel
   the plain port binds to `127.0.0.1`; every response carries
   `X-Frame-Options`, `nosniff`, a referrer policy, and HSTS on https.
   Still open: a per-account brake in addition to the per-address one, so
   one address behind a shared proxy cannot lock out a whole office.

## Next: throughput and robustness at 10,000 recipients

6. **Mailbox-wide 5xx handling and a "retry failed" button.** A
   mailbox-level failure (SMTP 421 "too many connections", Graph 503) is
   classified per row today; after `MAX_SEND_RETRIES` the row fails for
   good while the mailbox was the problem. Treat `scope: mailbox` errors
   as a pause with backoff that counts against no row, and give the
   Outbox a "retry failed" action. Files: `harvey/agents/sender.py`,
   `harvey/dashboard.py`, `harvey/web/app.js`.

7. **Batch the database writes.** Uploading 10,000 recipients and staging
   their first emails commits one row at a time. Wrap the upload and the
   staging pass in one transaction each (`executemany`), and add an index
   on `outbox(campaign_id, prospect_id, step)` lookups the writer repeats.
   Files: `bb/recipients.py`, `harvey/state.py` (`commit_campaign`,
   `stage_outbox`).

8. **Reuse one SMTP connection per lane.** Each send opens, authenticates,
   and closes a connection. Keep a connection per lane open for the batch
   (`aiosmtplib.SMTP` with `connect()` once, `send_message` per row,
   reconnect on error), which is also what relays expect. Classify Graph
   errors the way SMTP ones are classified (`auth_failed`, `daily_limit`,
   `retryable`). Files: `harvey/integrations/smtp_mail.py`,
   `harvey/integrations/microsoft.py`.

9. **Worker health you can see.** A worker writes its log, nothing else.
   Add a heartbeat file the dashboard reads (last cycle, next cycle, what
   it is doing), a restart limit so a crashing worker is not revived
   forever, log rotation (`RotatingFileHandler`, 5 × 10 MB), and a free-disk
   check before uploads. Files: `harvey/main.py`, `bb/workspaces.py`
   (`keep_workers_alive`, `revive_worker`), `harvey/logging` setup.

10. **A dashboard that explains itself.** Show the model's status
    (`brain_status` already exists in Harvey's own dashboard but not the
    web one), a projected finish time from the current pace and the queue,
    and reply and bounce rates per campaign. Files: `bb/workspaces.py`
    (`_sending_status`), `bb/templates/dashboard.html`.

## Then: product and accounts

11. **Account lifecycle.** Users cannot change their own password; admins
    cannot delete a user (only disable); disabling stops the Harvey worker
    but not the mail command center's worker. Add self-service password
    change, delete with workspace removal, and make disable stop every
    process. Files: `bb/app.py`, `bb/accounts.py`, `bb/workspaces.py`,
    `bb/mail/worker.py`.

12. **Gmail on a quick tunnel.** One-click Gmail needs a fixed redirect
    URL; a quick tunnel's address changes on every restart, so the Google
    client refuses it. Say so on the Mailbox page and steer those sites to
    SMTP with an app password or to a named tunnel. Also warn seven days
    before a Google token issued by an OAuth app still "in testing" expires.
    Files: `bb/gmail_oauth.py`, `bb/templates/mailbox.html`.

13. **Recipients as a first-class page.** Uploads land in prospects with
    no way to search, filter by status, or export the people a campaign
    reached. Add a Recipients page with search, status counts, and CSV
    export. Files: `bb/app.py`, new template, `harvey/state.py`.

14. **Upload preview and hygiene.** Show the first rows and the column
    mapping before import, drop role addresses (`info@`, `sales@`) by
    default with a checkbox to keep them, and dedupe across uploads.
    Files: `bb/recipients.py`, `bb/templates/recipients.html`.

15. **A Replies page with actions.** Replies are visible but every action
    (answer, close, hand off) needs the Harvey dashboard or the mail
    command center. Add answer / close / snooze on the web app's
    Conversations page and an email or push notification for warm replies.
    Files: `bb/app.py`, `bb/templates/conversations.html`,
    `harvey/agents/handler.py`.

## Later: isolation and cost

16. **Tenant isolation.** Each workspace's secrets are a JSON file with
    owner-only permissions, and every worker runs as the same system user.
    Seal secrets with the site key (the mail command center's `crypto.py`
    already does this), and run workers under a per-user `HARVEY_HOME`
    with no access to the site's own environment.

17. **Egress policy.** A user's model endpoint (`brain.base_url`) and
    mailbox hosts are whatever they type. Refuse private and loopback
    addresses, and allow-list the known providers unless an admin opts a
    user out. Files: `bb/app.py` (`/apikey`, `/mailbox`),
    `bb/mail_autoconfig.py`.

18. **A per-person drafting cap.** In per-person mode every recipient
    costs a model call; 10,000 recipients on a paid key is a bill nobody
    expected. Estimate before writing (count × average tokens × price),
    show it on Profile, and stop at the user's daily cap with the reason on
    the dashboard. Files: `harvey/agents/writer.py`, `harvey/brain.py`,
    `bb/templates/profile.html`.

19. **Outbox review by segment and A/B subjects.** Approving 10,000 rows
    one at a time is not review. Approve by segment (one representative
    email per segment), and let a campaign carry two subjects with the
    reply rate shown per subject. Files: `harvey/dashboard.py`,
    `harvey/agents/writer.py`, `harvey/state.py`.

20. **Prompt caching on Claude.** The system prompt and the product skills
    are identical on every call; mark them `cache_control: ephemeral` so
    the per-person mode costs a fraction of today. Files: `harvey/brain.py`.
