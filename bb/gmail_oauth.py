"""Gmail OAuth for a hosted site: the web redirect flow.

Harvey's own ``harvey gmail auth`` opens a browser on the operator's machine
and listens on localhost. On a server there is no local browser, so the
site sends the user to Google with a redirect back to
``BB_BASE_URL/gmail/callback``, exchanges the code there, and stores the
refresh token in the user's workspace in exactly the file format
``harvey.integrations.gmail`` reads. The Google Cloud OAuth client must be
of type "Web application" with that callback URL registered.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

import httpx

from harvey.integrations.gmail import API_BASE, AUTH_URL, SCOPE, TOKEN_URL

STATE_TTL_SECONDS = 600


def redirect_uri(settings) -> str:
    return settings.base_url + "/gmail/callback"


def sign_state(secret: str, user_id: str, nonce: str, now: float | None = None) -> str:
    stamp = str(int(now or time.time()))
    msg = f"{user_id}:{nonce}:{stamp}"
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


def auth_url(settings, state: str, login_hint: str = "") -> str:
    params = {
        "client_id": settings.gmail_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


async def exchange_code(settings, code: str, redirect_uri: str = "") -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri or globals()["redirect_uri"](settings),
        })
    if resp.status_code != 200:
        raise RuntimeError(f"Google rejected the sign-in ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    if not data.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token. Remove this app from your Google "
                           "account's third-party access list and connect again.")
    return data


async def refresh_access_token(settings, refresh_token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh rejected ({resp.status_code}).")
    return resp.json().get("access_token", "")


async def account_email(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(API_BASE + "/profile",
                                headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        raise RuntimeError(f"Could not read the Gmail profile ({resp.status_code}).")
    return str(resp.json().get("emailAddress") or "")


def save_token(workspace, data: dict, email: str):
    workspace.ensure()
    workspace.gmail_token_file.write_text(json.dumps({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    }, indent=2))
    try:
        workspace.gmail_token_file.chmod(0o600)
    except OSError:
        pass
    workspace.gmail_account_file.write_text(json.dumps({"email": email, "connected_at": int(time.time())}))


async def test_connection(settings, workspace) -> tuple[bool, str]:
    try:
        stored = json.loads(workspace.gmail_token_file.read_text())
        token = await refresh_access_token(settings, stored["refresh_token"])
        email = await account_email(token)
        return True, f"Connected as {email}"
    except (OSError, ValueError, KeyError):
        return False, "Gmail is not connected yet."
    except Exception as e:
        return False, str(e)
