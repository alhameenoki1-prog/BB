"""Temporary, isolated Microsoft Graph authentication diagnostic."""

import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
import msal
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

SCOPES = ["User.Read"]
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
FLOW_TTL = 600

_pending = {}
_lock = threading.Lock()


def config():
    return {
        "client_id": os.environ["BB_GRAPH_TEST_CLIENT_ID"],
        "tenant_id": os.environ["BB_GRAPH_TEST_TENANT_ID"],
        "secret": os.environ["BB_GRAPH_TEST_CLIENT_SECRET"],
        "allowed_user": os.environ["BB_GRAPH_TEST_ALLOWED_USER_ID"],
    }


def callback_url(request: Request):
    # Fixed configured origin; never trust arbitrary Host headers.
    base_url = os.environ["BB_BASE_URL"].rstrip("/")
    return base_url + "/diagnostics/microsoft/callback"


def client(cfg):
    return msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        client_credential=cfg["secret"],
        authority=f'https://login.microsoftonline.com/{cfg["tenant_id"]}',
        token_cache=msal.TokenCache(),
    )


def result_page(title, message):
    import html
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Microsoft Graph Security Lab</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f5f6f8;
display:grid;place-items:center;min-height:100vh;margin:0}}
main{{background:white;padding:36px;border-radius:12px;
max-width:460px;box-shadow:0 8px 30px #0001}}
a{{color:#2563eb}}
</style>
</head>
<body><main>
<h2>{html.escape(title)}</h2>
<p>{html.escape(message)}</p>
<a href="/diagnostics/microsoft">Return to diagnostic</a>
</main></body></html>"""
    )


@router.post("/diagnostics/microsoft/start")
def start(request: Request):
    cfg = config()
    auth_client = client(cfg)

    flow = auth_client.initiate_auth_code_flow(
        scopes=SCOPES,
        redirect_uri=callback_url(request),
        prompt="select_account",
    )

    if "auth_uri" not in flow or "state" not in flow:
        return result_page("Authentication unavailable", "Could not start Microsoft sign-in.")

    browser_binding = secrets.token_urlsafe(32)
    binding_hash = hashlib.sha256(browser_binding.encode()).hexdigest()

    with _lock:
        now = time.monotonic()
        for key, item in list(_pending.items()):
            if now - item["created"] > FLOW_TTL:
                _pending.pop(key, None)

        _pending[flow["state"]] = {
            "flow": flow,
            "created": now,
            "binding": binding_hash,
        }

    response = RedirectResponse(flow["auth_uri"], status_code=303)
    response.set_cookie(
        "bb_graph_test_binding",
        browser_binding,
        max_age=FLOW_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/diagnostics/microsoft",
    )
    return response


@router.get("/diagnostics/microsoft/callback")
def callback(request: Request):
    state = request.query_params.get("state", "")
    binding = request.cookies.get("bb_graph_test_binding", "")

    with _lock:
        item = _pending.pop(state, None)

    if not item or not binding:
        return result_page("Session expired", "Start a new Microsoft sign-in test.")

    actual_hash = hashlib.sha256(binding.encode()).hexdigest()

    if (
        time.monotonic() - item["created"] > FLOW_TTL
        or not hmac.compare_digest(actual_hash, item["binding"])
    ):
        return result_page("Invalid session", "The authentication session could not be verified.")

    cfg = config()

    try:
        result = client(cfg).acquire_token_by_auth_code_flow(
            item["flow"],
            dict(request.query_params),
        )
    except ValueError:
        return result_page("Authentication rejected", "Microsoft sign-in validation failed.")

    token = result.get("access_token")

    if not token:
        return result_page("Authentication failed", "Microsoft did not complete authentication.")

    try:
        with httpx.Client(timeout=15) as http:
            response = http.get(
                GRAPH_ME,
                headers={"Authorization": f"Bearer {token}"},
            )

        if response.status_code != 200:
            return result_page(
                "Graph diagnostic failed",
                f"Microsoft Graph returned HTTP {response.status_code}.",
            )

        profile = response.json()

        if profile.get("id", "").lower() != cfg["allowed_user"].lower():
            return result_page(
                "Account not authorized",
                "This Microsoft account is not permitted for the diagnostic.",
            )

        return result_page(
            "Microsoft Graph authentication successful",
            "The authorized test account was verified and Microsoft Graph returned HTTP 200.",
        )
    except httpx.HTTPError:
        return result_page(
            "Connection failed",
            "The Microsoft Graph request could not be completed.",
        )
