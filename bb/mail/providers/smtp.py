"""Authenticated SMTP submission for inbox adapters and send-only accounts."""

import asyncio
import smtplib
import ssl
from email import policy

import aiosmtplib

from bb.mail.providers.base import AuthError, MailAccount, PartialDeliveryError, PermanentError, TransientError

AZURE_SMTP_HOST = "smtp.azurecomm.net"
AZURE_SMTP_PORT = 587
AZURE_MAX_RECIPIENTS = 50
AZURE_MAX_MESSAGE_BYTES = 10_000_000
AZURE_MAX_ATTACHMENT_BYTES = 7_000_000


class SmtpAccount(MailAccount):
    name = "smtp"
    max_recipients = None
    max_message_bytes = None
    authentication_required = False

    def _smtp_check(self):
        creds = self.creds
        host, port = creds["smtp_host"], int(creds.get("smtp_port") or 587)
        server = None
        try:
            context = ssl.create_default_context()
            if port in (465, 2465):
                server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=20)
                server.starttls(context=context)
            server.login(creds["username"], creds["password"])
        except smtplib.SMTPAuthenticationError as exc:
            raise AuthError(f"SMTP sign-in refused ({exc.smtp_code}). Check the username and password.") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise TransientError(f"Could not connect to {host} over SMTP ({type(exc).__name__}).") from exc
        finally:
            if server is not None:
                server.close()

    async def _record_sent_copy(self, message):
        """Only accounts with a connected inbox keep a provider-side copy."""

    async def send(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", from_name="", attachments=None, cc=None, bcc=None) -> tuple[str, str, str]:
        recipients = list(dict.fromkeys([*to, *(cc or []), *(bcc or [])]))
        if self.max_recipients is not None and len(recipients) > self.max_recipients:
            raise PermanentError(f"Azure Email allows up to {self.max_recipients} recipients per message.")
        message = self.build_mime(
            to=to, subject=subject, body_text=body_text, body_html=body_html, in_reply_to=in_reply_to,
            references=references, reply_to=reply_to, from_name=from_name, attachments=attachments, cc=cc,
        )
        if self.max_message_bytes is not None and len(message.as_bytes(policy=policy.SMTP)) > self.max_message_bytes:
            raise PermanentError("Azure Email requires the complete MIME message to be 10 MB or smaller.")
        creds = self.creds
        if self.authentication_required and (not creds.get("username") or not creds.get("password")):
            raise AuthError("Reconnect the SMTP sender with its username and password.")
        host, port = creds["smtp_host"], int(creds.get("smtp_port") or 587)
        use_tls = port in (465, 2465)
        loopback = host.lower() in ("localhost", "127.0.0.1", "::1")
        start_tls = False if use_tls else (None if loopback else True)
        try:
            refused, _response = await aiosmtplib.send(
                message, hostname=host, port=port, username=creds["username"], password=creds["password"],
                start_tls=start_tls, use_tls=use_tls, timeout=40, recipients=recipients,
            )
            if refused:
                raise PartialDeliveryError("SMTP accepted only some recipients; check delivery before resending")
        except aiosmtplib.SMTPAuthenticationError as exc:
            raise AuthError(f"SMTP sign-in refused ({exc.code}). Check the username and password.") from exc
        except aiosmtplib.SMTPRecipientsRefused as exc:
            codes = [int(getattr(item, "code", 0)) for item in exc.recipients]
            if codes and all(500 <= code < 600 for code in codes):
                raise PermanentError("SMTP permanently refused all recipients (5xx); verify addresses and content") from exc
            if any(500 <= code < 600 for code in codes):
                raise PermanentError("SMTP reported mixed permanent/temporary recipient refusals; review the recipients") from exc
            raise TransientError("SMTP temporarily refused recipients; retry after backoff") from exc
        except aiosmtplib.SMTPResponseException as exc:
            code = int(getattr(exc, "code", 0))
            if 500 <= code < 600:
                raise PermanentError(f"SMTP permanently refused the message ({code})") from exc
            raise TransientError(f"SMTP rejected the send ({code}); retry if temporary") from exc
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise TransientError(f"SMTP submission uncertain: {type(exc).__name__}") from exc
        await self._record_sent_copy(message)
        return message["Message-ID"], "", ""


class AzureSmtpAccount(SmtpAccount):
    name = "azure_smtp"
    max_recipients = AZURE_MAX_RECIPIENTS
    max_message_bytes = AZURE_MAX_MESSAGE_BYTES
    authentication_required = True

    def __init__(self, mailbox: dict, creds: dict, env: dict):
        super().__init__(mailbox, {**creds, "smtp_host": AZURE_SMTP_HOST, "smtp_port": AZURE_SMTP_PORT}, env)

    async def test(self) -> str:
        if not self.creds.get("username") or not self.creds.get("password"):
            raise AuthError("Enter the Azure SMTP username and Microsoft Entra client secret.")
        await asyncio.to_thread(self._smtp_check)
        return "Azure SMTP authentication passed. Azure checks the MailFrom address when the first message is submitted."
