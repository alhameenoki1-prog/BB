"""Sign-in setup done from the admin's own sign-in: Microsoft's app is
registered through Graph, Google's client file is read as dropped."""

import asyncio
import json

import httpx
import pytest

from bb import signin_auto
from tests.test_bb_web import client, settings  # noqa: F401


def _transport(log, token_answers, app_status=201, app_body=None):
    answers = list(token_answers)

    def handler(request: httpx.Request):
        log.append((request.method, str(request.url), request.content.decode() if request.content else ""))
        url = str(request.url)
        if url.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "dev-1", "user_code": "ABCD-1234", "interval": 1, "expires_in": 900,
                                             "verification_uri": "https://microsoft.com/devicelogin"})
        if url.endswith("/token"):
            return answers.pop(0)
        if url.endswith("/applications"):
            body = app_body if app_body is not None else {
                "id": "obj-1", "appId": "11111111-2222-3333-4444-555555555555",
                "passwordCredentials": [{"secretText": "s3cret~value"}]}
            return httpx.Response(app_status, json=body)
        if url.endswith("/addPassword"):
            return httpx.Response(200, json={"secretText": "added~secret"})
        return httpx.Response(404, json={"error": {"message": "unexpected " + url}})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_microsoft_is_set_up_from_the_admins_sign_in(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(signin_auto, "_pause", instant)
    log, saved = [], []
    transport = _transport(log, [httpx.Response(400, json={"error": "authorization_pending"}),
                                 httpx.Response(400, json={"error": "slow_down"}),
                                 httpx.Response(200, json={"access_token": "tok"})])

    def save(client_id, secret, tenant):
        saved.append((client_id, secret, tenant))
        return []

    job = await signin_auto.start("admin-1", "https://bb.example.com", save,
                                  client_factory=lambda: httpx.AsyncClient(transport=transport))
    assert job.state == "waiting" and job.user_code == "ABCD-1234" and signin_auto.job_for("admin-1") is job
    await job.task
    assert job.state == "done", job.error
    assert saved == [("11111111-2222-3333-4444-555555555555", "s3cret~value", "common")]
    # Microsoft's public app for scripts, asking only to manage app registrations.
    code = next(c for m, u, c in log if u.endswith("/devicecode"))
    assert "client_id=14d82eec-204b-4c2f-b7e8-296a70dab67e" in code and "Application.ReadWrite.All" in code
    # The app as the manual steps describe it: both addresses, the mail permissions, a secret.
    app = json.loads(next(c for m, u, c in log if u.endswith("/applications")))
    assert app["web"]["redirectUris"] == ["https://bb.example.com/microsoft/callback", "https://bb.example.com/mail/microsoft/callback"]
    assert app["signInAudience"] == "AzureADandPersonalMicrosoftAccount" and app["api"]["requestedAccessTokenVersion"] == 2
    ids = {a["id"] for a in app["requiredResourceAccess"][0]["resourceAccess"]}
    assert ids == set(signin_auto.MAIL_SCOPES.values()) and app["passwordCredentials"]
    assert "Saved: Sign in with Microsoft is on." in job.steps
    signin_auto.cancel("admin-1")


@pytest.mark.asyncio
async def test_microsoft_setup_says_why_it_stopped(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(signin_auto, "_pause", instant)
    factory = lambda t: (lambda: httpx.AsyncClient(transport=t))   # noqa: E731
    # Declined on Microsoft's page: nothing is set up.
    log = []
    job = await signin_auto.start("a", "https://bb.example.com", lambda *a: [],
                                  client_factory=factory(_transport(log, [httpx.Response(400, json={"error": "authorization_declined"})])))
    await job.task
    assert job.state == "failed" and "declined" in job.error and not any(u.endswith("/applications") for _, u, _ in log)
    # An account that may not register apps: the reason, and what to do.
    t = _transport([], [httpx.Response(200, json={"access_token": "tok"})], app_status=403,
                   app_body={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges to complete the operation."}})
    job = await signin_auto.start("a", "https://bb.example.com", lambda *a: [], client_factory=factory(t))
    await job.task
    assert job.state == "failed" and "Insufficient privileges" in job.error and "an admin of it can" in job.error
    # A plain-http address (not localhost): work and school accounts only, said so.
    log, saved = [], []
    t = _transport(log, [httpx.Response(200, json={"access_token": "tok"})], app_body={"id": "o", "appId": "a-1", "passwordCredentials": []})
    job = await signin_auto.start("a", "http://bb.example.com", lambda *a: saved.append(a) or [], client_factory=factory(t))
    await job.task
    assert job.state == "done" and saved == [("a-1", "added~secret", "organizations")] and "Personal Outlook" in job.done_note
    assert json.loads(next(c for m, u, c in log if u.endswith("/applications")))["signInAudience"] == "AzureADMultipleOrgs"
    signin_auto.cancel("a")


def test_googles_client_file_is_read_as_dropped():
    base = "https://bb.example.com"
    good = {"web": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-x",
                    "redirect_uris": [base + "/gmail/callback", base + "/mail/gmail/callback"]}}
    values, problems, warnings = signin_auto.read_google_client(json.dumps(good).encode(), base)
    assert values == {"gmail_client_id": "123-abc.apps.googleusercontent.com", "gmail_client_secret": "GOCSPX-x"}
    assert not problems and not warnings
    one = {"web": {**good["web"], "redirect_uris": [base + "/gmail/callback"]}}
    _, problems, warnings = signin_auto.read_google_client(json.dumps(one), base)
    assert not problems and "/mail/gmail/callback" in warnings[0]
    assert "Desktop app" in signin_auto.read_google_client(json.dumps({"installed": {}}), base)[1][0]
    assert "not the JSON file" in signin_auto.read_google_client(b"\x89PNG", base)[1][0]


def test_the_setup_page_does_it_for_the_admin(client, monkeypatch):
    from tests.test_bb_web import ADMIN, _login
    s = client.settings
    for field, key in (("gmail_client_id", "GMAIL_CLIENT_ID"), ("gmail_client_secret", "GMAIL_CLIENT_SECRET"),
                       ("ms_client_id", "MS_CLIENT_ID"), ("ms_client_secret", "MS_CLIENT_SECRET")):
        monkeypatch.setattr(s, field, "")
        monkeypatch.setitem(s.env_at_start, key, "")
        monkeypatch.delitem(s.env, key, raising=False)
    assert _login(client, ADMIN["email"], ADMIN["password"]).status_code == 303
    page = client.get("/admin/signin").text
    assert "Set it up with my Microsoft sign-in" in page and "Download JSON" in page and "flows/enableapi?apiid=gmail.googleapis.com" in page
    # Google: the dropped file switches it on, and a missing address is said.
    good = {"web": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-file",
                    "redirect_uris": ["http://testserver/gmail/callback"]}}
    r = client.post("/admin/signin", data={"provider": "google"},
                    files={"client_json": ("client_secret_123.json", json.dumps(good), "application/json")})
    loc = r.headers["location"]
    assert "Sign%20in%20with%20Google%20is%20on" in loc and "mail/gmail/callback" in loc
    assert s.gmail_client_id == "123-abc.apps.googleusercontent.com" and s.gmail_client_secret == "GOCSPX-file"
    # Microsoft: the button starts the sign-in, and the page shows the code until it is done.
    started = {}

    async def fake_start(admin_id, base_url, save, client_factory=None):
        job = signin_auto.Job(state="waiting", user_code="WXYZ-9876", expires_at=__import__("time").time() + 600)
        signin_auto._JOBS[admin_id] = job
        started.update(base=base_url, save=save)
        return job

    monkeypatch.setattr(signin_auto, "start", fake_start)
    r = client.post("/admin/signin/microsoft/auto")
    assert r.status_code == 303 and r.headers["location"] == "/admin/signin#microsoft"
    page = client.get("/admin/signin").text
    assert "WXYZ-9876" in page and "microsoft.com/devicelogin" in page
    assert client.get("/admin/signin/microsoft/auto").json()["state"] == "waiting"
    # What the setup saves takes effect like the form.
    assert asyncio.run(started["save"]("1a2b3c4d-1111-2222-3333-444455556666", "v~1", "common")) == []
    assert s.ms_client_id == "1a2b3c4d-1111-2222-3333-444455556666" and s.ms_client_secret == "v~1"
    client.post("/admin/signin/microsoft/auto/cancel")
    assert client.get("/admin/signin/microsoft/auto").json()["state"] == "none"
