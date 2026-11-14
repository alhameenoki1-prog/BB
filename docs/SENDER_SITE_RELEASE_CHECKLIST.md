# BB sender: existing-site release gate

This checklist applies to the existing BB web application and its `/mail` pages. It is **not** permission to deploy, send live email, or change production configuration. PR #3 stays draft until all required checks are evidenced.

## Scope and user journey

1. Log in as a test user to the existing site. An anonymous visitor must be redirected from `/mail`, `/mail/accounts`, and `/mail/jobs`.
2. Connect a **test-owned** Gmail, Microsoft Graph, or SMTP mailbox through the existing account flow. Verify account ownership and scopes; never ask a user to paste a normal account password into a bulk-import flow.
3. Confirm the connected account is visible only in its owner's workspace, and the worker state, error, and reconnect instructions are visible.
4. Draft a message addressed only to `recipient@example.test`. Require explicit recipient authorization and an approval action before the send is queued.
5. Route transport calls to a localhost mock sink. Confirm one accepted message, a visible job record, and no live network delivery.
6. Exercise rejected credentials, transient connection failures, duplicate enqueue, two simultaneous workers, cancellation, and worker restart. Verify a possibly accepted send is quarantined for human review, never silently replayed.
7. Verify unsubscribe/suppression enforcement, rate caps, audit records, and safe secret handling before enabling any real recipient or production transport.

## Reproducible pre-release verification

From a fresh checkout of the feature branch, in a disposable environment with no production credentials:

```sh
python -m pip install -e ".[dev]"
python -m compileall -q bb harvey
python -m pytest -q tests/test_bb_web.py tests/test_queue_recovery_boundaries.py tests/test_queue_delivery_recovery.py tests/test_removed_simulation.py
python -m pytest -q tests/test_mailpit_transport_integration.py tests/test_smtp_failure_classification.py
```

The full GitHub Actions matrix must also complete successfully, including Windows Python 3.11 and 3.12 shard 3. A local-only sender evaluation passing is necessary but **does not prove** Gmail/Graph authorization, inbox placement, or live delivery.

## Rollout decision

- **Blocked:** CI still running/failing, sender UI tests incomplete, account ownership or consent not verified, or mock transport unavailable.
- **Reviewable:** All tests pass, owner-scoped website flow is demonstrated, and the release diff is reviewed.
- **Deployable only after explicit authorization:** Deployment target, credentials source, DNS/domain authentication, provider quotas, suppression policy, monitoring, rollback plan, and an approved test-recipient list are documented.

Keep Gmail, Graph, and SMTP adapters intact. Do not restore credential-harvesting simulations, reusable session-token capture, cloned login pages, or quota-evasion transports.
