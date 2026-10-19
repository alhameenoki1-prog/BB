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
    render_parts,
)

logger = logging.getLogger("harvey.microsoft")

TOKEN_FILE = DATA_DIR / "m365_token.json"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
INBOX_MESSAGES = "/mailFolders/inbox/messages"
# Application (admin-consented) access: one token for the tenant, mailbox
# named in the path. Needs Mail.Send + Mail.ReadWrite *application*
# permissions with admin consent.
APP_SCOPE = "https://graph.microsoft.com/.default"
# Graph refuses file attachments above 3 MB on a single POST.
MAX_GRAPH_ATTACHMENT = 3 * 1024 * 1024
# Inbox pages read per cycle (limit messages each). More than this and the
# read fails, and sending is held, rather than acting on a partial inbox.
MAX_INBOX_PAGES = 40
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


class MicrosoftProvider(MailProvider):
    name = "microsoft"

    def __init__(self, config, env, transport=None):
        self.config = config
        self.client_id = getattr(env, "m365_client_id", "")
        self.tenant = getattr(env, "m365_tenant_id", "") or "organizations"
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
            async with self._client() as client:
                resp = await client.post(_token_url(self.tenant), data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": stored["refresh_token"],
                    "scope": SCOPE,
                })
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
    ) -> SendResult:
        """Draft, then send, so the RFC Message-ID is known (bounces and
        replies are matched on it). A reply is drafted with createReply on
        the prospect's message, which sets In-Reply-To/References."""
        plain, html, image_path = render_parts(
            body, getattr(self.config, "outreach", None))
        content = {
            "subject": subject,
            "body": ({"contentType": "HTML", "content": html} if html
                     else {"contentType": "Text", "content": plain}),
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        }
        graph_attachments = _graph_attachments(image_path if html else None, attachments)
        draft = None
        try:
            await self._check_identity()
            if thread_ref:
                try:
                    draft = await self._request(
                        "POST", f"{self._user_path}/messages/{thread_ref}/createReply"
                    )
                    await self._request(
                        "PATCH", f"{self._user_path}/messages/{draft['id']}", json=content)
                except GraphError as e:
                    # Their message is gone (deleted/moved): send unthreaded.
                    logger.warning(f"Microsoft: createReply failed ({e}); sending as new.")
                    if draft and draft.get("id"):
                        await self._delete_draft(draft["id"])
                    draft = None
            if draft is None:
                draft = await self._request(
                    "POST", f"{self._user_path}/messages", json=content)
            for attachment in graph_attachments:
                await self._request(
                    "POST", f"{self._user_path}/messages/{draft['id']}/attachments",
                    json=attachment)
            await self._request("POST", f"{self._user_path}/messages/{draft['id']}/send")
        except (GraphError, KeyError) as e:
            if draft and draft.get("id"):
                await self._delete_draft(draft["id"])
            logger.error(f"Microsoft send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        return SendResult(
            ok=True,
            message_id=draft.get("internetMessageId", ""),
            thread_ref=draft.get("conversationId", ""),
        )

    async def _delete_draft(self, draft_id: str):
        try:
            await self._request("DELETE", f"{self._user_path}/messages/{draft_id}")
        except GraphError as e:
            logger.warning(f"Microsoft: couldn't delete unsent draft {draft_id}: {e}")

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """The last week of the inbox, newest first, minus your own mail.
        Every page is read (``limit`` is only the page size), following
        Graph's @odata.nextLink as-is. Raises on any failure, including a
        later page: the heartbeat then holds sends, because an unread reply
        can't have stopped its follow-ups. Never returns a partial inbox."""
        me = await self._check_identity()
        since = (datetime.now(timezone.utc) - timedelta(days=REPLY_WINDOW_DAYS))
        url = str(httpx.URL(f"{GRAPH}{self._user_path}{INBOX_MESSAGES}", params={
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc",
            "$top": str(max(1, int(limit))),
            "$select": _SELECT,
        }))
        seen_urls = {url}
        seen_ids = set()
        out = []
        for page in range(1, MAX_INBOX_PAGES + 1):
            data = await self._request_url(
                "GET", url, headers={"Prefer": 'outlook.body-content-type="text"'},
            )
            items = data.get("value")
            if not isinstance(items, list):
                raise GraphError(f"inbox page {page}: 'value' is missing or not a list")
            for item in items:
                msg = self._parse_message(item)
                # A message can repeat across pages if mail arrives mid-read.
                if msg and msg.from_email != me and msg.provider_id not in seen_ids:
                    seen_ids.add(msg.provider_id)
                    out.append(msg)
            next_link = data.get("@odata.nextLink")
            if next_link is None:
                return out
            url = _checked_next_link(next_link, self._user_path)
            if url in seen_urls:
                raise GraphError(f"inbox page {page}: @odata.nextLink repeats an earlier page")
            seen_urls.add(url)
        raise GraphError(
            f"inbox has more than {MAX_INBOX_PAGES} pages; refusing a partial read"
        )

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
            is_bounce=looks_like_bounce(from_email, subject),
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
            await self._request("GET", self._user_path, params={"$select": "mail"})
            await self._request(
                "GET", f"{self._user_path}{INBOX_MESSAGES}",
                params={"$top": "1", "$select": "id"},
            )
        except GraphError as e:
            text = str(e)
            if "Graph 403" in text or "Graph 401" in text:
                text += (" — grant the app Mail.Send and Mail.ReadWrite as APPLICATION "
                         "permissions in Entra and press 'Grant admin consent'.")
            elif "Graph 404" in text:
                text += " — no mailbox with that address in this tenant (check M365_MAILBOX)."
            return False, text[:400]
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


def _checked_next_link(link, user_path: str = "/me") -> str:
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
        or path.lower() != ("/v1.0" + user_path + INBOX_MESSAGES).lower()
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
