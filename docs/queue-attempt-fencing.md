# Queue attempt fencing — implementation plan

Status: **not implemented**. This document records a remaining reliability risk in `bb/mail/jobs.py`; it is not a release approval.

## Observed code behavior

`Jobs.claim()` atomically changes a queued row to running and increments `attempts`. However, `Jobs.progress()`, `Jobs.complete()`, `Jobs.fail()`, and `Jobs.release()` accept only a job ID and predicate on `status = 'running'`. After a worker loses its claim, a newer worker can claim the same job. An old worker's delayed callback may then see the newer `running` state and mutate the new attempt. The existing late-ack tests protect terminal/queued states, **not** a newly running attempt. This is especially important for retries and manual review.

## Required change

1. Add a unique opaque claim token (or monotonically increasing lease generation) to each job claim, returned to the worker alongside the job ID. Create the token within the same atomic transaction as the claim; a new claim must always get a new token, including after manual review.
2. Require the token for every worker-originated progress, completion, failure, and release transition. All writes must use `WHERE id = ? AND status = 'running' AND claim_token = ?`. Do not fall back to job-ID-only mutation on a token mismatch.
3. Propagate the token through worker call sites. Keep human retry/discard operations separately authorized and transactionally guarded; they must invalidate any previous claim.
4. For completion, merge progress inside the same guarded transaction to prevent a read-modify-write race. Return an explicit stale-claim result rather than reporting success when no row changes.
5. Preserve interrupted `send_draft` and `calendar_invite` quarantine: claim fencing does **not** make external SMTP/Graph acceptance exactly-once. Ambiguous deliveries still require human review before retry.

## Isolated acceptance tests

Use a temporary SQLite database, fake workers, and reserved `.test` mailbox identities; no provider network calls:

- Worker A claims a safe mailbox job; its lease expires; worker B reclaims it. A's progress, complete, fail, and release calls must not change B's running row or audit metadata.
- Worker B completes successfully with its own token. A's late callbacks must not alter the completed row.
- A stale send or invitation is quarantined rather than automatically reclaimed. A reviewed retry obtains a different claim token; A's delayed callback cannot affect that retry.
- Concurrent worker claims cannot share a token or both succeed for one job; unrelated lanes can still progress.
- Existing Gmail, Graph, SMTP, retry/backoff, cancellation, and cross-platform test suites remain green.

## Rollout constraints

Ship token schema migration, worker call-site updates, and tests in one reviewed change; do not introduce a partial optional-token compatibility path that silently bypasses fencing. Run the complete Windows/macOS/Linux matrix. Keep PR #3 draft, and do not merge, deploy, access secrets, or send real email without separate approval.
