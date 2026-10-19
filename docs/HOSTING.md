# Hosting BB for your users

BB is the web app around Harvey. You run one copy on a server; you create a
login for each person; each of them connects their own Gmail, describes who
they are and what they're reaching out about, uploads the people they want
to email, and starts their worker. Everything a user does lives in their own
workspace (`BB_HOME/workspaces/<user id>`): their config, database, Gmail
token and log. Workers are separate processes, one per user, started with
`HARVEY_HOME` pointing at that workspace.

## What you need

- A Linux server (any VPS with 2 GB RAM is plenty to start) with Docker and
  a domain name pointing at it, e.g. `outreach.example.com`.
- An **Anthropic API key** (console.anthropic.com). Every user's emails are
  written with it; each user has a daily spend cap in their profile.
- A **Google Cloud OAuth client** so users can connect Gmail (below).

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
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_CLIENT_ID=...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=...
BB_BASE_URL=https://outreach.example.com
BB_ADMIN_EMAIL=you@example.com
BB_ADMIN_PASSWORD=a-long-password-you-will-change
```

Optional: `REOON_API_KEY` / `ZEROBOUNCE_API_KEY` (email verification for
workers that prospect on their own), `BB_DEFAULT_DAILY_COST_USD`,
`BB_DEFAULT_MODEL`.

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

1. **Profile**: name, company, role, LinkedIn, what they're reaching out
   about and why it matters, how many emails a day, their sending hours, and
   whether they want to approve each email first (off = fully automatic).
2. **Gmail**: one click, Google sign-in, done. Emails are sent from that
   address and replies are read from its inbox.
3. **Recipients**: upload a CSV (up to 10,000 rows) with `email` and, ideally,
   `name`, `company`, `title`, `linkedin`, `website`, `notes`.
4. **Start the worker**. Every 15 minutes it reads each new recipient's
   company website, writes their email from the profile and those facts,
   sends what is due inside the daily limit and hours, and reads replies.
5. The banner on every page shows replies, sent, not-yet-sent, bounced and
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
a secondary domain. The profile's "max emails per day" is the control; the
worker also stops itself when bounces pass 5% of sends.

## Security notes

- Passwords are hashed with scrypt; sessions are random tokens stored hashed.
  Login is rate-limited per address. Cookies are `HttpOnly`, `SameSite=Lax`,
  and `Secure` when `BB_BASE_URL` is https.
- Users never see the Anthropic key or the Google client secret; both live
  only in the server's environment and are passed to worker processes.
- Gmail refresh tokens are stored per workspace with owner-only file
  permissions. Disconnecting deletes them; users can also revoke access in
  their Google account.
- Run behind https. Without it, session cookies cross the network in clear text.
