# Hosting BB for your users

BB is the web app around Harvey. You run one copy on a server; you create a
login for each person; each of them connects their own Gmail, describes who
they are and what they're reaching out about, uploads the people they want
to email, and starts their worker. Everything a user does lives in their own
workspace (`BB_HOME/workspaces/<user id>`): their config, database, Gmail
token and log. Workers are separate processes, one per user, started with
`HARVEY_HOME` pointing at that workspace.

## Run it on your Mac first (ten minutes)

You can run the whole thing on a Mac and use it from Safari before putting
it on a server. Open **Terminal** (⌘-space, type Terminal).

```bash
# 1. Python 3.11+ (macOS ships an old one). Homebrew makes this one line:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"   # skip if you have brew
brew install python@3.13

# 2. Get the code and install it
git clone https://github.com/alhameenoki1-prog/BB.git && cd BB
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .

# 3. Settings for the site, in a file so a restart never forgets them
#    (your login; users add their own AI key and mailbox later)
printf 'BB_BASE_URL=http://localhost:8000\nBB_ADMIN_EMAIL=you@example.com\nBB_ADMIN_PASSWORD=choose-a-long-password\n' > bb.env

# 4. Start it
python -m bb
```

On start it prints the address, where the data lives, and who the admins
are. `python -m bb status` prints the same without starting the server.
To update later, run `python -m bb update` in the same folder: it pulls
the code, installs anything new, and restarts the site when it runs as a
service (otherwise it tells you to press Ctrl+C and start it again).
Never just `git pull` under a running site: the pages on disk would be
newer than the program showing them. `python -m bb status` says when
that has happened, and `python -m bb restart` fixes it. Workers that are
still running the previous code are restarted on the new one.

Open <http://localhost:8000>, sign in with the admin email and password,
go to **Users** and create a login for each person.

If the site says "wrong email or password" on a fresh install, the two
`export` lines were not in effect when the server first started (the admin
account is only created on a first start with them set). Create the login
directly instead; this works whether or not the server is running:

```bash
python -m bb create-user you@example.com --admin     # asks for a password
python -m bb reset-password you@example.com          # change it later
python -m bb list-users
``` Everything is stored
under `BB/data/bb/` on your Mac. `Ctrl-C` stops it; `source
.venv/bin/activate && python -m bb` starts it again, reading `bb.env`.
Keep using the same `BB` folder: a fresh `git clone` elsewhere is a fresh
site with no accounts.

**Gmail one-click sign-in on a Mac:** Google only redirects to `localhost`
or to a real https domain, so for local use register
`http://localhost:8000/gmail/callback` on your Google OAuth client and
export `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` before step 4. Or skip
Google entirely: users can connect any mailbox with its own SMTP/IMAP
settings on the **Mailbox** page (Gmail works there with an app password).

**Letting other people use it from your Mac.** The Mac has to stay on and
awake (`caffeinate -i python -m bb` keeps it awake while running). The
site can open a public https address itself:

```bash
brew install cloudflared
echo 'BB_TUNNEL=cloudflare' >> bb.env
python -m bb
```

On start it prints `Share this link with your users: https://....trycloudflare.com`,
and the same link sits at the top of the admin's **Users** page with a Copy
button. The site keeps the tunnel running and restarts it if it dies.
**A free quick tunnel gets a new address every time the site starts**, so
after a restart send the new link (the Users page always shows the
current one). For an address that never changes, create a named tunnel on
your own domain in the Cloudflare dashboard (Zero Trust → Networks →
Tunnels, route your hostname to `http://localhost:8000`), then put its
token and address in `bb.env`:

```
BB_TUNNEL_TOKEN=eyJh...
BB_BASE_URL=https://outreach.yourdomain.com
```

Or move to a server as below.

**Keeping it running on the Mac.** `python -m bb` stops when the Terminal
window closes or the Mac restarts. Install it as a login service once and
it starts by itself, restarts if it crashes, and keeps the Mac from
idling to sleep while it runs:

```bash
python -m bb install-service      # python -m bb uninstall-service to undo
```

The site then reads `bb.env` on every start (including `BB_TUNNEL`), and
the log is at `data/bb/site.log`. Keep the lid open or set "prevent
automatic sleeping" in System Settings → Energy; a closed laptop still
sleeps. A Mac that stays on is a server with a tunnel; for something that
does not depend on a laptop at all, use a small server as below.

## Hosting on a server

The short way, on a fresh Ubuntu server (any provider: Hetzner, DigitalOcean,
Linode, a Lightsail box) with a domain name already pointing at its IP:

```bash
curl -fsSL https://raw.githubusercontent.com/alhameenoki1-prog/BB/main/deploy/install.sh | sudo bash -s -- \
    outreach.example.com you@example.com 'a-long-password'
```

That installs Docker, fetches the code into `/opt/bb`, writes `.env`, and
starts the site behind Caddy with a real certificate. It survives reboots
(`restart: unless-stopped`), the address never changes, and nothing
depends on your laptop. Update with `cd /opt/bb && git pull && docker
compose --profile tls up -d --build`. The long way follows.

### What you need

- A Linux server (any VPS with 2 GB RAM is plenty to start) with Docker and
  a domain name pointing at it, e.g. `outreach.example.com`.
- A **Google Cloud OAuth client** so users can connect Gmail in one click
  (below). Optional: users can always connect a mailbox with its own
  SMTP/IMAP settings instead.
- Nothing from an AI vendor: **each user picks their own AI provider** on
  the **AI provider** page (free tiers from Google Gemini or Groq, or a paid
  Anthropic/OpenAI account) and pastes their own key. It is checked when
  saved and stored only in their workspace; the site is never billed.

## 1. Google OAuth client

1. Google Cloud Console → a project → *APIs & Services* → enable the **Gmail API**.
2. *OAuth consent screen*: External, fill in the app name, support email and
   your domain. Add the scope `https://www.googleapis.com/auth/gmail.modify`.
3. *Credentials → Create credentials → OAuth client ID*, type **Web application**.
   Authorised redirect URI: `https://outreach.example.com/gmail/callback`
   (exactly `BB_BASE_URL` + `/gmail/callback`).
4. Copy the client ID and secret into `.env` as `GMAIL_CLIENT_ID` and
   `GMAIL_CLIENT_SECRET`.

While the consent screen is in **Testing**, only Google accounts you list as
test users (up to 100) can connect, and their tokens expire after 7 days.
Gmail's send/read scope is a *restricted* scope: to let anyone connect, you
submit the app for Google's verification, which includes a third-party
security assessment (the CASA process). Start that early; it takes weeks.

## 2. Configure

```bash
git clone <this repo> bb && cd bb
cp .env.example .env
$EDITOR .env
```

Set at least:

```
BB_BASE_URL=https://outreach.example.com
BB_ADMIN_EMAIL=you@example.com
BB_ADMIN_PASSWORD=a-long-password-you-will-change
GMAIL_CLIENT_ID=...apps.googleusercontent.com     # optional: one-click Gmail
GMAIL_CLIENT_SECRET=...
```

Optional: `REOON_API_KEY` / `ZEROBOUNCE_API_KEY` (email verification for
workers that prospect on their own), `BB_DEFAULT_DAILY_COST_USD`,
`BB_DEFAULT_MODEL`. `ANTHROPIC_API_KEY` is not used by the web app's
workers; users bring their own provider and key.

## 3. Run

```bash
mkdir -p data && sudo chown -R 1000:1000 data   # the container runs as uid 1000
docker compose up -d --build
```

The app listens on port 8000. Put a TLS-terminating reverse proxy in front
of it so users get https. With [Caddy](https://caddyserver.com) that is a
two-line `Caddyfile`:

```
outreach.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy fetches and renews the certificate itself. nginx or Traefik work the
same way; forward `Host` and `X-Forwarded-*` headers.

Sign in at `https://outreach.example.com` with `BB_ADMIN_EMAIL` /
`BB_ADMIN_PASSWORD`, then go to **Users** (the Admin section at the bottom
of the left menu, or `/admin/users`) and create a login for each person.
Only accounts with the admin role see that section. The admin account is
created once, on the very first start, from those two variables; changing
them later does nothing. If you are signed in as a plain user, or the
first start ran without them, give your login the role from the terminal:

```bash
python -m bb make-admin you@example.com      # or: python -m bb create-user you@example.com --admin
```
There is no self-registration. Tell them their email and password; you can
reset it from the same page.

## What a user does

1. **AI provider**: pick one and paste their own key. Google Gemini and
   Groq have free tiers (so this can cost them nothing), OpenRouter has
   free models, Ollama runs free on the hosting machine, and Anthropic or
   OpenAI are paid to the user's own account. The key is verified against
   the provider and kept in their workspace.
2. **Profile**: what they want from the people they write to (*inquiry*
   mode: hire them, buy from them, partner with them; or *offer* mode),
   name, company, role, LinkedIn, how many emails a day, sending hours,
   whether to approve each email first (off = fully automatic), how
   replies should be answered, and documents to attach to replies.
3. **Mailbox**: Gmail in one click, or any mailbox's own SMTP/IMAP settings
   (Gmail app password, Microsoft 365 with SMTP AUTH, Zoho, Fastmail, a web
   host's mail). Emails are sent from that address and replies are read
   from its inbox; nobody else's address is ever used.
4. **Recipients**: upload a CSV (up to 10,000 rows) with `email` and, ideally,
   `name`, `company`, `title`, `linkedin`, `website`, `notes`.
5. **Start the worker**. Every cycle (1, 2 or 5 minutes, per the speed on the profile) it reads each new recipient's
   company website, writes a one-to-one email from what that business does
   and what the user wants from them, sends what is due inside the daily
   limit and hours, reads replies, and answers them following the user's
   instructions, attaching their documents.
6. The banner on every page shows replies, sent, not-yet-sent, bounced and
   total recipients, refreshed every 30 seconds. **Replies** lists everyone
   who answered; **Outbox** holds emails awaiting approval.

## Anthropic keys

Two kinds of key come out of console.anthropic.com. A key created inside a
workspace starts with `sk-ant-api` and works on its own. A user key that
is not tied to a workspace starts with `sk-ant-usr-` and Anthropic refuses
it with "This API key is not scoped to a workspace" until the workspace
is named: paste the ID from Console → Settings → Workspaces into the
Workspace ID box on the AI provider page (or set `ANTHROPIC_WORKSPACE_ID`
for the CLI). Either kind also needs prepaid credit on the account
(Console → Settings → Billing); without it Anthropic answers "credit
balance is too low" and the page says so.

## Connecting a mailbox (what to tell your users)

The Mailbox page has these instructions built in, with buttons that fill
in the servers. In short:

**Gmail or Google Workspace.** Turn on 2-Step Verification at
myaccount.google.com/security, create an app password at
myaccount.google.com/apppasswords, then on the Mailbox page click
*Gmail / Google Workspace*, enter the full Gmail address as the username
and the 16-character app password as the password, and *Test and save*.
Replies arrive in the same inbox and the worker reads and answers them.
Servers: `smtp.gmail.com:587`, `imap.gmail.com:993`.

**Outlook / Microsoft 365 over SMTP.** (If you administer the tenant, use
Option C below instead.) Sending works for work or school accounts
once the admin allows *Authenticated SMTP* on the mailbox (Microsoft 365
admin center → Users → user → Mail → Manage email apps). Microsoft no
longer allows password sign-in over IMAP, so the worker cannot read a
Microsoft inbox: set *Reply-To* to a Gmail (or other IMAP) mailbox and put
that mailbox's IMAP server, username and app password in the IMAP fields.
Personal outlook.com and hotmail.com accounts cannot be used at all since
Microsoft removed password sign-in for mail apps in 2024. Servers:
`smtp.office365.com:587`, `outlook.office365.com:993`.

**Microsoft 365 admin app (Option C, high volume).** If you administer a
Microsoft 365 tenant, skip SMTP altogether: register an app in Microsoft
Entra and the worker sends and reads mail as one mailbox through Microsoft
Graph, with no app passwords, no IMAP and no SMTP AUTH toggle. Exchange
Online allows each mailbox 10,000 recipients a day and 30 messages a
minute, so this is the connection to use with the *Fast* speed. Steps
(the Mailbox page repeats them):

1. entra.microsoft.com → Applications → App registrations → New
   registration. Any name, *Accounts in this organizational directory
   only*, no redirect URI.
2. Overview → copy the *Application (client) ID* and *Directory (tenant)
   ID*.
3. API permissions → Add a permission → Microsoft Graph → *Application
   permissions* → `Mail.Send` and `Mail.ReadWrite` → *Grant admin consent*.
4. Certificates & secrets → New client secret → copy the *Value* (shown
   once; it expires, so note the date).
5. On the Mailbox page, Option C: paste the three IDs and the mailbox to
   send from (a licensed user or shared mailbox in that tenant), *Test and
   save*. The user's sending address becomes that mailbox.
6. Recommended: an [application access
   policy](https://learn.microsoft.com/graph/auth-limit-mailbox-access)
   so the app can reach only that one mailbox.

Personal outlook.com accounts have no tenant and cannot use this.

**Yahoo, iCloud, Zoho, Fastmail.** Each needs an app password from its
security settings; the page's buttons fill in the servers. A mailbox from a
web host is usually `mail.yourdomain.com` on ports 587/465 (SMTP) and 993
(IMAP).

## What makes the emails good or bad

Every draft is scored the way a spam filter would score it (marketing
words, capitals, exclamation marks, links, money, "Re:" subjects,
clichéd openers, very short bodies). A flagged draft is rewritten once;
if it still fails it waits in the Outbox marked "Held for your review"
with the reasons, even when approval is off. Prefer plain, specific words
about the actual job. If you would rather write the email yourself,
choose "Use my own message" on the profile: everyone gets your text with
their first name in the greeting, no AI writing, replies still handled;
the page warns if your text would trip filters but sends it as you wrote it.
Under that message you can add versions for particular sectors (a builders'
version, a financial advisers' version); contacts are sorted by what their
company does and get the matching version, everyone else the default.

Every enquiry email is built around the "what you want from them" sentence
on the profile. Vague in, vague out: "I want to work with you" produces
emails that hedge; "quotes for a 40 sqm kitchen extension in Lekki, work
to start in March" produces a request a builder can answer. The worker
also reads each company's website before writing (name, what they say
they do), so upload rows with a website or a company email address rather
than a personal one where you can. The brief the writer follows is
`prompts/inquiry_writer.md`; edit it to change the voice for everyone.

## The mail command center (`/mail`)

A second, newer side of the app: connect several mailboxes, mirror them,
see what needs a reply, and reply in batches or from a command bar. It is
being built in phases; sync, reply state, the composer and sending are in.

- **Accounts** (`/mail/accounts`): connect with Google (needs the site's
  OAuth client; while it is in testing mode Google ends the sign-in after
  about a week and the account shows a Reconnect banner) or with an email
  address and app password ("limited mode": polled, not pushed).
  Microsoft 365 mailboxes connect through the Graph API with an app
  registration (tenant id, client id, client secret; the app needs the
  Mail.ReadWrite and Mail.Send *application* permissions with admin
  consent). Passwords, tokens and secrets are stored encrypted. Each
  account has its own
  settings: display name, signature, **project link** (both required
  before anything is sent from it), default Reply-To, persona notes,
  auto-send, daily cap, send window, excluded senders, colour.
- **Scope**: the checkboxes at the top of every mail page choose which
  accounts the views work on; the choice is remembered.
- **Worker**: one background process per user runs sync, drafting and
  sending as jobs with retries. A job that fails every attempt waits on
  `/mail/jobs` to be retried or discarded. The worker is started when the
  first account connects, restarts with the site, and is revived if it dies.
- **Activity**: the feed on `/mail` is plain sentences, updated live.
- **Sync**: a connected mailbox is backfilled (the last year by default)
  with progress shown on the overview, then kept current every 90
  seconds; all accounts sync side by side. Nothing is stored twice.
- **Threads** (`/mail/threads`): every conversation with its state, which
  the app computes from who wrote last: *needs reply* (they did), *awaiting
  them* (you did, in a two-way thread), *replied* (you did and nobody has
  written back), *no reply needed* (automated or bulk mail). Open one for
  the full timeline; snooze or dismiss it by hand.
- **Replying**: on a thread, "Draft a reply" (optionally with a line of
  instructions) has the model write an answer in the first person using
  that account's persona notes. The draft shows its confidence and why,
  and the result of the writing check (no boilerplate, no spam patterns,
  no lists, long enough to be an answer); a draft that fails is rewritten
  once with the findings. Edit it, pick a Reply-To, tick "dry run" to see
  exactly what would go out, then Send. The greeting, the signature and
  the project link are added at send time, so they are on every email
  whatever the model wrote; an account with no signature or project link
  cannot send. The daily cap and send window per account are enforced at
  send time too. The reply leaves from the mailbox that received the
  original and is mirrored into the thread, which becomes *replied*.
- **Command bar** (top of `/mail`): say what you want in plain words.
  "What needs a reply in sales@?", "reply to everything for review", "dry
  run the two from Acme", "summarise the thread from Ann and draft a
  yes". It works on the accounts selected in the scope unless you name
  others. Questions and review or dry-run runs happen at once; anything
  that would send mail shows you the accounts and counts and waits for
  Confirm. With Anthropic as the AI provider the bar has full tool use;
  on other providers it follows a simpler protocol and tells you when it
  cannot act on a request rather than guessing.
- **Reply runs** (`/mail/runs`): answer everything that is waiting in one
  go. The page lists the unreplied threads of the selected accounts,
  grouped by mailbox, all ticked (excluded senders unticked). Choose how
  the run behaves: *draft and hold for review* (nothing is sent until you
  approve it on Review), *send the confident ones* (high-confidence drafts
  that pass the writing check go out; the rest wait for review), or *dry
  run* (draft everything, send nothing). Optional instructions and a
  Reply-To apply to the whole run. Accounts work side by side; within one
  mailbox replies go out in order, 20 to 60 seconds apart, inside that
  mailbox's daily cap and send window, each from the mailbox that
  received the original. The run page shows "Replying 4 of 17" with a
  per-thread status under each account and a Cancel button.
- **Review** (`/mail/review`): every draft waiting on a person, across
  the selected accounts, with approve / edit / discard and approve-all.
- **Audit log** (`/mail/audit`): everything the mail center did, in
  order, filtered by kind and account, and every email that was sent,
  dry-run, held or discarded with the account it left from, the run it
  belonged to and the confidence it had.
- **Compose** (`/mail/compose`): a new email from a short brief, through
  the same check, composer and send rules.
- **AI provider**: the mail center uses the key and model chosen on the
  site's AI provider page, so it costs the user, never the site.

## Going live: the checklist

Run `python -m bb doctor` where the site runs. It prints OK / WARN / FAIL
for each line below and what to change.

1. **One address that lasts.** Either a server with a domain
   (`deploy/install.sh`, the hands-off way) or a named Cloudflare tunnel
   on your own domain (`BB_TUNNEL_TOKEN` + `BB_BASE_URL` in `bb.env`). A
   quick tunnel is fine for a day; its address changes on every restart.
2. **The site restarts by itself.** On a server Docker does it; on a Mac
   `python -m bb install-service`.
3. **Settings in `bb.env`** (or `.env` on a server), not in a terminal
   window: `BB_BASE_URL`, the admin login, the tunnel, the Google client.
4. **An admin that is you**, and a login for each person (Users page).
5. **Each user's mailbox pace**: a personal Gmail or Outlook address is
   held to Normal; Workspace and Microsoft 365 to the Business pace; a
   sending service (SES, Brevo, Postmark, Mailgun, SendGrid) takes Relay
   or Custom. The Mailbox page's checks (SPF, DKIM, DMARC) should be green
   on the sending domain before a big list goes out.
6. **Warm-up on for a mailbox with no history**, off for one that already
   sends; quiet hours on or off as the audience needs.

## Why is nothing sending?

The Worker card on the dashboard says, in one line, what the sender is
doing right now and what to change when it is stuck: quiet hours (untick
"Only send during these hours" on the profile to send around the clock),
the daily maximum or the warm-up ramp, emails waiting for approval or
held by the spam check (Outbox), a paused AI provider, a worker that is
not running. Sending itself runs on its own clock and needs no AI call;
only writing new emails and answering replies do.

## Speed

Profile → Sending has five speeds. Gentle and Normal send one email at a
time with a pause between them, the way a person would. Fast sends two
at a time, two seconds apart, about 1,500 an hour. Relay sends eight at
a time, 100 a minute, and is meant for an SMTP sending service (Amazon
SES, Brevo, Postmark, Mailgun, SendGrid) connected on the Mailbox page
with its SMTP details; a personal Gmail, Outlook or Workspace mailbox
will be throttled or blocked at that rate. Custom takes your own
numbers for an external SMTP: emails per batch, seconds between batches,
the longest pause between two emails, and how many go at once. The
mailbox decides what is actually allowed: a personal gmail.com or
outlook.com address is held to Normal's pace and 450 / 250 a day; Google
Workspace or Microsoft 365 on a company domain to the Business pace (20 a
minute, two at a time) and 1,800 / 9,000 a day; a sending service gets
exactly the pace chosen. Whatever the speed, "Max emails per day" and the
warm-up ramp still cap the day, so for a big list set the maximum high
and switch the ramp off only on a mailbox that already sends a lot. In harvey.yaml the speed is
`usage.heartbeat_interval_minutes` plus `channels.email.sends_per_cycle`,
`send_concurrency` and `send_jitter_seconds`.

## What you'd like to happen next

On the Profile page, under Writing style, pick the one thing every first
email should ask for: a video call, a phone call, coffee, meeting in
person, a visit or tour, or details by email. Leave it on "Just a reply"
and the email ends on an easy question. Add a line of specifics (when you
are free, where you are) and it is used where it fits; for calls, your
booking link is added once under the ask. Replies steer toward the same
thing once someone is interested.

## Inbox auto-reply

The Inbox page answers mail from people who are not on the list. Switch
it on, write instructions as you would brief an assistant (what to say,
what never to promise, what to leave for you), and add the documents it
may share: a name, the link, and when that link is the right thing to
send. The worker reads each new email, decides whether a person would
answer it (notifications, newsletters, receipts and pitches are left
alone), writes a short reply in your name, and puts the exact links at
the bottom. With approval on, the reply waits in the Outbox; the Inbox
page lists every reply drafted or sent. Each sender is answered at most
once every three days so two auto-responders never loop. "Which
emails" chooses whether only new mail is answered or also unanswered mail
from the last week, 30 or 90 days; that backlog is worked through five a
cycle, and anything already replied to (by you or by BB) is skipped.

Connecting the mailbox itself is now one step for most people: on the
Mailbox page, "Log in with your email" takes the address and an app
password and finds the servers (Gmail, Outlook, Yahoo, iCloud, Zoho,
Fastmail, and custom domains hosted on Google Workspace or Microsoft 365
through their mail records). The full settings form is still there for
anything unusual.

## Names and greetings

Every email opens with a greeting line. The first name comes from the
upload, else the LinkedIn URL (`/in/jane-doe-…`), else a `first.last@`
address, else the company's team page matched against the address
(`jdoe@acme.com` next to a card for Jane Doe). When none of those gives a
name the line is "Hi there,"; a shared inbox such as `info@` or `office@`
gets "Hello Acme team,". The word (Hi, Hello, Hey, Dear, Good day) is on
the profile. Nothing is ever guessed from a single word like `jane@` or
`jdoe@`, because a wrong name is worse than none.

## Pausing, editing, starting over

The dashboard has three controls besides Start and Stop:

- **Pause for edits** stops the worker and remembers that it is meant to
  come back. Change the profile, mailbox or AI provider, then press
  **Resume**. Queued emails and open conversations wait where they were.
  (Saving the profile or the AI provider while the worker is running
  restarts it on its own, so Pause is only needed for several edits at
  once.)
- **Pause all sending** (in the Worker card) keeps the worker running and
  reading replies but sends nothing until **Resume sending**.
- **Start over** (under the Worker card, with a confirmation) stops the
  worker and deletes every recipient, every written and sent email, all
  replies and the counters. Profile, mailbox, AI provider, attachments and
  signature stay. Upload a new list and press Start to begin again.

## If the worker stops

The worker's log (dashboard → Worker) ends with a line that says why it
stopped. "Harvey stopped: SIGTERM, a stop request…" means the Stop button,
a settings change that stops the worker (removing the mailbox or the AI
provider, an admin disabling the account), or the system shutting down;
"Ctrl+C" or "the terminal closing" mean the terminal that ran it. A worker
that was never stopped from the dashboard but is no longer running (the
Mac rebooted, it crashed) is started again the next time its owner opens
the dashboard, and again when `python -m bb` starts. Press Start on the
dashboard to bring a stopped worker back by hand; nothing is lost either
way, it carries on from the outbox and the recipients it had not reached.

## Operations

- **Data**: everything is under `./data` on the host (`data/bb/bb.db` holds
  accounts; `data/bb/workspaces/<id>/` holds one user). Back that folder up.
- **Restarts**: `docker compose restart bb`. Workers that were running are
  started again on boot.
- **Logs**: `docker compose logs -f bb` for the web app; each user's worker
  log is `data/bb/workspaces/<id>/data/harvey.log` (the dashboard shows the
  tail).
- **Upgrading**: `git pull && docker compose up -d --build`.
- **Disabling someone**: Users → Disable. Their worker stops and their
  sessions end. Their workspace stays on disk until you delete the folder.

## Sending limits, honestly

A 10,000-row upload is accepted; 10,000 sends is a schedule, not a day.
Google caps free Gmail at 500 and Workspace at 2,000 messages a day,
Exchange Online allows 10,000 recipients a day, and cold email anywhere
near those numbers gets the account flagged. Safe is roughly 20–40 a day
from free Gmail, 50–150 from a warmed-up Workspace mailbox on a secondary
domain, and a few hundred from a Microsoft 365 mailbox with a history.

Three controls on the profile page:

- **Max emails per day** (0 = no limit, so only the mailbox's own ceiling
  applies).
- **Speed**: how often the worker wakes up and how many it sends each
  time. *Gentle* is 8 every 5 minutes, *Normal* 10 every 2 minutes,
  *Fast* 25 every minute (about 1,500 an hour; Microsoft 365 admin app or
  an aged Workspace mailbox only). Each cycle also reads and answers
  replies, so replies are handled within a minute or two at any speed.
- **Warm-up** (on by default): a mailbox new to the worker starts at 20 a
  day and gains 10 a day, counted from its first send, never above the
  daily max. Switch it off for a mailbox that already sends plenty of mail.

The worker also writes emails only a day or two ahead of what it can send
and stops itself when bounces pass 5% of sends. Free AI tiers have hourly
request limits; when one is hit the worker pauses and resumes on its own.

## Getting delivered (why mail gets blocked)

Start with Outbox → **Bounced**. Every email the receiving server sent
back is there with the server's own words and what to do about them. A
few "no such address" bounces are normal list noise. Several that say
"unsolicited", "blocked", "rate" or "unauthenticated" mean the mailbox or
domain is the problem, and the fixes below apply.

The Mailbox page checks the sending domain's SPF, DKIM, DMARC and MX
records every time it is opened and says exactly which record to add. The
things that matter, in order:

1. **Send from a domain you own**, with SPF, DKIM and DMARC published.
   Gmail and Yahoo now reject unauthenticated bulk mail outright. A free
   gmail.com or outlook.com address is signed by Google or Microsoft, but
   shares its reputation with everyone else on that domain and is filtered
   harder; the page says so when it sees one.
2. **Warm up and pace.** The warm-up ramp and the speeds above exist for
   this. A fresh mailbox that sends hundreds on day one is blocked within
   hours.
3. **Plain first emails.** The worker sends plain text unless a signature is
   set, and then an HTML part with the plain text alongside. The signature
   image is embedded (not loaded from a URL), and there is no tracking
   pixel or link rewriting. Keep the signature to a name, a title and one
   or two links.
4. **An opt-out path.** Every email carries a `List-Unsubscribe` header
   (`channels.email.list_unsubscribe`), which Gmail and Yahoo require from
   bulk senders, and the reply handler stops a thread on any "no".
5. **Clean lists.** Bounces damage reputation faster than anything else;
   the worker trips its kill switch at 5% for that reason.

## Security notes

- Passwords are hashed with scrypt; sessions are random tokens stored hashed.
  Login is rate-limited per address. Cookies are `HttpOnly`, `SameSite=Lax`,
  and `Secure` when `BB_BASE_URL` is https.
- Each user's Anthropic key and mailbox password live only in their
  workspace (`data/secrets.json`, owner-only permissions) and are handed to
  their own worker process. The Google client secret lives only in the
  server's environment.
- Gmail refresh tokens are stored per workspace with owner-only file
  permissions. Disconnecting deletes them; users can also revoke access in
  their Google account.
- Attachments users upload are kept under their workspace's `attachments/`
  folder; the worker only ever attaches files by name from that folder.
- Run behind https. Without it, session cookies cross the network in clear text.
- IMAP and SMTP connections verify the server's certificate, so a mailbox
  password never goes to an impostor. A host with a self-signed certificate
  is refused; use its real name, not an IP.
- The site trusts `X-Forwarded-For` only from `BB_TRUSTED_PROXIES`
  (default `127.0.0.1`: cloudflared or Caddy on the same computer; the
  Docker image also trusts the private ranges its Caddy container uses).
  Set it to your proxy's address if the proxy is elsewhere; otherwise a
  visitor could forge the header and dodge the login brake, or trip it for
  everyone behind the proxy.
- With a tunnel the plain-http port listens on `127.0.0.1` only. Set
  `BB_HOST=0.0.0.0` to open it to the local network as well.
- Every response carries `X-Frame-Options: DENY`, `nosniff`, a referrer
  policy, and HSTS once `BB_BASE_URL` is https.
