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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from bb import gmail_oauth, recipients as recipients_mod
from bb.accounts import Accounts, LoginThrottle
from bb.settings import Settings
from bb.workspaces import COMMON_TIMEZONES, GOALS, TONES, Workspace, all_workspaces

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
        return await render(request, "dashboard.html", user, logs=ws.log_tail(40),
                            problems=settings.problems() if user["role"] == "admin" else [])

    @app.get("/api/summary")
    async def api_summary(user: dict = Depends(current_user)):
        return await workspace_for(user).summary()

    @app.post("/worker/{action}")
    async def worker(action: str, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if action == "start":
            ok, detail = ws.start_worker()
        elif action == "stop":
            ok, detail = await ws.stop_worker()
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

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "profile.html", user, profile=ws.profile(), tones=TONES,
                            goals=GOALS, timezones=COMMON_TIMEZONES)

    @app.post("/profile")
    async def profile_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = dict(await request.form())
        try:
            ws.write_profile(form)
        except ValidationError as e:
            first = e.errors()[0]
            detail = f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
            return await render(request, "profile.html", user, profile={**ws.profile(), **form},
                                tones=TONES, goals=GOALS, timezones=COMMON_TIMEZONES,
                                error=f"Could not save: {detail}")
        except ValueError as e:
            return await render(request, "profile.html", user, profile={**ws.profile(), **form},
                                tones=TONES, goals=GOALS, timezones=COMMON_TIMEZONES, error=str(e))
        return _redirect("/profile", msg="Profile saved. Restart the worker to apply changes."
                         if ws.worker_running() else "Profile saved.")

    # ── Gmail ──

    @app.get("/gmail", response_class=HTMLResponse)
    async def gmail_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "gmail.html", user, connected=ws.gmail_connected(),
                            account=ws.gmail_account(),
                            configured=bool(settings.gmail_client_id and settings.gmail_client_secret),
                            redirect_uri=gmail_oauth.redirect_uri(settings))

    @app.post("/gmail/connect")
    async def gmail_connect(user: dict = Depends(current_user)):
        if not (settings.gmail_client_id and settings.gmail_client_secret):
            return _redirect("/gmail", error="The site has no Google OAuth client configured yet.")
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
            return _redirect("/gmail", error=f"Google said: {error}")
        if not gmail_oauth.verify_state(settings.secret_key(), user["id"], nonce, state):
            return _redirect("/gmail", error="The sign-in did not match this session. Try again.")
        ws = workspace_for(user)
        try:
            data = await gmail_oauth.exchange_code(settings, code)
            email = await gmail_oauth.account_email(data.get("access_token", ""))
        except Exception as e:
            return _redirect("/gmail", error=str(e))
        gmail_oauth.save_token(ws, data, email)
        # The persona's address must be the mailbox Harvey sends from.
        cfg = ws.config()
        if cfg and (cfg.get("persona") or {}).get("email") != email:
            prof = ws.profile()
            prof["email"] = email
            try:
                ws.write_profile({**prof, "auto_prospect": "on" if prof["auto_prospect"] else "",
                                  "require_approval": "on" if prof["require_approval"] else ""})
            except (ValueError, ValidationError):
                pass
        response = _redirect("/gmail", msg=f"Gmail connected as {email}.")
        response.delete_cookie(GMAIL_NONCE_COOKIE, path="/gmail")
        return response

    @app.post("/gmail/disconnect")
    async def gmail_disconnect(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if ws.worker_running():
            await ws.stop_worker()
        ws.disconnect_gmail()
        return _redirect("/gmail", msg="Gmail disconnected.")

    @app.post("/gmail/test")
    async def gmail_test(user: dict = Depends(current_user)):
        ok, detail = await gmail_oauth.test_connection(settings, workspace_for(user))
        return _redirect("/gmail", msg=detail if ok else "", error="" if ok else detail)

    # ── recipients ──

    @app.get("/recipients", response_class=HTMLResponse)
    async def recipients_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "recipients.html", user, recent=await ws.recent_recipients(),
                            max_rows=recipients_mod.MAX_ROWS)

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
                await ws.stop_worker()
            return _redirect("/admin/users", msg=f"Disabled {target['email']} and stopped their worker.")
        if action == "enable":
            accounts.set_disabled(user_id, False)
            return _redirect("/admin/users", msg=f"Enabled {target['email']}.")
        raise HTTPException(status_code=404)

    return app
