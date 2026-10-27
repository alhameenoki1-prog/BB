# Harvey — Autonomous AI Sales Agent

Harvey is an autonomous sales agent that thinks with Claude through the Anthropic API (or, opt-in, Codex on a ChatGPT plan). It finds prospects, writes cold emails, sends them from your own mailbox, handles replies, and books meetings — all on its own.

**You (Claude) are the guide.** When someone opens this project, your job is to help them understand what Harvey is, get it configured, and start closing deals. Be conversational, not robotic. Explain things simply. Ask one thing at a time.

## Always end a summary of changes with the update commands

The owner asked for this and it is never skipped: every summary of work
that was committed and pushed ends with what to type in a terminal to put
the new code on their machine. Use exactly this block (the folder is
wherever BB was cloned, `~/BB` by default):

```bash
cd ~/BB
source .venv/bin/activate
python -m bb update
```

`python -m bb update` pulls the code, installs anything new and restarts
the site when it runs as a service; running workers move to the new code
by themselves. When the site runs from a terminal window instead, add the
line it prints: press Ctrl+C in that window, then `python -m bb`. For a
Docker server, the block is `cd /opt/bb && git pull && docker compose up -d --build`.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need install
2. Is `harvey` importable? → If not, dependencies need installing
3. Does `.env` exist with real values (`ANTHROPIC_API_KEY` plus a mail provider)? → If not, they need API keys
4. Does `harvey.yaml` have real values (not "Your Company")? → If not, they need product training
5. Does `data/harvey.db` exist? → If not, Harvey hasn't run yet

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Harvey — an autonomous AI sales agent. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It thinks with Claude through the Anthropic API, billed per token, with a daily spend cap you control.
>
> Let me help you get it set up. It takes about 5 minutes. First, let me install the dependencies..."

**If partially set up:**
> "Looks like Harvey is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Harvey is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Harvey to Users

People will ask "how does this work?" — explain it simply:

- **"What does Harvey do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to replies.

- **"How does it find people?"** → It searches the web (DuckDuckGo, Bing, Google) for companies matching your target profile, visits their websites, and finds team members. For emails, it learns each company's address pattern (from their site, or a free Hunter domain lookup) and verifies a single candidate through a free verification tier — every address is tagged verified / catch-all / guess, and only deliverable ones get sent. No expensive tools needed.

- **"How does it write emails?"** → It uses proven cold email frameworks (like AIDA and PAS) with strict rules — short, personal, no AI-sounding language. Each email is tailored to the specific person and their company.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It can't delete files, access your bank account, or do anything outside its sales workflow. Everything it does is logged in a local SQLite database you can inspect anytime.

- **"What does it cost?"** → Anthropic API usage for the model calls (billed per token; the default model is `claude-opus-5-5`, and `usage.max_daily_cost_usd` in harvey.yaml caps the daily spend — the Usage tab and `harvey usage` show the running estimate), plus a mailbox (~$7/mo Google Workspace). Prospecting on the free sources, website reading, and reply handling need nothing else.

- **"Can it use ChatGPT / Codex instead of Claude?"** → Yes, opt-in, and that path is a subscription rather than per-token billing. Install Codex CLI 0.159.3 (`npm install -g @openai/codex@0.159.3`; other versions are refused), run `codex login` with ChatGPT, and set `brain: { provider: codex, model: gpt-5.5 }` in harvey.yaml (or choose codex in `harvey setup`, which suggests gpt-5.5, or `harvey train <url> --provider codex --model gpt-5.5`). gpt-5.5 is the verified text-only model; a blank model fails on current accounts because the live catalog hides it, and code-mode models are refused. Claude stays the default; Harvey never switches providers on its own. Each Codex call runs isolated with no tools — see "Using a ChatGPT plan instead (Codex)" in the README. macOS and Linux only; on Windows, Codex is refused, so use Claude. Don't edit the user's own `~/.codex` config to make it work.

- **"What's the dashboard?"** → Run `harvey dashboard` to see a local web UI at localhost:5555. It opens on **Today** — anything waiting on a decision, then the pipeline. Tabs for Signals, Companies, Contacts, Campaigns, Outbox, Conversations, Activity, Usage, Settings.

- **"What are signals?"** → Signals are the facts Harvey collects about a business: who its current agency is, whether it's running ads, whether it has online booking, how it ranks. **Harvey proposes; you confirm.** Nothing is collected until the user says yes on the Signals tab (or `harvey signals --confirm ...`). That keeps spend intentional and makes every prospect list explainable — a cohort is a query over signals a human chose, not a black box.

---

## The hosted web app (bb/)

BB is a second way to run everything: a website where an admin creates logins
and each user gets their own isolated Harvey (`HARVEY_HOME` = their workspace
under `BB_HOME/workspaces/<id>`). Each user picks **their own AI provider and key**
(Gemini/Groq free tiers, Ollama, or paid Anthropic/OpenAI; never the site's), connects Gmail via the web OAuth flow or any mailbox's
own SMTP/IMAP settings, fills a profile that becomes their `harvey.yaml`
(including `outreach:` — inquiry vs offer mode, what they want from
recipients, how replies are answered, attachments for replies), uploads
recipients (stored with `email_status: provided`, which sends; the
Recipients page's **Verify & clean** button runs a free check — syntax, live
MX, disposable domains, role inboxes — and moves provably-dead addresses to a
reversible **Excluded** list the writer and sender skip, `harvey/listclean.py`,
`Workspace.clean_list`/`excluded_recipients`/`restore_recipient`, CLI `harvey
verify`/`clean` (also `--csv <file>` to clean a file in place); a reply or
reconnect scan skips a provably-dead contact before drafting
(`bb/mail/runs.py`, `listclean.sweep`)), and runs a
worker process. Per-user settings also cover speed (gentle/normal/fast/relay/custom, held per mailbox tier by `Workspace.mailbox_tier` (consumer → Gentle, business → `BUSINESS_PACE`), ceiling in `channels.email.mailbox_daily_ceiling`: `heartbeat_interval_minutes` + `channels.email.sends_per_cycle` + `send_concurrency` lanes + `send_jitter_seconds`; the sender gates first, then sends through parallel lanes), a warm-up
ramp, the writing style (`outreach.style`), the closing ask (`outreach.ask`: zoom / phone / coffee / in_person / visit / email, plus `ask_detail`; `Writer._ask_block`, `Handler._outreach_context`), the need per kind of business with its full-scope link (`outreach.sector_needs`, `outreach.sender_line`, `outreach.details_url`; `harvey/enquiry.py` picks the row for a recipient's sector, resolves the `[SCOPE LINK]` marker once per conversation, `spamcheck.vague_risk` holds campaign-sounding drafts, `spamcheck.subject_risk` / `subject_from_need` keep every subject the thing itself in the email's own words (writer `_fix_subject`, sender `_fit_for_sending` which rewrites a failing email once by the model before holding it under **Needs review**, where nothing else waits on it), `enquiry.example_copying` (facts and six-word runs lifted from the prompt's worked examples) makes the writer and handler ask the model once more for its own words, `outreach.goal` (the profile's "What this campaign is for") goes into every writer brief, handler reply, command-center draft and Outbox Rewrite (`Workspace.rewrite_item`, the per-email Rewrite button with an AI instruction or a manual edit), the command center attaches the profile's reply files per `attach_on` (`bb/mail/sending._reply_attachments`), `harvey/health.py` checks the mailbox login every 30 minutes and flags a stall (dashboard and Mailbox page; `bb/alerts.py` sends each new problem once by email via `BB_ALERT_SMTP_*` and/or the user's webhook, reminds daily, notes when it clears), a changed writing brief on profile save rewrites unsent first emails of people not yet written to (`Workspace.writing_brief`, `rewrite_unsent_now`), and a 5xx recipient refusal at send time is handled as a bounce (`Sender._record_bad_address`; Vade Secure rows in `BOUNCE_HINTS`) with `listcheck.check_domains` marking dead domains invalid before anything is written; a soft no (`enquiry.is_soft_no`) gets one closing note with the brief (`enquiry.closing_scope_sentence`, handler intents objection/not_interested, command center `apply_scope_link`) and a bounce or rejection (`mail_provider.looks_automated`, which now catches `looks_like_bounce`: postmaster / mailer-daemon senders and delivery-failure subjects) is automated mail, so a reply run never drafts to it (`runs._auto_or_bounce`, thread state `no_reply_needed`); a hard no (`enquiry.is_opt_out`) is suppressed everywhere (prospect `opted_out`; mailbox `excluded` list via `bb/mail/runs.suppress`); `bb/mail/runs.reconnect_candidates` + `create_reconnect_run` + `drafting.draft_reconnect` are the follow-up runs that continue two-way threads from the last N months with the project link, one per contact; `prompts/inquiry_writer.md` and `prompts/inquiry_handler.md` are the briefs; `bb/mail/scope.py` gives the command center the same facts), an email signature with an
inline image (`outreach.signature_html/_image`; left blank on the profile it
is built automatically from the name/role/company/LinkedIn fields with the
image underneath — `build_auto_signature`, `AUTO_SIG_MARKER`, placed by
`render_parts` — and rebuilt on every profile save), and a Microsoft 365
admin-app mailbox (`channels.email.provider: microsoft_app`, Graph client
credentials), or one-click Microsoft sign-in for Outlook, Hotmail and
Microsoft 365 (`channels.email.provider: microsoft`; `bb/microsoft_oauth.py`,
site settings `MS_CLIENT_ID/MS_CLIENT_SECRET/MS_TENANT`, token in the
workspace's `data/m365_token.json`; the command center's `providers/graph.py`
runs as `/me` when the creds hold a refresh token and `service.persist_creds`
stores Microsoft's rotated token). Sign-ins survive a password change;
`Workspace.needs_reconnect` turns a refused login into a Reconnect card
on the dashboard and Mailbox page. Links go out as labelled anchors
(`mail_provider.body_to_html`, `link_text`; `drafting.finalize(link_labels=)`). The Mailbox page runs SPF/DKIM/DMARC/MX checks
(`bb/deliverability.py`). `outreach.inbox_autoreply` + `inbox_instructions` + `inbox_links` answer
mail from strangers with the right document links (Handler
`_auto_reply_inbox`; `inbox_backlog_days` also sweeps older unanswered
mail via each provider's `get_unanswered`; web Inbox page). The Mailbox
page's "Log in with your email" derives SMTP/IMAP servers from the
address (`bb/mail_autoconfig.py`); its "paste the whole login" box reads a login in any shape (a provider's settings block, an `imap://`/`smtp://` URL, JSON, or one `address password` line) into those fields (`bb/mail_paste.py`, `/mailbox/paste`, `/mail/accounts/paste`). `harvey/spamcheck.py` scores every draft like a filter would (rewrite once,
then hold for review; `vague_risk` adds the campaign-phrasing score in inquiry mode); first emails go as plain text with a links-free signature (`channels.email.plain_first_email`), segment mode rotates three wordings (`EmailStep.variant`, `Writer._add_variants`), and a spam-block bounce pauses the sender (`Handler._stand_down_after_block`, `Sender._in_cooloff`) instead of marking the address invalid; `outreach.use_template` sends the user's own message
instead of AI text (`outreach.template_variants`, other wordings the AI writes
once via "Write a general message for me" or `/profile/general-message/variants`
(`Workspace._write_variants`, `TEMPLATE_VARIANTS` = 5), are rotated per
recipient by `EmailStep.variant` with no AI call at sending time); in an AI mode `outreach.template_fallback` sends that
message to any batch the model cannot write (rate limit, no credit) instead
of waiting (`Writer.template_fallback`), and "Write a general message for me"
(`Workspace.generate_general_message`, `/profile/general-message`) has the AI
draft one direct, industry-agnostic message + subject from the profile once.
When replies can't be read and handled (the model is rate-limited or paused),
the sender still drains **first emails** — `sender.run(first_only=True)` in
`main._mail_pass` and the heartbeat's send phase; a first email has no reply
to miss, follow-ups wait — the paused heartbeat writes the general message
and sends first emails instead of sleeping an hour, and a fallback batch is
named `your message · <batch>` so the sender sends it as the operator's own
words with no AI spam-fix in front; the handler records why a pass was
incomplete (`Handler.last_problem`), the skip reason carries it, and the
dashboard adds it as a note to the real state (sending first emails, the
day's limit, nothing approved) with a link to the page that fixes it
(`Workspace._sending_status`). In that state `get_outbox(first_only=True)`
leaves follow-ups out in SQL, an AI-written campaign on autopilot stays a
draft, `main._first_emails_pass` reads bounces with no model
(`Handler.run(model=False)`) before sending, `Writer.run(use_model=False)`
makes no model call at all, the over-budget heartbeat still runs
`health.run_check` (the stall alert counts first emails only and holds off
when the day's allowance is used), and `Sender.auth_refused` keeps a refused
login on the dashboard. The Microsoft providers (`harvey/integrations/microsoft.py`,
`microsoft` sign-in and `microsoft_app` admin app) read the inbox from a saved
position (`graph_cursor.json`, oldest first, `MAX_INBOX_PAGES` a pass,
`backlog_pending` when more waits, `rewind_to` for a message the model could
not handle; delivery reports in Junk Email are read too, `JUNK_MESSAGES`)
so a bounce flood is worked through rather than failing every read. A delivery report is matched through its In-Reply-To, the Message-ID it
quotes, or the address it names (`mail_provider.bounced_recipient`,
`original_message_ids`, `state.find_outbox_sent_to`); Microsoft's verdicts on
the sender (`SENDER_BLOCKED`, `SENDER_BLOCK_KINDS`: 5.1.8 bad outbound sender
and the rest) stop sending with the kill switch and the Restricted-entities
fix in its text (`Handler._stop_after_sender_block`), and ten bounces in
fifteen minutes against the last hour's sends stop it too
(`Handler._bounce_burst`, timed by the reports' arrival, `MIN_SENDS_FOR_BURST`). The read position moves as the
handler consumes (`advance_to`); a reply left for the model is kept by id in
`replies_deferred` and fetched back with `fetch_message`; `is_delay_notice`
keeps delay warnings out of `_handle_bounce`; Microsoft's tenant daily cap
(`DAILY_LIMIT_HIT`, 5.7.233) is a 24-hour pause with the first emails queued
again (`RETRY_KINDS`); a blocked first email is queued
again (`state.requeue_first_email`) and `sending_resumed_at` (written by
Resume) makes older block reports inert; `Sender._record_bad_address` stops
the same way on a sender block at send time. `harvey/names.py` resolves missing recipient
names (LinkedIn slug, first.last address, team page) and puts the greeting
line on every email (`outreach.greeting`); a worker that dies is revived
on the next dashboard view; the dashboard has Pause for edits / Resume /
Start over, and uploading recipients confirms the free signals so the
profile stage runs without the CLI's confirm step. Start it with `python -m bb` (needs `BB_BASE_URL`,
`BB_ADMIN_EMAIL`, `BB_ADMIN_PASSWORD`; `GMAIL_CLIENT_ID`/`SECRET` of a *Web
application* OAuth client are optional). Google and Microsoft sign-in can instead be switched on from
Users → Sign-in setup (`/admin/signin`, `bb/signin_setup.py`, `BB_HOME/signin.json`); Microsoft's app is
registered there automatically through Graph from the admin's own sign-in (device code with Microsoft
Graph Command Line Tools, `bb/signin_auto.py`), Google's `client_secret_….json` is dropped on the page. Deployment and Mac quick-start:
`docs/HOSTING.md`. `python -m bb install-service` keeps it running (launchd/systemd, `bb/service.py`); `python -m bb doctor` is the go-live check; `deploy/install.sh` + `docker compose --profile tls` is the one-command server install with Caddy. `BB_TUNNEL=cloudflare` makes the site run a cloudflared quick tunnel itself (`bb/tunnel.py`; the link is printed and shown on Users; `BB_TUNNEL_TOKEN` for a named tunnel). Code: `bb/app.py` (routes),
`bb/workspaces.py` (per-user home, config writer, worker control),
`bb/recipients.py` (CSV import), `bb/gmail_oauth.py`, `bb/accounts.py`.
When someone asks to "host this" or "give users logins", that is this.
**Several campaigns at once** (`/campaigns`, `bb/campaign_routes.py`): one
outreach campaign per account, each a Harvey workspace of its own under
`campaigns/<id>/` (`bb/campaigns.py` registry; `Workspace(settings, user,
campaign_id)`, `bind_account` copies the Accounts entry's connection in,
`selected()`/`select()` drive the menu's campaign switch; the AI provider,
mail DB, mail worker and alerts stay per user), reply campaigns
(`runs.create_reply_campaign`, worker `reply_campaign` tick) and scans
(`runs.create_reconnect_runs`) one per account, pause/resume
(`runs.pause_run/resume_run`), a job lane per run (`jobs.lane`), and the
per-user ledger `harvey/ledger.py` (`data/sendlog.db`) that splits each
account's daily limit (`service.account_daily_limit`) and holds opt-outs
for every campaign. Replies have no send limit anywhere: `Ledger.reserve(free=True)` records an answer without counting or
holding it, reply-only members take no part of the limit, `sending.is_answer` skips the command center's
cap, and Harvey's `Sender._drain_due` limits only `sequence` items (scans stay limited). Only the mailbox's own
maximum stops a reply (`limits.hard` via `service.set_ledger_limit`, `mailbox_daily_ceiling` in the Sender), counted over
everything; first emails stop a tenth short of it. A send pass takes nothing new after `PASS_SECONDS`. Accounts can be added in bulk (`/mail/accounts/bulk`,
`/mail/accounts/graph/bulk`). **Wake up now** (`/worker/wake`,
`/campaigns/outreach/<id>/wake`; `Workspace.wake_worker`) reaches a
running worker through `data/worker.wake` (and the sender sends test copies
of the next queued first email to the addresses in `data/send_test.json`,
`wake.request_test_send` / `take_test_send`, `Sender._send_test_copies`,
`/worker/test-send`; `/outbox/resend-last` queues the last
N sent first emails again via `state.requeue_last_first_emails`); the worker's `harvey/wake.py`
beat (`data/worker.beat`, every 10 s) feeds `Workspace.worker_doing` and
the site's stuck-worker restart (`restart_if_stuck`, `restart_mail_if_stuck`
in `keep_workers_alive`). Every worker sleep goes through `wake.sleep`
(wall clock, wakeable); don't add a bare `asyncio.sleep` wait to the loops.
The **mail command center** (`bb/mail/`, pages under `/mail`) is the newer
multi-mailbox side: `db.py` (per-user `data/mail.db`, WAL), `crypto.py`
(sealed credentials), `jobs.py` (SQLite queue, retries, dead-letter),
`worker.py` (`python -m bb.mail.worker <workspace>`), `providers/` (gmail,
imap, graph adapters), `service.py` (connect/check/scope/migration),
`routes.py`, `ingest.py` (idempotent store + threading + counts),
`state.py` (reply-state rules), `parse.py` (RFC 822), `brain.py` (the
user's model, Anthropic or OpenAI-compatible, from `BB_LLM_*` env; `complete_json` reads JSON leniently,
asks once more, and with `salvage="body"` keeps an unreadable answer as a low-confidence draft; an empty answer is
recovered from a reasoning model's `reasoning_content`/`reasoning` field or list-shaped `content`, and a filter block,
a 200-body error or an answer that stays empty twice is named with its cause),
`drafting.py` (draft_reply / draft_new, `qa_check` writing check,
`finalize` adds greeting + signature + project link at send time),
`sending.py` (cap, window, dry run, send from the receiving mailbox,
mirror into the thread), `runs.py` (batch reply runs: one `reply_run`
job per mailbox, review / auto / dry-run modes, confidence routing, cap
checked before drafting, 20–60 s pacing, cancel), `commands.py` (the
command bar: Anthropic tool use via `MailBrain.tool_turn`, JSON fallback
on other providers, sends become a pending action a person confirms).
`providers/graph.py` (Microsoft 365 by client credentials: sync by
received time, createReply for threading, drafts for known Message-IDs),
`/mail/audit` (audit log). All phases of the build brief are in.

---

## Setup Flow

Walk through these steps conversationally. Ask one thing at a time. Don't overwhelm.

### Step 1: Install Dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

Then install the browser for LinkedIn prospecting:
```bash
python -m playwright install chromium
```

### Step 2: API Keys (`.env`)

Harvey needs an **Anthropic API key** (the brain), an **email provider** (to send + read replies) and, strongly recommended, an **email verifier**. Pick one email provider:

```
# --- Brain: Claude via the Anthropic API (brain.provider: claude, default) ---
ANTHROPIC_API_KEY=          # console.anthropic.com → API keys. Not needed for brain.provider: codex

# --- Email provider: choose ONE, set channels.email.provider to match ---
GMAIL_CLIENT_ID=            # Recommended: Gmail/Workspace. Then run 'harvey gmail auth'
GMAIL_CLIENT_SECRET=
# --- or SMTP+IMAP (AgentMail, Fastmail, Workspace app password) ---
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
IMAP_HOST=  IMAP_PORT=993
# --- or legacy Instantly ---
INSTANTLY_API_KEY=

# --- Email verification (add at least one; else emails stay 'guess') ---
REOON_API_KEY=              # 600 free/mo — best free tier
ZEROBOUNCE_API_KEY=         # 100 free/mo — best for M365/Workspace catch-alls
HUNTER_API_KEY=             # 50 free/mo + pattern lookup

# --- Discovery (optional — the free OpenStreetMap source needs no key) ---
DATAFORSEO_LOGIN=  DATAFORSEO_PASSWORD=   # cheapest local-business + SERP data
DATAFORSEO_SANDBOX=                       # any value routes to the free sandbox

# --- Optional ---
LINKEDIN_EMAIL=  LINKEDIN_PASSWORD=   # LinkedIn prospecting
CLOUDFLARE_ACCOUNT_ID=  CLOUDFLARE_API_TOKEN=   # deep JS crawling for training
SERPER_API_KEY=            # web search + discovery (2,500 free, then a $50 prepaid pack)
```

**Recommended provider — Gmail:** for <50 cold emails/day, a real Google Workspace mailbox on a *dedicated secondary domain* (never the main one) is the most deliverable, cheapest (~$7/mo) option. Set `channels.email.provider: gmail` in harvey.yaml, put the OAuth client id/secret in `.env`, then run `harvey gmail auth` (one-time browser login). SMTP works with any mailbox (AgentMail, Fastmail). Instantly still works as a legacy option.

**Email verification matters:** Harvey learns each company's email *pattern* and verifies ONE candidate rather than guessing (raw SMTP probing is not viable from a locally-running agent — outbound port 25 is usually blocked, and Google Workspace / Microsoft 365 accept everything from an unknown IP). Without a verifier key, found emails are marked `guess` and are **never sent**.

**Approval by default:** with a native provider, every outgoing email waits in the **Outbox** for your approval (dashboard Outbox tab, or `harvey outbox`). Once you trust the output, set `channels.email.require_approval: false` in harvey.yaml for full autopilot.

After getting the Instantly API key, test it:
```bash
source .venv/bin/activate && python3 -c "
import asyncio, httpx
async def test():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get('https://api.instantly.ai/api/v2/accounts', headers={'Authorization': 'Bearer API_KEY_HERE'})
        print('Connected!' if r.status_code == 200 else f'Failed: {r.status_code}')
asyncio.run(test())
"
```

### Step 3: Product Training

This is the most important step. Harvey needs to know what it's selling.

**Option A — Train from a website URL (recommended):**
```bash
source .venv/bin/activate && python -m harvey.trainer https://their-website.com
```
This generates: `harvey.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `harvey.yaml` and `skills/product_knowledge.md`:
- What's your company name?
- What do you sell? (product/service name and one-line description)
- What does it cost?
- What are the top 3-5 benefits?
- Who's your target? (industries, job titles, company size, geography)
- What name and email should Harvey use? (the "from" identity)
- What objections do you usually hear? How do you respond?
- **Offers & closing:**
  - What's the primary offer? (subscription, service, etc.)
  - Is there a low-commitment entry? (free trial, free audit, etc.)
  - What's the goal? (book a call, start a trial, get a reply)
  - How should meetings be booked? (calendar link, suggest times, ask preference)
  - How long is the call? (default: 15 minutes)
  - Who takes the meeting?

### Step 4: Confirm Signals

Harvey ships a catalog of ~19 signals it knows how to collect, all **proposed** and
none active. Nothing is prospected against until the user confirms them.

Send them to `harvey dashboard` → **Signals**, where each one shows a
plain-language description, what it costs, and how many companies already carry
it. Or from the terminal:

```bash
harvey signals                      # review the catalog
harvey signals --confirm free       # turn on everything that costs nothing
harvey signals --confirm SERP_RANK  # the one paid discovery signal
```

Most signals are free (they read pages a business already publishes). Only
`SERP_RANK` and `EMAIL_STATUS` consume credits — the cost note on each row says so.

Once signals are confirmed, the **cohort builder** at the bottom of the Signals
tab turns them into a target list: pick what a good prospect must have (and what
disqualifies them) and it counts the matches live.

### Step 5: Find Businesses

Discovery is the only stage that spends money, so it always estimates first.

```bash
harvey discover --providers                        # the menu, with real prices
harvey discover --estimate                         # projected spend, then exits
harvey discover                                    # free OpenStreetMap source
harvey discover --provider dataforseo_listings --max-spend 2.00
```

The dashboard's **Discover** tab is the same thing with the tradeoffs laid out
side by side: what each source does, what it costs, its free tier, and which
`.env` keys it needs. Nothing is called until the user presses Run.

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** (default) | free | unlimited | Trying the whole pipeline with no account. Thin coverage for service-area trades — OSM maps premises, so it has under 2,000 roofers in the entire US. A trial source, not a complete list. |
| **DataForSEO Business Listings** | $0.372/1k businesses | $1 credit | Local trades, clinics, contractors. Phone + domain + rating + whether the listing is claimed, and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.0018 a search at depth 30 | $1 credit + free sandbox | Rank as the buying signal. Positions 11-30 are the sweet spot. |
| **Serper** | ~$0.30-$1.00/1k | 2,500 free, no card | The easiest paid one to try. |

Cities are geocoded automatically (cached forever, one lookup per city), or set
`icp.geo_coordinates` in harvey.yaml to control the radius:
`"Denver, CO": "39.7392,-104.9903,50"`.

**Depth 20-30, not 100.** Google removed 100-results-per-page in September 2025,
so depth 100 is now billed as ten pages at nearly every provider. Below rank 30
it is mostly directories anyway.

### Step 6: Behavior Settings

These go in `harvey.yaml` under `usage:`. Use sensible defaults unless they want to customize:
- `max_daily_cost_usd`: 0 = no cap. Claude only: estimated API spend per day before Harvey pauses until tomorrow. Suggest a cap (e.g. 5) to anyone worried about the bill.
- `max_daily_claude_percent`: 80 (Codex only: how much of the ChatGPT plan's quota to use)
- `heartbeat_interval_minutes`: 15 (how often Harvey checks for work)
- `quiet_hours`: 22:00-07:00 in their timezone
- `max_daily_sends`: 50 (email send limit)

### Step 7: Start Harvey

```bash
source .venv/bin/activate && harvey run
```

Or open the dashboard:
```bash
harvey dashboard
```

---

## After Setup — Ongoing Help

Users will come back with questions and tasks. Common ones:

- **"Show me what Harvey has done"** → Run `harvey status` or `harvey dashboard`
- **"The emails aren't good"** → Offer mode (selling): edit `skills/email_frameworks.md` and `prompts/writer.md`. Inquiry mode (the user is the prospective customer or partner, the web app's default): edit `prompts/inquiry_writer.md`; the sales skills are not loaded there, and the request comes from `outreach.intent`. Show them the current rules and help them adjust.
- **"Harvey isn't finding the right people"** → Check the ICP config in `harvey.yaml`. Adjust industries, titles, company_size, geography.
- **"I want to change what Harvey says"** → Skills are in `skills/`, prompts are in `prompts/`. Both are plain markdown files. Edit them directly.
- **"Train Harvey on a different product"** → `harvey train <new-url>`
- **"How do I see the database?"** → It's at `data/harvey.db`. They can open it with any SQLite tool, or ask you to query it.
- **"I just want the prospect list"** → `harvey export` writes a sequencer-ready CSV (verified/risky emails only; `--all` for everything). Also available as Export buttons on the dashboard's Contacts tab. Harvey is valuable purely as a list-builder even if the user never lets it send.

---

## How Harvey Works (Technical Reference)

### Architecture
- **Heartbeat loop** (`main.py`): Every cycle → check quiet hours (optional: `usage.quiet_hours.enabled`) → check budget → decide → act → log → sleep (45 s, `BUSY_SLEEP_SECONDS`, while deliverable recipients still wait for an email and `Writer.room()` has room; the full interval otherwise). The writer runs every cycle beside the chosen action while prospects are `new`; the list check (`listcheck.run_all`, `_bounded_listcheck`) and website research ride along under their own budgets. Native providers with `channels.email.send_every_seconds` also run `_send_loop`: read replies then drain the outbox every N seconds under `_mail_lock`, independent of research/writing and of the brain; outbox rows are claimed atomically (`state.claim_outbox_item`), send errors carry `SendResult.retryable/scope/auth_failed/daily_limit` (`smtp_mail.classify_smtp_error`, Gmail `_request_full` reasons) and are retried per row or per pass (`MAX_SEND_RETRIES`), a campaign is `writing` while first emails are personalised (`state.commit_campaign`, `release_writing_campaigns`), and the skip reason is stored in settings `sender_skip_reason` for the dashboard. Website research (`_bounded_profile`: `PROFILE_PER_CYCLE` businesses, `PROFILE_BUDGET_SECONDS`) rides along every cycle on its own clock and never gates the writer or sender; the writer runs classification, segment sequences, personal drafts (`MODEL_CONCURRENCY`) and team-page lookups (`LOOKUP_CONCURRENCY`) concurrently
- **Brain** (`brain.py`): Calls Claude through the Anthropic API with the official SDK (`anthropic.AsyncAnthropic`): one `messages.create` per prompt, no tools, a fixed system prompt that marks quoted content as data, bounded retries, and a server-side refusal fallback on supported models. Records exact per-call tokens and an estimated cost into `usage_events`; `usage.max_daily_cost_usd` caps the day. `brain.provider: codex` swaps in the isolated Codex runtime (`codex.py`) instead, and `brain.provider: openai` posts to any OpenAI-compatible `/chat/completions` endpoint at `brain.base_url` (OpenAI, Gemini, Groq, OpenRouter, Ollama; key in `LLM_API_KEY`)
- **State** (`state.py`): SQLite at `data/harvey.db` with tables for companies, prospects, campaigns, conversations, actions, usage
- **Usage tracking** (`brain.py`, `state.py`): per-call attribution (agent/task/model/tokens/estimated cost) for Harvey's own model calls only. Claude is budgeted in dollars (`usage.max_daily_cost_usd`); Codex against the ChatGPT plan's real quota windows (`usage.max_daily_claude_percent`), falling back to a daily call count when they can't be read. Dashboard has a Usage tab; CLI has `harvey usage`.
- **Skills** (`skills/`): Markdown knowledge files injected into agent prompts
- **Signals** (`signals.py`, `state.py`): every fact Harvey learns is an OBSERVATION — a row in `observations`, never a column. A new signal needs no migration, re-observing over time is a free time series, and confidence + provenance travel with the fact. The vocabulary in `signal_codes` is governed (a trigger rejects any code not in it) and gated: only `status = 'confirmed'` signals are collected, and only a human sets that. `state.cohort(require, exclude)` does the set intersection in SQL.
- **Dashboard** (`dashboard.py` + `harvey/web/`): FastAPI JSON API plus plain HTML/CSS/JS served from disk — no build step. Edit `harvey/web/app.css` or `app.js` and reload the page.
- **DISCOVER** (`collectors/discover.py`): the only stage that spends money, so cost discipline lives in the collector, not the database — every run estimates first, checks a spend cap between batches, reads a kill switch, and flushes observations per batch. Providers are adapters behind one interface (`DiscoveryProvider`); adding one is a small class. Junk filtering matches *patterns*, not a literal blocklist. Entity resolution is the normalised domain, falling back to the provider's `external_id` for businesses with no website.

### Sub-Agents
- **Scout**: Python does all web searching (DuckDuckGo → Bing → Google → Serper API) and email resolution (pattern-first: cache → scraped mailto → Hunter domain search → default, then verify one candidate via Reoon/ZeroBounce/Hunter/SMTP; catch-alls flagged `risky`). Claude only scores/personalizes found data. Scout also collects **buying signals**: tech stack detected on each company's site (HubSpot, Shopify, Intercom, ~35 tools — zero extra requests) and hiring signals from careers pages. With `pip install python-jobspy` (optional), a job-board strategy discovers companies actively hiring for roles in `icp.hiring_signals` (falls back to `icp.titles`) — the strongest in-market signal. Signals land in prospects' personalization notes and boost their score.
- **Writer**: Generates 3-email sequences (Email 1 < 75 words, Email 2 < 75, Email 3 < 40). Strict ban list on AI patterns.
- **Sender**: Native providers (gmail/smtp) render merge vars per prospect and STAGE each email into the `outbox` (pending_review → approved → sent) with a scheduled send time; each heartbeat drains due, approved items with human-like pacing, the daily cap, the deterministic pre-send gate, and stop-on-reply. Legacy Instantly path deploys campaigns via API. Enforces daily send limits either way.
- **Handler**: Polls the provider (or Instantly) for replies, dedups them, classifies intent, advances conversation stage, and queues auto-responses through the same outbox approval ladder. On native providers it also detects bounces → marks the address invalid, cancels the prospect's queued sends, and trips a global kill switch past a bounce-rate threshold.
- **Analyst**: Runs on idle cycles. Generates `data/analytics.json` with pipeline stats and insights.

### Conversation Stages
`initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

### Priority Order
handle_replies > send_campaigns > write_campaigns > prospect > idle (run analyst)

### Key Commands
```bash
source .venv/bin/activate    # Always activate venv first
harvey run                   # Start the heartbeat loop
harvey dashboard             # Web UI at http://localhost:5555
harvey setup                 # Re-run setup wizard
harvey train <url>           # Train on a product website
harvey status                # Pipeline summary
harvey usage                 # estimated Claude API spend (or Codex quota) + per-agent token usage
harvey export                # Deliverable prospects → sequencer-ready CSV (prospects.csv)
harvey export --all          # Full raw list, no filters
harvey gmail auth            # One-time Gmail OAuth (when provider: gmail)
harvey gmail test            # Verify the Gmail connection
harvey outbox                # Review queued emails; --approve <id> / --approve-all / --reject <id>
harvey signals               # The signal vocabulary; --confirm / --reject CODES (or 'free' / 'all')
harvey discover              # Find businesses; --providers / --estimate / --provider <key>
harvey sending pause|resume  # Kill switch for all outbound
```

### Common Issues
- **"command not found: harvey"**: Activate venv first: `source .venv/bin/activate`
- **"ModuleNotFoundError: No module named 'harvey'" after install (macOS)**: Python 3.13 silently ignores `.pth` files that carry the macOS hidden file flag, and some Macs propagate that flag into `.venv`. Run `harvey install` again (it auto-fixes by linking the package), or manually: `ln -s "$(pwd)/harvey" .venv/lib/python3.13/site-packages/harvey`
- **"externally-managed-environment"**: Use a venv, not system Python
- **SQLite errors**: The `data/` directory is created automatically on first run
- **Claude calls fail**: the log line says why. "not scoped to a workspace" → the key starts with `sk-ant-usr-`; set `ANTHROPIC_WORKSPACE_ID` (Console → Settings → Workspaces) or use a key created inside a workspace. A 401 → `ANTHROPIC_API_KEY` in `.env` is missing or wrong (Harvey pauses until it's fixed and restarted; `harvey status --check` confirms). "credit balance" → add credit in the Anthropic Console (Harvey pauses an hour, then retries). 404 → `brain.model` in harvey.yaml names a model that doesn't exist; leave it blank for `claude-opus-5-5`. Harvey never switches to Codex on its own.
- **Codex calls fail** (`brain.provider: codex`): the log line says why — `codex login` with ChatGPT (also when the login "has expired or is due for renewal": Harvey never renews it), install the supported Codex version (`harvey status --check` shows what's wrong without a model call; while Codex is unusable Harvey pauses whole cycles on purpose), pick a `brain.model` without code mode, or remove whatever Codex config layer is forcing tools/hooks/MCP back on. Never weaken Harvey's isolation checks to get past it.
- **Instantly API 401**: Wrong API key or needs the Growth plan for API access
- **Google search rate limiting**: Harvey automatically falls back to DuckDuckGo and Bing. For reliable search, add a Serper API key ($5/mo).
