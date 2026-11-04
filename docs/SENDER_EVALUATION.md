# BB sender research and acceptance gates

Research date: 2026-11-04. This is an evaluation plan, **not** a deliverability benchmark. No public-inbox mail has been sent or new transport integrated.

## Verified local test results (2026-11-04)

| Check | Result | Scope |
|---|---|---|
| Existing BB SMTP adapter -> Mailpit | PASS: 40/40 local submissions; 0.241s on one CI runner | Local sink only; no inbox placement |
| SMTP failure classification, authentication and partial recipient handling | PASS: 11/11 total sender tests including Mailpit scenarios | Mocked/isolated responses, no public mail |
| KumoMTA Docker SMTP null-queue listener | PASS: 12/12 acknowledgements; 0.027s | Kumo accepted and discarded messages |
| KumoMTA Docker HTTP `/api/inject/v1` | PASS: 10/10; 0.012s | API injection accepted and discarded messages |
| Ubuntu/Python 3.11 and 3.12 full BB regression (earlier head) | PASS | Latest exact-head result still pending |
| macOS/Python 3.11 and 3.12 full BB regression (earlier head) | 998 passed, 1 flaky test failed | Invalid 2-character ciphertext substring test fixed; rerun pending |
| Windows/Python 3.11 and 3.12 | Baseline platform failures remain | Unix-only process signals, tunnel mocks, path separators |
| Gmail/Outlook/Yahoo inbox placement | NOT TESTED | Requires controlled seeds and owned domains |
| Postal/Hyvor runtime and spooling | NOT TESTED | Separate service/dependency setup needed |

Evidence: [BB sender workflow run](https://github.com/alhameenoki1-prog/BB/actions/runs/41855649544), [SMTP classification workflow](https://github.com/alhameenoki1-prog/BB/actions/runs/41834111116), [full BB regression](https://github.com/alhameenoki1-prog/BB/actions/runs/41834111302).

**Decision:** KumoMTA passes the isolated SMTP and HTTP injection gates. It is approved for *further non-production evaluation only*, not production, until queue durability, crash recovery, performance under sustained load, cost and controlled public-inbox placement are verified. Avoid interpreting fast sink acknowledgements as deliverability or full end-to-end throughput.

## Current BB
- Keep Gmail API, Microsoft Graph and authenticated IMAP/SMTP adapters until an alternative beats them in a controlled trial.
- `bb/mail/sending.py` delegates to `bb/mail/providers`; `bb/mail/worker.py` manages jobs.
- Fix the pre-existing test-suite and Python 3.11 compatibility issues before comparing provider integrations.

## Candidate repos

| Project | Category | License (repo metadata) | Candidate use | Decision |
|---|---|---|---|---|
| [Postal](https://github.com/postalserver/postal) | Self-hosted outbound email server | MIT | HTTP submission + delivery/bounce webhooks | Pilot |
| [KumoMTA](https://github.com/KumoCorp/kumomta) | High-throughput outbound MTA | Apache-2.0 | HTTP injection, durable event hooks, direct-to-MX | Pilot |
| [HYVOR Relay](https://github.com/hyvor/relay) | Self-hosted delivery API | AGPL-3.0 | Idempotent REST API, transaction/campaign queues, bounces, DNS checks | Pilot (license review) |
| [ZoneMTA](https://github.com/zone-eu/zone-mta) | Outbound MTA | EUPL-1.2 | HTTP submission, MX delivery, queue and transport control | Alternate pilot |
| [Mox](https://github.com/mjl-/mox) | Full mail server | MIT | Small-scale self-hosted delivery; `localserve` failure simulator | Pilot as test tool |
| [Stalwart](https://github.com/stalwartlabs/stalwart) | Full mail server | AGPL/commercial dual | JMAP HTTP submission, standard SMTP MTA | Evaluate for provider compatibility |
| [Haraka](https://github.com/haraka/Haraka) | Pluggable SMTP server | MIT | Direct-to-MX, extensible inbound/outbound hooks | Evaluate only |
| [Mailpit](https://github.com/axllent/mailpit) | Local delivery sink | MIT | SMTP capture and configurable failure simulation | Pilot as test tool |
| [useSend](https://github.com/usesend/usesend) | SES application front end | AGPL-3.0 | Study delivery status and campaign UI | Reference only |
| [Plunk](https://github.com/useplunk/plunk) | SES application front end | AGPL-3.0 | Study provider API and campaign orchestration | Reference only |
| [listmonk](https://github.com/knadh/listmonk) | Newsletter manager | AGPL-3.0 | Study batching and subscription management | Reference only |
| [Maddy](https://github.com/foxcpp/maddy) | Full mail server | GPL-3.0 | SMTP and IMAP | Lower priority |

Cloud baseline to compare with independent servers: [Amazon SES HTTP API](https://docs.aws.amazon.com/ses/latest/dg/send-email-api.html). Current published SES pricing: [pricing](https://aws.amazon.com/ses/pricing/). New SES accounts have sandbox quotas and recipient restrictions, and production sending requires approved access.

## Important protocol distinction
- HTTP, REST, and JMAP are *submission methods* from BB to a service. For normal public email, a downstream mail server still typically delivers via SMTP to the destination MX.
- ZoneMTA's delivery-to-HTTP targets custom webhook endpoints. It is **not** a generic replacement for sending to arbitrary public mailboxes.
- Self-hosted direct-to-MX eliminates per-message relay charges but incurs server/IP/DNS/TLS/reputation/abuse-handling costs. No project has a guaranteed inbox rate.
- Track `queued`, `accepted by transport`, `accepted by destination MX`, `deferred`, `bounced`, `complained`, and `observed inbox/spam` as distinct states.

## Evaluation stages

### Stage 0: BB baseline
- Green CI on supported Python versions and three target operating systems.
- Existing Gmail/Graph/IMAP adapters pass their tests without sending email on the network.
- All existing code removal tests pass.

### Stage 1: Static audit
- License compatible with BB's intended distribution and hosted deployment.
- Maintained upstream, documented installation and API, signed webhook support or documented delivery events.
- Run without hardcoded credentials, insecure default public relays or unprotected management surfaces.

### Stage 2: Local integration, zero internet recipients
- Isolated Docker test infrastructure. Send a batch of 1,000 distinct test messages to Mailpit.
- Inject transient 421/450/451 and permanent 550 failures; simulate timeouts and worker restarts.
- Verify no messages lost; no duplicate deliveries after ambiguous provider acknowledgements when idempotency is available.
- Verify retry backoff, quarantine and suppression, event correlation and secret redaction.
- Record acceptance throughput, queue latency (p50/p95), CPU/memory, operational setup time, and dependency count.

### Stage 3: Authenticated sender readiness
- Domain ownership verification, aligned SPF/DKIM/DMARC, proper reverse DNS, TLS, RFC 5322 content, non-open-relay behavior.
- Complaint feedback loop or webhook, hard-bounce suppression, one-click unsubscribe for subscribed marketing email.
- No quota bypass, rotation to evade blocks, or spoofed authentication.

### Stage 4: Controlled live inbox evaluation
- Use only operator-owned sending domains and controlled/consenting seed inboxes on Gmail, Outlook, Yahoo and other providers.
- Keep sender domain, legitimate content class, subject, identity and recipient cohort comparable, and run multiple days.
- Observe inbox vs junk vs rejected vs absent; separate recipient-MX acceptance from observed placement.
- Do not extrapolate a strong inbox-placement probability from a few seed messages.
- Record actual monetary cost per 1,000, operational cost, reputation/abuse monitoring and deliverability incidents.

### Decision
- A **real** test result is required before marking a transport `Adopt`.
- Use Mailpit as a development dependency only.
- Start with SES API as an operational cost/delivery baseline, and test Postal, HYVOR Relay, KumoMTA. Keep ZoneMTA as an alternate.
- Prefer only one self-hosted MTA in production; avoid shipping multiple overlapping sending stacks.
- Do not merge deployment or sending integrations until build/tests, domain controls, license review and controlled placement observations are complete.

## Primary sources
- [KumoMTA HTTP injection](https://docs.kumomta.com/userguide/operation/httpinjection/)
- [Postal HTTP API](https://docs.postalserver.io/developer/api/)
- [Postal webhook statuses](https://docs.postalserver.io/developer/webhooks/)
- [HYVOR Relay API and idempotency](https://relay.hyvor.com/docs/send-emails)
- [Mox localserve](https://github.com/mjl-/mox/blob/main/doc.go)
- [Mailpit local sink and chaos tests](https://github.com/axllent/mailpit)
- [Gmail sender requirements](https://support.google.com/mail/answer/14229414)
- [Yahoo sender requirements](https://senders.yahooinc.com/best-practices/)
