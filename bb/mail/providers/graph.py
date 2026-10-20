"""Microsoft 365 through the Graph API with an app registration's client
credentials (tenant id, client id, client secret), the same kind of app
the Mailbox page's "Microsoft 365 admin app" option uses. Mail is read
and sent as /users/{address}; threading uses Graph's conversationId and
replies are created with createReply so Outlook keeps the thread."""

import time
from email.utils import make_msgid

import httpx

from bb.mail.providers.base import AuthError, MailAccount, RawMessage, TransientError

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"
PAGE = 50
SELECT = ("id,conversationId,internetMessageId,subject,bodyPreview,body,from,toRecipients,ccRecipients,"
          "receivedDateTime,sentDateTime,isDraft,parentFolderId")


def _addr(r: dict) -> dict:
    e = (r or {}).get("emailAddress") or {}
    return {"name": e.get("name") or "", "email": (e.get("address") or "").lower()}


class GraphAccount(MailAccount):
    name = "graph"

    def __init__(self, mailbox, creds, env):
        super().__init__(mailbox, creds, env)
        self._access, self._expires = "", 0.0
        self._sent_folder = ""

    @property
    def _me(self) -> str:
        return f"/users/{self.address}"

    async def _token(self) -> str:
        if self._access and time.time() < self._expires - 60:
            return self._access
        tenant, cid, secret = self.creds.get("tenant_id", ""), self.creds.get("client_id", ""), self.creds.get("client_secret", "")
        if not (tenant and cid and secret):
            raise AuthError("the tenant id, client id and client secret are all needed")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(f"{LOGIN}/{tenant}/oauth2/v2.0/token",
                                      data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": SCOPE})
        except httpx.HTTPError as e:
            raise TransientError(f"could not reach Microsoft sign-in: {e}") from e
        if r.status_code in (400, 401):
            try:
                why = r.json().get("error_description", "")
            except ValueError:
                why = r.text
            raise AuthError(f"Microsoft refused the app credentials: {why[:200]}")
        if r.status_code >= 400:
            raise TransientError(f"Microsoft sign-in answered {r.status_code}: {r.text[:160]}")
        data = r.json()
        self._access = data.get("access_token", "")
        self._expires = time.time() + int(data.get("expires_in") or 3600)
        if not self._access:
            raise AuthError("Microsoft returned no access token")
        return self._access

    async def _call(self, method: str, path: str, *, params=None, json=None, absolute=False):
        token = await self._token()
        url = path if absolute else GRAPH + path
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.request(method, url, params=params, json=json, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise TransientError(f"could not reach Microsoft Graph: {e}") from e
        if r.status_code in (401, 403):
            raise AuthError(f"Microsoft Graph refused the request ({r.status_code}): check the app's Mail.ReadWrite and "
                            f"Mail.Send application permissions and admin consent for {self.address}")
        if r.status_code == 404:
            raise LookupError(path)
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"Microsoft Graph answered {r.status_code}")
        if r.status_code >= 400:
            raise TransientError(f"Microsoft Graph error {r.status_code}: {r.text[:160]}")
        if r.status_code in (202, 204) or not r.content:
            return {}
        return r.json()

    async def test(self) -> str:
        try:
            inbox = await self._call("GET", f"{self._me}/mailFolders/inbox")
        except LookupError:
            raise AuthError(f"no mailbox {self.address} in this tenant (or the app may not read it)")
        await self._sent_id()
        return f"Inbox reachable ({inbox.get('totalItemCount', 0)} messages)."

    async def _sent_id(self) -> str:
        if not self._sent_folder:
            try:
                self._sent_folder = (await self._call("GET", f"{self._me}/mailFolders/sentitems"))["id"]
            except LookupError:
                self._sent_folder = ""
        return self._sent_folder

    def _raw(self, m: dict, sent_id: str) -> RawMessage | None:
        if m.get("isDraft"):
            return None
        from harvey.integrations.mail_provider import html_to_text
        sender = _addr(m.get("from"))
        outbound = (sent_id and m.get("parentFolderId") == sent_id) or sender["email"] == self.address
        body = (m.get("body") or {}).get("content") or ""
        if ((m.get("body") or {}).get("contentType") or "").lower() == "html":
            body = html_to_text(body)
        return RawMessage(provider_id=m["id"], thread_key=m.get("conversationId") or "", message_id=m.get("internetMessageId") or "",
                          from_email=sender["email"], from_name=sender["name"],
                          to=[_addr(r) for r in m.get("toRecipients") or []], cc=[_addr(r) for r in m.get("ccRecipients") or []],
                          subject=m.get("subject") or "", body=body, snippet=(m.get("bodyPreview") or "")[:200],
                          folder="SENT" if outbound else "INBOX", sent_at=m.get("sentDateTime") or m.get("receivedDateTime") or "",
                          outbound=bool(outbound))

    async def _pages(self, since: str, on_batch, descending: bool):
        sent_id = await self._sent_id()
        params = {"$select": SELECT, "$top": PAGE, "$orderby": f"receivedDateTime {'desc' if descending else 'asc'}"}
        if since:
            params["$filter"] = f"receivedDateTime ge {since}"
        url, absolute = f"{self._me}/messages", False
        done, newest = 0, since
        while url:
            data = await self._call("GET", url, params=params if not absolute else None, absolute=absolute)
            batch = [r for r in (self._raw(m, sent_id) for m in data.get("value", []) or []) if r]
            for m in data.get("value", []) or []:
                if (m.get("receivedDateTime") or "") > (newest or ""):
                    newest = m["receivedDateTime"]
            done += len(data.get("value", []) or [])
            if batch:
                await on_batch(batch, done, max(done, self._total_hint))
            url, absolute = data.get("@odata.nextLink") or "", True
        return done, newest

    _total_hint = 0

    async def backfill(self, since_days: int, on_batch) -> dict:
        try:
            inbox = await self._call("GET", f"{self._me}/mailFolders/inbox")
            self._total_hint = int(inbox.get("totalItemCount") or 0)
        except LookupError:
            self._total_hint = 0
        since = ""
        if since_days and since_days > 0:
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - int(since_days) * 86400))
        done, newest = await self._pages(since, on_batch, descending=True)
        return {"cursor": newest or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "backfill_done": True,
                "total_seen": done, "sent_folder": await self._sent_id()}

    async def sync(self, state: dict, on_batch) -> dict:
        state = dict(state or {})
        cursor = state.get("cursor") or ""
        if not cursor:
            state["cursor"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return state
        # One minute of overlap: the store is idempotent, so a repeat costs nothing.
        try:
            t = time.mktime(time.strptime(cursor[:19], "%Y-%m-%dT%H:%M:%S")) - 60
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(t))
        except ValueError:
            since = cursor
        done, newest = await self._pages(since, on_batch, descending=False)
        state["cursor"] = newest or cursor
        state["last_seen"] = done
        return state

    async def send(self, *, to, subject, body_text, body_html, in_reply_to="", references="",
                   reply_to="", from_name="", attachments=None) -> tuple[str, str, str]:
        recipients = [{"emailAddress": {"address": a}} for a in to]
        patch = {"subject": subject, "body": {"contentType": "HTML", "content": body_html or body_text},
                 "toRecipients": recipients}
        if reply_to and reply_to.lower() != self.address:
            patch["replyTo"] = [{"emailAddress": {"address": reply_to}}]
        draft = None
        if in_reply_to:
            try:
                found = await self._call("GET", f"{self._me}/messages",
                                         params={"$filter": f"internetMessageId eq '{in_reply_to.replace(chr(39), chr(39) * 2)}'",
                                                 "$select": "id,conversationId", "$top": 1})
            except LookupError:
                found = {}
            original = (found.get("value") or [{}])[0]
            if original.get("id"):
                draft = await self._call("POST", f"{self._me}/messages/{original['id']}/createReply", json={})
                draft = await self._call("PATCH", f"{self._me}/messages/{draft['id']}", json=patch) or draft
        if draft is None:
            draft = await self._call("POST", f"{self._me}/messages", json=patch)
        await self._call("POST", f"{self._me}/messages/{draft['id']}/send", json=None)
        message_id = draft.get("internetMessageId") or make_msgid(domain=self.address.split("@")[-1])
        return message_id, "", draft.get("conversationId") or ""
