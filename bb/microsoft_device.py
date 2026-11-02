"""Microsoft OAuth 2.0 device authorization flow.

BB displays Microsoft's *device* user_code. The owner enters this code only
on Microsoft's own verification_uri, never on a BB imitation login form.
No access or refresh tokens are returned to the browser.
"""
import httpx

from harvey.integrations.microsoft import SCOPE
from bb import microsoft_oauth

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def client_id(settings):
    return getattr(settings, "ms_device_client_id", "") or settings.ms_client_id


async def start(settings):
    cid = client_id(settings)
    if not cid:
        raise ValueError("Microsoft device authorization requires an app client ID.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                microsoft_oauth.authority(settings) + "/oauth2/v2.0/devicecode",
                data={"client_id": cid, "scope": SCOPE},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError("Could not reach Microsoft to start authorization.") from exc
    result = response.json()
    if response.status_code != 200:
        raise RuntimeError("Microsoft device authorization failed: " + str(
            result.get("error_description") or result.get("error") or response.status_code
        )[:240])
    if not all(result.get(k) for k in ("device_code", "user_code", "verification_uri", "expires_in")):
        raise RuntimeError("Microsoft did not return a complete device authorization.")
    uri = str(result["verification_uri"])
    if not uri.startswith("https://") or not (
        uri.split("/")[2].lower() in ("microsoft.com", "www.microsoft.com", "login.microsoftonline.com")
    ):
        raise RuntimeError("Microsoft returned an unexpected verification address.")
    return {
        "device_code": str(result["device_code"]),
        "user_code": str(result["user_code"]),
        "verification_uri": uri,
        "expires_in": max(60, min(int(result["expires_in"]), 1800)),
        "interval": max(5, min(int(result.get("interval") or 5), 60)),
        "client_id": cid,
        "scopes": SCOPE,
    }


async def poll(settings, device_code, cid):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                microsoft_oauth.authority(settings) + "/oauth2/v2.0/token",
                data={"client_id": cid, "grant_type": DEVICE_GRANT,
                      "device_code": device_code},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError("Microsoft authorization service is temporarily unreachable.") from exc
    result = response.json()
    if response.status_code == 200:
        if not result.get("access_token") or not result.get("refresh_token"):
            raise RuntimeError("Microsoft did not issue an offline-access authorization.")
        return "connected", result
    code = str(result.get("error") or "")
    if code in ("authorization_pending", "slow_down"):
        return code, {}
    if code in ("expired_token", "authorization_declined", "access_denied", "bad_verification_code"):
        return "failed", {"message": "Microsoft device authorization expired or was declined."}
    return "failed", {"message": "Microsoft authorization failed: " +
                       str(result.get("error_description") or code or response.status_code)[:200]}
