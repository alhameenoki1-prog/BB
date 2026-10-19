# Project Brief: AI-Personalized Gmail Outreach System

_Working name: BB. Drafted 2026-10-19._

## 1. One-paragraph pitch

A web app where a user connects their Gmail, describes who they are (name, company, LinkedIn, what they are asking for), and uploads a list of people they want to reach. For every recipient the system runs a research pass ("background check") on public information about that person and their company, writes an email tailored to them, and sends it from the user's own Gmail at a safe pace. Replies, bounces, and unsent emails are tracked automatically and surfaced as a live counter in the app and as notifications.

## 2. Who it is for

- Founders and freelancers sending inquiries, partnership asks, or sales emails.
- Job seekers reaching recruiters and hiring managers.
- Researchers, journalists, or students contacting many experts.
- Anyone who today copies and pastes the same email 200 times and gets ignored.

The common thread: they already have a list of names, they want each email to feel hand-written, and they want to know who answered without digging through their inbox.

## 3. Core user flow

1. **Sign in with Google.** OAuth asks for permission to send mail and read mail metadata. Tokens are encrypted at rest.
2. **Set up a sender profile.** Name, role, company, LinkedIn URL, website, a short "what I do" blurb, and the goal of the campaign (what a good outcome looks like).
3. **Create a campaign.** Give it a name and a goal. Paste or upload recipients as a CSV: at minimum an email address, ideally also name, company, title, and LinkedIn URL.
4. **Research runs in the background.** For each recipient the system collects public signals (see section 5) and stores a short "dossier".
5. **Drafts are generated.** One email per recipient, written from the sender profile plus the dossier. The user can review a sample, edit the tone, and either approve all or approve one by one.
6. **Sending is scheduled.** A queue sends at a controlled rate from the user's Gmail, inside their chosen hours and time zone, with randomized gaps between sends.
7. **Tracking kicks in.** The system watches the Gmail inbox for replies on the threads it created, detects bounces, and updates counters: sent, remaining, replied, bounced, failed.
8. **Notifications.** A banner in the app, an optional browser push, a daily summary email, and optionally a Slack message when someone replies.
9. **Follow-ups (phase 2).** If no reply after N days, send a short follow-up in the same thread, up to a configurable number of touches. Stop immediately when anyone replies.

## 4. Key features, grouped

### Must have (MVP)
- Google sign-in and Gmail connection.
- Sender profile.
- Campaign with CSV upload and column mapping.
- Research step per recipient (company website first; LinkedIn and others as available).
- AI drafting with a short, plain-text style and a required unsubscribe or opt-out line.
- Review screen with "approve all" and per-email edit.
- Send queue with rate limits, sending windows, and pause/resume.
- Reply and bounce detection on the campaign's threads.
- Dashboard counters: queued, sent, failed, bounced, replied, reply rate.
- In-app banner plus daily email digest.

### Should have (v1.1)
- Automatic follow-up sequences in-thread.
- Reply classification: interested, not now, wrong person, unsubscribe, out of office.
- AI-drafted reply suggestions the user approves before sending.
- Suppression list (never email these addresses again) and global unsubscribe handling.
- Export of results to CSV or Google Sheets.

### Nice to have (later)
- Multiple connected mailboxes per user, with round-robin sending to raise daily capacity.
- Microsoft 365 / Outlook support.
- Team workspaces and shared campaigns.
- Email address finding and verification when the user only has a name and company.
- A/B testing of openers.
- CRM sync (HubSpot, Pipedrive).

## 5. What "background check" means in practice

The research step gathers only public, professional information. Sources in rough order of value and ease:

| Source | What it gives | Access method | Notes |
|---|---|---|---|
| Company website | What they do, recent news, team page, careers, tech used | Fetch and parse | Free, reliable, no terms-of-service risk |
| Search engine results | Recent articles, talks, podcasts | Serper, Brave Search API, or similar | Cheap paid API |
| LinkedIn | Title, tenure, past roles, posts | Third-party enrichment API (Proxycurl, Apollo, People Data Labs) | Scraping LinkedIn directly violates its terms and gets accounts banned. Use a licensed provider or let the user paste the profile text. |
| GitHub, personal site, X, Substack | Interests and writing style | Public APIs or fetch | Only when the recipient links them |
| Crunchbase, funding news | Stage, recent raise | Paid API or search | Good trigger for timing |
| Email verification | Does the address exist | ZeroBounce, NeverBounce, or SMTP check | Protects sender reputation |

The output of research is a compact dossier: 5 to 10 bullet facts, 2 to 3 candidate "hooks" the email could open with, and a confidence score. Low-confidence recipients get a less specific email rather than an invented fact. The drafting prompt must be told never to assert anything not in the dossier.

## 6. The 10,000 email question

The goal is "no practical limit, up to 10,000 per campaign". The system should accept that many recipients, but sending them is bounded by Gmail, not by the app:

| Account type | Hard Google cap per day | Safe cold-outreach rate per day |
|---|---|---|
| Free @gmail.com | 500 | 20 to 40 |
| Google Workspace | 2,000 | 50 to 150, after warm-up |

Sending cold email near the hard cap from a single mailbox gets the account flagged within days. The product should therefore:

- Accept the full list and show a projected completion date based on the user's daily limit.
- Default to a conservative daily limit and let the user raise it with a warning.
- Support connecting several mailboxes and spreading sends across them (later phase).
- Hard stop the campaign when the bounce rate crosses a threshold (for example 5 percent).
- Recommend, in onboarding, that high-volume users send from a secondary domain rather than their primary one.

So: 10,000 recipients is fine as a campaign size. 10,000 sends is a multi-week schedule for one mailbox, or a multi-mailbox feature.

## 7. Compliance and trust

- **CAN-SPAM (US):** accurate sender identity, a physical postal address, and a working opt-out honored within 10 days. Build the opt-out line and address into every template.
- **GDPR / PECR (EU, UK):** B2B cold email is allowed under legitimate interest in many cases but recipients must be able to object easily, and you should not process more data than needed. Keep dossiers minimal and deletable.
- **Google OAuth verification:** the Gmail send and read scopes are "restricted" scopes. Before the app can be used by the public, Google requires a verification review and, for restricted scopes, a third-party security assessment (the CASA process). Budget time and some money for this before launch. During development the app works for test users without it.
- **Google API Services User Data Policy:** you may not use Gmail data to train general models or sell it. Document this in the privacy policy.
- **Transparency to the user:** show them exactly what the research step found before anything is sent. Nothing sends without an approval step the user can turn off deliberately.

## 8. Suggested architecture

```
Browser (Next.js or React)
   |
API server (TypeScript/Node or Python/FastAPI)
   |-- Auth: Google OAuth, encrypted token store
   |-- Postgres: users, profiles, campaigns, recipients, dossiers, drafts, sends, events
   |-- Job queue (Redis + BullMQ, or Postgres-backed queue)
         |-- research worker   -> fetch sources, call LLM to build dossier
         |-- drafting worker   -> call LLM with profile + dossier -> draft
         |-- sending worker    -> Gmail API users.messages.send, rate limited per mailbox
         |-- inbox watcher     -> Gmail push notifications (Pub/Sub) or polling of thread IDs
   |-- Notifications: in-app events table, web push, email digest, Slack webhook
```

Reply detection detail: every sent message gets its Gmail `threadId` stored. The watcher either subscribes to Gmail push notifications via Google Pub/Sub or polls `history.list` for changes, then checks whether any new message in a tracked thread has a sender other than the user. Bounces arrive as messages from `mailer-daemon` referencing the original message ID.

LLM: one model for research summarization and drafting is enough at the start. Keep prompts in version-controlled files so tone can be tuned without code changes.

## 9. Open-source repositories to build on

Searched GitHub on 2026-10-19. Ranked by fit with this idea.

| Repo | Stars | License | Why it is relevant | Gaps for this project |
|---|---|---|---|---|
| [ethanplusai/harvey](https://github.com/ethanplusai/harvey) | ~1,580 | MIT | Closest match. Python, Gmail API sending, free per-prospect profiler that reads company sites, writes personalized first lines, reply handler that classifies intent and detects bounces, bounce kill switch, local dashboard with queue and conversations. | Single-user, runs locally, driven by Claude Code or Codex CLI rather than an API. Needs a multi-user web layer and standard LLM API calls to become a hosted product. |
| [Kandid-ai/AgentSDR](https://github.com/Kandid-ai/AgentSDR) | ~43 | MIT | Modern Next.js + Postgres stack, sequences, AI reply classification with drafted answers, "action required" queue, analytics. Good reference for the data model and dashboard. | Connects Google Workspace via service account with domain-wide delegation, not per-user OAuth. Weak on first-touch research. |
| [warmbly/warmbly](https://github.com/warmbly/warmbly) | ~460 | Apache 2.0 | Production-grade sending engine in Go: multi-step sequences, per-mailbox caps, warmup, unified inbox, bounce and complaint handling, CRM, webhooks. | Heavy stack (Go, Postgres, Redis, event bus). No AI research or personalization layer. Best as a sending backend if the project grows. |
| [dilpreet92/coldtrail](https://github.com/dilpreet92/coldtrail) | ~17 | MIT | Single Rust binary, Gmail via OAuth or app password, AI-written subject and body per company, pipeline UI. | Solo-maintainer, company-level not person-level research, manual reply marking. |
| [ahanikotb/openoutreach](https://github.com/ahanikotb/openoutreach) | ~8 | not stated | Minimal Gmail-based Lemlist clone in Go with thread reply checking and follow-up scheduling. Small enough to read in an afternoon. | Very early, 5 commits, no license, no AI. |

Also worth skimming: [buyukakyuz/email-sleuth](https://github.com/buyukakyuz/email-sleuth) for finding and verifying addresses, and [emelia-io/claude-outreach](https://github.com/emelia-io/claude-outreach) for a well-structured set of outreach prompts and a reply-handling playbook.

### Recommendation

Fork **harvey** as the starting point. It already implements the three hard parts of this idea: research-based personalization, Gmail API sending with safety controls, and reply/bounce handling. The work then becomes:

1. Replace the Claude Code / Codex CLI dependency with direct LLM API calls.
2. Put a multi-user web app with Google sign-in in front of it (profiles, campaigns, per-user Gmail tokens).
3. Add the counters, banner, and notification layer.
4. Borrow AgentSDR's reply classification UI and data model.

If you would rather own every line, build fresh in TypeScript on the architecture in section 8 and use harvey and AgentSDR as reference implementations.

## 10. Risks and how to handle them

| Risk | Mitigation |
|---|---|
| Gmail accounts get suspended for spam | Conservative defaults, warm-up guidance, bounce kill switch, verified addresses only |
| LinkedIn blocks scraping | Use licensed enrichment APIs or user-pasted profiles. Never automate LinkedIn logins. |
| The AI invents facts about a recipient | Dossier-only prompting, confidence score, mandatory review of a sample before bulk approval |
| Google OAuth review delays launch | Start the verification and CASA process early; run a closed beta with test users meanwhile |
| Legal exposure (spam laws) | Opt-out link and postal address in every email, suppression list, data deletion on request |
| Users expect 10,000 sends in a day | Show a projected schedule up front; make multi-mailbox a paid or later feature |

## 11. Suggested milestones

1. **Week 1 to 2:** Google sign-in, Gmail connection, send a single hand-written email from the app, store thread ID, detect a reply. Proves the hardest integration.
2. **Week 3 to 4:** Campaign + CSV upload, research worker against company websites only, AI drafting, review screen.
3. **Week 5 to 6:** Rate-limited queue, bounce handling, dashboard counters, in-app banner, daily digest email.
4. **Week 7 to 8:** LinkedIn enrichment via a provider, reply classification, follow-up sequences. Submit Google OAuth verification.
5. **After:** Multiple mailboxes, team features, CRM sync.

## 12. Open questions for you

- Is this a personal tool, or a product other people sign up for? That decides whether to fork harvey as-is or build the multi-user layer.
- Which LLM provider do you want to standardize on, and do you want users to bring their own key?
- Do you want every email approved before sending by default, or fully automatic by default with approval as an option?
- Budget for enrichment data (LinkedIn via a provider costs roughly a few cents per lookup)?
- Will the first users send from free Gmail accounts or Google Workspace? This changes the safe daily limits.
