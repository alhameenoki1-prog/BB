# Queue attempt fencing — current implementation and remaining risk

Status: **attempt-number fencing implemented for participating workers; strict enforcement remains incomplete**. This is not release approval. Verified against `bb/mail/jobs.py` on the feature branch on 2026-11-21.

## Implemented behavior

- `claim()` increments `attempts` atomically when moving a due queued job to running.
- `progress()`, `complete()`, `fail()`, and `release()` accept `expected_attempt`. When supplied, their SQL guards prevent callbacks from an earlier attempt from modifying a newer running attempt.
- `complete()` serializes progress read/merge and completion inside a `BEGIN IMMEDIATE` transaction.
- `retry()` keeps attempt numbers monotonic after manual review instead of resetting them.
- Stale `send_draft` and `calendar_invite` jobs are quarantined for review, not automatically redelivered; safe non-delivery jobs can be requeued.
- Isolated SQLite tests cover stale callbacks, manual review, mixed recovery, retry backoff, and claim concurrency.

## Remaining risk — optional guard

All four worker-originated mutation methods still default `expected_attempt=None`, and their predicates allow that value to bypass the attempt check. An unguarded caller could mutate a newly claimed attempt by job ID alone. Attempt numbers are sufficient to distinguish successive claims only if **every** worker-originated call supplies the expected value. Do not describe the present design as unconditional fencing.

Before promoting PR #3 from draft:

1. Audit every call site of `Jobs.progress`, `Jobs.complete`, `Jobs.fail`, and `Jobs.release`, including cancellation, worker shutdown, calendar and delivery paths.
2. Require an attempt identifier for worker callbacks, removing the optional bypass (or separate explicitly authorized administrative transitions). Make a missing or mismatched identifier fail closed, with a documented stale result.
3. Add a negative test showing an omitted identifier cannot mutate a running job, plus cross-worker stale callback tests for each transition.
4. Confirm all Windows, macOS and Linux CI jobs pass on the **new exact head**. Preserve authenticated Gmail, Graph and SMTP behavior.

## Delivery semantics

Attempt fencing prevents stale *local* worker callbacks; it cannot prove whether a remote SMTP/API service accepted an interrupted delivery. Keep uncertain sends and invitations in manual review. Do not automatically retry ambiguous deliveries or claim exactly-once external delivery.

## Scope constraints

Use only local SQLite, fake workers, mock sinks and reserved `.test` addresses for tests. Do not merge, deploy, send real mail, access secrets, or modify production settings without explicit approval.
