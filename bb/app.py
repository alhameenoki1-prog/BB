"""The web app: FastAPI + server-rendered pages.

Auth is a session cookie; accounts are created by an admin. Every page a
user sees is scoped to their own workspace (their Harvey home), so one
install serves many people without any of Harvey's code knowing.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from bb import deliverability, gmail_oauth, recipients as recipients_mod
from bb.accounts import Accounts, LoginThrottle
from bb.settings import Settings
from bb.workspaces import (ATTACH_POLICIES, COMMON_TIMEZONES, GOALS, GREETINGS, LLM_PRESETS,
                           OUTREACH_MODES, SPEEDS, STYLES, TONES, Workspace, all_workspaces)

logger = logging.getLogger("bb.app")

SESSION_COOKIE = "bb_session"
GMAIL_NONCE_COOKIE = "bb_gmail_nonce"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _redirect(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    params = []
    if msg:
        params.append("msg=" + quote(msg))
    if error:
        params.append("error=" + quote(error))
    if params:
        url += ("&" if "?" in url else "?") + "&".join(params)
    return RedirectResponse(url, status_code=303)


async def check_anthropic_key(key: str) -> tuple[bool, str]:
    """Is this the user's working Anthropic key? Listing models costs
    nothing and fails fast on a bad key."""
    import anthropic

    if not key.startswith("sk-ant-"):
        return False, "Anthropic keys start with sk-ant-. Copy the whole key from console.anthropic.com."
    if key.startswith("sk-ant-oat"):
        return False, ("That is a Claude.ai sign-in token, not an API key. API keys start with "
                       "sk-ant-api and come from console.anthropic.com → API keys.")

    def said(e) -> str:
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
        return str(getattr(e, "message", "") or e)[:200]

    try:
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=20)
        await client.models.list(limit=1)
    except anthropic.AuthenticationError:
        return False, "Anthropic rejected that key."
    except anthropic.PermissionDeniedError:
        return False, "That key is not allowed to use the API (check its permissions in the Console)."
    except anthropic.BadRequestError as e:
        message = said(e)
        if "credit" in message.lower() or "billing" in message.lower():
            return False, ("The key is valid, but the Anthropic account has no credit, so every call "
                           "would fail. Add prepaid credit at console.anthropic.com → Settings → "
                           "Billing (a few dollars goes a long way), or choose Google Gemini or Groq "
                           "above, which have free tiers.")
        return False, f"Anthropic refused the request: {message}"
    except anthropic.RateLimitError:
        return True, "Key accepted (Anthropic is rate-limiting it right now; the worker waits and retries)."
    except anthropic.APIStatusError as e:
        return False, f"Anthropic answered {e.status_code}: {said(e)}"
    except Exception as e:  # network, proxy, outage
        return False, f"Could not reach Anthropic to check the key: {type(e).__name__}: {str(e)[:120]}"
    return True, "Key accepted."


async def check_openai_compatible(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """Does this endpoint answer with this key? Listing models is free on
    every OpenAI-compatible API; the model name is checked against the list
    when the list is readable, and only warned about otherwise."""
    import httpx

    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return False, "The API address must start with https:// (or http:// for a local server)."
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
    except httpx.HTTPError as e:
        return False, f"Could not reach {base_url}: {type(e).__name__}."
    if resp.status_code in (401, 403):
        return False, "That provider rejected the key."
    if resp.status_code >= 400:
        return False, f"{base_url} answered {resp.status_code}: {resp.text[:160]}"
    def bare(name: str) -> str:
        # Gemini lists "models/gemini-2.5-flash"; requests take either form.
        name = str(name or "")
        return name[len("models/"):] if name.startswith("models/") else name

    try:
        ids = {bare(m.get("id")) for m in (resp.json().get("data") or []) if isinstance(m, dict)}
        ids.discard("")
    except ValueError:
        ids = set()
    if ids and model and bare(model) not in ids:
        shown = sorted(i for i in ids if not any(w in i for w in ("embedding", "image", "audio", "tts", "vision")))
        return False, (f"Connected, but the model '{model}' is not in this provider's list. "
                       "Pick one of: " + ", ".join(shown[:14]) + ("…" if len(shown) > 14 else ""))
    return True, "Connected."


async def test_m365(settings_dict: dict) -> tuple[bool, str]:
    """Sign in as the Azure app and read the mailbox, exactly as the worker will."""
    from types import SimpleNamespace

    from harvey.integrations.microsoft import MicrosoftAppProvider

    env = SimpleNamespace(m365_tenant_id=settings_dict["m365_tenant_id"], m365_client_id=settings_dict["m365_client_id"],
                          m365_client_secret=settings_dict["m365_client_secret"],
                          m365_mailbox=settings_dict["m365_mailbox"])
    try:
        return await MicrosoftAppProvider(None, env).test_connection()
    except Exception as e:
        return False, f"Connection failed: {e}"


async def check_domain(domain: str) -> dict:
    return await deliverability.check_domain(domain)


async def test_smtp(smtp: dict) -> tuple[bool, str]:
    """Log in to the mailbox's SMTP and IMAP servers with the given settings,
    exactly as the worker will."""
    from types import SimpleNamespace

    from harvey.integrations.smtp_mail import SmtpImapProvider

    env = SimpleNamespace(
        smtp_host=smtp["smtp_host"], smtp_port=int(smtp["smtp_port"] or 587),
        smtp_username=smtp["smtp_username"], smtp_password=smtp["smtp_password"],
        imap_host=smtp["imap_host"] or smtp["smtp_host"], imap_port=int(smtp["imap_port"] or 993),
        imap_username=smtp["imap_username"] or smtp["smtp_username"],
        imap_password=smtp["imap_password"] or smtp["smtp_password"],
    )
    try:
        return await SmtpImapProvider(None, env).test_connection()
    except Exception as e:
        return False, f"Connection failed: {e}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.home.mkdir(parents=True, exist_ok=True)
    accounts = Accounts(settings.accounts_db)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # First boot: the admin account from the environment.
        if accounts.count() == 0 and settings.admin_email and settings.admin_password:
            try:
                accounts.create_user(settings.admin_email, settings.admin_password, role="admin")
                logger.info("Created the admin account %s", settings.admin_email)
            except ValueError as e:
                logger.error("Could not create the admin account: %s", e)
        # Workers that were running before a restart come back.
        for ws in all_workspaces(settings, accounts):
            if ws.wants_autostart() and not ws.worker_running() and not ws.user["disabled"]:
                ok, detail = ws.start_worker()
                logger.info("Autostart worker for %s: %s", ws.user["email"], detail)
        yield

    app = FastAPI(title="BB", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.accounts = accounts

    # ── auth plumbing ──

    def current_user(request: Request) -> dict:
        user = accounts.session_user(request.cookies.get(SESSION_COOKIE))
        if not user:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return user

    def current_admin(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Admins only.")
        return user

    def workspace_for(user: dict) -> Workspace:
        return Workspace(settings, user).ensure()

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        """A form post from another site is refused (the cookie is SameSite
        too; this is the second lock)."""
        if request.method in ("POST", "PUT", "DELETE"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                host = urlparse(origin).netloc
                allowed = {urlparse(settings.base_url).netloc, request.headers.get("host", "")}
                if host not in allowed:
                    return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
        return await call_next(request)

    async def render(request: Request, name: str, user: dict | None = None, **ctx):
        summary = None
        if user:
            summary = await workspace_for(user).summary()
        return templates.TemplateResponse(request, name, {
            "user": user, "summary": summary, "settings": settings,
            "msg": request.query_params.get("msg", ""),
            "error": ctx.pop("error", "") or request.query_params.get("error", ""),
            **ctx,
        })

    def set_session_cookie(response, token: str):
        response.set_cookie(
            SESSION_COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
            samesite="lax", secure=settings.secure_cookies, path="/",
        )

    # ── health ──

    @app.get("/health")
    async def health():
        return {"ok": True}

    # ── login / logout ──

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if accounts.session_user(request.cookies.get(SESSION_COOKIE)):
            return _redirect("/")
        return await render(request, "login.html")

    @app.post("/login")
    async def login(request: Request, email: str = Form(""), password: str = Form("")):
        key = request.client.host if request.client else "unknown"
        if not throttle.allow(key):
            return await render(request, "login.html",
                                error="Too many attempts. Wait a few minutes and try again.")
        user = accounts.authenticate(email, password)
        if not user:
            throttle.record(key)
            return await render(request, "login.html", error="Wrong email or password.")
        throttle.reset(key)
        token = accounts.create_session(user["id"], settings.session_days * 86400)
        response = _redirect("/")
        set_session_cookie(response, token)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        accounts.delete_session(request.cookies.get(SESSION_COOKIE))
        response = _redirect("/login", msg="Signed out.")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ── dashboard ──

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        revived = ws.revive_worker()
        return await render(request, "dashboard.html", user, logs=ws.log_tail(40),
                            problems=settings.problems() if user["role"] == "admin" else [],
                            msg=revived or request.query_params.get("msg", ""))

    @app.get("/api/summary")
    async def api_summary(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        ws.revive_worker()
        return await ws.summary()

    @app.post("/worker/{action}")
    async def worker(action: str, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if action == "start":
            ok, detail = ws.start_worker()
            if ok:
                ws.paused_file.unlink(missing_ok=True)
        elif action == "stop":
            ok, detail = await ws.stop_worker()
            ws.paused_file.unlink(missing_ok=True)
        elif action == "pause":
            ok, detail = await ws.pause_worker()
        elif action == "resume":
            ok, detail = ws.resume_worker()
        elif action == "reset":
            ok, detail = await ws.reset_data()
        else:
            raise HTTPException(status_code=404)
        return _redirect("/", msg=detail if ok else "", error="" if ok else detail)

    @app.post("/sending/{action}")
    async def sending(action: str, user: dict = Depends(current_user)):
        if action not in ("pause", "resume"):
            raise HTTPException(status_code=404)
        await workspace_for(user).set_sending_paused(action == "pause")
        return _redirect("/", msg="Sending paused." if action == "pause" else "Sending resumed.")

    # ── profile ──

    def _profile_ctx(ws: Workspace, profile: dict) -> dict:
        return dict(profile=profile, tones=TONES, goals=GOALS, timezones=COMMON_TIMEZONES,
                    modes=OUTREACH_MODES, policies=ATTACH_POLICIES, attachments=ws.attachments(),
                    speeds=SPEEDS, styles=STYLES, greetings=GREETINGS,
                    signature_image=ws.signature_image())

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "profile.html", user, **_profile_ctx(ws, ws.profile()))

    @app.post("/profile")
    async def profile_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = dict(await request.form())
        try:
            ws.write_profile(form)
        except ValidationError as e:
            first = e.errors()[0]
            detail = f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
            return await render(request, "profile.html", user,
                                **_profile_ctx(ws, {**ws.profile(), **form}),
                                error=f"Could not save: {detail}")
        except ValueError as e:
            return await render(request, "profile.html", user,
                                **_profile_ctx(ws, {**ws.profile(), **form}), error=str(e))
        restarted = await ws.restart_worker_if_running("the new profile")
        return _redirect("/profile", msg="Profile saved. The worker was restarted with the new settings."
                         if restarted else "Profile saved.")

    # ── the user's own Anthropic key ──

    @app.get("/apikey", response_class=HTMLResponse)
    async def apikey_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        llm = ws.llm()
        return await render(request, "apikey.html", user, presets=LLM_PRESETS, llm=llm,
                            configured=ws.llm_configured(), masked=ws.mask(llm["api_key"]))

    @app.post("/apikey")
    async def apikey_save(user: dict = Depends(current_user), provider: str = Form(""),
                          api_key: str = Form(""), model: str = Form(""), base_url: str = Form("")):
        ws = workspace_for(user)
        key = api_key.strip()
        if not provider:
            provider = "anthropic" if key.startswith("sk-ant-") else "gemini"
        if provider not in LLM_PRESETS:
            return _redirect("/apikey", error="Choose a provider from the list.")
        preset = LLM_PRESETS[provider]
        current = ws.llm()
        if not key and current["provider"] == provider and current["api_key"]:
            key = current["api_key"]  # blank keeps the saved key
        model = (model or preset["model"]).strip()
        base_url = (base_url or preset["base_url"]).strip().rstrip("/")
        if provider == "anthropic":
            if not key:
                return _redirect("/apikey", error="Paste your Anthropic key first.")
            ok, detail = await check_anthropic_key(key)
        else:
            if not key and not preset.get("no_key"):
                return _redirect("/apikey", error=f"Paste your {preset['label']} key first.")
            if not model:
                return _redirect("/apikey", error="Enter a model name.")
            ok, detail = await check_openai_compatible(base_url, key, model)
        if not ok:
            return _redirect("/apikey", error=detail)
        ws.save_llm(provider, key, model=model, base_url=base_url)
        restarted = await ws.restart_worker_if_running("the new AI provider")
        return _redirect("/apikey", msg=f"{preset['label']} saved and verified ({model or 'default model'})."
                         + (" The worker was restarted to use it." if restarted else ""))

    @app.post("/apikey/remove")
    async def apikey_remove(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if ws.worker_running():
            await ws.stop_worker("the AI provider was removed")
        ws.clear_llm()
        return _redirect("/apikey", msg="AI provider removed. The worker was stopped.")

    # ── mailbox: Gmail (OAuth) or the mailbox's own SMTP/IMAP ──

    @app.get("/mailbox", response_class=HTMLResponse)
    async def mailbox_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        smtp = ws.smtp_settings()
        m365 = ws.m365_settings()
        sending = ws.sending_address()
        domain = deliverability.domain_of(sending)
        report = await check_domain(domain) if domain and ws.mailbox_connected() else None
        return await render(request, "mailbox.html", user, provider=ws.mail_provider(),
                            gmail_connected=ws.gmail_connected(), account=ws.gmail_account(),
                            gmail_available=bool(settings.gmail_client_id and settings.gmail_client_secret),
                            smtp={**smtp, "smtp_password": "", "imap_password": ""},
                            smtp_configured=ws.smtp_configured(),
                            smtp_masked=ws.mask(smtp["smtp_password"]),
                            m365={**m365, "m365_client_secret": ""}, m365_configured=ws.m365_configured(),
                            m365_masked=ws.mask(m365["m365_client_secret"]),
                            sending=sending, report=report,
                            redirect_uri=gmail_oauth.redirect_uri(settings))

    @app.post("/mailbox/m365")
    async def mailbox_m365_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = dict(await request.form())
        current = ws.m365_settings()
        m365 = {}
        for key in current:
            value = str(form.get(key, "") or "").strip()
            m365[key] = value or (current[key] if key == "m365_client_secret" else "")
        m365["m365_mailbox"] = m365["m365_mailbox"].lower()
        if not all(m365.values()):
            return _redirect("/mailbox", error="Tenant ID, client ID, client secret and mailbox are all required.")
        ok, detail = await test_m365(m365)
        if not ok:
            return _redirect("/mailbox", error=f"Not saved: {detail}")
        ws.save_secrets(**m365)
        ws.set_mail_provider("microsoft_app")
        ws.rewrite_config()
        return _redirect("/mailbox", msg=f"Microsoft 365 saved. {detail}")

    @app.post("/mailbox/m365/test")
    async def mailbox_m365_test(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if not ws.m365_configured():
            return _redirect("/mailbox", error="No Microsoft 365 app settings saved yet.")
        ok, detail = await test_m365(ws.m365_settings())
        return _redirect("/mailbox", msg=detail if ok else "", error="" if ok else detail)

    @app.post("/mailbox/m365/remove")
    async def mailbox_m365_remove(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if ws.worker_running() and ws.mail_provider() == "microsoft_app":
            await ws.stop_worker("the Microsoft 365 settings were removed")
        ws.save_secrets(**{k: "" for k in ws.m365_settings()})
        ws.set_mail_provider("gmail" if ws.gmail_connected() else "smtp")
        ws.rewrite_config()
        return _redirect("/mailbox", msg="Microsoft 365 settings removed.")

    # ── signature image ──

    @app.post("/signature/image")
    async def signature_image_upload(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = await request.form()
        upload = form.get("image")
        if not hasattr(upload, "read") or not getattr(upload, "filename", ""):
            return _redirect("/profile", error="Choose an image file.")
        try:
            name = ws.set_signature_image(upload.filename, await upload.read())
        except ValueError as e:
            return _redirect("/profile", error=str(e))
        return _redirect("/profile", msg=f"Signature image {name} saved. Use {{{{logo}}}} in the signature to place it.")

    @app.get("/signature/image")
    async def signature_image_get(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        name = ws.signature_image()
        if not name:
            raise HTTPException(404)
        return FileResponse(ws.signature_dir / name)

    @app.post("/signature/image/delete")
    async def signature_image_delete(user: dict = Depends(current_user)):
        workspace_for(user).remove_signature_image()
        return _redirect("/profile", msg="Signature image removed.")

    @app.get("/gmail")
    async def gmail_page_moved():
        return _redirect("/mailbox")

    @app.post("/mailbox/provider")
    async def mailbox_choose(user: dict = Depends(current_user), provider: str = Form("gmail")):
        ws = workspace_for(user)
        try:
            ws.set_mail_provider(provider)
        except ValueError as e:
            return _redirect("/mailbox", error=str(e))
        ws.rewrite_config()
        label = "Gmail" if provider == "gmail" else "your mailbox's SMTP/IMAP settings"
        return _redirect("/mailbox", msg=f"Emails will go through {label}."
                         + (" Restart the worker to apply." if ws.worker_running() else ""))

    @app.post("/mailbox/smtp")
    async def mailbox_smtp_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = dict(await request.form())
        current = ws.smtp_settings()
        smtp = {}
        for key in current:
            value = str(form.get(key, "") or "").strip()
            # Blank password fields keep the stored password.
            smtp[key] = value or (current[key] if key.endswith("password") else "")
        if not (smtp["smtp_host"] and smtp["smtp_username"] and smtp["smtp_password"]):
            return _redirect("/mailbox", error="SMTP server, username and password are required.")
        for port in ("smtp_port", "imap_port"):
            if smtp[port] and not str(smtp[port]).isdigit():
                return _redirect("/mailbox", error=f"{port.replace('_', ' ')} must be a number.")
        ok, detail = await test_smtp(smtp)
        if not ok:
            return _redirect("/mailbox", error=f"Not saved: {detail}")
        ws.save_secrets(**smtp)
        ws.set_mail_provider("smtp")
        ws.rewrite_config()
        return _redirect("/mailbox", msg=f"Mailbox saved. {detail}")

    @app.post("/mailbox/smtp/test")
    async def mailbox_smtp_test(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if not ws.smtp_configured():
            return _redirect("/mailbox", error="No SMTP settings saved yet.")
        ok, detail = await test_smtp(ws.smtp_settings())
        return _redirect("/mailbox", msg=detail if ok else "", error="" if ok else detail)

    @app.post("/mailbox/smtp/remove")
    async def mailbox_smtp_remove(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if ws.worker_running() and ws.mail_provider() == "smtp":
            await ws.stop_worker("the SMTP mailbox was removed")
        ws.save_secrets(**{k: "" for k in ws.smtp_settings()})
        if ws.gmail_connected():
            ws.set_mail_provider("gmail")
        ws.rewrite_config()
        return _redirect("/mailbox", msg="SMTP settings removed.")

    # ── attachments sent with replies ──

    @app.post("/attachments")
    async def attachments_upload(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = await request.form()
        added = []
        for upload in form.getlist("files"):
            # A file input left empty arrives as a blank string or a nameless part.
            if not hasattr(upload, "read") or not getattr(upload, "filename", ""):
                continue
            data = await upload.read()
            if not data:
                continue
            try:
                added.append(ws.add_attachment(upload.filename, data))
            except ValueError as e:
                return _redirect("/profile", error=str(e))
        if not added:
            return _redirect("/profile", error="Choose at least one file.")
        return _redirect("/profile", msg=f"Added {', '.join(added)}. They go out with your replies.")

    @app.post("/attachments/{name}/delete")
    async def attachments_delete(name: str, user: dict = Depends(current_user)):
        workspace_for(user).remove_attachment(name)
        return _redirect("/profile", msg="Attachment removed.")

    # ── Gmail OAuth (the site's Google client) ──

    @app.post("/gmail/connect")
    async def gmail_connect(user: dict = Depends(current_user)):
        if not (settings.gmail_client_id and settings.gmail_client_secret):
            return _redirect("/mailbox", error="The site has no Google OAuth client configured yet. "
                                               "Use your mailbox's SMTP settings instead.")
        nonce = secrets.token_urlsafe(16)
        state = gmail_oauth.sign_state(settings.secret_key(), user["id"], nonce)
        response = RedirectResponse(
            gmail_oauth.auth_url(settings, state, login_hint=workspace_for(user).profile().get("email", "")),
            status_code=303)
        response.set_cookie(GMAIL_NONCE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
                            secure=settings.secure_cookies, path="/gmail")
        return response

    @app.get("/gmail/callback")
    async def gmail_callback(request: Request, user: dict = Depends(current_user),
                             code: str = "", state: str = "", error: str = ""):
        nonce = request.cookies.get(GMAIL_NONCE_COOKIE, "")
        if error:
            return _redirect("/mailbox", error=f"Google said: {error}")
        if not gmail_oauth.verify_state(settings.secret_key(), user["id"], nonce, state):
            return _redirect("/mailbox", error="The sign-in did not match this session. Try again.")
        ws = workspace_for(user)
        try:
            data = await gmail_oauth.exchange_code(settings, code)
            email = await gmail_oauth.account_email(data.get("access_token", ""))
        except Exception as e:
            return _redirect("/mailbox", error=str(e))
        gmail_oauth.save_token(ws, data, email)
        ws.set_mail_provider("gmail")
        ws.rewrite_config()  # the persona sends from the connected mailbox
        response = _redirect("/mailbox", msg=f"Gmail connected as {email}.")
        response.delete_cookie(GMAIL_NONCE_COOKIE, path="/gmail")
        return response

    @app.post("/gmail/disconnect")
    async def gmail_disconnect(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if ws.worker_running() and ws.mail_provider() == "gmail":
            await ws.stop_worker("Gmail was disconnected")
        ws.disconnect_gmail()
        if ws.smtp_configured():
            ws.set_mail_provider("smtp")
        ws.rewrite_config()
        return _redirect("/mailbox", msg="Gmail disconnected.")

    @app.post("/gmail/test")
    async def gmail_test(user: dict = Depends(current_user)):
        ok, detail = await gmail_oauth.test_connection(settings, workspace_for(user))
        return _redirect("/mailbox", msg=detail if ok else "", error="" if ok else detail)

    # ── recipients ──

    @app.get("/recipients", response_class=HTMLResponse)
    async def recipients_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "recipients.html", user, recent=await ws.recent_recipients(),
                            sectors=await ws.sector_breakdown(), max_rows=recipients_mod.MAX_ROWS)

    @app.post("/recipients")
    async def recipients_upload(user: dict = Depends(current_user),
                                file: UploadFile | None = File(None), pasted: str = Form("")):
        text = pasted or ""
        if file is not None and file.filename:
            raw = await file.read()
            if len(raw) > 20 * 1024 * 1024:
                return _redirect("/recipients", error="That file is over 20 MB.")
            text = raw.decode("utf-8-sig", errors="replace")
        rows = recipients_mod.parse_rows(text)
        if not rows:
            return _redirect("/recipients", error="No rows found. Upload a CSV with an email column, "
                                                  "or paste one address per line.")
        ws = workspace_for(user)
        result = await recipients_mod.import_rows(ws.state(), rows)
        msg = f"Added {result['added']} recipient(s)."
        if result["duplicates"]:
            msg += f" {result['duplicates']} already on your list."
        if result["invalid"]:
            msg += f" {result['invalid']} skipped (no valid email)."
        if result["too_many"]:
            msg += f" {result['too_many']} beyond the {recipients_mod.MAX_ROWS:,} limit were ignored."
        return _redirect("/recipients", msg=msg)

    # ── outbox ──

    @app.get("/outbox", response_class=HTMLResponse)
    async def outbox_page(request: Request, user: dict = Depends(current_user),
                          status: str = "pending_review"):
        if status not in ("pending_review", "approved", "sent", "failed", "rejected"):
            status = "pending_review"
        ws = workspace_for(user)
        return await render(request, "outbox.html", user, items=await ws.outbox(status), status=status)

    @app.post("/outbox/approve-all")
    async def outbox_approve_all(user: dict = Depends(current_user)):
        state = workspace_for(user).state()
        await state.init_db()
        n = await state.approve_outbox()
        return _redirect("/outbox", msg=f"Approved {n} email(s).")

    @app.post("/outbox/{item_id}/{action}")
    async def outbox_action(item_id: str, action: str, user: dict = Depends(current_user)):
        state = workspace_for(user).state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item:
            raise HTTPException(status_code=404)
        if action == "approve":
            await state.approve_outbox(item_id)
            return _redirect("/outbox", msg="Approved.")
        if action == "reject":
            await state.update_outbox_item(item_id, status="rejected", error="rejected in the web app")
            return _redirect("/outbox", msg="Rejected; it will not be sent.")
        raise HTTPException(status_code=404)

    # ── replies ──

    @app.get("/replies", response_class=HTMLResponse)
    async def replies_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "replies.html", user, conversations=await ws.conversations())

    # ── admin ──

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request, admin: dict = Depends(current_admin)):
        rows = []
        for u in accounts.list_users():
            ws = Workspace(settings, u)
            rows.append({**u, "running": bool(ws.worker_running()), "configured": ws.is_configured(),
                         "gmail": ws.gmail_connected()})
        return await render(request, "admin_users.html", admin, users=rows, problems=settings.problems())

    @app.post("/admin/users")
    async def admin_create_user(admin: dict = Depends(current_admin), email: str = Form(""),
                                password: str = Form(""), role: str = Form("user")):
        try:
            accounts.create_user(email, password, role if role in ("admin", "user") else "user")
        except ValueError as e:
            return _redirect("/admin/users", error=str(e))
        return _redirect("/admin/users", msg=f"Created {email.strip().lower()}. Send them the password.")

    @app.post("/admin/users/{user_id}/password")
    async def admin_set_password(user_id: str, admin: dict = Depends(current_admin),
                                 password: str = Form("")):
        if not accounts.get_user(user_id):
            raise HTTPException(status_code=404)
        try:
            accounts.set_password(user_id, password)
        except ValueError as e:
            return _redirect("/admin/users", error=str(e))
        return _redirect("/admin/users", msg="Password changed; their sessions were signed out.")

    @app.post("/admin/users/{user_id}/{action}")
    async def admin_toggle(user_id: str, action: str, admin: dict = Depends(current_admin)):
        target = accounts.get_user(user_id)
        if not target:
            raise HTTPException(status_code=404)
        if action == "disable":
            if target["id"] == admin["id"]:
                return _redirect("/admin/users", error="You can't disable your own account.")
            accounts.set_disabled(user_id, True)
            ws = Workspace(settings, target)
            if ws.worker_running():
                await ws.stop_worker("the account was disabled by the admin")
            return _redirect("/admin/users", msg=f"Disabled {target['email']} and stopped their worker.")
        if action == "enable":
            accounts.set_disabled(user_id, False)
            return _redirect("/admin/users", msg=f"Enabled {target['email']}.")
        raise HTTPException(status_code=404)

    return app
