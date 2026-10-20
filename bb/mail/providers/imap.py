"""IMAP + SMTP with a password (an app password at Gmail, Yahoo, iCloud).
"Limited mode": no push, no history API, so changes are found by polling
and by UID ranges."""

import asyncio
import imaplib
import smtplib
import socket
import ssl

from bb.mail.providers.base import AuthError, MailAccount, TransientError


class ImapAccount(MailAccount):
    name = "imap"

    def _imap(self):
        c = self.creds
        try:
            conn = imaplib.IMAP4_SSL(c["imap_host"], int(c.get("imap_port") or 993), timeout=20)
        except (socket.gaierror, socket.timeout, OSError, ssl.SSLError) as e:
            raise TransientError(f"could not reach {c['imap_host']}: {e}") from e
        try:
            conn.login(c.get("imap_username") or c["username"], c.get("imap_password") or c["password"])
        except imaplib.IMAP4.error as e:
            raise AuthError(f"IMAP sign-in refused: {str(e)[:160]}") from e
        return conn

    def _smtp_check(self):
        c = self.creds
        host, port = c["smtp_host"], int(c.get("smtp_port") or 587)
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                s = smtplib.SMTP(host, port, timeout=20)
                s.starttls()
        except (socket.gaierror, socket.timeout, OSError, ssl.SSLError, smtplib.SMTPException) as e:
            raise TransientError(f"could not reach {host}: {e}") from e
        try:
            s.login(c["username"], c["password"])
        except smtplib.SMTPAuthenticationError as e:
            raise AuthError(f"SMTP sign-in refused: {str(e)[:160]}") from e
        except smtplib.SMTPException as e:
            raise TransientError(f"SMTP error: {e}") from e
        finally:
            try:
                s.quit()
            except Exception:
                pass

    def _test_sync(self) -> str:
        conn = self._imap()
        try:
            status, data = conn.select("INBOX", readonly=True)
            count = int(data[0]) if status == "OK" and data and data[0].isdigit() else 0
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        self._smtp_check()
        return f"Signed in; {count:,} messages in the inbox."

    async def test(self) -> str:
        return await asyncio.get_event_loop().run_in_executor(None, self._test_sync)
