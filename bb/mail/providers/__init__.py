"""Mailbox adapters. Each knows how to test a connection, back-fill, sync
incrementally and send for one kind of account."""

from bb.mail.providers.base import AuthError, MailAccount, TransientError  # noqa: F401


def account_for(mailbox: dict, creds: dict, env: dict) -> "MailAccount":
    provider = mailbox.get("provider")
    if provider == "gmail":
        from bb.mail.providers.gmail import GmailAccount
        return GmailAccount(mailbox, creds, env)
    if provider == "imap":
        from bb.mail.providers.imap import ImapAccount
        return ImapAccount(mailbox, creds, env)
    if provider == "graph":
        from bb.mail.providers.graph import GraphAccount
        return GraphAccount(mailbox, creds, env)
    raise ValueError(f"unknown provider {provider!r}")
