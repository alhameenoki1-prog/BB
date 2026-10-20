"""What every mailbox adapter provides."""

from dataclasses import dataclass, field


class AuthError(Exception):
    """The credentials no longer work: the account needs reconnecting."""


class TransientError(Exception):
    """Network, rate limit, outage: try again later."""


@dataclass
class RawMessage:
    provider_id: str
    thread_key: str                 # provider thread id, or the first Message-ID of the chain
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    from_email: str = ""
    from_name: str = ""
    to: list = field(default_factory=list)
    cc: list = field(default_factory=list)
    subject: str = ""
    body: str = ""
    snippet: str = ""
    headers: dict = field(default_factory=dict)
    folder: str = ""
    sent_at: str = ""
    outbound: bool = False


class MailAccount:
    name = "base"

    def __init__(self, mailbox: dict, creds: dict, env: dict):
        self.mailbox = mailbox
        self.creds = creds
        self.env = env
        self.address = (mailbox.get("address") or "").lower()

    async def test(self) -> str:
        """Verify the credentials. Returns a note for the page; raises
        AuthError when the account must be reconnected, TransientError
        when the provider could not be reached."""
        raise NotImplementedError

    async def backfill(self, since_days: int, on_batch) -> dict:
        """Phase 2: mirror the mailbox. ``on_batch(list[RawMessage], done, total)``."""
        raise NotImplementedError

    async def sync(self, state: dict, on_batch) -> dict:
        """Phase 2: incremental sync from ``state``; returns the new state."""
        raise NotImplementedError

    async def send(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", attachments=None) -> str:
        """Phase 5: send; returns the Message-ID."""
        raise NotImplementedError
