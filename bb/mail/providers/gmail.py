"""Gmail through OAuth and the Gmail API. The refresh token is the stored
credential; access tokens are minted as needed. A refresh that Google
refuses (``invalid_grant``, the usual ~7-day expiry of a testing-mode
client) raises AuthError, and the mailbox shows "Reconnect".

Backfill lists messages (optionally only the last N days) and reads each
one in full; incremental sync follows the History API from the
historyId recorded at backfill, falling back to a short re-list when
Google says the history is too old."""

import base64
import re
import time
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import httpx

from bb.mail.providers.base import AuthError, MailAccount, RawMessage, TransientError

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
PAGE = 100


class GmailAccount(MailAccount):
    name = "gmail"

    def __init__(self, mailbox, creds, env):
        super().__init__(mailbox, creds, env)
        self._access = ""
        self._expires = 0.0

    async def _token(self) -> str:
        if self._access and time.time() < self._expires - 60:
            return self._access
        client_id, client_secret = self.env.get("GMAIL_CLIENT_ID", ""), self.env.get("GMAIL_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise TransientError("the site has no Google OAuth client configured")
        if not self.creds.get("refresh_token"):
            raise AuthError("no refresh token stored")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(TOKEN_URL, data={
                    "client_id": client_id, "client_secret": client_secret,
                    "refresh_token": self.creds["refresh_token"], "grant_type": "refresh_token"})
        except httpx.HTTPError as e:
            raise TransientError(f"could not reach Google: {e}") from e
        if r.status_code == 400 and "invalid_grant" in r.text:
            raise AuthError("Google no longer accepts this sign-in (the token expired or was revoked)")
        if r.status_code >= 400:
            raise TransientError(f"Google token error {r.status_code}: {r.text[:160]}")
        data = r.json()
        self._access = data.get("access_token", "")
        self._expires = time.time() + float(data.get("expires_in", 3600))
        if not self._access:
            raise AuthError("Google returned no access token")
        return self._access

    async def _get(self, path: str, params: dict | None = None) -> dict:
        token = await self._token()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(API + path, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise TransientError(f"could not reach Gmail: {e}") from e
        if r.status_code in (401, 403):
            raise AuthError(f"Gmail refused the request ({r.status_code}): reconnect the account")
        if r.status_code == 404:
            raise LookupError(path)
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"Gmail answered {r.status_code}")
        if r.status_code >= 400:
            raise TransientError(f"Gmail error {r.status_code}: {r.text[:160]}")
        return r.json()

    async def test(self) -> str:
        profile = await self._get("/profile")
        address = str(profile.get("emailAddress", "")).lower()
        if address and self.address and address != self.address:
            raise AuthError(f"signed in as {address}, not {self.address}")
        total = profile.get("messagesTotal")
        return f"Connected as {address or self.address}" + (f"; {int(total):,} messages." if total else ".")

    # ── parsing ──

    @staticmethod
    def _text(payload: dict) -> str:
        def decode(part) -> str:
            data = (part.get("body") or {}).get("data", "")
            if not data:
                return ""
            try:
                return base64.urlsafe_b64decode(data + "===").decode(errors="replace")
            except Exception:
                return ""
        mime = payload.get("mimeType", "")
        if mime.startswith("text/plain"):
            return decode(payload)
        for part in payload.get("parts", []) or []:
            text = GmailAccount._text(part)
            if text:
                return text
        if mime.startswith("text/html"):
            html = decode(payload)
            html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
            html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
            return re.sub(r"<[^>]+>", " ", html)
        return ""

    def parse(self, data: dict) -> RawMessage | None:
        payload = data.get("payload") or {}
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        from_name, from_email = parseaddr(headers.get("from", ""))
        from_email = (from_email or "").lower()
        if not from_email and not data.get("id"):
            return None
        labels = data.get("labelIds") or []
        outbound = "SENT" in labels or from_email == self.address
        sent_at = ""
        try:
            ms = int(data.get("internalDate", 0))
            if ms:
                from datetime import datetime, timezone
                sent_at = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            pass
        if not sent_at and headers.get("date"):
            try:
                sent_at = parsedate_to_datetime(headers["date"]).isoformat()
            except Exception:
                sent_at = ""
        body = self._text(payload) or data.get("snippet", "")
        keep = {k: v[:500] for k, v in headers.items() if k in (
            "message-id", "in-reply-to", "references", "list-unsubscribe", "list-id", "precedence",
            "auto-submitted", "x-autoreply", "x-auto-response-suppress", "reply-to", "from", "to", "cc", "date")}
        folder = "SENT" if "SENT" in labels else ("INBOX" if "INBOX" in labels else (labels[0] if labels else ""))
        return RawMessage(
            provider_id=data.get("id", ""), thread_key=data.get("threadId", ""),
            message_id=headers.get("message-id", ""), in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""), from_email=from_email, from_name=from_name or "",
            to=[{"name": n, "email": a.lower()} for n, a in getaddresses([headers.get("to", "")]) if a],
            cc=[{"name": n, "email": a.lower()} for n, a in getaddresses([headers.get("cc", "")]) if a],
            subject=headers.get("subject", ""), body=body, snippet=data.get("snippet", "")[:160],
            headers=keep, folder=folder, sent_at=sent_at, outbound=outbound,
        )

    async def _read_many(self, ids: list[str]) -> list[RawMessage]:
        out = []
        for mid in ids:
            try:
                data = await self._get(f"/messages/{mid}", params={"format": "full"})
            except LookupError:
                continue   # deleted between listing and reading
            msg = self.parse(data)
            if msg:
                out.append(msg)
        return out

    # ── backfill and sync ──

    async def backfill(self, since_days: int, on_batch) -> dict:
        profile = await self._get("/profile")
        start_history = str(profile.get("historyId", ""))
        params = {"maxResults": PAGE, "includeSpamTrash": "false"}
        if since_days and since_days > 0:
            params["q"] = f"newer_than:{int(since_days)}d"
        total_hint = int(profile.get("messagesTotal") or 0)
        done = 0
        page_token = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            listing = await self._get("/messages", params=params)
            ids = [m["id"] for m in listing.get("messages", []) or []]
            if ids:
                msgs = await self._read_many(ids)
                done += len(ids)
                await on_batch(msgs, done, max(total_hint, done) if not since_days else max(done, int(listing.get("resultSizeEstimate") or done)))
            page_token = listing.get("nextPageToken")
            if not page_token:
                break
        return {"historyId": start_history, "backfill_done": True, "total_seen": done}

    async def sync(self, state: dict, on_batch) -> dict:
        state = dict(state or {})
        history_id = str(state.get("historyId") or "")
        if not history_id:
            profile = await self._get("/profile")
            state["historyId"] = str(profile.get("historyId", ""))
            return state
        params = {"startHistoryId": history_id, "historyTypes": "messageAdded", "maxResults": 500}
        ids: list[str] = []
        newest = history_id
        try:
            while True:
                data = await self._get("/history", params=params)
                for h in data.get("history", []) or []:
                    for added in h.get("messagesAdded", []) or []:
                        m = added.get("message") or {}
                        if m.get("id") and m["id"] not in ids:
                            ids.append(m["id"])
                newest = str(data.get("historyId") or newest)
                if not data.get("nextPageToken"):
                    break
                params["pageToken"] = data["nextPageToken"]
        except LookupError:
            # History too old: re-list the last week (a few pages at most)
            # and move the cursor forward.
            params = {"maxResults": PAGE, "q": "newer_than:7d"}
            for _ in range(10):
                listing = await self._get("/messages", params=params)
                ids += [m["id"] for m in listing.get("messages", []) or [] if m["id"] not in ids]
                if not listing.get("nextPageToken"):
                    break
                params["pageToken"] = listing["nextPageToken"]
            profile = await self._get("/profile")
            newest = str(profile.get("historyId", newest))
        if ids:
            msgs = await self._read_many(ids)
            await on_batch(msgs, len(msgs), len(msgs))
        state["historyId"] = newest
        state["backfill_done"] = True
        return state
