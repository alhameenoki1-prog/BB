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
                   reply_to="", from_name="", attachments=None) -> tuple[str, str, str]:
        """Send one message. Returns (Message-ID, provider id of the sent
        copy or "", provider thread key or "")."""
        raise NotImplementedError

    def build_mime(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", from_name="", attachments=None):
        import mimetypes
        from email.message import EmailMessage
        from email.utils import make_msgid
        from pathlib import Path

        msg = EmailMessage()
        msg["From"] = f"{from_name} <{self.address}>" if from_name else self.address
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1])
        if reply_to and reply_to.lower() != self.address:
            msg["Reply-To"] = reply_to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        for path in attachments or []:
            p = Path(path)
            if not p.is_file():
                continue
            ctype, _enc = mimetypes.guess_type(p.name)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        return msg
