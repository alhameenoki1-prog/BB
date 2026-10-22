# Changelog

Notable changes to Harvey. Dates are release dates; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and versions
follow [semver](https://semver.org/), with the caveat that Harvey is pre-1.0 and
minor versions can still change behaviour.

## [Unreleased]

### Changed

- **The pipeline does not wait its turn.** Writing now runs every cycle
  while recipients are waiting for an email, beside reading replies and
  sending, instead of only when nothing else needs doing; the writer's
  lead over the sender is two days of the daily limit or twenty send
  batches and never under a hundred first emails in per-person mode
  (`Writer.room`), so the sender never idles; per-person drafting handles
  40 people per segment per cycle (was 8) with `brain.concurrency` model
  calls in flight (default 6, 1 to 16). While there is still writing to
  do, the next cycle follows in 45 seconds rather than a full heartbeat.
  The uploaded list's domain and verifier checks moved off the writer's
  path: they ride along the heartbeat under a 45-second budget, sixteen
  lookups and six verifier calls at a time, and the sender re-reads the
  address status before every send so an address found dead after
  staging is cancelled, not sent.

### Added

- **A soft no still gets the brief, once.** "Not interested", "booked
  up", "not what we do" now gets one closing note: thanks for the
  straight answer, nothing to do on their side, and the brief left with
  them "in case it's ever relevant" (the link introduced as exactly that,
  the file attached per the policy), no question, no ask, no follow-ups;
  the conversation closes and nothing else is written to them. The
  command center's reply runs do the same. A HARD no is different and is
  detected by code, never left to the model: unsubscribe, remove me, stop
  emailing, "this is spam", hostility, a legal threat. Those get nothing,
  ever: the worker marks the person opted out (a re-upload keeps that), a
  legal or compliance threat is also flagged for a human, and in the
  command center the address joins the mailbox's excluded list, the
  thread is put away, and sending to it is refused.
- **Follow up with past contacts.** The command center's Reply runs page
  has a second run: scan the conversations you already had in the last
  3, 6 or 12 months and continue each one in its own thread. Only
  two-way threads with real people count (they wrote by hand, you
  answered); automated and bulk senders, colleagues at your own domains,
  excluded and suppressed addresses, threads put away by hand, contacts
  already followed up with, and threads where the brief's link already
  went out are left out, and each contact gets one message per scan (the
  most recent thread). Each follow-up is written from that thread: it
  picks up the specific thing it was about, bridges to the project the
  way that thread allows, carries the brief's link once, and ends with at
  most one question. The model first judges whether the thread is one to
  continue at all; personal or private threads (family, a doctor, a bank,
  HR, a receipt) are skipped as "not suitable" and never asked again. A
  draft that shares nothing with the thread fails the check and is written
  again; what still cannot be bridged waits on Review while the confident
  ones send, paced and capped like replies, threaded under the last
  message so it lands in the same conversation in their client.

- **Sending without you in the loop.** Before an email is staged, the
  sender runs the spam check and a subject check on the rendered email;
  what fails is rewritten once by the model (with the campaign goal in
  front of it) and checked again, and only what still fails goes to the
  Outbox's **Needs review** tab, where nothing else waits on it: the rest
  keeps sending, and the dashboard says so instead of calling the
  pipeline stuck. The writer's own hold uses the same words.
- **Subjects a person would type.** `spamcheck.subject_risk` scores every
  subject on its own: boilerplate ("Quick question", "Exciting
  opportunity", "Partnership proposal", "Following up", "Introduction"),
  clickbait and punctuation, a gloomy or pleading tone, capitals, a
  subject about the act of writing rather than the thing, a one-word or
  overlong subject, and a subject that shares no content word with the
  email it sits on. A subject that fails is replaced, in the writer, the
  segment variants, the sender and the Rewrite, with a plain one built
  from the sender's own need (`subject_from_need`): "About 40 tons of
  rebar a month for a year", "A builder for a 24-unit residential block
  in Towson". The writer's subject rule says the same.
- **Rewrite on every queued email.** Every scheduled or held email in the
  Outbox has a Rewrite button that opens two forms: an instruction for
  the assistant ("shorter; end by offering the brief"), which rewrites it
  with your own AI provider under the campaign goal, keeps the greeting
  line and the sign-off and runs the same checks before saving; or the
  subject and body to edit by hand, with Save and (for a held email) Save
  and approve. Scheduled emails can also be cancelled there.
- **The campaign goal.** A new profile field, "What this campaign is for",
  stored as `outreach.goal`, goes into every first email, follow-up and
  segment variant (the writer), every reply and inbox answer (the
  handler), every command-center draft and reply run, and every Rewrite.
  It is written as the sender's own words to serve, never copied, and it
  outranks the shape of the worked examples.
- **Command-center replies carry the project brief.** The files on the
  profile's attachments list (the project brief) are now attached by the
  command center too, on the first reply in a thread or on every reply as
  the "Send my attachments" policy says, and the drafting prompt tells
  the model the brief is attached so the reply refers to it; the scope
  link still goes where the marker is. The activity log records each
  attachment.
- **Is the mailbox still sending?** The worker logs in to the mailbox
  (SMTP and IMAP, as `harvey status --check` does) every 30 minutes and
  watches delivery: a failed login, or approved emails due for an hour and
  a half with nothing delivered and no deliberate pause (quiet hours,
  warm-up, the daily cap, a cool-off, the kill switch), raises one plain
  sentence that the dashboard and the Mailbox page show in red and the
  log repeats until it clears (`harvey/health.py`). The Mailbox page shows
  the last check and the last delivery.
- **An address the mail server refuses is a bounce.** A permanent refusal
  of the recipient at send time (a 5xx on RCPT, such as Vade Secure's
  `550 5.7.1 Recipient rejected ... #_407`, which means the recipient's
  domain does not exist) no longer sits in Failed as an SMTP error: the
  address is marked invalid, the person's queued emails are cancelled, a
  dead domain holds the company's other first emails for review, the
  bounce rate counts it, and the Bounced tab explains it in plain words.
  Every uploaded address's domain is also looked up before anything is
  written, with no verifier key needed: a domain with no mail server is
  marked invalid and never sent to.

### Fixed

- **Review fixes across the last week's changes.** An IMAP page bigger
  than one pass now goes oldest first and the position stops at the last
  message actually read (newer replies were skipped); a mailbox read for
  the first time takes its position from the server (UIDNEXT), so a reply
  opened on the phone before the first pass is not lost; a reply the model
  could not handle (provider down, worker stopped) is read again next
  cycle. A bounce is classified by its verdict, not by the DKIM header
  Gmail quotes below it, and "authentication" only means the real
  authentication codes; a block notice that matches none of our sent
  emails cannot pause the mailbox; only a nonexistent address (never a
  full mailbox or a block) holds the other first emails to that company,
  and the follow-ups are left alone. The list check marks an uploaded
  address invalid only when the domain does not exist or takes no mail;
  a DNS lookup that merely failed is tried again next cycle, and a
  catch-all stays "provided" with a note. A reply to someone who said no
  carries no scope link even if the model wrote the marker, the link
  goes above the sign-off, and the model is told to ask them for a time
  unless the standing instructions give one. Saving the Inbox page no
  longer erases the needs rows, and a profile save that is refused shows
  the rows as typed, not the stored ones. The tunnel adopts a recorded
  process only when it can be seen to be cloudflared, refuses one started
  with another token, and the site says on shutdown that the link is
  still up and how to stop it.

### Changed

- **Nothing in the brief is a sentence to copy.** A simulation of the
  writer and the handler (drafts written from the real rendered prompts
  and scored with Harvey's own checks) read as one person asking another,
  but found the prompts nudging toward the same words every time or
  toward facts nobody gave. Now: the call sentence in a first email is
  described, not quoted, and must be written fresh; the sector asks say
  which details come only "if the need gives them" (a spec, a date, a
  purpose, a horizon) and the brief says a missing one is left out, never
  guessed; the sentence introducing the full scope describes what it
  covers from the facts given, never contents the model was not told
  about (drawings, a site plan, a schedule); the pointer to the brief is
  a statement and does not count as the one question; a reply to a no
  is told the brief is not for this reply and asks only who could take
  it on; the background field is marked as the sender's own words, never
  a pitch. A company whose name is an ordinary word ("Build") is now
  caught only written as a name, capitalised or in capitals, however the
  upload cased it, so "a builder" no longer trips the company-name retry
  or gets scrubbed out of the email.
- **A draft that lifts the examples' sentences is written again.** A
  second simulation with three independent stand-ins and six judges found
  the drafts still copying the worked examples' closing questions and
  sentences nearly word for word ("and who should I send the bar list to
  for pricing?", "Start is April; the drawings and permits are done"). Any
  run of six words a first email or a reply shares with the examples, and
  not with the sender's own need, line or instructions, is now caught by
  code and the model is asked once more for its own words (writer,
  segment variants and handler alike); "bar list" and two other example
  phrases join the list of example-only facts. The handler's examples no
  longer share one closing line, say "I" rather than a company "we", and
  the brief says a reply ends either with the question about a day or
  with one short pointer to the brief, never both. The ask bullet follows
  the chosen next step: a visit is a visit, and "a reply by email" adds
  no call sentence at all. "Walk you through", "glad to" and "happy to"
  join the banned list.
- **The quick-tunnel link survives restarts and updates.** A
  `trycloudflare.com` address lives as long as its cloudflared process,
  and the site used to start a new one on every start, so every update
  broke the link people had (Cloudflare error 1016). cloudflared now runs
  detached, recorded in `BB_HOME/tunnel.json`, and a restarting site adopts
  it; the service units leave it alone (`AbandonProcessGroup`,
  `KillMode=process`). `python -m bb tunnel status|stop|restart` manages it;
  `doctor` and the link check recognise the site's own tunnel and a dead
  address.
- **A guessed list, not a spam verdict.** Microsoft 365 answers a name
  that is not in a company's directory with `550 5.4.1 Recipient address
  rejected: Access denied`, which Gmail shows as "Message blocked". It is
  now named for what it is on the dashboard. When one address at a
  company bounces that way, the other queued emails to that company wait
  for your approval (the pattern is probably wrong for the whole company).
  When the server has a verifier key (Reoon, ZeroBounce or Hunter), every
  uploaded address is checked before it is written to: nonexistent ones are
  never sent, domains that do not exist (or publish no mail server) are
  invalid without spending a credit, and a catch-all domain (where nothing
  can be proven) is kept as given and noted.
- **Rewrite the unsent emails after an update.** First emails written with
  the old brief ("our current needs") that are still queued are counted on
  the dashboard with a button that cancels them and has the worker write
  them again from the profile; nothing sent is touched. A segment's
  sequence is also no longer reused once the writing brief itself has
  changed, so an update takes effect on the next batch by itself.
- **Getting past the spam verdict.** A "blocked" bounce ("likely
  unsolicited mail", "message blocked", "policy", "reputation") is a
  verdict on the sender, not a bad address. Four changes: the first email
  to each person goes as **plain text** with a links-free, image-free
  signature (`channels.email.plain_first_email`, on by default; the full
  signature goes on replies and follow-ups); segment mode writes **three
  wordings** of the first email and the sender rotates them, so a hundred
  recipients never get one identical body; a block now **pauses sending**
  for six hours (a day after the third block that day), drops the
  follow-ups to that person and keeps the address valid instead of
  marking it invalid; and a personal gmail.com or outlook.com mailbox is
  held to the **Gentle** pace. Subjects about the act ("Looking to start
  work with your team", "reaching out") are scored as spam.
- **Replies always carry the full scope.** The first reply in every
  conversation introduces the scope document and includes its link,
  asked for or not, in a sentence that says what is in it and that it is
  the quickest way to see whether this is their kind of job, and the
  reply ends by pointing them to it. Once it has gone out in a thread it
  is not sent again, and a no, an opt-out or a complaint never gets it.
  The command center does the same; the Inbox auto-reply (mail from
  strangers) sends it when the model chooses to or when they asked for
  details.
- **The worker no longer marks your replies as read.** The SMTP/IMAP
  reader used an UNSEEN search and a fetch that set the read flag, so a
  reply the worker had seen looked already-read in your own mail client
  (or was simply missed if you had opened it first). It now reads by UID
  position, opens the inbox read-only and PEEKs bodies; the position is
  kept per mailbox in `data/imap_cursor.json`.
- **Why emails bounce.** The dashboard groups bounces by what the
  receiving server said (no such address, sending too fast, judged
  unsolicited, authentication failed…) with what to do about each, and
  says which ones are the list and which are the mailbox.
- **Enquiries read as one person writing to another.** The first email
  in inquiry mode was coming out as a campaign letter ("Our team has a
  current need we would like to talk through with you"). The profile now
  collects the need **per kind of business** (a Construction row, a
  Financial advisory row, a Manufacturing row, each in your words, each
  with an optional link to the full scope) and a one-line **about you**;
  the writer picks the row for the recipient's sector and writes from an
  examples-first brief (four enquiries done right, the rejected one
  labelled wrong, a dozen literal rules a small model can follow). The
  sender may say who they are in half a line; the recipient's company is
  still never named. A deterministic **vagueness gate** beside the spam
  check holds any draft with campaign phrasing or nothing concrete from
  the need. The closing ask is one plain sentence before the request,
  never the question itself; the no-name greeting is "Hi," not "Hi there,".
- **Replies stay on the need and send the full scope once.** Inquiry mode
  has its own reply brief (no sales skills, no "tie back to value"): answer
  first, invent nothing, confirm the next step. When someone wants details
  the reply says in one sentence what is in the scope document and why it
  helps them answer, and the link goes in **by code**, once per
  conversation, matched to their sector. The Inbox auto-reply knows the
  same documents (listed on the Inbox page), and the mail command center's
  reply runs and command bar draft from the same profile, so "send the
  scope link when they ask" in a run's instructions does what it says.
- **The first reply no longer stops the writer.** On Gmail, SMTP and
  Microsoft 365 the mailbox is read every cycle anyway, so an open
  conversation now runs the handler *alongside* the writer instead of
  replacing it. Before, the first warm reply ended new outreach for everyone
  else until that conversation closed.
- **Mail connections verify the server's certificate.** IMAP (and the
  mailbox check's SMTP) used the standard library's default context, which
  verifies nothing; anyone on the path could have read the mailbox
  password. A host with a self-signed certificate is now refused.
- **Go-live hardening.** The site trusts `X-Forwarded-For` only from
  `BB_TRUSTED_PROXIES` (default the local cloudflared or Caddy), so a
  visitor cannot forge an address to dodge the login brake or trip it for
  everyone; with a tunnel the plain-http port listens on this computer
  only (`BB_HOST` overrides); every response carries `X-Frame-Options`,
  `nosniff`, a referrer policy, and HSTS on https.
- **`docs/IMPROVEMENTS.md`** lists what a review of the whole system found
  worth building next, in order, so the roadmap is not only in chat.
- **Paces that fit the mailbox.** A personal address (gmail.com,
  outlook.com, yahoo, icloud) is held to Normal's pace; Google Workspace
  and Microsoft 365 on a company domain get a Business pace (20 a minute,
  two at a time, 1,800 or 9,000 a day); a sending service (SES, Brevo,
  Postmark, Mailgun, SendGrid) gets the pace you choose. A new **Custom**
  speed takes your own numbers for an external SMTP: emails per batch,
  seconds between batches, the pause between two emails, and how many go
  at once. The mailbox's own daily ceiling is written as its own setting
  (`channels.email.mailbox_daily_ceiling`) and applied on top of your
  daily maximum, so your number survives a move to another mailbox.
- **Send failures sorted by what they mean.** A refused login (Gmail
  sign-in expired, wrong SMTP password) pauses the pass and leaves every
  row approved and untouched, with the reason on the dashboard; the
  mailbox's daily ceiling pushes the row to tomorrow without counting as
  a retry; a recipient-level 4xx (greylisting, a full mailbox) retries
  only that row; throttling and network errors end the pass. Gmail's 403
  is read for its reason, so a disabled Gmail API or a missing permission
  is a clear final error, not a day of retries. Submission ports 587 and
  2587 always upgrade to TLS.
- **A mailbox that cannot be read holds sending.** An IMAP or Gmail read
  failure used to look like "no new replies" and sending carried on;
  now it is reported (`MailReadError`), the sender waits, and the
  dashboard says why.
- **The writer and the sender no longer race.** While first emails are
  being personalised the campaign is `writing`, invisible to the sender,
  so the segment template can never be sent over a personal draft; an
  interrupted run hands its campaigns to the sender on the next cycle.
  Creating a campaign and marking its people is one transaction. A reply
  pass that runs every 30 s skips the older-mail sweep, which the
  heartbeat still does; a stopping worker finishes the email in flight
  before the send loop is cancelled; the dashboard's skip reason is
  rewritten by the first pass of a new worker.
- **Sending has its own clock.** With a native mailbox the worker now
  runs a sender loop beside the heartbeat: every `send_every_seconds`
  (30 s at Fast and Relay, 2 min at Normal, 5 min at Gentle) it reads
  replies and then drains the outbox, whatever research and writing are
  doing. The heartbeat and the loop share one lock so two drains never
  overlap, every outbox row is claimed atomically before it is sent, and
  rows left mid-send by a dead worker are failed rather than sent twice.
  Sending no longer waits on the AI provider at all: the mailbox needs no
  model, and stop-on-reply is done before any model call. On native
  mailboxes the heartbeat keeps writing while drafts wait to be staged.
- **Quiet hours are optional.** A new profile sends around the clock;
  "Only send during these hours" switches the window on. The window's end
  minute is no longer counted as quiet, the log line says when it ends
  and how to turn it off, and changing the mailbox or the inbox settings
  keeps the choice instead of switching quiet hours back on.
- **Temporary refusals are retried.** A 4xx from the SMTP server (Gmail's
  "try again later", a relay's throttling), a network error, or a 429 /
  403 / 5xx from the Gmail API puts the email back in the queue for ten
  minutes (up to five times) and ends that pass, instead of marking it
  failed and burning through the list. TLS is chosen from what the server
  offers (so ports 25, 2525 and 2587 work), IMAP sockets time out, and a
  sending-only relay (SES, Brevo, Postmark, Mailgun, SendGrid...) is not
  polled for replies.
- **The bounce kill switch needs a real sample.** It now engages after 50
  sends (was 10) and the web app's threshold is 15% (was 5%), so one
  bounce in an uploaded, unverified list no longer pauses everything.
- **The writer keeps up.** Its backlog counts only first emails the
  sender can actually send (not follow-ups scheduled days ahead, not rows
  held for review), it loads at most 2,000 new rows per cycle instead of
  the whole list, a segment written in the last day reuses its sequence
  instead of calling the model again, the spam rewrite in segment mode
  now lands, and creating a campaign cannot be cut in half by the cycle
  timeout. The daily call counter applies to Codex only; API keys are
  bounded by the spend cap.
- **Personal mailboxes are protected.** Fast and Relay are held to
  Normal's pace on Gmail (connected through Google or by SMTP), Outlook,
  Yahoo, iCloud, Zoho and the like, and the profile says so. The
  dashboard's Worker card now says in one line what the sender is doing
  and why it is not sending (quiet hours, a cap, warm-up, approval, held
  mail, a paused provider, a dead worker), with the thing to change.
- **Sending in parallel, and a Relay speed.** The sender now gates every
  due email first and then sends through several lanes at once
  (`channels.email.send_concurrency`), each lane pausing between its own
  sends (`send_jitter_seconds`, the shortest pause a quarter of it). The
  Profile's speeds set both: Gentle and Normal stay one at a time with a
  pause, Fast sends two at a time two seconds apart (1,500 an hour), and
  the new **Relay** speed sends eight at a time, 100 a minute, for an
  SMTP sending service such as Amazon SES, Brevo, Postmark or Mailgun.
  The daily limit and warm-up still apply on top.
- **Research four times faster again.** 24 websites at once (was 6), 200
  businesses per heartbeat (was 40), shorter timeouts, a connection pool
  sized to match.
- **Big uploads move faster.** Website research now reads six sites at
  once, 40 per heartbeat, on its own two-minute clock: slow sites stop the
  research stage for that cycle instead of running the whole heartbeat
  into its timeout and cancelling the writer with it. The writer sorts
  contacts into sectors, writes segment sequences and drafts personal
  first emails four model calls at a time instead of one after another,
  and fetches team pages for missing names five at a time. Research never
  gates writing or sending: the daily limit, speed and warm-up settings
  on the Profile page decide how fast a list goes out.
- **The dashboard says why sending stopped.** The Pipeline card shows
  today's allowance in words: "Warm-up day 1: 20 of 20 sent today. Starts
  at 20 a day and rises by 10 each day", or "120 of 500 sent today (your
  daily limit)", with the switch to flip if you are in a hurry.

### Added

- **`python -m bb update` and `restart`.** `update` pulls the code,
  installs anything new and restarts the site when it runs as a service;
  `restart` restarts the service on the code now on disk. `python -m bb`
  refuses to start a second copy and says whether the running one is on
  older code than the files on disk (`/health` reports the version;
  `status` and `doctor` compare it). Templates render a missing variable
  as empty instead of an Internal Server Error, so a half-applied update
  (new pages on disk, old program running) degrades instead of breaking.
- **The site checks its own public link.** The Users page opens the
  public address from the internet and says whether it works, telling a
  dead quick-tunnel hostname, a tunnel that is not connected (Cloudflare
  1033) and a localhost-only address apart, with "Check again";
  `python -m bb doctor` does the same and warns when two cloudflared
  processes are running (two addresses). (`bb/linkcheck.py`.)
- **`python -m bb doctor`**: is the site ready for other people? It checks
  the public address (https, quick tunnel vs named tunnel), cloudflared,
  admins, the settings file, the session secret, the Google client, the
  data folder and the login/boot service, and says what to fix.
  `install-service` folds the shell's exported settings into `bb.env` so
  the service starts the same way the terminal did, and says plainly
  when a computer has neither launchd nor systemd. The server installer
  installs git when missing, quotes the admin password for compose,
  completes an older `.env`, and the app port is bound to loopback behind
  Caddy. Workers are reaped before they are waited on, so a stop takes a
  second, not thirty.
- **Keep it running.** `python -m bb install-service` writes a launchd
  agent (macOS, with caffeinate so the Mac does not idle to sleep) or a
  systemd user unit (Linux) that starts the site at login or boot and
  restarts it if it stops; `uninstall-service` removes it. The site also
  checks every two minutes for workers that should be running and starts
  them again, and a worker's pid is verified to be a worker before it is
  trusted. `deploy/install.sh` installs the whole thing on a fresh Ubuntu
  server with Docker and Caddy (automatic HTTPS on your domain) in one
  command; `docker compose --profile tls` adds Caddy to an existing
  server install. A mailbox change restarts a running worker, as a
  profile save does, and when the site starts after an update it
  restarts every worker that is still running the previous code.
- **The site runs its own public tunnel.** With `BB_TUNNEL=cloudflare` in
  `bb.env` (and cloudflared installed) the site starts a Cloudflare quick
  tunnel itself, reads the `trycloudflare.com` address, prints "Share
  this link with your users: …" and shows it with a Copy button at the
  top of the admin's Users page, where its status (starting, up, down,
  missing) is visible. Cookies and links follow the public address. The
  tunnel is restarted if it dies and stopped with the site. A quick
  tunnel's address changes on every start, which the page says; a named
  tunnel (`BB_TUNNEL_TOKEN` plus `BB_BASE_URL`) keeps one address.
  (`bb/tunnel.py`.)
- **Site settings in a file, and a startup report.** `python -m bb` reads
  `bb.env` (or `.env`) next to the code, so a restart in a new terminal
  keeps `BB_BASE_URL`, the admin login and the Google client. On start it
  prints the address, where accounts and workspaces live, and who the
  admins are (with the fix when there is none); `python -m bb status`
  prints the same without starting, and `python -m bb make-admin EMAIL`
  gives an existing login the admin role.
- **"What you'd like to happen next"** on the Profile page: a video call,
  a phone call, coffee, meeting in person, a visit or tour, or details by
  email (or nothing, for an email that ends on an easy question). The
  choice becomes the one closing ask of every first email, follow-ups
  repeat it once, and replies steer toward it once someone is interested.
  An optional line of specifics ("Tuesdays after 2pm", "I'm in Lagos the
  week of the 20th") is used where it fits, and for calls the booking
  link goes under the ask. `outreach.ask` / `outreach.ask_detail` in
  harvey.yaml.
- **A readable Recent activity panel.** The dashboard feed is now parsed
  from the worker log into events with a time, the agent and an icon;
  routine checks ("Checking pipeline state", "No new replies", decisions)
  are left out, repeats are folded with a count, and the raw log sits
  behind a toggle. The mail center's feed drops the worker's own
  start/stop chatter and shows times as HH:MM.
- **Mail command center, phase 8 (Microsoft 365, audit log, hardening).**
  A Microsoft 365 mailbox connects through the Graph API with an app
  registration's tenant id, client id and client secret (`Mail.ReadWrite`
  and `Mail.Send` application permissions): backfill and incremental sync
  by received time with Graph's conversation id as the thread, replies
  created with `createReply` so Outlook keeps the conversation, new mail
  sent as a draft so its Message-ID is known and the sent copy is stored
  once. An expired secret shows the Reconnect banner with the credentials
  form open. `/mail/audit` is the audit log: every activity line with
  kind and account filters and paging, plus every email that was sent,
  dry-run, held or discarded with the account, the recipient, the run it
  came from and its confidence. The Jobs page gained "Retry all failed".
  (`bb/mail/providers/graph.py`, `service.connect_graph`.)
- **Mail command center, phase 7 (the command bar).** A plain-language
  bar at the top of `/mail`: "how many need a reply in sales@?", "reply
  to everything for review", "summarise the thread from Ann and draft a
  yes". With Anthropic as the provider the model drives real tools
  (list accounts, counts, threads, a thread summary, a draft, a reply
  run, a send) in a short loop; on any other provider the same actions
  are reached through a one-call-per-turn JSON protocol, and when the
  model cannot follow it the bar says so plainly instead of guessing.
  The bar works on the selected accounts unless the request names
  others. Anything that sends mail (an auto reply run, sending a draft)
  is never done on the model's word: it becomes a pending action showing
  the exact accounts and counts, with Confirm and Cancel, and the bar
  refuses new requests until that is settled. The last exchanges stay on
  the page. (`bb/mail/commands.py`, `MailBrain.tool_turn`.)
- **Mail command center, phase 6 (batch reply runs).** `/mail/runs` lists
  every thread waiting for a reply across the selected accounts, grouped
  by the mailbox that received it, with checkboxes (senders on an
  account's excluded list are shown but unticked). Start a run in one of
  three modes: draft and hold for review, send the confident ones
  (high-confidence drafts that pass the writing check go out, the rest
  wait on Review), or dry run. A line of instructions and a Reply-To can
  apply to the whole run. The worker takes one job per mailbox, so
  accounts proceed side by side while each mailbox sends in order, paced
  20 to 60 seconds apart, within its own daily cap (the cap is checked
  before drafting, so nothing is spent on a reply that cannot go) and
  send window; every reply leaves from the mailbox that received the
  original. The run page shows "Replying 4 of 17", a progress bar and a
  live per-thread status under each account, with Cancel; the overview
  shows runs in progress. An item that fails to draft fails alone and the
  run goes on; a worker that dies mid-draft picks the item up again.
  (`bb/mail/runs.py`, `reply_run` job.)
- **Mail command center, phase 5 (composer, drafting, sending).** On a
  thread page, "Draft a reply" asks the user's own model (the AI provider
  chosen on the site, Anthropic or any OpenAI-compatible endpoint, via
  `bb/mail/brain.py`) for an answer written in the first person from that
  mailbox's persona notes. Every draft passes a deterministic writing
  check (`bb/mail/drafting.py`: boilerplate, spam score, list formatting,
  length; money/legal/commitment topics lower the confidence) and is
  regenerated once with the findings when it fails. The draft lands in an
  editable composer with its confidence and reason; sending is a job that
  refuses an account with no signature or project link, enforces the
  daily cap and send window, can run as a dry run, and always puts the
  greeting on top and the signature plus project link at the bottom
  (`finalize`), whatever the model wrote. Replies go out from the mailbox
  that received the original (SMTP + IMAP append to Sent in limited mode,
  Gmail `messages/send` with the thread id), with an optional Reply-To,
  and the sent message is mirrored into the thread so its state flips to
  *replied*. `/mail/review` lists every draft waiting on a person with
  approve / edit / discard and approve-all; `/mail/compose` writes a
  brand-new email from a short brief. Sent mail is stored once even when
  the next sync sees it again (Message-ID dedup).
- **Mail command center, phases 2 and 3 (sync and reply state).** Each
  mailbox is mirrored into the local database: a backfill (the last 365
  days by default, `backfill_days` per mailbox, 0 for everything) with
  live "Synced 2,340 / 11,200" progress, then an incremental sync every
  90 seconds (Gmail History API; IMAP UID ranges per folder with
  UIDVALIDITY resets; Gmail's All Mail folder when present). Messages
  are stored once per provider id, threaded by provider thread id or
  In-Reply-To / References, and every thread carries a persisted state
  (needs reply, awaiting them, replied, no reply needed, snoozed,
  dismissed) with per-mailbox live counts. Pages: `/mail/threads`
  (filter by state, account, text) and `/mail/threads/<id>` (the full
  timeline, who sent what, snooze / dismiss). Sync now and full re-sync
  per account; a sign-in that dies mid-sync becomes the Reconnect banner.
- **Mail command center, phase 1 (foundation).** A new `bb/mail` package
  and pages under `/mail`: several mailboxes per user with credentials
  sealed by AES-256-GCM (`bb/mail/crypto.py`), a per-user SQLite in WAL
  mode with tables for mailboxes, threads, messages, participants, reply
  runs, drafts, activity, rules and jobs (`bb/mail/db.py`), a SQLite job
  queue with atomic claims, exponential backoff, stale-lock recovery and
  a dead-letter page (`bb/mail/jobs.py`, `/mail/jobs`), a background
  worker process per user that runs jobs for different mailboxes
  concurrently (`bb/mail/worker.py`), Gmail (OAuth) and IMAP/SMTP
  ("limited mode") adapters whose connection checks turn an expired
  sign-in into a Reconnect banner rather than a crash, per-mailbox
  settings with a required signature and project link (sending refuses
  without them), an account scope selector that persists, a plain-English
  activity feed streamed over server-sent events, and a one-time
  migration of the old single mailbox and profile values.

- **Backlog scan for the inbox auto-reply.** `outreach.inbox_backlog_days`
  (0, 7, 30 or 90 on the Inbox page) also answers mail already in the
  inbox from that many days back that nobody replied to, from this
  mailbox by hand or by BB, five a cycle. Providers gained
  `get_unanswered`: Gmail checks each thread for a message from the
  mailbox, IMAP checks the Sent folder's In-Reply-To / References (or
  falls back to unread mail only when no Sent folder opens), Microsoft
  Graph checks Sent Items by conversation.
- **Log in with your email.** The Mailbox page takes an address and a
  password and works out the SMTP and IMAP servers (known providers, else
  the domain's mail records, else smtp./imap. of the domain), tests, and
  saves; `bb/mail_autoconfig.py`.

- **Inbox auto-reply.** With `outreach.inbox_autoreply` on, new mail from
  anyone not on the list is answered in the operator's name following
  `outreach.inbox_instructions`, with the fitting entries from
  `outreach.inbox_links` (label, url, when to share) appended as exact
  links and, optionally, the reply attachments. Automated mail
  (notifications, newsletters, receipts, auto-replies, lists) is skipped,
  each sender is answered at most once in three days, the sender becomes
  an `inbox` contact with a conversation, and the reply goes through the
  outbox like any other (approval, spam check, threading). Web app: the
  Inbox page.

- **Sector versions of your own message.** `outreach.sector_messages`: a
  list of {sectors, subject, body, follow-ups}. Contacts are sorted into
  sectors (one model call per 25, no writing), everyone in a listed sector
  gets that version, and the rest get the default message. One campaign
  per version. Profile → "Use my own message" → "Different versions for
  particular sectors".

- **Spam-risk check on every draft** (`harvey/spamcheck.py`): a
  deterministic score for the things filters react to in cold mail
  (marketing vocabulary, capitals, exclamation marks, links, money,
  fake-reply or marketing subjects, template openers, phishing-short
  bodies). The writer rewrites a flagged draft once with the findings
  spelled out; anything still flagged is queued as *pending review* with
  the reasons on the Outbox item, even on autopilot. The operator's own
  message is never held, only warned about on the profile page.
- **Use my own message.** `outreach.use_template` with
  `template_subject`, `template_body` and two optional follow-ups: no
  model call, one campaign, `{{first_name}}` and the greeting filled per
  person, replies still handled. Profile → "How the emails are written".
- The inquiry brief gained a "staying out of the junk folder" section,
  and web-app inquiry mode no longer adds a `List-Unsubscribe` header (a
  personal enquiry is not bulk mail).

- **No company names in enquiry emails.** Inquiry-mode emails never name
  the recipient's company or domain, nor the sender's own company: the
  sender reads as a prospective client, and the recipient is "you", "your
  team" or what they do. The brief says so, a draft that slips is
  rewritten once, and whatever remains is scrubbed (merge fields
  included). Shared inboxes are greeted "Hello team,".
- **Bounce reasons.** A bounce now records the receiving server's own
  words and what to do about them on the sent item (`error`:
  "Bounced: …"), with hints for no-such-address, authentication (SPF /
  DKIM / DMARC), rate limits, "unsolicited mail", blocked content, full
  mailboxes and unreachable servers. The web app's Outbox has a Bounced
  view and the dashboard's Bounced number links to it.

- **Enquiry emails that read like enquiries.** Inquiry mode now has its
  own brief (`prompts/inquiry_writer.md`) instead of the cold-sales prompt
  plus sales skill files: state the request from `outreach.intent`, one
  natural reference to the recipient at most, one easy question, no
  narration about inboxes or what could be told from the website, no
  hedging ("client or partner, whichever fits"), and sentence-case
  subjects that name the request. Follow-ups are a short nudge and a
  courteous close. The web app requires the "what you want" sentence in
  inquiry mode.
- **Real company names.** A row with no company gets a readable name from
  its domain (north-west-build.co.uk → North West Build), and the profile
  stage replaces a bare-domain name with what the site calls itself
  (og:site_name or the title) plus its meta description, on the company
  and its prospects. Merge fields and prompts never show a domain.
- **Role tiers.** A shared inbox (info@, office@) is always the admin tier;
  a personal address with no title is never admin, so nobody is addressed
  as a mailroom.

- **Anthropic user-scoped keys.** A key that is not tied to a workspace
  (`sk-ant-usr-…`) needs the workspace named on every request:
  `ANTHROPIC_WORKSPACE_ID` in `.env`, or the Workspace ID box on the web
  app's AI provider page. The key check explains this, and says what
  Anthropic said (no credit, a sign-in token, anything else) instead of
  the exception class.

- **Dashboard controls.** *Pause for edits* stops the worker and shows a
  Resume button (nothing revives it meanwhile); saving the profile or AI
  provider restarts a running worker so the change applies at once; *Start
  over* clears recipients, emails, replies and counters while keeping the
  profile, mailbox, AI provider, attachments and signature.
- **Free signals on by default in the web app.** Uploading recipients
  confirms every signal whose cost note starts with "free", so each
  company's website is read before its email is written. The CLI's
  "confirm signals first" step is unchanged.

- **Names and greetings.** Every email and reply opens with a greeting
  line that fits what is known: "Hi Jane," / "Hello Acme team," for a
  shared inbox such as info@ / "Hi there," when nobody's name is known; the
  word is `outreach.greeting` (Hi, Hello, Hey, Dear, Good day). Names the
  upload left blank are worked out before writing (`harvey/names.py`): from
  the LinkedIn URL, from a first.last address, or from the company's team
  page matched against the address (one fetch per domain, 15 a cycle). A
  name is never guessed; `{{first_name}}` is for the greeting only.
- **A clearer stop.** The worker's last log line says why it stopped (the
  dashboard's Stop button, a settings change, Ctrl+C, the terminal
  closing) instead of a joke about needing a break, the web app writes
  who stopped a worker into its log, and a worker that dies on its own is
  started again the next time its dashboard is opened.

- **Microsoft 365 admin-app provider** (`channels.email.provider:
  microsoft_app`). Client-credentials sign-in with `M365_TENANT_ID`,
  `M365_CLIENT_ID`, `M365_CLIENT_SECRET` and `M365_MAILBOX`; sends and
  reads as that one mailbox under `/users/<mailbox>` on Microsoft Graph.
  Both Microsoft providers now send file attachments and an HTML body.
  The web app's Mailbox page has it as Option C with the Entra steps.
- **Email signature.** `outreach.signature_html` (links, line breaks) with
  an optional `outreach.signature_image` embedded inline by `cid:` and
  placed with `{{logo}}`; plain-text mail gets a text rendering. Mail
  stays plain text until a signature is set, then goes out as
  multipart/alternative with the plain part alongside. Web app: Profile →
  Email signature and Signature image.
- **Engaging writing style.** `outreach.style: engaging` (the web app's
  default) briefs the writer for a 90–130-word, personal, easy-to-answer
  first email instead of the sub-75-word classic (`concise`).
- **Speed and warm-up.** `channels.email.sends_per_cycle` (default 8)
  replaces the fixed per-cycle size, and `channels.email.warmup_ramp`
  ramps a mailbox from 20 a day up by 10 a day from its first send. The
  web app's profile offers Gentle / Normal / Fast (1-, 2- or 5-minute
  cycles) instead of the 15-minute heartbeat.
- **Deliverability.** A `List-Unsubscribe` header on every outgoing email
  (`channels.email.list_unsubscribe`), and a DNS check of the sending
  domain's SPF, DKIM, DMARC and MX records on the web app's Mailbox page
  with the exact record to add for each failure.

- **Sectors, role tiers, and one email per segment.** Before writing, the
  writer sorts unsorted contacts (in batches of 25, one model call each)
  into a fixed sector vocabulary (construction, financial_advisory,
  banking, hr_recruiting, …) and a role tier (executive, manager,
  individual, admin), recorded on the prospect and the company. Campaigns
  are one sector × one tier, and the sequence prompt says how the position
  changes the message (an owner gets a short ask for a decision; a shared
  inbox gets a two-line request to pass it on). With
  `outreach.personalize_each: false` (the web app's default) one sequence
  is written per segment and sent to everyone in it with merge fields; the
  per-person first email remains the default for the CLI. The Recipients
  page shows the sector × role breakdown.
- **SMTP Reply-To.** `SMTP_REPLY_TO` (the Mailbox page's Reply-To field)
  sets the header on outgoing SMTP mail.
- **`brain.provider: openai` — any OpenAI-compatible endpoint.** Set
  `brain.base_url` and `brain.model` and put the key in `LLM_API_KEY` (none
  needed for a local server such as Ollama). Works with OpenAI, Google
  Gemini, Groq, OpenRouter, Together and others; calls are counted in a new
  `openai_calls` column. The web app's "AI provider" page offers these as
  presets, several with free tiers, so a hosted install can cost its users
  nothing. Daily sending can be unlimited (`max_daily_sends: 0`), the web
  profile no longer asks for a target audience or a spend cap, and the
  Mailbox page leads with SMTP when one-click Gmail isn't configured.
- **Inquiry mode (`outreach.mode: inquiry`).** The sender is the prospective
  client or partner: each email is written from what the recipient's
  business does and asks for that (a financial advisor about taking you on,
  a builder about a job, a studio about working together), with
  `outreach.intent` saying what you want in your words. The default `offer`
  mode is unchanged.
- **Auto-replies follow your instructions and carry your files.**
  `outreach.reply_instructions` is injected into every reply the handler
  drafts; `outreach.reply_attachments` (file names under
  `HARVEY_HOME/attachments`) are attached to the first reply in each
  conversation, every reply, or never (`outreach.attach_on`). The outbox
  gained `attachments_json`; Gmail and SMTP providers attach files
  (Microsoft 365 logs and sends without, for now).
- **The writer drafts about two days ahead of the sender**
  (`2 × max_daily_sends` queued), so a 10,000-row list is worked through at
  the daily limit instead of being written at once.
- **Web app: users bring their own Anthropic key** (verified against the
  API, stored in their workspace, billed to them), connect **any mailbox
  over its own SMTP/IMAP** as an alternative to Gmail OAuth, set what they
  want from recipients and how replies are answered, and upload
  attachments for replies. A Mac quick-start and tunnel instructions were
  added to docs/HOSTING.md.
- **BB, a hosted multi-user web app (`bb/`).** `python -m bb` (the Docker
  image's default) serves a site where an admin creates logins (no
  self-registration) and each user gets an isolated Harvey: their own
  profile form that writes `harvey.yaml`, Gmail connected through the web
  OAuth redirect flow, CSV recipient upload (up to 10,000 rows, stored as
  `email_status: provided`), an outbox to approve or reject emails, a
  replies page, pause/resume, and a worker process started with
  `HARVEY_HOME` pointing at their workspace. A banner on every page shows
  replies, sent, not yet sent, bounced and total recipients. Server settings:
  `BB_BASE_URL`, `BB_ADMIN_EMAIL`/`BB_ADMIN_PASSWORD`, `BB_SECRET_KEY`
  (generated if blank), `BB_HOME`. See docs/HOSTING.md.
- **`HARVEY_HOME`.** Harvey's instance files (harvey.yaml, .env, data/,
  per-home skills/) can live outside the checkout; the default is unchanged.
  Prompts and shipped skills stay with the code; a `skills/` file in the
  home overrides the shipped one.
- **`icp.auto_prospect`** (default true). Off, the heartbeat never goes
  looking for new businesses and only writes to the prospects it has.
- **`email_status: provided`** for addresses the operator supplied. The
  gate, writer and sender treat it as deliverable.

### Changed

- **The Claude brain calls the Anthropic API directly.** `brain.provider:
  claude` (the default) no longer shells out to the Claude Code CLI. It uses
  the official `anthropic` SDK: one `messages.create` per prompt with no tools,
  a fixed system prompt that marks quoted content as data, bounded retries
  with backoff on 429/5xx/network errors, and the server-side refusal
  fallback (`brain.fallbacks`, default on) on supported models. Set
  `ANTHROPIC_API_KEY` in `.env`; the model defaults to `claude-opus-5-5`
  (`brain.model` to change it, `brain.reasoning_effort` low…max for depth).
  Every call records exact tokens plus an estimated cost in `usage_events`,
  and the new `usage.max_daily_cost_usd` pauses Harvey for the day once the
  estimate reaches it. A rejected key or unknown model pauses the brain with
  the reason in the log, `harvey status --check` and the dashboard; an
  account out of credit pauses it for an hour. The Claude Code OAuth quota
  gauge (`integrations/quota.py`) is gone with the CLI; the dashboard's
  Usage tab and `harvey usage` show estimated spend instead, and the
  breakdown tables gained a cost column. Docker no longer installs the CLI
  or mounts `~/.claude`. `usage.max_daily_claude_percent` now applies to
  Codex only. Codex is unchanged.

### Added

- **Microsoft 365 mail provider.** Set `channels.email.provider: microsoft` to
  send and read through a Microsoft 365 / Outlook mailbox via Microsoft Graph,
  for domains whose mail lives in Exchange Online (which no longer accepts
  passwords over IMAP). Signs in once with OAuth + PKCE (`harvey microsoft
  auth`; no client secret, no stored password), drafts then sends so the
  Message-ID is known, threads replies with `createReply`, reads the last 7
  days of the inbox without marking anything read, refuses to act as any
  mailbox other than `persona.email`, and holds sending when the inbox can't
  be read. See [docs/microsoft365.md](docs/microsoft365.md).
- **Optional per-recipient compliance check (Precleared).** Set
  `compliance.precleared.enabled: true` with `sender_country: GB` or `US` to
  have each new recipient checked before the first cold email against UK
  PECR, US CAN-SPAM, Canada's CASL, Germany's UWG and France's rules. Any
  verdict but `ok_to_email` fails that outbox item with the rule and the next
  step. If the check can't be made (daily allowance used up, network down, a
  malformed response), cold emails wait for a later cycle while replies still
  go out. One check per address, cached for 30 days; changing the sender
  country, address source or product description triggers a fresh check. The
  receipt id is logged with each send. Native providers only. Off by default.

## [0.3.0] — 2026-10-13

### Added

- **Opt-in Codex brain (ChatGPT plan).** Set `brain.provider: codex` (or pick
  it in `harvey setup`, or `harvey train --provider codex`) to think with the
  Codex CLI on a ChatGPT subscription instead of Claude. Claude remains the
  default and there is no automatic fallback between providers. Each call
  runs a throwaway, isolated `codex app-server` with no tools; Harvey verifies
  the lockdown from Codex's own reports before sending any prompt, aborts on
  any attempted tool use, refuses API-key logins and custom endpoints
  (checked before your login is exposed to Codex), and never changes your own
  Codex settings or login: each call gets a private copy of the login without
  its refresh token, so an expired login must be renewed with Codex itself or
  `codex login`. Requires Codex CLI 0.159.3; other versions are refused. See
  the README section "Using a ChatGPT plan instead (Codex)".
- `brain.model` picks the model for either provider, for Harvey only. For
  Codex use `gpt-5.5` (suggested by `harvey setup`): the verified text-only
  model. Blank auto-selection only picks listed models without code-execution
  tools, and on current ChatGPT accounts there are none; code-mode models are
  refused.
- If Codex can't be used (expired or renewal-due login, unsupported CLI,
  unsafe Codex config, rejected model, plan usage limit), Harvey pauses the
  heartbeat instead of running agents that would fail: replies stay unread
  and unescalated, queued emails wait, and the reason shows in the log,
  `harvey status` and the dashboard. It resumes on its own once the cause is
  fixed. `harvey status --check` runs the same check (no prompt sent) on demand
  and exits non-zero when it fails. The Codex provider supports macOS and
  Linux; it is refused on Windows (use Claude there).

### Changed

- Each heartbeat now sends email after the agents that read and answer
  replies have finished, and native mail providers (Gmail/SMTP) read the
  mailbox every cycle, so a reply always cancels its queued follow-ups before
  that cycle's sends (previously the two could run at the same time, and a
  reply arriving with no open conversation waited for a later cycle). This
  applies to Claude and Codex alike.
- A reply is only recorded once its reply draft (if it needs one) is
  written, so a brain outage part-way never half-records it. If a working
  brain returns no usable draft, the conversation is flagged for a human
  instead of being left open with no answer.
- Usage is tracked per provider. The budget gate, `harvey usage`, `harvey
  status` and the dashboard read and label the active provider's own quota;
  existing usage history is kept and attributed to Claude.

### Fixed

- Dashboard HTML and text assets now use UTF-8 explicitly, fixing Windows
  decoding failures. Regression tests cover HTML, CSS, JavaScript and SVG.

## [0.2.1] — 2026-10-13

A security release. Update if you run Harvey.

### Security

- **Claude now runs with no tools.** Every Claude call used to run with
  `--dangerously-skip-permissions`, and prompts include text Harvey doesn't
  control: prospect email replies and scraped web pages. Instructions hidden
  in that text could make Claude run commands, read or write files, or use
  the operator's connected MCP servers. Calls now run with `--tools ""` and
  `--strict-mcp-config`; Harvey only ever needed text back, so nothing else
  changes. Reported privately by [@hackchang](https://github.com/hackchang).
  If you ran an earlier version with live replies coming in, check the
  account that ran Harvey for unexpected files or activity.

### Fixed

- `harvey run` no longer drops into the setup wizard when the trained config
  lives in `harvey.local.yaml`; the dashboard reads it too.
- A heartbeat cycle is capped at 10 minutes, so one hung agent can't freeze
  Harvey, and quiet hours are re-checked every 5 minutes so a Mac waking from
  sleep resumes on time.
- Sites that only answer on plain HTTP or `www.` are no longer marked
  unreachable.
- Profile and contact backfills rotate through every company instead of
  retrying the same few forever.
- The trainer writes full place names ("Solon, Ohio") and real job titles, so
  discovery doesn't geocode to the wrong city and found contacts aren't
  silently discarded.

### Added

- Contacts are backfilled for companies found without any, from their own team
  pages and, if a key is set, Hunter's named-people search.
- `HUNTER_MONTHLY_SEARCHES` (default 25) sets one Hunter budget shared by every
  domain search.

Thanks to [@sshaw0729-dotcom](https://github.com/sshaw0729-dotcom) for the
fixes above (#2).

## [0.2.0] — 2026-09-13

The release that made Harvey worth running. The previous version could write
and send email but could not reliably find anyone to send it to, and it needed
a paid third-party platform to send at all. Both are fixed.

### Added

- **A prospecting pipeline that works.** Four stages, each independently
  runnable and re-runnable: DISCOVER (who exists) → PROFILE (what they are) →
  ENRICH (who decides) → VERIFY (can you reach them).
- **DISCOVER, with a provider menu.** Four sources behind one adapter
  interface, shown side by side in the dashboard with what each does, what it
  costs, its free tier and which keys it needs. The default — OpenStreetMap —
  needs no account, no card and no key, so the whole pipeline can be run
  before paying anyone. `harvey discover`.
- **Cost discipline in the collector.** Every discovery run estimates before it
  spends, refuses to start over your cap, re-checks the cap between batches,
  reads a kill switch, and flushes observations per batch so a run that dies
  keeps what it learned. `harvey discover --estimate` prints the projection and
  exits.
- **PROFILE.** Free — three HTTP requests per business, no browser, no model
  call. Reads the incumbent agency out of the site footer, ad pixels, missing
  schema, abandoned blogs, open roles and named people. Chained onto DISCOVER
  and run on every heartbeat. `harvey profile`.
- **A signal catalog you confirm.** Harvey knows how to collect 23 signals and
  collects none of them until a human says so. Each shows a plain-language
  description, what it costs, and how many companies already carry it.
  `harvey signals`.
- **The cohort builder.** Pick what a prospect must have and what disqualifies
  them; see the matching companies live. A prospect list is a query you can
  explain, not a file.
- **Native mail providers.** Gmail (REST + OAuth) and any SMTP+IMAP mailbox.
- **The outbox approval ladder.** Every outgoing email waits for approval by
  default (`pending_review → approved → sent`), reviewed one at a time in a
  decisions desk with `A`/`R`/`J`/`K`. A deterministic pre-send gate — no model
  involved — rejects unrendered merge tags, banned phrases, over-length bodies,
  too many links, HTML, undeliverable addresses and recipient mismatches.
- **Bounce handling and a kill switch.** A bounce marks the address invalid,
  cancels that prospect's queued sends, and trips a global pause past a
  configured bounce rate. `harvey sending pause|resume`.
- **Claude usage tracking.** Per-call attribution by agent, task and model, plus
  live subscription-quota gauges read the same way `/usage` does, so Harvey
  throttles itself and leaves headroom for your own interactive work.
  `harvey usage`.
- **CSV export.** `harvey export` writes a sequencer-ready list. Harvey is worth
  running as a list-builder even if you never let it send.
- **A dashboard worth looking at.** Opens on Today — what needs a human — with
  an activity feed and a rail of pipeline figures, collector runs and setup.
  Light and dark are the same design re-tokenised; appearance follows your OS by
  default. Fonts are vendored so it renders with the network off.
- **A Python version guard.** `python3 -m venv` on macOS builds from the system
  3.9, which cannot run Harvey. It now fails immediately with the fix instead of
  surfacing later as an unrelated-looking ImportError.

### Changed

- **The data model.** Every fact Harvey learns is now an observation — a row in
  `observations`, never a column. A new signal needs no migration, re-observing
  over time is a free time series, and confidence and provenance travel with the
  fact. The vocabulary is governed: a database trigger rejects any signal code
  not in `signal_codes`.
- **Email finding is pattern-first.** Harvey derives the domain's address format
  and verifies one candidate rather than brute-forcing name variations, and
  grades every address honestly as `verified` / `risky` / `guess` / `invalid`.
  Only deliverable addresses are ever sent to.
- **The default mail provider** is now `gmail`. Instantly is still supported as
  a legacy option.
- **Discovery depth defaults to 30, not 100.** Google removed
  100-results-per-page in September 2025, so depth 100 is now billed as ten
  pages nearly everywhere — and below rank 30 it is mostly directories.
- **The dashboard's HTML, CSS and JS** are real files in `harvey/web/` instead
  of a 1,500-line string literal in `dashboard.py`. Still no build step.

### Fixed

- **Guessed emails were being reported as verified** whenever the domain merely
  had an MX record. The migration downgrades every legacy `email_verified=1` row
  to `guess`.
- **Cohorts counted the wrong companies.** PROFILE records a false boolean when
  it checks and finds nothing — which is correct — but `cohort()` matched on the
  row existing, so "companies running Google Ads" silently meant "companies we
  checked for Google Ads", which was all of them. Cohorts now read the newest
  observation per company and signal, and require it to be true.
- **Usage tracking counted every Claude session on the machine**, not just
  Harvey's. The transcript reconciler is gone; the ledger contains only rows
  Harvey's own Brain wrote.
- **Page titles were being extracted as people** ("Request Service", "Commercial
  Services"), and titles bled between team members.
- **Confirming a signal before anything had loaded the catalog** reported success
  while changing nothing.
- **The free OSM provider answered unmapped search terms with every registered
  office in the area** — coffee roasters and churches for a roofing query. It now
  refuses the query and names the trades it can search.

### Security

- Test fixtures no longer carry real businesses' phone numbers or street
  addresses.
- `harvey.local.yaml` (gitignored) overrides the tracked `harvey.yaml`, so a
  fork can hold real product configuration without it reaching a public repo.

## [0.1.0]

Initial release: the heartbeat loop, five sub-agents, the skills library, the
website trainer, Instantly-based sending, and the first dashboard.
