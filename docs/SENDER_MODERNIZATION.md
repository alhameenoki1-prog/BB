# BB sender modernization — implementation baseline

This branch removes the credential-collection simulation and consolidates the duplicated BB application module setup. The existing Gmail, Microsoft Graph and IMAP/SMTP mailbox adapters remain intact.

## Design goals

- Keep mail transport independent of campaign logic and provider credentials.
- Introduce optional, individually configured provider adapters after testing, including managed email APIs and conventional SMTP relays.
- Add durable idempotency keys, bounded retry logic, actual provider delivery states, feedback-loop processing, and suppression lists.
- Model cost per thousand accepted messages and costs by provider without mistaking acceptance for inbox delivery.
- Build dry-run benchmarks against a local test sink before enabling new transports.
- Retain mailbox ownership verification, authenticated connection, provider quotas and permission-based campaigns.
- Treat a direct-to-MX MTA as separately operated infrastructure with genuine DNS, TLS, hosting and reputation requirements; do not enable it as a quota or abuse-control bypass.

## Existing code entry points

- `bb/mail/sending.py`: validates a draft and delegates delivery to a connected provider.
- `bb/mail/providers/base.py`: MailAccount interface and MIME creation.
- `bb/mail/providers/__init__.py`: provider dispatch.
- `bb/mail/providers/gmail.py`: Gmail API adapter.
- `bb/mail/providers/graph.py`: Microsoft Graph adapter.
- `bb/mail/providers/imap.py`: IMAP and SMTP adapter.
- `bb/mail/worker.py`: worker queue runner.

## Release checks

Run `python -m compileall -q bb harvey` and `pytest -q`, including `tests/test_removed_simulation.py`. Do not merge on the basis of unrun tests. Existing per-user workspace data is not deleted by this branch; any historical captured material requires a separate, controlled data-retention review and purge.
