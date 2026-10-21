"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages and marks them seen after retrieval.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import aiosmtplib

from harvey.integrations.mail_provider import (
    add_deliverability_headers,
    attach_files,
    set_body,
    InboundMessage,
    MailProvider,
    MailReadError,
    SendResult,
    is_relay_host,
    looks_like_bounce,
)

logger = logging.getLogger("harvey.smtp")


DAILY_LIMIT_WORDS = ("sending limit", "limit for sending", "5.4.5", "quota exceeded", "daily user sending")
AUTH_WORDS = ("username and password not accepted", "authentication", "invalid credentials", "5.7.8", "5.7.9",
              "application-specific password", "sign-in", "login failed")


def classify_smtp_error(e: Exception) -> dict:
    """What an SMTP failure means for the queue: {retryable, scope, auth_failed, daily_limit}.
    4xx and network trouble are temporary; a recipient-level 4xx (greylisting,
    a full mailbox) touches only that row; throttling, connect errors and the
    daily ceiling stop the pass; a refused login stops the pass and is never a
    retry count against the email."""
    text = str(e).lower()
    out = {"retryable": False, "scope": "row", "auth_failed": False, "daily_limit": False}
    if isinstance(e, aiosmtplib.SMTPAuthenticationError) or any(w in text for w in AUTH_WORDS):
        return {**out, "retryable": True, "scope": "mailbox", "auth_failed": True}
    if any(w in text for w in DAILY_LIMIT_WORDS):
        return {**out, "retryable": True, "scope": "mailbox", "daily_limit": True}
    if isinstance(e, aiosmtplib.SMTPRecipientsRefused):
        codes = [getattr(r, "code", 0) for r in (getattr(e, "recipients", None) or [])]
        return {**out, "retryable": any(400 <= c < 500 for c in codes), "scope": "row"}
    code = getattr(e, "code", None)
    if isinstance(code, int):
        if 400 <= code < 500:
            # 421/454: the server is throttling the session; 450/451: usually this recipient
            return {**out, "retryable": True, "scope": "mailbox" if code in (421, 454) or "too many" in text or "try again" in text else "row"}
        return out
    if isinstance(e, (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPTimeoutError, aiosmtplib.SMTPServerDisconnected,
                      OSError, asyncio.TimeoutError)):
        return {**out, "retryable": True, "scope": "mailbox"}
    if any(w in text for w in ("try again", "throttl", "rate limit", "too many", "temporar", "4.7.0", "421", "454")):
        return {**out, "retryable": True, "scope": "mailbox"}
    return out


def _retryable_smtp_error(e: Exception) -> bool:
    return classify_smtp_error(e)["retryable"]


class SmtpImapProvider(MailProvider):
    name = "smtp"

    def __init__(self, config, env):
        self.config = config
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.reply_to = (getattr(env, "smtp_reply_to", "") or "").strip()
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        attachments: list | None = None,
    ) -> SendResult:
        persona = self.config.persona
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = f"{persona.name} <{persona.email or self.smtp_user}>"
        msg["Subject"] = subject
        message_id = make_msgid(domain=(persona.email or self.smtp_user).split("@")[-1])
        msg["Message-ID"] = message_id
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        add_deliverability_headers(msg, persona.email or self.smtp_user, self.config)
        set_body(msg, body, getattr(self.config, "outreach", None))
        attach_files(msg, attachments)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=self._start_tls(),
                use_tls=self._use_tls(),
                timeout=30,
            )
        except Exception as e:
            kind = classify_smtp_error(e)
            logger.error(f"SMTP send failed to {to_email}: {e}" + (" (temporary; will retry)" if kind["retryable"] else ""))
            return SendResult(ok=False, error=str(e)[:300], **kind)
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    def _use_tls(self) -> bool:
        return self.smtp_port in (465, 2465)

    def _start_tls(self):
        # Submission ports must upgrade (a server that drops STARTTLS gets no
        # password); elsewhere upgrade whenever the server offers it.
        if self._use_tls():
            return False
        return True if self.smtp_port in (587, 2587) else None

    def _no_inbox(self) -> bool:
        """A sending-only relay (SES, Brevo, Postmark...) has no inbox to read."""
        return is_relay_host(self.imap_host)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        if self._no_inbox():
            logger.debug(f"IMAP: {self.imap_host} is a sending relay; nothing to read.")
            return []
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_unseen, limit)
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            raise MailReadError(f"could not read the inbox at {self.imap_host}: {str(e)[:160]}") from e

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
        try:
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return []
            ids = data[0].split()[-limit:]
            for msg_id in ids:
                status, parts = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self._parse_rfc822(parts[0][1])
                if parsed:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    async def get_unanswered(self, days: int = 30, limit: int = 100) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_unanswered, days, limit)
        except Exception as e:
            logger.error(f"IMAP backlog scan failed: {e}")
            return []

    SENT_FOLDERS = ("Sent", "[Gmail]/Sent Mail", "Sent Items", "Sent Messages", "INBOX.Sent", "Sent Mail")

    def _fetch_unanswered(self, days: int, limit: int) -> list[InboundMessage]:
        """Inbox mail SINCE ``days`` ago (read or not) minus anything the
        Sent folder answers (In-Reply-To / References naming its id).
        When no Sent folder can be opened, only unread mail is returned,
        so nothing already dealt with by hand is answered twice."""
        from datetime import datetime, timedelta

        since = (datetime.utcnow() - timedelta(days=max(1, int(days)))).strftime("%d-%b-%Y")
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
        try:
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX", readonly=True)
            status, data = conn.search(None, f'(SINCE "{since}")')
            if status != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()[-limit:]
            inbox: list[InboundMessage] = []
            for msg_id in ids:
                status, parts = conn.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self._parse_rfc822(parts[0][1])
                if parsed and parsed.from_email != (self.smtp_user or "").lower():
                    inbox.append(parsed)
            if not inbox:
                return []
            answered: set[str] = set()
            sent_opened = False
            for folder in self.SENT_FOLDERS:
                try:
                    status, _ = conn.select(f'"{folder}"', readonly=True)
                except Exception:
                    status = "NO"
                if status != "OK":
                    continue
                sent_opened = True
                status, data = conn.search(None, f'(SINCE "{since}")')
                if status != "OK" or not data or not data[0]:
                    break
                for sid in data[0].split()[-500:]:
                    status, parts = conn.fetch(sid, "(BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)])")
                    if status != "OK" or not parts or not isinstance(parts[0], tuple):
                        continue
                    for token in parts[0][1].decode(errors="replace").split():
                        if token.startswith("<") and token.endswith(">"):
                            answered.add(token)
                break
            if not sent_opened:
                conn.select("INBOX", readonly=True)
                status, data = conn.search(None, f'(UNSEEN SINCE "{since}")')
                unseen_ids = set(data[0].split()) if status == "OK" and data and data[0] else set()
                return [m for i, m in zip(ids, inbox) if i in unseen_ids][:limit]
            return [m for m in inbox if not m.message_id or m.message_id not in answered]
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def _parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=self._start_tls(),
                use_tls=self._use_tls(), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        if self._no_inbox():
            return True, f"SMTP connected as {self.smtp_user}; {self.imap_host} is a sending relay, so replies are not read from it"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
