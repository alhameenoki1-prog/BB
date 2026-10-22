"""IMAP + SMTP with a password (an app password at Gmail, Yahoo, iCloud).
"Limited mode": no push, no history API, so changes are found by polling
and UID ranges. Backfill walks the inbox and the sent folder (Gmail's
"All Mail" when present, which holds both); incremental sync asks each
folder for UIDs above the last one seen and starts over for a folder
whose UIDVALIDITY changed."""

import asyncio
import imaplib
import re
import smtplib
import socket
import ssl
from datetime import datetime, timedelta, timezone

from bb.mail.parse import parse_rfc822
from bb.mail.providers.base import AuthError, MailAccount, TransientError

BATCH = 40
SENT_NAMES = ("[Gmail]/Sent Mail", "Sent", "Sent Items", "Sent Messages", "INBOX.Sent", "Sent Mail")
ALL_MAIL = "[Gmail]/All Mail"
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"?(?P<delim>[^"\s]+)"?\s+"?(?P<name>.+?)"?$')


class ImapAccount(MailAccount):
    name = "imap"

    # ── connections ──

    def _imap(self):
        c = self.creds
        try:
            conn = imaplib.IMAP4_SSL(c["imap_host"], int(c.get("imap_port") or 993), timeout=30,
                                     ssl_context=ssl.create_default_context())
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
            ctx = ssl.create_default_context()  # the stdlib default verifies nothing
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
            else:
                s = smtplib.SMTP(host, port, timeout=20)
                s.starttls(context=ctx)
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
            self._logout(conn)
        self._smtp_check()
        return f"Signed in; {count:,} messages in the inbox."

    async def test(self) -> str:
        return await asyncio.get_event_loop().run_in_executor(None, self._test_sync)

    @staticmethod
    def _logout(conn):
        try:
            conn.logout()
        except Exception:
            pass

    # ── sending ──

    async def send(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", from_name="", attachments=None) -> tuple[str, str, str]:
        import aiosmtplib

        msg = self.build_mime(to=to, subject=subject, body_text=body_text, body_html=body_html,
                              in_reply_to=in_reply_to, references=references, reply_to=reply_to, from_name=from_name,
                              attachments=attachments)
        c = self.creds
        port = int(c.get("smtp_port") or 587)
        try:
            # Implicit TLS on 465/2465, STARTTLS required on 587/2587, and
            # whenever offered on any other port: never a password in clear.
            use_tls = port in (465, 2465)
            await aiosmtplib.send(msg, hostname=c["smtp_host"], port=port, username=c["username"], password=c["password"],
                                  start_tls=False if use_tls else (True if port in (587, 2587) else None),
                                  use_tls=use_tls, timeout=40)
        except aiosmtplib.SMTPAuthenticationError as e:
            raise AuthError(f"SMTP sign-in refused: {str(e)[:160]}") from e
        except (aiosmtplib.SMTPException, OSError) as e:
            raise TransientError(f"send failed: {str(e)[:200]}") from e
        message_id = msg["Message-ID"]
        # Best effort: a copy in the Sent folder, so the mailbox itself shows it.
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._append_sent, msg.as_bytes())
        except Exception:
            pass
        return message_id, "", ""

    def _append_sent(self, raw: bytes):
        import imaplib as _imaplib
        conn = self._imap()
        try:
            folders = self._folders(conn)
            sent = next((f for f, is_sent in folders if is_sent), None)
            if sent is None and folders and folders[0][0] == ALL_MAIL:
                sent = "[Gmail]/Sent Mail"
            if sent:
                conn.append(f'"{sent}"', "(\\Seen)", _imaplib.Time2Internaldate(__import__("time").time()), raw)
        finally:
            self._logout(conn)

    # ── folders ──

    def _folders(self, conn) -> list[tuple[str, bool]]:
        """[(folder, is_sent)] worth syncing: the inbox and the sent folder,
        or Gmail's All Mail alone (it holds both and more)."""
        names, sent_flagged = [], None
        try:
            status, rows = conn.list()
        except Exception:
            status, rows = "NO", []
        if status == "OK":
            for row in rows or []:
                text = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
                m = _LIST_RE.search(text)
                if not m:
                    continue
                name = m.group("name").strip('"')
                flags = m.group("flags").lower()
                if "\\noselect" in flags:
                    continue
                names.append(name)
                if "\\sent" in flags:
                    sent_flagged = name
        if ALL_MAIL in names:
            return [(ALL_MAIL, False)]
        sent = sent_flagged or next((n for n in SENT_NAMES if n in names), None)
        out = [("INBOX", False)]
        if sent:
            out.append((sent, True))
        return out

    @staticmethod
    def _select(conn, folder: str) -> int:
        status, data = conn.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise TransientError(f"could not open folder {folder}")
        status, data = conn.response("UIDVALIDITY")
        try:
            return int(data[0]) if data and data[0] else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _uids(conn, query: str) -> list[int]:
        status, data = conn.uid("SEARCH", None, query)
        if status != "OK" or not data or not data[0]:
            return []
        return sorted(int(x) for x in data[0].split())

    def _fetch(self, conn, uids: list[int], folder: str, is_sent: bool) -> list:
        if not uids:
            return []
        status, parts = conn.uid("FETCH", ",".join(str(u) for u in uids), "(UID FLAGS BODY.PEEK[])")
        if status != "OK":
            raise TransientError(f"fetch failed in {folder}")
        out = []
        for part in parts or []:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            head = part[0].decode(errors="replace") if isinstance(part[0], bytes) else str(part[0])
            m = re.search(r"UID (\d+)", head)
            if not m:
                continue
            uid = m.group(1)
            flags = head.lower()
            msg = parse_rfc822(part[1], provider_id=f"{folder}:{uid}", folder=folder,
                               outbound=True if is_sent else None)
            if msg is None:
                continue
            if not is_sent and (msg.from_email or "").lower() == self.address:
                msg.outbound = True
            if "\\seen" not in flags and "\\answered" not in flags:
                msg.headers["x-bb-unseen"] = "1"
            out.append(msg)
        return out

    # ── backfill and sync (run in a thread; callbacks are coroutines) ──

    async def backfill(self, since_days: int, on_batch) -> dict:
        loop = asyncio.get_event_loop()
        state: dict = {"folders": {}, "backfill_done": False}
        conn = await loop.run_in_executor(None, self._imap)
        try:
            folders = await loop.run_in_executor(None, self._folders, conn)
            since = ""
            if since_days and since_days > 0:
                since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
            plan = []
            for folder, is_sent in folders:
                validity = await loop.run_in_executor(None, self._select, conn, folder)
                uids = await loop.run_in_executor(None, self._uids, conn, f'(SINCE "{since}")' if since else "ALL")
                plan.append((folder, is_sent, validity, uids))
            total = sum(len(u) for _, _, _, u in plan)
            done = 0
            for folder, is_sent, validity, uids in plan:
                await loop.run_in_executor(None, self._select, conn, folder)
                last = 0
                for i in range(0, len(uids), BATCH):
                    chunk = uids[i:i + BATCH]
                    msgs = await loop.run_in_executor(None, self._fetch, conn, chunk, folder, is_sent)
                    done += len(chunk)
                    last = max(last, max(chunk))
                    await on_batch(msgs, done, total)
                state["folders"][folder] = {"uidvalidity": validity, "last_uid": last or (max(uids) if uids else 0),
                                            "sent": is_sent}
            state["backfill_done"] = True
            state["total_seen"] = total
            return state
        finally:
            await loop.run_in_executor(None, self._logout, conn)

    async def sync(self, state: dict, on_batch) -> dict:
        loop = asyncio.get_event_loop()
        state = dict(state or {})
        folders_state = dict(state.get("folders") or {})
        conn = await loop.run_in_executor(None, self._imap)
        try:
            folders = await loop.run_in_executor(None, self._folders, conn)
            new_total = 0
            for folder, is_sent in folders:
                validity = await loop.run_in_executor(None, self._select, conn, folder)
                fs = folders_state.get(folder) or {"uidvalidity": validity, "last_uid": 0, "sent": is_sent}
                if fs.get("uidvalidity") != validity:
                    fs = {"uidvalidity": validity, "last_uid": 0, "sent": is_sent}   # folder was rebuilt: re-read it
                uids = await loop.run_in_executor(None, self._uids, conn, f"UID {int(fs['last_uid']) + 1}:*")
                uids = [u for u in uids if u > int(fs["last_uid"])]
                for i in range(0, len(uids), BATCH):
                    chunk = uids[i:i + BATCH]
                    msgs = await loop.run_in_executor(None, self._fetch, conn, chunk, folder, is_sent)
                    new_total += len(msgs)
                    await on_batch(msgs, new_total, new_total)
                    fs["last_uid"] = max(fs["last_uid"], max(chunk))
                folders_state[folder] = fs
            state["folders"] = folders_state
            state["backfill_done"] = True
            return state
        finally:
            await loop.run_in_executor(None, self._logout, conn)
