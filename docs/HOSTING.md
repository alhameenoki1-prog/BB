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

# 3. Settings for the site (your login; users add their own AI key and mailbox later)
export BB_BASE_URL=http://localhost:8000
export BB_ADMIN_EMAIL=you@example.com
export BB_ADMIN_PASSWORD='choose-a-long-password'

# 4. Start it
python -m bb
```

Open <http://localhost:8000>, sign in with the admin email and password,
go to **Users** and create a login for each person. Everything is stored
under `BB/data/bb/` on your Mac. `Ctrl-C` stops it; `source
.venv/bin/activate && python -m bb` starts it again (the settings in step 3
need exporting again in a new Terminal window, or put them in a file you
`source`).

**Gmail one-click sign-in on a Mac:** Google only redirects to `localhost`
or to a real https domain, so for local use register
`http://localhost:8000/gmail/callback` on your Google OAuth client and
export `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` before step 4. Or skip
Google entirely: users can connect any mailbox with its own SMTP/IMAP
settings on the **Mailbox** page (Gmail works there with an app password).

**Letting other people use it from your Mac.** The Mac has to stay on and
awake (`caffeinate -i python -m bb` keeps it awake while running). Give it a
public https address with a tunnel:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://something.trycloudflare.com` address. Set
`BB_BASE_URL` to that address and restart `python -m bb`, then send people
the address. The free quick tunnel changes its address every time you start
it; for a stable one, create a named tunnel on your own domain
(`cloudflared tunnel login`, then follow Cloudflare's "named tunnel"
steps) or move to a server as below.

## Hosting on a server

### What you need

- A Linux server (any VPS with 2 GB RAM is plenty to start) with Docker and
  a domain name pointing at it, e.g. `outreach.example.com`.
- A **Google Cloud OAuth client** so users can connect Gmail in one click
  (below). Optional: users can always connect a mailbox with its own
  SMTP/IMAP settings instead.
- Nothing from Anthropic: **each user adds their own API key** on the
  **AI key** page, and their model usage is billed to them. The key is
  checked against Anthropic when saved and stored only in their workspace.

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
workers; users bring their own.

## 3. Run

```bash
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
`BB_ADMIN_PASSWORD`, then go to **Users** and create a login for each person.
There is no self-registration. Tell them their email and password; you can
reset it from the same page.

## What a user does

1. **AI key**: paste their own Anthropic API key. It is verified and kept in
   their workspace; the writing is billed to them.
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
5. **Start the worker**. Every 15 minutes it reads each new recipient's
   company website, writes a one-to-one email from what that business does
   and what the user wants from them, sends what is due inside the daily
   limit and hours, reads replies, and answers them following the user's
   instructions, attaching their documents.
6. The banner on every page shows replies, sent, not-yet-sent, bounced and
   total recipients, refreshed every 30 seconds. **Replies** lists everyone
   who answered; **Outbox** holds emails awaiting approval.

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
Google caps free Gmail at 500 and Workspace at 2,000 messages a day, and
cold email anywhere near that gets the account flagged. Safe is roughly
20–40 a day from free Gmail and 50–150 from a warmed-up Workspace mailbox on
a secondary domain. The profile's "max emails per day" is the control: the
worker sends up to 8 emails every 15-minute cycle inside the sending hours
until the day's maximum is reached (a 12-hour window allows up to about
380 a day), writes emails only about two days ahead of what it can send
(so a big upload does not run up the AI bill in one go), and stops itself
when bounces pass 5% of sends.

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
