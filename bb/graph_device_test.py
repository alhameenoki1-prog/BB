"""Temporary Microsoft Graph Security Lab diagnostic.

Uses Microsoft device-code authentication and delegated User.Read.
Does not persist access tokens or refresh tokens.
"""

import os
import msal
import httpx


SCOPES = ["User.Read"]


def run_graph_test():
    client_id = os.environ.get("BB_GRAPH_TEST_CLIENT_ID", "")
    tenant_id = os.environ.get("BB_GRAPH_TEST_TENANT_ID", "")
    allowed_user = os.environ.get("BB_GRAPH_TEST_ALLOWED_USER_ID", "")

    if not all((client_id, tenant_id, allowed_user)):
        return {"success": False, "message": "Graph test configuration is incomplete."}

    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=msal.TokenCache(),
    )

    flow = app.initiate_device_flow(scopes=SCOPES)

    if "user_code" not in flow:
        return {"success": False, "message": "Microsoft device flow could not start."}

    print(flow["message"])

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        return {
            "success": False,
            "message": result.get("error_description", "Microsoft authentication failed."),
        }

    with httpx.Client(timeout=15) as client:
        response = client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {result['access_token']}"},
        )

    if response.status_code != 200:
        return {"success": False, "graph_status": response.status_code}

    profile = response.json()

    if profile.get("id", "").lower() != allowed_user.lower():
        return {
            "success": False,
            "message": "Authenticated account does not match the permitted test account.",
        }

    return {
        "success": True,
        "graph_status": 200,
        "account": profile.get("userPrincipalName", ""),
        "message": "Microsoft Graph authentication succeeded.",
    }
