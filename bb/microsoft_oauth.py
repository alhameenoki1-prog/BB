"""Microsoft sign-in for a hosted site: the web redirect flow.

The same shape as ``bb.gmail_oauth``: the site sends the user to
Microsoft (personal Outlook/Hotmail or a work/school Microsoft 365
account), Microsoft sends them back to ``BB_BASE_URL/microsoft/callback``
with a code, the site exchanges it for a refresh token and stores that in
the user's workspace in the file ``harvey.integrations.microsoft`` reads
(``data/m365_token.json``). The app registration in Entra must be a Web
platform app with that callback URL (and ``/mail/microsoft/callback`` for
the command center), the delegated permissions Mail.ReadWrite, Mail.Send,
User.Read and offline_access, and "accounts in any organizational
directory and personal Microsoft accounts" when both kinds should work.

The user signs in on Microsoft's own page with the session their browser
already has; no password ever reaches this site. The refresh token keeps
working after they change their password; only a revoked authorization,
a disconnect here, or a tenant policy that revokes tokens on password
change ends it, and then the pages show Reconnect.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import httpx

from harvey.integrations.microsoft import LOGIN, SCOPE

GRAPH = "https://graph.microsoft.com/v1.0"
STATE_TTL_SECONDS = 600


def redirect_uri(settings) -> str:
    return settings.base_url + "/microsoft/callback"


def sign_state(secret: str, user_id: str, nonce: str, now: float | None = None) -> str:
    stamp = str(int(now or time.time()))
    msg = f"ms:{user_id}:{nonce}:{stamp}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{stamp}.{sig}"


def verify_state(secret: str, user_id: str, nonce: str, state: str) -> bool:
    try:
        stamp, _ = state.split(".", 1)
        if time.time() - int(stamp) > STATE_TTL_SECONDS:
            return False
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(sign_state(secret, user_id, nonce, float(stamp)), state)


def authority(settings) -> str:
    return f"{LOGIN}/{settings.ms_tenant or 'common'}"


def auth_url(settings, state: str, login_hint: str = "", redirect: str = "") -> str:
    params = {
        "client_id": settings.ms_client_id,
        "redirect_uri": redirect or redirect_uri(settings),
        "response_type": "code",
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return authority(settings) + "/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params)


def _why(resp) -> str:
    try:
        return str(resp.json().get("error_description") or resp.text)[:200]
    except ValueError:
        return resp.text[:200]


async def exchange_code(settings, code: str, redirect: str = "") -> dict:
    data = {
        "client_id": settings.ms_client_id,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect or redirect_uri(settings),
        "scope": SCOPE,
    }
    if settings.ms_client_secret:
        data["client_secret"] = settings.ms_client_secret
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(authority(settings) + "/oauth2/v2.0/token", data=data)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Microsoft could not be reached: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Microsoft rejected the sign-in ({resp.status_code}): {_why(resp)}")
    out = resp.json()
    if not out.get("refresh_token"):
        raise RuntimeError("Microsoft returned no refresh token (offline_access was not granted). Try again and allow "
                           "\"Maintain access to data you have given it access to\".")
    return out


async def refresh_access_token(settings, refresh_token: str) -> dict:
    """{access_token, refresh_token?}: Microsoft may rotate the refresh token;
    the caller stores the new one when it does."""
    data = {"client_id": settings.ms_client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": SCOPE}
    if settings.ms_client_secret:
        data["client_secret"] = settings.ms_client_secret
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(authority(settings) + "/oauth2/v2.0/token", data=data)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Microsoft could not be reached: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Microsoft no longer accepts this sign-in ({resp.status_code}): {_why(resp)}. Reconnect the mailbox.")
    return resp.json()


async def account_email(access_token: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(GRAPH + "/me", headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"Microsoft could not be reached: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Could not read the Microsoft profile ({resp.status_code}).")
    me = resp.json()
    return str(me.get("mail") or me.get("userPrincipalName") or "").lower()


def save_token(workspace, data: dict, email: str):
    workspace.ensure()
    workspace.microsoft_token_file.write_text(json.dumps({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    }, indent=2))
    try:
        workspace.microsoft_token_file.chmod(0o600)
    except OSError:
        pass
    workspace.microsoft_account_file.write_text(json.dumps({"email": email, "connected_at": int(time.time())}))


async def test_connection(settings, workspace) -> tuple[bool, str]:
    try:
        stored = json.loads(workspace.microsoft_token_file.read_text())
        fresh = await refresh_access_token(settings, stored["refresh_token"])
        if fresh.get("refresh_token") and fresh["refresh_token"] != stored["refresh_token"]:
            workspace.microsoft_token_file.write_text(json.dumps({**stored, "refresh_token": fresh["refresh_token"],
                                                                  "refreshed_at": int(time.time())}, indent=2))
        email = await account_email(fresh.get("access_token", ""))
        return True, f"Connected as {email}"
    except (OSError, ValueError, KeyError):
        return False, "Microsoft is not connected yet."
    except Exception as e:
        return False, str(e)
