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

    async def fake_start(admin_id, base_url, save, client_factory=None, current_client_id=""):
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


# ── what the review found ──


def _graph(log, existing=None, token_answers=None):
    """Microsoft's endpoints, with this site's app already in the directory when ``existing`` is given."""
    answers = list(token_answers or [httpx.Response(200, json={"access_token": "tok"})])

    def handler(request: httpx.Request):
        url = str(request.url)
        log.append((request.method, url, request.content.decode() if request.content else ""))
        if url.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "d", "user_code": "CODE-1", "interval": 1, "expires_in": 900})
        if url.endswith("/token"):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        if "/applications(appId=" in url and request.method == "GET":
            return httpx.Response(200, json=existing) if existing else httpx.Response(404, json={"error": {"message": "not found"}})
        if request.method == "PATCH":
            return httpx.Response(204)
        if url.endswith("/addPassword"):
            return httpx.Response(200, json={"secretText": "fresh~secret"})
        if url.endswith("/applications"):
            return httpx.Response(201, json={"id": "new-obj", "appId": "new-app", "passwordCredentials": [{"secretText": "s"}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def instant(monkeypatch):
    async def no_wait(_s):
        await asyncio.sleep(0)               # no wait, but the loop still turns
    monkeypatch.setattr(signin_auto, "_pause", no_wait)


@pytest.mark.asyncio
async def test_setting_up_again_updates_the_sites_app_so_signed_in_mailboxes_keep_working(instant):
    log, saved = [], []
    existing = {"id": "obj-9", "appId": "app-9", "signInAudience": "AzureADandPersonalMicrosoftAccount",
                "web": {"redirectUris": ["https://old.example/microsoft/callback"]},
                "requiredResourceAccess": [{"resourceAppId": signin_auto.GRAPH_APP,
                                            "resourceAccess": [{"id": signin_auto.MAIL_SCOPES["User.Read"], "type": "Scope"}]}]}
    t = _graph(log, existing)
    job = await signin_auto.start("r1", "https://bb.example.com", lambda *a: saved.append(a) or [],
                                  client_factory=lambda: httpx.AsyncClient(transport=t), current_client_id="app-9")
    await job.task
    assert job.state == "done" and saved == [("app-9", "fresh~secret", "common")]          # same app, new secret
    assert not any(m == "POST" and u.endswith("/applications") for m, u, _ in log)         # nothing new registered
    patch = json.loads(next(c for m, u, c in log if m == "PATCH"))
    assert patch["web"]["redirectUris"][0] == "https://old.example/microsoft/callback"      # its own addresses kept
    assert set(signin_auto.redirect_uris("https://bb.example.com")) <= set(patch["web"]["redirectUris"])
    assert {a["id"] for a in patch["requiredResourceAccess"][0]["resourceAccess"]} == set(signin_auto.MAIL_SCOPES.values())
    assert "keep working" in job.done_note
    # Not in the directory signed in to: a new one, and the page says what that means.
    log, saved = [], []
    job = await signin_auto.start("r1", "https://bb.example.com", lambda *a: saved.append(a) or [],
                                  client_factory=lambda: httpx.AsyncClient(transport=_graph(log)), current_client_id="app-9")
    await job.task
    assert job.state == "done" and saved[0][0] == "new-app" and any("Reconnect" in s for s in job.steps)
    signin_auto.cancel("r1")


@pytest.mark.asyncio
async def test_the_setup_never_hangs_or_doubles_and_waits_out_hiccups(instant):
    # Microsoft cannot be reached at the start: a failed job, not one stuck "starting".
    def down(request):
        raise httpx.ConnectError("no route")
    job = await signin_auto.start("h1", "https://bb.example.com", lambda *a: [],
                                  client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    assert job.state == "failed" and "Could not reach Microsoft" in job.error and job.task is None
    # A non-JSON answer: the same.
    job = await signin_auto.start("h1", "https://bb.example.com", lambda *a: [],
                                  client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    assert job.state == "failed"
    # A network error, a 429 and a 503 while waiting for the sign-in are waited out.
    log, saved = [], []
    t = _graph(log, token_answers=[httpx.ReadTimeout(""), httpx.Response(429, text="slow"), httpx.Response(503, text="busy"),
                                   httpx.Response(400, json={"error": "authorization_pending"}),
                                   httpx.Response(200, json={"access_token": "tok"})])
    job = await signin_auto.start("h1", "https://bb.example.com", lambda *a: saved.append(a) or [],
                                  client_factory=lambda: httpx.AsyncClient(transport=t))
    await job.task
    assert job.state == "done" and saved
    # Two clicks at once start one sign-in, and the first one's wait is stopped.
    gate = asyncio.Event()

    async def slow_code(request):
        await gate.wait()
        return httpx.Response(200, json={"device_code": "d", "user_code": "X", "interval": 1, "expires_in": 900})

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if str(request.url).endswith("/devicecode"):
                return await slow_code(request)
            return httpx.Response(400, json={"error": "authorization_pending"})
    a = asyncio.ensure_future(signin_auto.start("h2", "https://bb.example.com", lambda *x: [],
                                                client_factory=lambda: httpx.AsyncClient(transport=Slow())))
    b = asyncio.ensure_future(signin_auto.start("h2", "https://bb.example.com", lambda *x: [],
                                                client_factory=lambda: httpx.AsyncClient(transport=Slow())))
    await asyncio.sleep(0.05)
    gate.set()
    j1, j2 = await a, await b
    assert j1 is not j2 and signin_auto.job_for("h2") is j2
    for _ in range(5):
        await asyncio.sleep(0)
    assert j1.task.cancelled() and not j2.task.done()           # the first wait was stopped, one goes on
    signin_auto.cancel("h2")


@pytest.mark.asyncio
async def test_cancel_cannot_stop_it_halfway_through_setting_up(instant):
    release = asyncio.Event()
    saved = []

    async def save(*a):
        await release.wait()                 # restarting workers takes a while
        saved.append(a)
        return []
    job = await signin_auto.start("c1", "https://bb.example.com", save,
                                  client_factory=lambda: httpx.AsyncClient(transport=_graph([])))
    for _ in range(50):
        await asyncio.sleep(0)
        if job.state == "working":
            break
    assert job.state == "working"
    assert signin_auto.cancel("c1") is False and signin_auto.job_for("c1") is job       # refused while it sets up
    release.set()
    await job.task
    assert job.state == "done" and saved
    signin_auto.cancel("c1")


def test_odd_google_files_are_refused_in_words():
    base = "https://bb.example.com"
    for bad in ({"web": "x"}, {"web": {"client_id": "a", "client_secret": "b", "redirect_uris": 5}}, [1, 2]):
        values, problems, _ = signin_auto.read_google_client(json.dumps(bad), base)
        assert not values and problems
    # A trailing slash on a registered address still counts as that address.
    ok = {"web": {"client_id": "1-a.apps.googleusercontent.com", "client_secret": "s",
                  "redirect_uris": [base + "/gmail/callback/", base + "/mail/gmail/callback", {"odd": 1}]}}
    assert signin_auto.read_google_client(json.dumps(ok), base)[2] == []


def test_the_setup_routes_refuse_what_they_cannot_do(client, monkeypatch):
    from tests.test_bb_web import ADMIN, _login
    s = client.settings
    assert _login(client, ADMIN["email"], ADMIN["password"]).status_code == 303
    # A huge file is refused before it is read whole.
    r = client.post("/admin/signin", data={"provider": "google"},
                    files={"client_json": ("big.json", b"{" + b" " * 70000 + b"}", "application/json")})
    assert "far%20bigger" in r.headers["location"]
    # A secret or tenant fixed by the server: the automatic setup is not offered and refused.
    monkeypatch.setattr(s, "ms_client_id", "")
    monkeypatch.setitem(s.env_at_start, "MS_CLIENT_ID", "")
    monkeypatch.setitem(s.env_at_start, "MS_CLIENT_SECRET", "from-bb-env")
    assert "Set it up with my Microsoft sign-in" not in client.get("/admin/signin").text
    r = client.post("/admin/signin/microsoft/auto")
    assert "server%27s%20own%20settings" in r.headers["location"] or "server's own settings" in r.headers["location"]
