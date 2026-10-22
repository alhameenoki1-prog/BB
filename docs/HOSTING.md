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

**The link survives restarts and updates.** The site runs cloudflared as
its own detached process and records it in `data/bb/tunnel.json`; a site
that restarts (an update, `python -m bb restart`, Ctrl+C and start again)
adopts the running tunnel instead of starting a new one, so the address
your users have keeps working. It changes only when the computer reboots,
cloudflared crashes, or you ask for a new one with `python -m bb tunnel
restart`; `python -m bb tunnel status` shows the current link. A link that
has died shows Cloudflare's error 1016 ("Origin DNS error"): send the
current one. For an address that never changes, use a named tunnel on
your own domain (below).

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

## 1b. Microsoft app registration (Outlook, Hotmail, Microsoft 365 sign-in)

Optional, and the same idea as the Google client: one app registration
for the site lets every user connect an Outlook, Hotmail or Microsoft 365
mailbox with a click on Microsoft's own sign-in page. (A tenant you
administer can also use the admin app on the Mailbox page, which needs
no sign-in per user; this one is for everyone else, personal accounts
included.)

1. entra.microsoft.com → *Applications → App registrations → New
   registration*. Any name. Supported account types: **Accounts in any
   organizational directory and personal Microsoft accounts** (pick
   *this directory only* if the site is for one company).
2. *Redirect URI*: platform **Web**, value `https://outreach.example.com/microsoft/callback`
   (exactly `BB_BASE_URL` + `/microsoft/callback`). Under
   *Authentication*, add a second Web redirect URI
   `https://outreach.example.com/mail/microsoft/callback` for the
   command center.
3. *API permissions → Add a permission → Microsoft Graph → Delegated
   permissions*: `Mail.ReadWrite`, `Mail.Send`, `User.Read`,
   `offline_access`. No admin consent is needed for these; each user
   consents when they sign in (a tenant admin can grant it for everyone).
4. *Certificates & secrets → New client secret*, copy the *Value*.
5. `.env`: `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, and `MS_TENANT` only if
   you chose *this directory only* (the tenant id or domain; the default
   `common` takes both work and personal accounts).

What the user sees: *Sign in with Microsoft* on the Mailbox page (and on
the command center's Accounts page) sends them to Microsoft, which uses
the browser session they already have and shows the consent screen once.
No password reaches the site. Microsoft hands the site a refresh token,
stored in the user's workspace (`data/m365_token.json`, the file the
worker's Microsoft provider reads) and, in the command center, sealed in
the mail database. Mail is then sent and read through Microsoft Graph as
that account, replies keep Outlook's conversation, and the token keeps
working after the user changes their password.

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

**Outlook, Hotmail or Microsoft 365, one click.** When the site has a
Microsoft app registration (section 1b), the Mailbox page shows *Sign in
with Microsoft*: the user signs in on Microsoft's page, with the account
their browser already holds or any other, allows mail access once, and
is back on the page connected. Personal outlook.com and hotmail.com
accounts work this way (the only way, since Microsoft removed password
sign-in for them), and so do work accounts without any admin SMTP
toggle. Sending and reading replies both go through Microsoft Graph.

**Outlook / Microsoft 365 over SMTP.** (If you administer the tenant, use
Option C below instead; if the site has the Microsoft sign-in above,
use that.) Sending works for work or school accounts
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

### Changing your password, and Reconnect

A Google or Microsoft sign-in is an authorization, not a stored password:
the site holds a refresh token and mints access tokens from it, so the
connection survives a password change. It ends only when the user
disconnects on the Mailbox page, revokes the site's access in their
Google or Microsoft account, or, on some Microsoft 365 tenants, when the
tenant's policy signs every app out after a password change. A Google
client still in testing mode also ends sign-ins after seven days.

SMTP/IMAP with a password or an app password is the one connection a
password change does break (an app password usually survives it; a
normal password does not).

When a sign-in stops being accepted, nothing crashes and nothing queued
is lost: the worker's half-hourly login check (and the sender, if it
hits the refusal first) records it, the dashboard shows *Your mailbox
needs reconnecting* with a button to the Mailbox page, and the Mailbox
page shows a red *Reconnect your mailbox* card with the one action that
fits the connection: *Reconnect Google*, *Reconnect Microsoft*, or the
password form. One sign-in puts it right and sending resumes. The
command center's Accounts page marks such a mailbox *reconnect* with
the same one-click button beside it; until then that mailbox is skipped
by reply runs and the others carry on.

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

In an inquiry workspace every draft here, in a reply run, from the
command bar or on a thread, is written from the same profile as the
worker's: who you are, what you need from each kind of business, your
full-scope links, the next step you want and your standing instructions.
A run's "Instructions for every reply" can therefore say "send the scope
link when they ask for details": the model marks where it belongs, and
the right link goes in once per thread, never written by the model. The
first reply in a thread carries it whether or not they asked, except to
someone who said no, opted out or complained: that reply gets an answer
and no link, whatever the model drafted.

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

### A no is answered once, an opt-out never

Someone who says no politely ("not interested", "we're booked", "not
what we do") gets one closing note: thanks for the straight answer,
nothing to do on their side, and the brief left with them in case it is
ever relevant (the link, and the file if the policy attaches one). No
question, no ask, no follow-ups; the conversation closes. Someone who
asks to be left alone ("unsubscribe", "remove me", "stop emailing",
"this is spam"), is hostile, or threatens legal action gets nothing,
ever: the worker marks them opted out (a legal threat is also flagged
for you), the command center puts the address on that mailbox's
excluded list and refuses to send to it, and a later upload cannot bring
them back. These are detected by code, not by the model, because they
are what gets a domain blacklisted when ignored.

### Follow up with past contacts

Below the reply threads on the Reply runs page, "Follow up with past
contacts" scans the conversations you already had in the last 3, 6 or
12 months and continues each one in its own thread. Only two-way threads
with real people count (they wrote by hand, you answered); newsletters
and notifications, colleagues at your own domains, excluded and
suppressed addresses, threads you put away, contacts already followed up
with, and threads where the brief's link already went out are left out.
Each contact gets one message per scan, in their most recent thread. The
follow-up is written from that thread, never from a template: it picks
up the specific thing the exchange was about, bridges to the project the
way that thread allows, carries the brief's link once and ends with at
most one question. The model first judges whether the thread is one to
continue at all; a personal or private exchange is skipped as "not
suitable" and not asked again. A draft that shares nothing with the
thread fails the check and is rewritten; what still cannot be bridged
waits on Review, and the confident ones send paced and capped like
replies, threaded under the last message. If an account's history was
fetched for fewer days than the window, the page says so; connect it
with a longer history to see older threads.

### Replies carry the project brief

In an inquiry workspace the command center's replies deliver the brief
whatever the person wrote: the scope link goes where the model marks
it, once per thread, and the files on the profile's attachments list
(upload the project brief there) are attached to the first reply in a
thread, or to every reply, as the profile's "Send my attachments" policy
says. The drafting prompt tells the model the brief is attached so the
reply refers to it in a sentence, and the activity log records each
attachment. The campaign goal is in front of every reply draft too.

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
   held to Gentle; Workspace and Microsoft 365 to the Business pace; a
   sending service (SES, Brevo, Postmark, Mailgun, SendGrid) takes Relay
   or Custom. The Mailbox page's checks (SPF, DKIM, DMARC) should be green
   on the sending domain before a big list goes out.
6. **Warm-up on for a mailbox with no history**, off for one that already
   sends; quiet hours on or off as the audience needs.

## Replies, Reply-To and your own mailbox

The worker reads replies from the IMAP inbox of the connected mailbox
(or the Gmail account). It reads by position and never marks a message
read, so your mail client keeps its unread badges and you see every
reply there as well as on the Replies page. If you set a **Reply-To**
address on the Mailbox page, people's replies go to that address
instead; the worker only sees what still arrives in the connected inbox
(bounces and out-of-office notes, which ignore Reply-To), so leave
Reply-To blank unless you will answer those replies by hand.

## Why is nothing sending?

The Worker card on the dashboard says, in one line, what the sender is
doing right now and what to change when it is stuck: quiet hours (untick
"Only send during these hours" on the profile to send around the clock),
the daily maximum or the warm-up ramp, emails waiting for approval or
held by the spam check (Outbox), a paused AI provider, a worker that is
not running. Sending itself runs on its own clock and needs no AI call;
only writing new emails and answering replies do.

### Sending on its own

With approval off (the default), nothing waits for you. Before an email
is staged the sender runs the spam check and a subject check on the
rendered email; what fails is rewritten once by the model, with your
campaign goal in front of it, and checked again. Only an email that still
fails goes to the Outbox's **Needs review** tab, and nothing else waits on
it: the rest of the campaign keeps sending, and the dashboard counts the
held ones as a queue for you to clear, not as a stuck pipeline. Clear it
whenever you come back: Rewrite, edit, approve or reject each one.

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
outlook.com address is held to Gentle's pace and 450 / 250 a day; Google
Workspace or Microsoft 365 on a company domain to the Business pace (20 a
minute, two at a time) and 1,800 / 9,000 a day; a sending service gets
exactly the pace chosen. Whatever the speed, "Max emails per day" and the
warm-up ramp still cap the day, so for a big list set the maximum high
and switch the ramp off only on a mailbox that already sends a lot. In harvey.yaml the speed is
`usage.heartbeat_interval_minutes` plus `channels.email.sends_per_cycle`,
`send_concurrency` and `send_jitter_seconds`.

### From upload to the first email, and keeping up

Sending pace is one thing; how fast the worker researches, writes and
hands emails to the sender is another, and it is built not to wait:

- **Writing runs every cycle** while recipients are waiting for an email,
  beside reading replies and sending, never queued behind them. It keeps
  a lead over the sender of two days of the daily limit or twenty send
  batches, never under a hundred first emails in per-person mode, so the
  sender never idles; a 10,000-row upload is still worked through in
  pieces, not drafted at once.
- **Cycles follow quickly while there is work.** When deliverable
  recipients still have no email and the writer has room, the next cycle
  starts 45 seconds after the last one instead of a full heartbeat later
  (the speed presets' heartbeat is already one to five minutes; this
  matters most for `harvey run` with its 15-minute default). Quiet hours,
  the AI spend cap and a paused provider still apply.
- **The list check never delays the writer.** The domain lookups (and the
  verifier, with a key) run beside the writer under a 45-second budget per
  cycle, sixteen lookups and six verifier calls at a time; the sender
  takes one last look at the address before each send, so an address
  found dead after its email was staged is cancelled, not sent.
- **Research is parallel already**: 200 businesses a cycle, 24 sites read
  at once, under a two-minute budget, and it never gates the writer.
- **Drafting runs several model calls at once**: six by default
  (`brain.concurrency` in harvey.yaml, 1 to 16). A free-tier key with a
  low per-minute limit wants 2; a paid Anthropic or OpenAI key takes 8 to
  12. Per-person mode drafts up to 40 people per segment per cycle; segment
  mode writes one sequence per segment and is not model-bound.

## What you need, by kind of business (and the full scope)

In inquiry mode the emails are only as concrete as what you type. The
profile asks for the need in your words, **per kind of business** when
your list mixes trades: a Construction row ("a builder for a 24-unit
block in Towson, drawings done, start in April"), a Financial advisory
row ("an advisor for about three million of family savings after a
property sale"), a Manufacturing row ("about 40 tons of rebar a month
for a year, delivered in Baltimore"). Contacts are sorted by sector
automatically; the email to a builder is written from the construction
row, the email to an advisor from the advisory row, and anyone else gets
the general line. Add a one-line **about you** ("I run Greystone
Development, a residential developer in Baltimore") so the reader can
place you; the recipient's company is still never named.

Each row can carry a **link to the full scope** (a brief, a spec, a
folder) and what to call it. Nothing in the first email links anywhere.
When someone replies wanting details, the reply says in one sentence
what is in the document and why it helps them answer, then includes the
link, once per conversation, matched to their sector. The Inbox
auto-reply and the mail command center read the same rows, so a reply
run told "send the scope link when they ask" sends the right one.

A need nobody could repeat back ("start working with your team") is
refused when you save, with the reason. Every draft is also checked for
campaign phrasing ("our team has a current need", "how you usually
start", "a good fit") and for carrying at least one concrete token from
your need; a draft that fails is rewritten once, then held for review.

## What you'd like to happen next

On the Profile page, under Writing style, pick the one thing every first
email should ask for: a video call, a phone call, coffee, meeting in
person, a visit or tour, or details by email. Leave it on "Just a reply"
and the email ends on an easy question. Add a line of specifics (when you
are free, where you are) and it is used where it fits; for calls, your
booking link is added once under the ask. Replies steer toward the same
thing once someone is interested.

## The campaign goal

"What this campaign is for" on the profile is one paragraph in your own
words about what every email should achieve and how it should end, for
example: *the goal of the first email is to get permission to send the
project brief; every first email ends with an open question offering it,
something like "want me to send over the project brief so you have the
full scope before we talk?"; replies answer what they asked and always
get the brief to them.* It is stored as `outreach.goal` and goes into
every first email, follow-up and segment variant the writer produces,
every reply and inbox answer the worker writes, every command-center
draft and reply run, and every Rewrite from the Outbox. The model is told
to serve it in its own words each time, never to copy its wording, and
that it outranks the shape of the worked examples.

## Inbox auto-reply

In inquiry mode the auto-reply already knows the full-scope documents from
your profile (the Inbox page lists them under "Documents it already
knows"): a stranger who writes in about rebar gets the rebar spec, with a
line on what is in it, and the reply is written as the customer, not as a
seller. Add any other documents below them.

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
name the line is just "Hi,"; a shared inbox such as `info@` or `office@`
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

### Rewriting a queued email

Every scheduled or held email in the Outbox has a **Rewrite** button.
It opens two forms: tell the assistant how to rewrite it ("shorter; lead
with the April start; end by offering the brief") and it is rewritten
with your own AI provider under your campaign goal, keeping the facts,
the greeting line and the sign-off, then checked like every other email
before it is saved; or edit the subject and body yourself and press
Save (a held email also has Save and approve). A scheduled email can be
cancelled there too. Subjects are checked on their own everywhere: a
boilerplate, clickbait or gloomy subject, or one that names nothing in
the email, is replaced with a plain one built from your need.

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

## Is the mailbox still sending?

The worker logs in to the connected mailbox every 30 minutes (SMTP and
IMAP, the way `harvey status --check` does) and watches delivery. A
failed login, or approved emails due for an hour and a half with nothing
delivered and no deliberate pause to explain it (quiet hours, the warm-up
ramp, the daily cap, a cool-off after a spam block, the kill switch),
raises one plain sentence that the dashboard and the Mailbox page show
in red and the worker log repeats until it clears. The Mailbox page's
"Is it still sending?" card shows the last check, its result and the
last email delivered. The alert clears on its own once a check passes
and mail leaves again.

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

**A "blocked" bounce is a spam verdict, not a bad address.** "Likely
unsolicited mail", "message blocked", "policy" or "reputation" in a
bounce means the receiving side, or Google itself on the way out, judged
the email as bulk. BB treats it differently from "no such address": the
address stays valid, the follow-ups to that person are dropped, and the
sender pauses for six hours (a full day after the third block that day)
so the next batch does not deepen the hole. The dashboard says so, and
"Why emails bounce" groups the verdicts.

**Read the remote server's line, not Gmail's label.** Gmail calls every
rejection "Message blocked". `550 5.4.1 Recipient address rejected:
Access denied` from an `outlook.com` server is Microsoft 365 saying the
address is not in that company's directory: the list guessed the name.
Those addresses are marked invalid, the other queued emails to the same
company wait for your approval, and the dashboard groups them under "no
such address (Microsoft 365...)". With a `REOON_API_KEY` or
`ZEROBOUNCE_API_KEY` in the server's `.env`, every uploaded address is
checked before anything is written to it.

What BB does on its own to avoid the verdict:

- **Plain first emails.** The first email to each person goes as plain
  text with a signature stripped of links and the image (name, title and
  company only). The full signature goes on every reply and follow-up.
  The switch is on the profile, under the signature.
- **Three wordings per segment.** In segment mode the writer produces
  three wordings of the first email and the sender rotates them, so a
  hundred recipients never get one identical body. Each recipient always
  gets the same wording, so follow-ups match.
- **A gentle pace for personal mailboxes.** A gmail.com or outlook.com
  address is held to the Gentle speed whatever you pick; Normal and Fast
  are for Workspace or Microsoft 365 on your own domain. To send more,
  connect more mailboxes rather than pushing one harder.
- **Subjects about the thing, not the act.** "Looking to start work with
  your team" and "reaching out" are scored as spam before anything is
  sent; a subject should be the request as a customer would type it
  ("Rebar, around 40 tons a month from spring").


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

### Your own mail provider refusing an address

Some providers (OVH, Open-Xchange hosted mail and others behind Vade
Secure) refuse a recipient before sending: `550 5.7.1 Recipient rejected
- OXSUS003_407 - https://postmaster-oxsus.vadesecure.com/...#_407`. Code
407 means the recipient's domain does not exist; 406 a malformed
address; 402 an address the filter will not deliver to. These are bad
addresses in the upload (`yejn6oig@ss1.xfourrich.com` is the shape of a
made-up one), not a verdict on your mailbox. The worker now treats such a
refusal as a bounce that never had to travel: the address is marked
invalid, that person's queued emails are cancelled, a dead domain holds
the company's other first emails for your review, and the Bounced tab
explains it. Every uploaded address's domain is also looked up before
anything is written, with no verifier key needed, so an address at a
domain with no mail server is marked invalid and never sent to.

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
