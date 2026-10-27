"""Microsoft 365 / Outlook mailbox provider — Microsoft Graph, no SDK.

Microsoft has turned off password sign-in for IMAP in Exchange Online, so
the SMTP+IMAP provider can't read an M365 inbox. This one uses OAuth:

Setup (one-time):
1. https://entra.microsoft.com → App registrations → New registration.
   Name "Harvey", "Accounts in this organizational directory only".
   Redirect URI: platform "Public client/native (mobile & desktop)",
   value http://localhost:8766
2. Copy the Application (client) ID and Directory (tenant) ID into .env as
   M365_CLIENT_ID / M365_TENANT_ID. No client secret: Harvey is a public
   client and signs in with PKCE.
3. Run ``harvey microsoft auth`` — opens a browser, you sign in and accept
   Mail.ReadWrite + Mail.Send, and the refresh token is stored in
   data/m365_token.json (mode 600). Revoke it any time at
   https://myaccount.microsoft.com → Apps.

Harvey never marks your mail read or moves it: it reads the last 7 days of
the inbox and remembers what it has handled. Mail is sent as the signed-in
mailbox, under its own display name.
"""

import asyncio
import base64
import hashlib
import http.server
import json
import logging
import mimetypes
import secrets
import socket
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from harvey.paths import DATA_DIR
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
    booking_url,
    render_parts,
)

logger = logging.getLogger("harvey.microsoft")

TOKEN_FILE = DATA_DIR / "m365_token.json"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
INBOX_MESSAGES = "/mailFolders/inbox/messages"
# Delivery reports that an inbox rule or the junk filter moved out of the
# inbox are read from here too (reports only; nothing else in Junk is).
JUNK_MESSAGES = "/mailFolders/junkemail/messages"
# Application (admin-consented) access: one token for the tenant, mailbox
# named in the path. Needs Mail.Send + Mail.ReadWrite *application*
# permissions with admin consent.
APP_SCOPE = "https://graph.microsoft.com/.default"
# Graph refuses file attachments above 3 MB on a single POST.
MAX_GRAPH_ATTACHMENT = 3 * 1024 * 1024
# Inbox pages read per pass (limit messages each). A flood (a bounce storm
# after a blocked send) is read this much at a time, oldest first, from the
# position the last pass reached; the rest waits for the next pass and the
# handler is told there is more (backlog_pending), so follow-ups wait.
MAX_INBOX_PAGES = 40
# The read position per mailbox (the newest receivedDateTime handled), kept
# in a small file like the IMAP cursor. Each pass re-reads this much before
# it, so a message that arrived while a page was being read is not missed
# (the handler skips what it has already processed).
CURSOR_FILE = DATA_DIR / "graph_cursor.json"
# A message read from the Reply-To mailbox: its id carries this prefix, so a
# reply to it is drafted there (createReply) and it is fetched back from there.
REPLY_TO_REF = "rt|"
REPLY_TO_HEADER = "x-bb-reply-to-mailbox"
CURSOR_OVERLAP = timedelta(minutes=10)
# Once this often a pass reads the whole week instead of from the position:
# a reply the junk filter held and the person moved to the Inbox hours
# later carries its old receivedDateTime, which the position would skip.
FULL_SWEEP_EVERY = timedelta(hours=1)
LOGIN = "https://login.microsoftonline.com"
SCOPE = (
    "offline_access https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/User.Read"
)
LOOPBACK_PORT = 8766
REPLY_WINDOW_DAYS = 7
MAX_RETRY_AFTER_SECONDS = 10

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

_SELECT = ",".join((
    "id", "subject", "from", "body", "internetMessageId", "conversationId",
    "receivedDateTime", "internetMessageHeaders",
))


class GraphError(RuntimeError):
    pass


def _load_token() -> dict | None:
    try:
        if TOKEN_FILE.is_file():
            data = json.loads(TOKEN_FILE.read_text())
            if isinstance(data, dict) and data.get("refresh_token"):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read Microsoft token file: {e}")
    return None


def _save_token(data: dict):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _token_url(tenant: str) -> str:
    return f"{LOGIN}/{tenant or 'organizations'}/oauth2/v2.0/token"


def _parse_time(value) -> datetime | None:
    """A Graph receivedDateTime (ISO 8601, Z or an offset) as an aware
    datetime; None when it is missing or unreadable."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Graph gives seven fractional digits; fromisoformat takes at most six.
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        while rest and rest[0].isdigit():
            digits += rest[0]; rest = rest[1:]
        text = head + ("." + digits[:6] if digits else "") + rest
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


class MicrosoftProvider(MailProvider):
    name = "microsoft"

    def __init__(self, config, env, transport=None):
        self.config = config
        self.client_id = getattr(env, "m365_client_id", "")
        self.tenant = getattr(env, "m365_tenant_id", "") or "organizations"
        # A web app registration (the hosted site's) is a confidential client
        # and must send its secret with the refresh; the CLI's public client has none.
        self.client_secret = getattr(env, "m365_client_secret", "") or ""
        self._transport = transport  # httpx.MockTransport in tests
        self._access_token = ""
        self._access_expires = 0.0
        self._me = ""
        # Graph path of the mailbox Harvey acts as: the signed-in user here,
        # /users/<mailbox> for the admin-app provider below.
        self._user_path = "/me"

    def is_configured(self) -> bool:
        return bool(self.client_id and _load_token())

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport)

    async def _token(self) -> str:
        """Valid access token. Microsoft rotates the refresh token on every
        use, so the new one is saved each time."""
        if self._access_token and time.time() < self._access_expires - 60:
            return self._access_token
        stored = _load_token()
        if not stored:
            raise GraphError("not authorized. Run 'harvey microsoft auth'.")
        try:
            form = {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
                "scope": SCOPE,
            }
            if self.client_secret and self._user_path == "/me":
                form["client_secret"] = self.client_secret
            async with self._client() as client:
                resp = await client.post(_token_url(self.tenant), data=form)
        except httpx.HTTPError as e:
            raise GraphError(f"token refresh failed: {e}") from e
        if resp.status_code != 200:
            raise GraphError(
                f"token refresh rejected ({resp.status_code}): {resp.text[:200]}. "
                "Re-run 'harvey microsoft auth'."
            )
        data = resp.json()
        if data.get("refresh_token"):
            _save_token({**stored, "refresh_token": data["refresh_token"],
                         "refreshed_at": int(time.time())})
        self._access_token = data.get("access_token", "")
        self._access_expires = time.time() + float(data.get("expires_in", 3600))
        if not self._access_token:
            raise GraphError("token refresh returned no access token")
        return self._access_token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        return await self._request_url(method, f"{GRAPH}{path}", **kwargs)

    async def _request_url(self, method: str, url: str, *, headers=None, **kwargs) -> dict:
        """One Graph call. Raises GraphError on failure; retries once when
        Graph asks to slow down (429/503 with a short Retry-After). The
        result is always a JSON object; only 202/204 may be empty (send,
        delete), and come back as {}."""
        path = url[len(GRAPH):] if url.startswith(GRAPH) else url
        token = await self._token()
        all_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        for attempt in (1, 2):
            try:
                async with self._client() as client:
                    resp = await client.request(
                        method, url, headers=all_headers, **kwargs
                    )
            except httpx.HTTPError as e:
                raise GraphError(f"{method} {path} failed: {e}") from e
            if resp.status_code in (429, 503) and attempt == 1:
                try:
                    wait = float(resp.headers.get("Retry-After", "5"))
                except ValueError:
                    wait = 5.0
                if wait <= MAX_RETRY_AFTER_SECONDS:
                    await asyncio.sleep(wait)
                    continue
            break
        if resp.status_code >= 400:
            raise GraphError(f"Graph {resp.status_code} on {method} {path}: {resp.text[:200]}")
        if not resp.content:
            if resp.status_code in (202, 204):
                return {}
            raise GraphError(f"Graph {resp.status_code} on {method} {path}: empty response")
        try:
            data = resp.json()
        except ValueError as e:
            raise GraphError(f"Graph {resp.status_code} on {method} {path}: malformed JSON") from e
        if not isinstance(data, dict):
            raise GraphError(
                f"Graph {resp.status_code} on {method} {path}: expected a JSON object, "
                f"got {type(data).__name__}"
            )
        return data

    async def _my_address(self) -> str:
        if not self._me:
            me = await self._request(
                "GET", self._user_path, params={"$select": "mail,userPrincipalName"})
            self._me = (me.get("mail") or me.get("userPrincipalName") or "").lower()
        return self._me

    async def _check_identity(self) -> str:
        """The signed-in mailbox must be persona.email: a token for the wrong
        account (say, the admin who approved the app) would otherwise send
        cold email from that account."""
        me = await self._my_address()
        expected = (getattr(getattr(self.config, "persona", None), "email", "") or "").strip().lower()
        if expected and me != expected:
            raise GraphError(
                f"signed in as {me}, but persona.email is {expected}. Run "
                f"'harvey microsoft auth' again and sign in as {expected}."
            )
        return me

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        attachments: list | None = None,
        plain: bool = False,
    ) -> SendResult:
        """Draft, then send, so the RFC Message-ID is known (bounces and
        replies are matched on it). A reply is drafted with createReply on
        the prospect's message, which sets In-Reply-To/References."""
        plain, html, image_path = render_parts(
            body, getattr(self.config, "outreach", None), plain=plain, booking=booking_url(self.config))
        content = {
            "subject": subject,
            "body": ({"contentType": "HTML", "content": html} if html
                     else {"contentType": "Text", "content": plain}),
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        }
        reply_to = getattr(self, "reply_to", "") or ""
        if reply_to:
            content["replyTo"] = [{"emailAddress": {"address": reply_to}}]
        graph_attachments = _graph_attachments(image_path if html else None, attachments)
        draft = None
        # A message that came into the Reply-To mailbox is answered from
        # there, in its thread (that is where the conversation lives).
        user_path, ref = self._user_path, thread_ref
        if thread_ref.startswith(REPLY_TO_REF) and reply_to:
            user_path, ref = f"/users/{reply_to}", thread_ref[len(REPLY_TO_REF):]
        try:
            await self._check_identity()
            if ref:
                try:
                    draft = await self._request(
                        "POST", f"{user_path}/messages/{ref}/createReply"
                    )
                    await self._request(
                        "PATCH", f"{user_path}/messages/{draft['id']}", json=content)
                except GraphError as e:
                    # Their message is gone (deleted/moved): send unthreaded.
                    logger.warning(f"Microsoft: createReply failed ({e}); sending as new.")
                    if draft and draft.get("id"):
                        await self._delete_draft(draft["id"], user_path)
                    draft = None
                    user_path = self._user_path
            if draft is None:
                draft = await self._request(
                    "POST", f"{user_path}/messages", json=content)
            for attachment in graph_attachments:
                await self._request(
                    "POST", f"{user_path}/messages/{draft['id']}/attachments",
                    json=attachment)
            await self._request("POST", f"{user_path}/messages/{draft['id']}/send")
        except (GraphError, KeyError) as e:
            if draft and draft.get("id"):
                await self._delete_draft(draft["id"], user_path)
            logger.error(f"Microsoft send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        return SendResult(
            ok=True,
            message_id=draft.get("internetMessageId", ""),
            thread_ref=draft.get("conversationId", ""),
        )

    async def _delete_draft(self, draft_id: str, user_path: str = ""):
        try:
            await self._request("DELETE", f"{user_path or self._user_path}/messages/{draft_id}")
        except GraphError as e:
            logger.warning(f"Microsoft: couldn't delete unsent draft {draft_id}: {e}")

    # True after a read that stopped at MAX_INBOX_PAGES with more to come:
    # the handler then reports the pass incomplete (follow-ups wait) and the
    # next pass carries on from the position this one reached.
    backlog_pending: bool = False

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """New inbox mail since the last read position (at most the last
        week), oldest first, minus your own mail. Pages are followed as
        Graph gives them (``limit`` is only the page size). Raises on any
        failure, including a later page: the heartbeat then holds sends,
        because an unread reply can't have stopped its follow-ups. A flood
        is read MAX_INBOX_PAGES pages at a time: what was read is returned,
        ``backlog_pending`` says more waits, and the position moves on so
        the next pass continues rather than starting over."""
        me = await self._check_identity()
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=REPLY_WINDOW_DAYS)
        cursor, swept = self._load_cursor(), self._load_swept()
        sweep = cursor is not None and (swept is None or now - swept >= FULL_SWEEP_EVERY)
        if cursor and not sweep:
            since = max(since, cursor - CURSOR_OVERLAP)
        seen_ids: set = set()
        out: list[InboundMessage] = []
        self.backlog_pending = False
        newest = await self._read_folder(INBOX_MESSAGES, since, me, limit, seen_ids, out, sweep=sweep)
        # Delivery reports moved to Junk Email (an inbox rule, the junk
        # filter): read as well, so a bounce never goes unseen. Only reports
        # are taken from there, and a Junk folder that cannot be read does
        # not fail the inbox read.
        try:
            junk_newest = await self._read_folder(JUNK_MESSAGES, since, me, limit, seen_ids, out,
                                                  reports_only=True, sweep=sweep)
        except GraphError as e:
            logger.warning(f"Microsoft: the Junk Email folder could not be read for delivery reports ({e}); "
                           "the inbox was read.")
            junk_newest = None
        if sweep:
            self._save_swept(now)
        # The position moves as the handler consumes messages (advance_to),
        # so a worker stopped mid-batch starts again where it left off. With
        # nothing to hand over, it moves to the newest thing seen.
        if not out:
            stamps = [t for t in (newest, junk_newest) if t is not None]
            if stamps:
                self._save_cursor(max(stamps))
        self.read_problem = ""
        if getattr(self, "reply_to", ""):
            out += await self._read_reply_to_mailbox(me, limit, now)
        return out

    # A part of the mail that could not be read (the Reply-To mailbox): the
    # handler reports the pass incomplete with this cause, so follow-ups wait.
    read_problem: str = ""

    async def _read_reply_to_mailbox(self, me: str, limit: int, now: datetime) -> list[InboundMessage]:
        """The Reply-To mailbox's inbox, from its own position: the human
        replies to our emails land there. Each message is tagged so the
        handler takes only replies from people on the list (that inbox is
        someone's ordinary mail too), and so a reply goes back from there."""
        path = f"/users/{self.reply_to}"
        since = now - timedelta(days=REPLY_WINDOW_DAYS)
        cursor = self._load_cursor(path)
        if cursor:
            since = max(since, cursor - CURSOR_OVERLAP)
        got: list[InboundMessage] = []
        try:
            newest = await self._read_folder(INBOX_MESSAGES, since, self.reply_to, limit, set(), got, user_path=path)
        except GraphError as e:
            self.read_problem = (f"the mailbox could not be read: replies go to {self.reply_to}, whose inbox this app "
                                 f"cannot read ({str(e)[:120]})")
            logger.warning(f"Microsoft: {self.read_problem}")
            return []
        out = []
        for msg in got:
            if msg.from_email in (me, self.reply_to):
                continue
            msg.headers[REPLY_TO_HEADER] = path
            msg.provider_id = REPLY_TO_REF + msg.provider_id
            msg.thread_ref = msg.provider_id
            out.append(msg)
        if not out and newest is not None:
            self._save_cursor(newest, path)
        return out

    async def fetch_message(self, provider_id: str) -> InboundMessage | None:
        """One message by its Graph id (a reply the handler put aside for
        the model and comes back for); None when it is gone."""
        if not provider_id or "/" in provider_id or "?" in provider_id:
            return None
        path, graph_id = self._user_path, provider_id
        foreign = provider_id.startswith(REPLY_TO_REF)
        if foreign:
            if not getattr(self, "reply_to", ""):
                return None
            path, graph_id = f"/users/{self.reply_to}", provider_id[len(REPLY_TO_REF):]
        try:
            data = await self._request(
                "GET", f"{path}/messages/{graph_id}",
                params={"$select": _SELECT}, headers={"Prefer": 'outlook.body-content-type="text"'},
            )
        except GraphError as e:
            if "Graph 404" in str(e):
                return None
            raise
        msg = self._parse_message(data)
        if msg is not None and foreign:
            msg.headers[REPLY_TO_HEADER] = path
            msg.provider_id = msg.thread_ref = provider_id
        return msg

    @staticmethod
    def _key_of(message) -> str:
        """Which mailbox's position a message belongs to ("" = the sending one)."""
        return str((getattr(message, "headers", None) or {}).get(REPLY_TO_HEADER) or "")

    def advance_to(self, message: InboundMessage) -> None:
        """The handler has dealt with this message (or put it aside by id):
        the position moves up to it, never back."""
        when = _parse_time(getattr(message, "date", ""))
        if when is None:
            return
        key = self._key_of(message)
        cursor = self._load_cursor(key)
        if cursor is None or when > cursor:
            self._save_cursor(when, key)

    async def _read_folder(self, folder: str, since: datetime, me: str, limit: int, seen_ids: set,
                           out: list, reports_only: bool = False, sweep: bool = False,
                           user_path: str = "") -> datetime | None:
        """Read one folder from ``since``, oldest first, at most
        MAX_INBOX_PAGES pages (backlog_pending when more waits), appending
        to ``out`` (only delivery reports with ``reports_only``). Returns the
        newest receivedDateTime seen, or None."""
        user_path = user_path or self._user_path
        url = str(httpx.URL(f"{GRAPH}{user_path}{folder}", params={
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$top": str(max(1, int(limit))),
            "$select": _SELECT,
        }))
        seen_urls = {url}
        newest = None
        taken = 0
        for page in range(1, MAX_INBOX_PAGES + 1):
            data = await self._request_url(
                "GET", url, headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            items = data.get("value")
            if not isinstance(items, list):
                raise GraphError(f"inbox page {page}: 'value' is missing or not a list")
            for item in items:
                msg = self._parse_message(item)
                if msg is None:
                    continue
                when = _parse_time(msg.date)
                if when and (newest is None or when > newest):
                    newest = when
                if reports_only and not msg.is_bounce:
                    continue
                # A message can repeat across pages if mail arrives mid-read.
                if msg.from_email != me and msg.provider_id not in seen_ids:
                    seen_ids.add(msg.provider_id)
                    out.append(msg)
                    taken += 1
            next_link = data.get("@odata.nextLink")
            if next_link is None:
                break
            url = _checked_next_link(next_link, user_path, folder)
            if url in seen_urls:
                raise GraphError(f"inbox page {page}: @odata.nextLink repeats an earlier page")
            seen_urls.add(url)
        else:
            if sweep:
                # The hourly look back over the week hit the cap: the usual
                # position still covers new mail, so this is not a backlog.
                logger.info(f"Microsoft: the hourly sweep of {folder.split('/')[2]} stopped after "
                            f"{MAX_INBOX_PAGES} pages; new mail is read from the position as usual.")
            else:
                self.backlog_pending = True
                logger.warning(f"Microsoft: {folder.split('/')[2]} has more than {MAX_INBOX_PAGES} pages of new mail; "
                               f"{taken} read this pass, the rest next pass.")
        return newest

    def _cursor_key(self) -> str:
        return self._user_path

    def _cursor_path(self) -> Path:
        return CURSOR_FILE

    def _cursor_data(self) -> dict:
        try:
            data = json.loads(self._cursor_path().read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _load_cursor(self, key: str = "") -> datetime | None:
        raw = self._cursor_data().get(key or self._cursor_key())
        return _parse_time(raw) if isinstance(raw, str) else None

    def _load_swept(self) -> datetime | None:
        raw = self._cursor_data().get(self._cursor_key() + ":swept")
        return _parse_time(raw) if isinstance(raw, str) else None

    def _save_cursor(self, when: datetime, key: str = "") -> None:
        self._save_stamp(key or self._cursor_key(), when)
        if key:
            return
        if self._load_swept() is None:
            # The first position counts as a sweep: the next one is due in an hour.
            self._save_swept(datetime.now(timezone.utc))

    def _save_swept(self, when: datetime) -> None:
        self._save_stamp(self._cursor_key() + ":swept", when)

    def _save_stamp(self, key: str, when: datetime) -> None:
        path = self._cursor_path()
        data = self._cursor_data()
        data[key] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        except OSError as e:
            logger.debug(f"Microsoft: read position not saved: {e}")

    def rewind_to(self, message: InboundMessage) -> None:
        """A message the handler could not process (the model was down, the
        worker was stopped) is read again next time: the position goes back
        to just before it. Messages come oldest first, so later ones in the
        same batch are re-read too (the handler skips what it has done)."""
        when = _parse_time(getattr(message, "date", ""))
        if when is None:
            return
        key = self._key_of(message)
        cursor = self._load_cursor(key)
        if cursor is None or cursor > when:
            self._save_cursor(when - timedelta(seconds=1), key)

    async def get_unanswered(self, days: int = 30, limit: int = 100) -> list[InboundMessage]:
        """Inbox mail from the last ``days`` days whose conversation has no
        message in Sent Items."""
        me = await self._check_identity()
        since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        data = await self._request(
            "GET", f"{self._user_path}{INBOX_MESSAGES}",
            params={"$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                    "$orderby": "receivedDateTime desc", "$top": str(min(limit, 100)), "$select": _SELECT},
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        items = data.get("value") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out, seen_threads = [], set()
        for item in items:
            msg = self._parse_message(item)
            if not msg or msg.from_email == me or msg.is_bounce:
                continue
            conv = str(item.get("conversationId") or "")
            if conv in seen_threads:
                continue
            seen_threads.add(conv)
            if conv:
                sent = await self._request(
                    "GET", f"{self._user_path}/mailFolders/sentitems/messages",
                    params={"$filter": f"conversationId eq '{conv}'", "$top": "1", "$select": "id"})
                if isinstance(sent, dict) and sent.get("value"):
                    continue
            out.append(msg)
        return out

    @staticmethod
    def _parse_message(item) -> InboundMessage | None:
        """One Graph message. A message with no sender address is skipped;
        one with fields of the wrong shape raises GraphError."""
        def obj(value, what) -> dict:
            if value is None:
                return {}
            if not isinstance(value, dict):
                raise GraphError(f"malformed inbox message: {what} is not an object")
            return value

        def text(value, what) -> str:
            if value is None:
                return ""
            if not isinstance(value, str):
                raise GraphError(f"malformed inbox message: {what} is not a string")
            return value

        if not isinstance(item, dict):
            raise GraphError("malformed inbox message: not an object")
        msg_id = text(item.get("id"), "id")
        if not msg_id:
            raise GraphError("malformed inbox message: no id")
        sender = obj(obj(item.get("from"), "from").get("emailAddress"), "from.emailAddress")
        from_email = text(sender.get("address"), "sender address").strip().lower()
        if not from_email:
            return None
        subject = text(item.get("subject"), "subject")
        raw_headers = item.get("internetMessageHeaders") or []
        if not isinstance(raw_headers, list):
            raise GraphError("malformed inbox message: internetMessageHeaders is not a list")
        headers = {}
        for h in raw_headers:
            h = obj(h, "header")
            headers[text(h.get("name"), "header name").lower()] = text(
                h.get("value"), "header value")
        body = text(obj(item.get("body"), "body").get("content"), "body content")
        return InboundMessage(
            provider_id=msg_id,
            from_email=from_email,
            subject=subject,
            body=body[:5000],
            message_id=text(item.get("internetMessageId"), "internetMessageId"),
            in_reply_to=headers.get("in-reply-to", ""),
            # The Graph id: what createReply needs to answer this message.
            thread_ref=msg_id,
            date=text(item.get("receivedDateTime"), "receivedDateTime"),
            is_bounce=looks_like_bounce(from_email, subject) or "x-ms-exchange-message-is-ndr" in headers,
            headers=headers,
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.client_id:
            return False, "M365_CLIENT_ID missing from .env"
        if not _load_token():
            return False, "Not authorized yet — run 'harvey microsoft auth'"
        try:
            me = await self._check_identity()
            await self._request(
                "GET", f"{self._user_path}{INBOX_MESSAGES}",
                params={"$top": "1", "$select": "id"},
            )
        except GraphError as e:
            return False, str(e)[:300]
        return True, f"Connected as {me}"


class MicrosoftAppProvider(MicrosoftProvider):
    """Microsoft 365 through an admin-consented app (client credentials).

    No browser sign-in and no per-user token: the tenant admin registers an
    app, grants it Mail.Send + Mail.ReadWrite as *application* permissions
    with admin consent, and creates a client secret. Harvey then sends and
    reads as the one mailbox named in M365_MAILBOX. Exchange Online's own
    ceiling applies (10,000 recipients a day per mailbox, 30 messages a
    minute), which is far above what any cold-outreach mailbox should do.
    """

    name = "microsoft_app"

    def __init__(self, config, env, transport=None):
        super().__init__(config, env, transport)
        self.client_secret = getattr(env, "m365_client_secret", "") or ""
        self.mailbox = (getattr(env, "m365_mailbox", "") or "").strip().lower()
        self.tenant = getattr(env, "m365_tenant_id", "") or ""
        self._user_path = f"/users/{self.mailbox}"
        reply_to = (getattr(env, "m365_reply_to", "") or "").strip().lower()
        self.reply_to = reply_to if reply_to and reply_to != self.mailbox else ""

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.tenant and self.mailbox)

    async def _token(self) -> str:
        if self._access_token and time.time() < self._access_expires - 60:
            return self._access_token
        if not self.is_configured():
            raise GraphError(
                "Microsoft 365 app not configured: M365_TENANT_ID, M365_CLIENT_ID, "
                "M365_CLIENT_SECRET and M365_MAILBOX are all needed.")
        try:
            async with self._client() as client:
                resp = await client.post(_token_url(self.tenant), data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                    "scope": APP_SCOPE,
                })
        except httpx.HTTPError as e:
            raise GraphError(f"token request failed: {e}") from e
        if resp.status_code != 200:
            raise GraphError(
                f"token request rejected ({resp.status_code}): {resp.text[:200]}. "
                "Check the tenant ID, client ID and client secret (secrets expire)."
            )
        data = resp.json()
        self._access_token = data.get("access_token", "")
        self._access_expires = time.time() + float(data.get("expires_in", 3600))
        if not self._access_token:
            raise GraphError("token request returned no access token")
        return self._access_token

    async def _my_address(self) -> str:
        return self.mailbox

    async def _check_identity(self) -> str:
        expected = (getattr(getattr(self.config, "persona", None), "email", "") or "").strip().lower()
        if expected and self.mailbox != expected:
            raise GraphError(
                f"M365_MAILBOX is {self.mailbox}, but persona.email is {expected}. "
                "They must be the same address."
            )
        return self.mailbox

    async def test_connection(self) -> tuple[bool, str]:
        missing = [name for name, value in (
            ("M365_TENANT_ID", self.tenant), ("M365_CLIENT_ID", self.client_id),
            ("M365_CLIENT_SECRET", self.client_secret), ("M365_MAILBOX", self.mailbox),
        ) if not value]
        if missing:
            return False, "Missing: " + ", ".join(missing)
        try:
            await self._check_identity()
            # The inbox itself, not the user's directory record: reading that
            # needs User.Read.All, a directory permission the mailbox
            # permissions do not include, and the mailbox is proven by its
            # inbox (an unknown address answers 404 here too).
            await self._request(
                "GET", f"{self._user_path}{INBOX_MESSAGES}",
                params={"$top": "1", "$select": "id"},
            )
        except GraphError as e:
            text = str(e)
            if "Graph 403" in text or "Graph 401" in text:
                text += (" — in the Entra tenant this mailbox belongs to, the app registration needs Mail.ReadWrite "
                         "and Mail.Send as APPLICATION permissions with 'Grant admin consent' pressed (each tenant "
                         "registers and consents the app itself), and the tenant ID, client ID and secret must be "
                         "that tenant's.")
            elif "Graph 404" in text:
                text += " — no mailbox with that address in this tenant (check M365_MAILBOX)."
            return False, text[:500]
        if self.reply_to:
            try:
                await self._request("GET", f"/users/{self.reply_to}{INBOX_MESSAGES}",
                                    params={"$top": "1", "$select": "id"})
            except GraphError:
                return True, (f"Connected as {self.mailbox} (admin app). Replies go to {self.reply_to}, but this app "
                              "cannot read that mailbox, so the worker cannot see who answered: follow-ups wait and "
                              "replies are not answered for you. Use a mailbox in this tenant to have them read.")
            return True, f"Connected as {self.mailbox} (admin app); replies go to {self.reply_to}, read too"
        return True, f"Connected as {self.mailbox} (admin app)"


def _graph_attachments(image_path, attachments) -> list[dict]:
    """Graph fileAttachment objects: the inline signature image (by
    contentId, so the HTML's cid: reference resolves) and the operator's
    files. Files too big for one POST are skipped with a warning."""
    out = []
    if image_path is not None:
        try:
            data = Path(image_path).read_bytes()
        except OSError as e:
            logger.warning(f"Microsoft: signature image unreadable ({e}); sending without it.")
        else:
            ctype = mimetypes.guess_type(str(image_path))[0] or "image/png"
            out.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": Path(image_path).name,
                "contentType": ctype,
                "contentBytes": base64.b64encode(data).decode("ascii"),
                "contentId": "signature-image",
                "isInline": True,
            })
    for path in attachments or []:
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as e:
            logger.warning(f"Microsoft: attachment {path.name} unreadable ({e}); skipped.")
            continue
        if len(data) > MAX_GRAPH_ATTACHMENT:
            logger.warning(
                f"Microsoft: attachment {path.name} is over 3 MB; skipped "
                "(Graph needs an upload session for that size).")
            continue
        out.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": path.name,
            "contentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "contentBytes": base64.b64encode(data).decode("ascii"),
        })
    return out


def _checked_next_link(link, user_path: str = "/me", folder: str = INBOX_MESSAGES) -> str:
    """Graph's @odata.nextLink, used whole (Microsoft: don't rebuild or
    edit $skip). It gets the bearer token, so it must point back at the
    inbox listing on Graph itself: https, graph.microsoft.com, the same
    path, no credentials, no fragment. Anything else raises GraphError."""
    if not isinstance(link, str) or not link:
        raise GraphError("@odata.nextLink is not a URL")
    if any(c in link for c in "#\\") or any(c.isspace() or ord(c) < 32 for c in link):
        raise GraphError("@odata.nextLink rejected: unexpected characters")
    try:
        url = httpx.URL(link)
    except (httpx.InvalidURL, ValueError) as e:
        raise GraphError(f"@odata.nextLink rejected: {e}") from e
    path = url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")
    if (
        url.scheme != "https"
        or url.host != GRAPH_HOST
        or url.port not in (None, 443)
        or url.userinfo
        or url.fragment
        or path.lower() != ("/v1.0" + user_path + folder).lower()
        or not url.query
    ):
        raise GraphError("@odata.nextLink rejected: not the Graph inbox listing")
    return link


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


class _LoopbackServer(http.server.HTTPServer):
    allow_reuse_address = True


class _LoopbackServerV6(_LoopbackServer):
    address_family = socket.AF_INET6


def run_auth_flow(client_id: str, tenant: str = "") -> bool:
    """One-time sign-in (authorization code + PKCE, loopback redirect).
    Blocks until the browser round-trip completes."""
    import threading
    import urllib.parse

    if not client_id:
        print("  Set M365_CLIENT_ID (and M365_TENANT_ID) in .env first.")
        print("  (entra.microsoft.com → App registrations → New registration)")
        return False
    tenant = tenant or "organizations"

    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    redirect_uri = f"http://localhost:{LOOPBACK_PORT}"
    auth_url = f"{LOGIN}/{tenant}/oauth2/v2.0/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = (query.get("error_description") or query.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Harvey is connected to Microsoft 365.</h2>You can close this tab.")
            done.set()

        def log_message(self, *args):
            pass

    # "localhost" is both 127.0.0.1 and ::1, and browsers differ in which
    # they try: listen on both (loopback only), or the redirect can land on
    # the one nobody is listening to.
    servers = [_LoopbackServer(("127.0.0.1", LOOPBACK_PORT), Handler)]
    try:
        servers.append(_LoopbackServerV6(("::1", LOOPBACK_PORT), Handler))
    except OSError:
        pass  # no IPv6 loopback on this machine
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"\n  Opening browser for Microsoft sign-in...\n  {auth_url}\n")
    webbrowser.open(auth_url)
    finished = done.wait(timeout=300)
    for server in servers:
        server.shutdown()
        server.server_close()
    if not finished:
        print("  Timed out waiting for the browser round-trip.")
        return False

    if result.get("error") or not result.get("code"):
        print(f"  Authorization failed: {result.get('error') or 'no code returned'}")
        return False

    resp = httpx.post(_token_url(tenant), data={
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": SCOPE,
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  Token exchange failed ({resp.status_code}): {resp.text[:300]}")
        return False
    data = resp.json()
    if not data.get("refresh_token"):
        print("  Microsoft returned no refresh token (offline_access missing?).")
        return False
    _save_token({
        "refresh_token": data["refresh_token"],
        "tenant": tenant,
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    })
    print(f"  ✓ Microsoft 365 authorized. Token stored at {TOKEN_FILE}")
    return True
