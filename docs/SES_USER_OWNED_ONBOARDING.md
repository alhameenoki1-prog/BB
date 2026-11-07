# BB SES — user-owned AWS onboarding (implementation in progress)

## Ownership and authorization
Each BB workspace connects only its own AWS account. Do **not** collect AWS console passwords, MFA codes, root credentials, or user access keys through a login form. Offer an AWS-hosted CloudFormation/IAM setup link for a narrowly scoped cross-account role with a unique ExternalId and short-lived STS AssumeRole credentials. Verify role ownership and store only role ARN, ExternalId (secret), region and configuration references. Per-user tenant boundaries must be enforced on every route, background job and callback. Do not automatically create an AWS account.

## Target experience
1. User chooses **Amazon SES** from `/mail/accounts` and supplies their owned sending domain and desired daily volume (default 5,000 recipients).
2. BB shows an official AWS setup link for a least-privilege role, with a reviewable permissions template. User approves it in AWS; BB verifies AssumeRole and retrieves `sesv2:GetAccount`.
3. BB creates or checks an SES domain identity; displays DKIM DNS records. Publish DNS automatically **only** if the user separately authorizes a compatible DNS provider. Verify SPF and DMARC independently.
4. BB collects truthful use-case, website, consent and complaint-handling information; presents it to the user for review. Request production access via `sesv2:PutAccountDetails` only after explicit confirmation. AWS alone decides approval and may require manual review.
5. BB reads actual regional quota and rate from `sesv2:GetAccount`; never display an assumed 5k/day entitlement. If quota is insufficient, guide the user through an AWS quota increase request where available.
6. Configure SES configuration set, SNS event destination and a signed SNS subscription endpoint. Verify SNS signatures and subscription requests, deduplicate events, correlate SES message IDs, and suppress hard bounces and complaints.
7. Test via SES simulator or owned consented recipients, then activate only after verified identity, production access, quota, sending approval and operational monitoring.

## Transport architecture
- The first code slice is `bb/mail/ses_transport.py`, an **isolated SES v2 API transport** with quota discovery, identity checks and raw MIME submission. It is **not yet wired into live sending**.
- Add `boto3` as an optional dependency before enabling this transport; do not make it mandatory for all BB users without a dependency decision.
- The existing `MailAccount` interface includes backfill and sync; SES cannot implement those. Introduce a send-only transport abstraction, preserving Gmail/Graph/IMAP inbox connections.
- The existing unique mailbox-address constraint prevents two providers for one sender address. Create a separate user-owned transport registry, keyed by workspace and transport ID, before connecting SES to the mail UI.
- Reuse existing approved-draft checks, queue, sending windows, suppression, logging and interrupted-send quarantine. Avoid blind retries on ambiguous acceptance.
- Use a dedicated provider event ledger, with provider message ID, recipient, type, timestamp and unique provider event ID; distinguish API acceptance from remote-server delivery.
- For optional SES SMTP, generate region-specific SMTP credentials only with separate explicit IAM authorization and encrypted storage. Prefer SES API and temporary role credentials.
- For outage failover, use only previously verified, approved regional identities and quotas; never route around an account suspension or reputation restriction.

## Cost and quota
AWS pricing, free tiers and region support change. Fetch current authoritative rates or link AWS pricing; show an estimate with assumptions and ancillary charges. Sending limits are recipient-based and rolling-24-hour, plus per-second caps. Requesting 10,000/day to support a 5,000/day target is **not a guarantee**. Do not use purchased/prewarmed SES accounts.

## Release gates
- Unit tests for quota, identity, send classification, region isolation and no-network behavior.
- Mocked role-assumption, account ownership, least-privilege authorization, DNS verification and production-request approval tests.
- SNS signature verification, duplicate events, malformed payloads, replay prevention, bounce/complaint suppression and message correlation tests.
- Integration with existing queue and website only after review, followed by full CI.
- Keep PR #3 draft, and do not deploy or send real mail without explicit authorization.
