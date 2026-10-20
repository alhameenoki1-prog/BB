"""The mail command center: several mailboxes per user, a local mirror of
every message, reply state per thread, reply runs, and a command bar.

Packages: ``db`` (per-user SQLite in WAL mode), ``crypto`` (credentials
at rest), ``jobs`` (the SQLite job queue with retries and a dead-letter
list), ``providers`` (Gmail, IMAP/SMTP, Microsoft Graph adapters),
``service`` (what the web app and the worker share), ``worker`` (the
background process), ``routes`` (the pages under /mail).
"""
