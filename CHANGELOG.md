# Changelog

Notable changes to Harvey. Dates are release dates; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and versions
follow [semver](https://semver.org/), with the caveat that Harvey is pre-1.0 and
minor versions can still change behaviour.

## [Unreleased]

### Added

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
