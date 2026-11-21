"""What every mailbox adapter provides."""

from dataclasses import dataclass, field
import mimetypes
from pathlib import Path


class AuthError(Exception):
    """The credentials no longer work: the account needs reconnecting."""


class TransientError(Exception):
    """Network, rate limit, outage: try again later."""


class PermanentError(Exception):
    """Provider explicitly refused a message: hold for review, never auto-retry."""


class PartialDeliveryError(PermanentError):
    """The server accepted the message for only a subset of recipients."""


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes
    content_type: str
    content_id: str = ""

    @classmethod
    def from_source(cls, source):
        if isinstance(source, cls):
            return source
        path = Path(source)
        if path.is_file():
            return cls(path.name, path.read_bytes(), cls.media_type(path.name))
        return None

    @staticmethod
    def media_type(name: str) -> str:
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # The email package reserves message/* for structured MIME parts.
        return "application/octet-stream" if content_type.startswith(("message/", "multipart/")) else content_type


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
                   reply_to="", from_name="", attachments=None, cc=None, bcc=None) -> tuple[str, str, str]:
        """Send one message. Returns (Message-ID, provider id of the sent
        copy or "", provider thread key or "")."""
        raise NotImplementedError

    def build_mime(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", from_name="", attachments=None, cc=None, bcc=None):
        from email.headerregistry import Address
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid

        msg = EmailMessage()
        msg["From"] = Address(display_name=from_name or "", addr_spec=self.address) if from_name else self.address
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        msg["Date"] = formatdate(usegmt=True)
        msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1])
        if reply_to and reply_to.lower() != self.address:
            msg["Reply-To"] = reply_to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        html_part = msg.get_body(preferencelist=("html",)) if body_html else None
        for source in attachments or []:
            attachment = Attachment.from_source(source)
            if attachment is None:
                continue
            maintype, subtype = attachment.content_type.split("/", 1)
            if attachment.content_id and html_part is not None:
                html_part.add_related(attachment.data, maintype, subtype, cte="base64",
                                      cid=f"<{attachment.content_id}>", filename=attachment.name,
                                      disposition="inline")
            else:
                msg.add_attachment(attachment.data, maintype, subtype, cte="base64", filename=attachment.name)
        return msg
