"""BB — the hosted, multi-user web app around Harvey.

One install serves many users. Each user gets an isolated Harvey home
(``HARVEY_HOME``: its own harvey.yaml, SQLite database, Gmail token and
log) under ``BB_HOME/workspaces/<user id>``, and a heartbeat worker process
that runs only against that home. The web app is the control plane:
accounts the admin creates, a profile form that writes harvey.yaml, Gmail
OAuth over the web redirect flow, recipient upload, outbox review, replies,
and live counters.
"""
