"""Sign-in setup done for the admin: their own sign-in does the steps.

**Microsoft.** The admin signs in on Microsoft's own page with the
account their browser is already signed in to (they type a short code
there; nothing reaches this site but a token for this one job). With that
sign-in the site registers the app in their directory through Microsoft
Graph, the steps of the Sign-in setup page in one call: the app, its two
redirect addresses, the mail permissions (Mail.ReadWrite, Mail.Send,
User.Read, offline_access) and a client secret. It then saves the app's
ID and secret like the form would, and the sign-in buttons work. The
sign-in uses Microsoft Graph Command Line Tools, Microsoft's own public
app for scripts that manage a directory (what ``Connect-MgGraph`` uses),
so no app has to exist first. The token is kept in memory for the
minutes this takes and then dropped.

**Google** has no such API: a site cannot create a Google sign-in client,
nor switch on the consent screen, for anyone. The page links each step
straight to its Google Cloud screen and takes the file Google offers when
the client is made (``client_secret_….json``), so nothing is copied by
hand; ``read_google_client`` reads it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("bb.signin_auto")

GRAPH_CLI_CLIENT = "14d82eec-204b-4c2f-b7e8-296a70dab67e"   # Microsoft Graph Command Line Tools
LOGIN = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SETUP_SCOPE = "https://graph.microsoft.com/Application.ReadWrite.All offline_access"
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
# Microsoft Graph's delegated permissions the mail sign-in asks for.
MAIL_SCOPES = {
    "Mail.ReadWrite": "024d486e-b451-40bb-833d-3e66d98c5c73",
    "Mail.Send": "e383f46e-2787-4529-855e-0e479a3ffac0",
    "User.Read": "e1fe6dd8-ba31-4d61-89e7-88639da4683d",
    "offline_access": "7427e0e9-2fba-42fe-b0c0-848c9e6a8182",
}


def redirect_uris(base_url: str) -> list[str]:
    return [base_url + "/microsoft/callback", base_url + "/mail/microsoft/callback"]


def app_body(base_url: str, audience: str = "AzureADandPersonalMicrosoftAccount") -> dict:
    """The app registration, as the Sign-in setup page's steps describe it."""
    host = base_url.split("://", 1)[-1].split("/", 1)[0]
    return {
        "displayName": f"BB mail sign-in ({host})"[:120],
        "signInAudience": audience,
        "api": {"requestedAccessTokenVersion": 2},     # required for personal accounts
        "web": {"redirectUris": redirect_uris(base_url)},
        "requiredResourceAccess": [{
            "resourceAppId": GRAPH_APP,
            "resourceAccess": [{"id": pid, "type": "Scope"} for pid in MAIL_SCOPES.values()],
        }],
        "passwordCredentials": [{"displayName": "BB site"}],
    }


@dataclass
class Job:
    """One admin's setup, while it runs and for a while after."""
    state: str = "starting"        # starting, waiting (for the sign-in), working, done, failed
    user_code: str = ""
    verification_uri: str = "https://microsoft.com/devicelogin"
    expires_at: float = 0.0
    steps: list = field(default_factory=list)
    error: str = ""
    done_note: str = ""
    client_id: str = ""            # what it saved, when done
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def view(self) -> dict:
        return {"state": self.state, "user_code": self.user_code, "verification_uri": self.verification_uri,
                "expires_in": max(0, int(self.expires_at - time.time())) if self.expires_at else 0,
                "steps": list(self.steps), "error": self.error, "done_note": self.done_note, "client_id": self.client_id}


_JOBS: dict[str, Job] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def job_for(admin_id: str) -> Job | None:
    job = _JOBS.get(admin_id)
    if job and job.state in ("done", "failed") and time.time() - job.started > 3600:
        _JOBS.pop(admin_id, None)
        return None
    if job and job.state == "starting" and job.task is None and time.time() - job.started > 120:
        _JOBS.pop(admin_id, None)        # a start that never got its code
        return None
    return job


def cancel(admin_id: str, force: bool = False) -> bool:
    """Stop waiting for the sign-in. Once it is setting things up (the app
    is being registered and saved, workers restarted) it runs to its end:
    stopping halfway could leave workers stopped. True when stopped."""
    job = _JOBS.get(admin_id)
    if job and job.state == "working" and not force:
        return False
    _JOBS.pop(admin_id, None)
    if job and job.task and not job.task.done():
        job.task.cancel()
    return True


def forget(admin_id: str):
    """The settings were changed by hand: a finished job's report is old news."""
    job = _JOBS.get(admin_id)
    if job and job.state in ("done", "failed"):
        _JOBS.pop(admin_id, None)


def _why(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if not isinstance(data, dict):
        return resp.text[:200]
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "")[:300]
    return str(data.get("error_description") or err or resp.text)[:300]


def _words(e: BaseException) -> str:
    return str(e) or type(e).__name__


def _client():
    return httpx.AsyncClient(timeout=30)


_pause = asyncio.sleep          # between looks at the sign-in (tests make it instant)


async def start(admin_id: str, base_url: str, save, client_factory=_client, current_client_id: str = "") -> Job:
    """Begin: ask Microsoft for a sign-in code, then wait for the admin in
    the background. ``save(client_id, secret, tenant)`` stores the result
    (returns problems in words, or [] when it worked). With
    ``current_client_id`` (the app this site uses now) that app is updated
    and given a new secret rather than replaced, so mailboxes already
    signed in keep working."""
    lock = _LOCKS.setdefault(admin_id, asyncio.Lock())
    async with lock:                       # two clicks never start two sign-ins
        if not cancel(admin_id):
            return _JOBS[admin_id]         # one is setting things up right now: that one goes on
        job = Job()
        _JOBS[admin_id] = job
        try:
            async with client_factory() as http:
                r = await http.post(f"{LOGIN}/devicecode", data={"client_id": GRAPH_CLI_CLIENT, "scope": SETUP_SCOPE})
            if r.status_code >= 400:
                raise RuntimeError(f"Microsoft did not start the sign-in: {_why(r)}")
            data = r.json()
            if not isinstance(data, dict) or not data.get("device_code") or not data.get("user_code"):
                raise RuntimeError("Microsoft's answer had no sign-in code in it.")
        except Exception as e:
            job.state = "failed"
            job.error = (_words(e) if isinstance(e, RuntimeError) else f"Could not reach Microsoft: {_words(e)}")[:500]
            return job
        job.user_code = str(data.get("user_code", ""))
        job.verification_uri = str(data.get("verification_uri") or job.verification_uri)
        job.expires_at = time.time() + int(data.get("expires_in") or 900)
        job.state = "waiting"
        job.task = asyncio.ensure_future(_finish(job, data, base_url, save, client_factory, current_client_id))
        return job


def _merge_app(app: dict, base_url: str) -> dict:
    """The changes that make an existing app what this site needs: its
    redirect addresses kept and ours added, the mail permissions added."""
    uris = list((app.get("web") or {}).get("redirectUris") or [])
    for u in redirect_uris(base_url):
        if u not in uris:
            uris.append(u)
    access = [dict(r) for r in app.get("requiredResourceAccess") or []]
    graph = next((r for r in access if r.get("resourceAppId") == GRAPH_APP), None)
    if graph is None:
        graph = {"resourceAppId": GRAPH_APP, "resourceAccess": []}
        access.append(graph)
    have = {x.get("id") for x in graph.get("resourceAccess") or []}
    graph["resourceAccess"] = list(graph.get("resourceAccess") or []) + [
        {"id": pid, "type": "Scope"} for pid in MAIL_SCOPES.values() if pid not in have]
    return {"web": {"redirectUris": uris}, "requiredResourceAccess": access}


async def _finish(job: Job, code: dict, base_url: str, save, client_factory, current_client_id: str = ""):
    try:
        token = await _wait_for_sign_in(job, code, client_factory)
        if not token:
            return
        job.state = "working"
        job.steps.append("Signed in to Microsoft.")
        # From here on it runs to its end, Cancel or not: the app, its secret
        # and the save (which restarts workers) are not left halfway.
        await asyncio.shield(_set_up(job, token, base_url, save, client_factory, current_client_id))
    except asyncio.CancelledError:
        if job.state != "working":
            raise
    except Exception as e:
        logger.warning("Microsoft sign-in setup failed: %s", _words(e))
        job.state, job.error = "failed", _words(e)[:500]


async def _set_up(job: Job, token: str, base_url: str, save, client_factory, current_client_id: str):
    try:
        async with client_factory() as http:
            headers = {"Authorization": f"Bearer {token}"}
            orgs_only = " Personal Outlook and Hotmail accounts cannot use it on this site's address; work and school accounts can."
            # Personal accounts need an https address (or localhost); without
            # one, or when Microsoft refuses it, the app is for work and
            # school accounts only.
            local = any(h in base_url for h in ("://localhost", "://127.0.0.1"))
            audience = "AzureADandPersonalMicrosoftAccount" if base_url.startswith("https://") or local else "AzureADMultipleOrgs"
            note = "" if audience.endswith("PersonalMicrosoftAccount") else orgs_only
            app, secret, reused = None, "", False
            if current_client_id:
                # The app this site already uses: updated in place, so every
                # mailbox signed in with it keeps working.
                r = await http.get(f"{GRAPH}/applications(appId='{current_client_id}')", headers=headers,
                                   params={"$select": "id,appId,web,requiredResourceAccess,signInAudience"})
                if r.status_code < 400:
                    app = r.json()
                    r = await http.patch(f"{GRAPH}/applications/{app['id']}", json=_merge_app(app, base_url), headers=headers)
                    if r.status_code >= 400:
                        raise RuntimeError(f"Microsoft would not update this site's app: {_why(r)}")
                    reused = True
                    audience = app.get("signInAudience") or audience
                    note = "" if str(audience).endswith("PersonalMicrosoftAccount") else orgs_only
                    job.steps.append("Updated this site's app: both sign-in addresses and the mail permissions are on it.")
                else:
                    job.steps.append("This site's current app is not in this directory (or not yours), so a new one is made: "
                                     "Microsoft mailboxes signed in with the old one will need Reconnect.")
            if app is None:
                r = await http.post(f"{GRAPH}/applications", json=app_body(base_url, audience), headers=headers)
                if r.status_code == 400 and audience.endswith("PersonalMicrosoftAccount"):
                    first = _why(r)
                    audience, note = "AzureADMultipleOrgs", orgs_only
                    r = await http.post(f"{GRAPH}/applications", json=app_body(base_url, audience), headers=headers)
                    if r.status_code == 400:
                        logger.info("both app shapes refused; the first said: %s", first)
                if r.status_code >= 400:
                    why = _why(r)
                    if r.status_code == 403 or "privilege" in why.lower() or "authorization" in why.lower():
                        why += (" Your account may not register apps in this directory: an admin of it can, or use a "
                                "directory of your own (a free Microsoft 365 developer account has one).")
                    raise RuntimeError(f"Microsoft would not register the app: {why}")
                app = r.json()
                secret = next((p.get("secretText") for p in app.get("passwordCredentials") or [] if p.get("secretText")), "")
                job.steps.append("Registered the app with both sign-in addresses and the mail permissions.")
            client_id = app.get("appId", "")
            if not secret:
                r = await http.post(f"{GRAPH}/applications/{app.get('id')}/addPassword",
                                    json={"passwordCredential": {"displayName": "BB site"}}, headers=headers)
                if r.status_code >= 400:
                    raise RuntimeError(f"The app is ready, but Microsoft would not make its secret: {_why(r)}")
                secret = r.json().get("secretText", "")
            if not client_id or not secret:
                raise RuntimeError("Microsoft's answer had no app ID or secret in it.")
            job.steps.append("Made the client secret.")
        problems = save(client_id, secret, "common" if str(audience).endswith("PersonalMicrosoftAccount") else "organizations")
        if asyncio.iscoroutine(problems):
            problems = await problems
        if problems:
            raise RuntimeError(" ".join(problems))
        job.client_id = client_id
        job.steps.append("Saved: Sign in with Microsoft is on.")
        job.done_note = (("Mailboxes already signed in keep working. " if reused else "")
                         + "Give Microsoft a minute to spread the change before the first sign-in." + note).strip()
        job.state = "done"
    except Exception as e:
        logger.warning("Microsoft sign-in setup failed: %s", _words(e))
        job.state, job.error = "failed", _words(e)[:500]


async def _wait_for_sign_in(job: Job, code: dict, client_factory) -> str:
    """Poll until the admin has signed in and agreed (the token), or the
    code ran out or was refused (then "" and the job says why). A network
    hiccup, a 429 or a 5xx is waited out, not taken as an answer."""
    interval = max(2, int(code.get("interval") or 5))
    async with client_factory() as http:
        while time.time() < job.expires_at:
            await _pause(interval)
            try:
                r = await http.post(f"{LOGIN}/token", data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": GRAPH_CLI_CLIENT, "device_code": code.get("device_code", "")})
            except httpx.HTTPError as e:
                logger.info("sign-in poll: %s; trying again", _words(e))
                interval = min(interval + 2, 30)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                interval = min(interval + 5, 30)
                continue
            try:
                answer = r.json()
            except ValueError:
                answer = {}
            if not isinstance(answer, dict):
                answer = {}
            if r.status_code < 400:
                if answer.get("access_token"):
                    return answer["access_token"]
                job.state, job.error = "failed", "Microsoft's answer to the sign-in had no token in it."
                return ""
            err = answer.get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("authorization_declined", "access_denied"):
                job.state, job.error = "failed", "The sign-in was declined on Microsoft's page, so nothing was set up."
            elif err in ("expired_token", "code_expired"):
                job.state, job.error = "failed", "The code ran out before the sign-in. Start again for a new one."
            else:
                why = _why(r)
                if "AADSTS65001" in why or "consent" in why.lower():
                    why += " (An admin of the directory has to agree to it once.)"
                job.state, job.error = "failed", f"Microsoft refused the sign-in: {why}"
            return ""
    job.state, job.error = "failed", "The code ran out before the sign-in. Start again for a new one."
    return ""


# ── Google ──


def read_google_client(raw: bytes | str, base_url: str) -> tuple[dict, list[str], list[str]]:
    """The client_secret_….json Google offers when a client is made:
    ({gmail_client_id, gmail_client_secret}, problems, warnings). A
    problem means nothing can be used; a warning (a redirect address
    missing from the client) means sign-in fails until it is added."""
    try:
        data = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return {}, ["That is not the JSON file Google gives for a client (client_secret_….json)."], []
    not_it = ["That is not the JSON file Google gives for a client (client_secret_….json)."]
    if not isinstance(data, dict):
        return {}, not_it, []
    if "web" not in data:
        kind = next(iter(data), "")
        if kind == "installed":
            return {}, ["That client is a Desktop app. Create one of type Web application (step 4) and use its file."], []
        return {}, ["That file has no Web application client in it (client_secret_….json from Google Auth Platform → Clients)."], []
    web = data["web"] or {}
    if not isinstance(web, dict) or not isinstance(web.get("redirect_uris") or [], list):
        return {}, not_it, []
    values = {"gmail_client_id": str(web.get("client_id") or "").strip(),
              "gmail_client_secret": str(web.get("client_secret") or "").strip()}
    problems = []
    if not values["gmail_client_id"]:
        problems.append("The file has no client ID in it.")
    if not values["gmail_client_secret"]:
        problems.append("The file has no client secret in it: Google shows the secret, and puts it in the file, only "
                        "when the client is made. Make a new secret for the client and download the file again.")
    have = {str(u).strip().rstrip("/") for u in web.get("redirect_uris") or [] if isinstance(u, str)}
    missing = [u for u in (base_url + "/gmail/callback", base_url + "/mail/gmail/callback") if u.rstrip("/") not in have]
    warnings = []
    if missing:
        warnings.append("The client does not list " + " and ".join(missing) + " as an authorized redirect URI: "
                        "add it under the client's Authorized redirect URIs, or Google refuses the sign-in.")
    return values, problems, warnings
