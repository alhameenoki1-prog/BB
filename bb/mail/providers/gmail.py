"""Gmail through OAuth and the Gmail API. The refresh token is the stored
credential; access tokens are minted as needed. A refresh that Google
refuses (``invalid_grant``, the usual ~7-day expiry of a testing-mode
client) raises AuthError, and the mailbox shows "Reconnect"."""

import time

import httpx

from bb.mail.providers.base import AuthError, MailAccount, TransientError

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


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
