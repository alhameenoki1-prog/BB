"""The web app: FastAPI + server-rendered pages.

Auth is a session cookie; accounts are created by an admin. Every page a
user sees is scoped to their own workspace (their Harvey home), so one
install serves many people without any of Harvey's code knowing.
"""

import asyncio
import contextvars
import os
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from bb import deliverability
from bb import gmail_oauth, microsoft_oauth, recipients as recipients_mod
from bb.linkcheck import check_public_url
from bb.mail_autoconfig import guess_settings
from bb.mail_paste import parse_pasted_login, to_smtp_fields
from bb.accounts import Accounts, LoginThrottle
from bb.settings import Settings
from bb.workspaces import (ASKS, ATTACH_POLICIES, COMMON_TIMEZONES, GOALS, GREETINGS, LLM_PRESETS,
                           MAX_INBOX_LINKS, MAX_SECTOR_MESSAGES, OUTREACH_MODES, MAX_SECTOR_NEEDS, SECTOR_LABELS, SPEEDS,
                           STYLES, TONES, Workspace, _sector_messages_from_form, _sector_needs_from_form,
                           all_workspaces)

logger = logging.getLogger("bb.app")

SESSION_COOKIE = "bb_session"
GMAIL_NONCE_COOKIE = "bb_gmail_nonce"
MS_NONCE_COOKIE = "bb_ms_nonce"
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


async def check_anthropic_key(key: str, workspace_id: str = "") -> tuple[bool, str]:
    """Is this the user's working Anthropic key? Listing models costs
    nothing and fails fast on a bad key. ``workspace_id`` is only needed for
    a key that is not scoped to a workspace (sk-ant-usr-…)."""
    import anthropic

    from harvey.brain import anthropic_headers

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
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=20,
                                          default_headers=anthropic_headers(workspace_id))
        await client.models.list(limit=1)
    except anthropic.AuthenticationError:
        return False, "Anthropic rejected that key."
    except anthropic.PermissionDeniedError:
        return False, "That key is not allowed to use the API (check its permissions in the Console)."
    except anthropic.BadRequestError as e:
        message = said(e)
        if "workspace" in message.lower():
            return False, ("This key is not tied to a workspace, so Anthropic needs the workspace ID with it. "
                           "Paste the ID from console.anthropic.com → Settings → Workspaces into the "
                           "Workspace ID box below, or create the key inside a workspace "
                           "(API keys → Create key → pick a workspace) and paste that key instead.")
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


def linked_text(text: str):
    """A message as text with its links clickable: [words](address) shows
    the words, a bare address shows itself; everything else escaped."""
    import html as _html

    from markupsafe import Markup

    from harvey.integrations.mail_provider import MD_LINK_RE, _INLINE_URL_RE, _href, _split_url
    text = text or ""
    out, pos = [], 0
    spans = []
    for m in MD_LINK_RE.finditer(text):
        spans.append((m.start(), m.end(), m.group(1), m.group(2)))
    for m in _INLINE_URL_RE.finditer(text):
        if any(a <= m.start() < b for a, b, _, _ in spans):
            continue
        url, tail = _split_url(m.group(1))
        if url:
            spans.append((m.start(), m.start() + len(url), url, url))
    for start, end, words, url in sorted(spans):
        if start < pos:
            continue
        out.append(_html.escape(text[pos:start]))
        out.append(f'<a href="{_html.escape(_href(url), quote=True)}" target="_blank" rel="noopener noreferrer">{_html.escape(words)}</a>')
        pos = end
    out.append(_html.escape(text[pos:]))
    return Markup("".join(out))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.home.mkdir(parents=True, exist_ok=True)
    accounts = Accounts(settings.accounts_db)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["linked"] = linked_text

    def on_words(text, labels=None):
        from harvey.integrations.mail_provider import embed_links
        return embed_links(text or "", labels or {}) if (text or "").strip() else (text or "")
    templates.env.filters["on_words"] = on_words
    # Templates are read from disk on every request. After `git pull` the
    # files on disk can be newer than the program serving them until it is
    # restarted; a missing variable then renders empty instead of a 500.
    from jinja2 import ChainableUndefined
    templates.env.undefined = ChainableUndefined

    from bb.tunnel import Tunnel
    tunnel = Tunnel(settings.tunnel_mode, settings.port, token=settings.tunnel_token,
                    public_url=settings.base_url, binary=settings.cloudflared, on_url=settings.set_public_url,
                    state_dir=settings.home)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await tunnel.start()
        # First boot: the admin account from the environment.
        if accounts.count() == 0 and settings.admin_email and settings.admin_password:
            try:
                accounts.create_user(settings.admin_email, settings.admin_password, role="admin")
                logger.info("Created the admin account %s", settings.admin_email)
            except ValueError as e:
                logger.error("Could not create the admin account: %s", e)
        # Workers that were running before a restart come back.
        for ws in all_workspaces(settings, accounts):
            for one in ws.all_campaigns():
                if one.wants_autostart() and not one.worker_running() and not ws.user["disabled"]:
                    ok, detail = one.start_worker()
                    logger.info("Autostart worker for %s (%s): %s", ws.user["email"], one.campaign_name(), detail)
            if ws.mail_autostart_file.exists() and not ws.mail_worker_running() and not ws.user["disabled"]:
                ok, detail = ws.start_mail_worker()
                logger.info("Autostart mail worker for %s: %s", ws.user["email"], detail)
        async def restart_old_code():
            # Workers that kept running on older code are restarted on the new
            # one, all at once, after the site is already answering requests.
            async def one(ws):
                try:
                    for one in ws.all_campaigns():
                        for what in await one.restart_workers_on_old_code():
                            logger.info("Restarted the %s for %s (%s) on the updated code", what, ws.user["email"], one.campaign_name())
                except Exception as e:
                    logger.warning("version check failed for %s: %s", ws.user["email"], e)
            await asyncio.gather(*(one(ws) for ws in all_workspaces(settings, accounts)))

        async def keep_workers_alive():
            # Every two minutes: any worker that should be running and is not
            # comes back, and one whose process is there but has stopped
            # answering (no beat for five minutes, on two checks in a row) is
            # restarted, whether or not anyone is looking at the dashboard.
            await restart_old_code()
            stuck_seen: dict = {}
            last = time.time()
            while True:
                await asyncio.sleep(120)
                # The whole machine slept (a Mac's lid closed): the workers'
                # beats are old for that reason alone. Look again next round.
                woke_from_sleep = time.time() - last > 300
                last = time.time()
                try:
                    for ws in all_workspaces(settings, accounts):
                        for one in ws.all_campaigns():
                            if one.revive_worker():
                                logger.info("Revived worker for %s (%s)", ws.user["email"], one.campaign_name())
                            elif woke_from_sleep:
                                stuck_seen.clear()
                            else:
                                note = await one.restart_if_stuck(stuck_seen)
                                if note:
                                    logger.warning("Worker for %s (%s): %s", ws.user["email"], one.campaign_name(), note)
                        if not ws.revive_mail_worker() and not woke_from_sleep:
                            note = await ws.restart_mail_if_stuck(stuck_seen)
                            if note:
                                logger.warning("Mail worker for %s: %s", ws.user["email"], note)
                except Exception as e:
                    logger.warning("worker check failed: %s", e)

        keeper = asyncio.create_task(keep_workers_alive())
        # Mailbox problems reach the user by email or webhook, not only on the pages.
        from bb import alerts as alerts_mod
        alerter = asyncio.create_task(alerts_mod.loop(settings, accounts))
        yield
        keeper.cancel()
        alerter.cancel()
        await tunnel.stop()
        if tunnel.pid and tunnel.mode == "quick" and tunnel.url:
            # The link stays up on purpose so a restart keeps it. Someone
            # stopping the site by hand should know it is still running.
            print(f"The public link {tunnel.url} is still up (cloudflared pid {tunnel.pid}) so it survives a "
                  "restart; `python -m bb tunnel stop` ends it.", flush=True)

    app = FastAPI(title="BB", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.tunnel = tunnel

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

    def base_workspace(user: dict) -> Workspace:
        """The user's own workspace: the main campaign and everything per user."""
        return Workspace(settings, user).ensure()

    # The campaign a submitted form was showing (?c= on its action, added by
    # the page): another tab switching campaigns in between must not move a
    # Start over, an upload or a profile save onto a different campaign.
    posted_campaign = contextvars.ContextVar("bb_posted_campaign", default="")

    def workspace_for(user: dict) -> Workspace:
        """The campaign the pages show (picked in the nav); the main one by
        default; for a form, the campaign that page was showing."""
        base = base_workspace(user)
        cid = posted_campaign.get()
        if cid:
            target = base.campaign(cid)
            if target is not None:
                return target.ensure()
        return base.selected().ensure()

    def campaign_taken(ws: Workspace, address: str) -> str:
        """The outreach campaign (other than the main one) that already sends
        from ``address``, or "": one campaign per account."""
        from bb import campaigns as campaigns_mod
        for row in campaigns_mod.on_account(ws.user_root, address):
            if row["id"] != campaigns_mod.MAIN:
                return row.get("name") or row["id"]
        return ""

    @app.middleware("http")
    async def campaign_of_the_form(request: Request, call_next):
        token = posted_campaign.set(request.query_params.get("c", "") if request.method == "POST" else "")
        try:
            return await call_next(request)
        finally:
            posted_campaign.reset(token)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Browser-side locks that cost nothing: no framing by other sites,
        no content sniffing, no referrer leaking the session's pages, and
        HSTS once the site is on https."""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

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
        campaign_nav = None
        if user:
            current = workspace_for(user)
            summary = await current.summary()
            options = [(w.campaign_id, w.campaign_name()) for w in current.all_campaigns()]
            campaign_nav = {"current": current.campaign_id, "name": current.campaign_name(), "options": options,
                            "main": current.is_main}
        return templates.TemplateResponse(request, name, {
            "user": user, "summary": summary, "settings": settings, "campaign_nav": campaign_nav,
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
        from bb.workspaces import code_version
        return {"ok": True, "version": code_version()}

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
        health = await ws.mailbox_health()
        return await render(request, "dashboard.html", user, logs=ws.log_tail(40), activity=ws.recent_activity(30),
                            bounces=await ws.bounce_summary(), stale=await ws.stale_queued(), health=health,
                            reconnect=ws.needs_reconnect(health),
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
        elif action == "wake":
            ok, detail = await ws.wake_worker("Wake up now on the dashboard")
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

    def _form_view(ws: Workspace, form: dict) -> dict:
        """The posted form over the stored profile, with checkboxes read from
        the form (an unticked box is simply absent from a post)."""
        merged = {**ws.profile(), **form}
        for box in ("require_approval", "auto_prospect", "warmup_ramp"):
            merged[box] = bool(form.get(box))
        if "quiet_form" in form:
            merged["quiet_enabled"] = bool(form.get("quiet_enabled"))
        # The row-shaped settings come back as the rows the person typed,
        # not the stored ones (a rejected save must not lose their edits).
        if any(k.startswith("sn_") for k in form):
            merged["sector_needs"] = _sector_needs_from_form(form)
        if any(k.startswith("sm_") for k in form):
            merged["sector_messages"] = _sector_messages_from_form(form)
        return merged

    def _profile_ctx(ws: Workspace, profile: dict) -> dict:
        return dict(profile=profile, link_names=ws.link_names(), tones=TONES, goals=GOALS, timezones=COMMON_TIMEZONES,
                    modes=OUTREACH_MODES, policies=ATTACH_POLICIES, attachments=ws.attachments(),
                    speeds=SPEEDS, styles=STYLES, greetings=GREETINGS, asks=ASKS, sector_labels=SECTOR_LABELS, max_sector_needs=MAX_SECTOR_NEEDS,
                    max_sector_messages=MAX_SECTOR_MESSAGES, signature_image=ws.signature_image(),
                    pace_held=ws.pace_held_reason(SPEEDS.get(profile.get("speed") or "normal", SPEEDS["normal"])))

    @app.get("/profile", response_class=HTMLResponse)
    async def profile_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "profile.html", user, **_profile_ctx(ws, ws.profile()))

    @app.post("/profile")
    async def profile_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        raw = await request.form()
        form = dict(raw)
        # Multi-select fields (sector pickers) keep every chosen value.
        for key in {k for k in raw.keys() if k.startswith(("sm_sectors_", "sn_sectors_"))}:
            form[key] = raw.getlist(key)
        before = ws.writing_brief()
        try:
            ws.write_profile(form)
        except ValidationError as e:
            first = e.errors()[0]
            detail = f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
            return await render(request, "profile.html", user,
                                **_profile_ctx(ws, _form_view(ws, form)),
                                error=f"Could not save: {detail}")
        except ValueError as e:
            return await render(request, "profile.html", user,
                                **_profile_ctx(ws, _form_view(ws, form)), error=str(e))
        rewritten = ""
        if before and ws.writing_brief() != before and str(form.get("rewrite_scheduled", "")).lower() in ("1", "on", "true", "yes"):
            # The goal, the request or the wording rules changed: emails
            # written before the change and not sent yet are written again.
            done, restarted = await ws.rewrite_unsent_now("written again under the new campaign goal and brief")
            if done["cancelled"] or done["prospects"]:
                rewritten = (f" {done['cancelled']} unsent email(s) to {done['prospects']} recipient(s) will be written again "
                             "under the new goal; follow-ups to people already emailed keep the wording of their thread.")
        else:
            restarted = await ws.restart_worker_if_running("the new profile")
        warning = ws.template_warning()
        msg = ("Profile saved. The worker was restarted with the new settings." if restarted else "Profile saved.") + rewritten
        if warning:
            return _redirect("/profile", msg=msg, error=warning)
        return _redirect("/profile", msg=msg)

    @app.post("/profile/general-message")
    async def profile_general_message(request: Request, user: dict = Depends(current_user)):
        """The AI writes one direct, industry-agnostic first email + subject
        from the profile and saves it as the message (the fallback in an AI
        mode). The whole profile form comes along so nothing typed is lost."""
        ws = workspace_for(user)
        raw = await request.form()
        form = dict(raw)
        for key in {k for k in raw.keys() if k.startswith(("sm_sectors_", "sn_sectors_"))}:
            form[key] = raw.getlist(key)
        try:
            ws.write_profile(form)      # keep what is on the page, the fallback box included
        except ValidationError as e:
            first = e.errors()[0]
            return _redirect("/profile", error=f"Fix the profile first — {'.'.join(str(p) for p in first['loc'])}: {first['msg']}")
        except ValueError as e:
            return _redirect("/profile", error=f"Fix the profile first — {e}")
        try:
            out = await ws.generate_general_message()
        except Exception as e:
            return _redirect("/profile", error=f"Could not write the message: {str(e)[:200]}")
        await ws.restart_worker_if_running("the general message")
        return _redirect("/profile", msg=f"Wrote a general message: “{out['subject']}”. It is saved — check it below and edit it if you like.")

    # ── the user's own Anthropic key ──

    @app.get("/apikey", response_class=HTMLResponse)
    async def apikey_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        llm = ws.llm()
        return await render(request, "apikey.html", user, presets=LLM_PRESETS, llm=llm,
                            configured=ws.llm_configured(), masked=ws.mask(llm["api_key"]))

    @app.post("/apikey")
    async def apikey_save(user: dict = Depends(current_user), provider: str = Form(""),
                          api_key: str = Form(""), model: str = Form(""), base_url: str = Form(""),
                          workspace_id: str = Form("")):
        ws = workspace_for(user)
        key = api_key.strip()
        workspace_id = workspace_id.strip()
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
            ok, detail = await check_anthropic_key(key, workspace_id)
        else:
            if not key and not preset.get("no_key"):
                return _redirect("/apikey", error=f"Paste your {preset['label']} key first.")
            if not model:
                return _redirect("/apikey", error="Enter a model name.")
            ok, detail = await check_openai_compatible(base_url, key, model)
        if not ok:
            return _redirect("/apikey", error=detail)
        ws.save_llm(provider, key, model=model, base_url=base_url, workspace_id=workspace_id)
        restarted = False
        for one in ws.all_campaigns():      # one AI provider per user: every campaign's worker picks it up
            restarted = await one.restart_worker_if_running("the new AI provider") or restarted
        await ws.restart_mail_worker_if_running()
        return _redirect("/apikey", msg=f"{preset['label']} saved and verified ({model or 'default model'})."
                         + (" The worker was restarted to use it." if restarted else ""))

    @app.post("/apikey/remove")
    async def apikey_remove(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        for one in ws.all_campaigns():      # no campaign keeps using a key that was removed
            if one.worker_running():
                await one.stop_worker("the AI provider was removed")
        await ws.stop_mail_worker()
        ws.clear_llm()
        return _redirect("/apikey", msg="AI provider removed. Every campaign's worker and the background mail worker were stopped.")

    # ── mailbox: Gmail (OAuth) or the mailbox's own SMTP/IMAP ──

    @app.get("/mailbox", response_class=HTMLResponse)
    async def mailbox_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        smtp = ws.smtp_settings()
        m365 = ws.m365_settings()
        sending = ws.sending_address()
        domain = deliverability.domain_of(sending)
        report = await check_domain(domain) if domain and ws.mailbox_connected() else None
        # For a custom or self-hosted SMTP server, also check the sending IP's
        # reverse DNS and blocklist standing (skipped for the big providers).
        host_report = None
        if report and smtp.get("smtp_host") and ws.mail_provider() == "smtp":
            host_report = await deliverability.check_sending_host(smtp["smtp_host"])
        health = await ws.mailbox_health()
        bound, choices = None, []
        if not ws.is_main:
            bound = ws.bound_mailbox()
            mdb = ws.mail_db()
            await mdb.init()
            from bb import campaigns as campaigns_mod
            ws.main().sync_main_registry()
            others = {(r.get("account") or "").lower() for r in campaigns_mod.load(ws.user_root) if r["id"] != ws.campaign_id}
            choices = [m for m in await mdb.mailboxes() if m["address"].lower() not in others and m.get("enabled", 1)]
        return await render(request, "mailbox.html", user, provider=ws.mail_provider(),
                            campaign_bound=bound, campaign_choices=choices, campaign_main=ws.is_main,
                            campaign_id=ws.campaign_id, campaign_label=ws.campaign_name(),
                            gmail_connected=ws.gmail_connected(), account=ws.gmail_account(),
                            gmail_available=bool(settings.gmail_client_id and settings.gmail_client_secret),
                            microsoft_available=bool(settings.ms_client_id),
                            microsoft_connected=ws.microsoft_connected(), ms_account=ws.microsoft_account(),
                            ms_redirect_uri=microsoft_oauth.redirect_uri(settings),
                            reconnect=ws.needs_reconnect(health),
                            alerts=ws.alert_settings(), alert_state=ws.alert_state(),
                            alert_email_ready=settings.alert_email_ready,
                            smtp={**smtp, "smtp_password": "", "imap_password": ""},
                            smtp_configured=ws.smtp_configured(),
                            smtp_masked=ws.mask(smtp["smtp_password"]),
                            m365={**m365, "m365_client_secret": ""}, m365_configured=ws.m365_configured(),
                            m365_masked=ws.mask(m365["m365_client_secret"]),
                            sending=sending, report=report, host_report=host_report, health=health,
                            redirect_uri=gmail_oauth.redirect_uri(settings))

    @app.post("/mailbox/alerts")
    async def mailbox_alerts_save(request: Request, user: dict = Depends(current_user)):
        from bb import alerts as alerts_mod
        ws = workspace_for(user)
        form = dict(await request.form())
        webhook = str(form.get("alert_webhook", "") or "").strip()
        problem = await asyncio.to_thread(alerts_mod.webhook_problem, webhook) if webhook else ""
        if problem:
            return _redirect("/mailbox", error=f"Alerts not saved: {problem}")
        try:
            ws.save_alert_settings(email=str(form.get("alert_email", "") or ""),
                                   email_on=bool(form.get("alert_email_on")), webhook=webhook)
        except ValueError as e:
            return _redirect("/mailbox", error=f"Alerts not saved: {e}")
        return _redirect("/mailbox", msg="Alert settings saved. Send a test alert to see it arrive.")

    @app.post("/mailbox/alerts/test")
    async def mailbox_alerts_test(user: dict = Depends(current_user)):
        from bb import alerts as alerts_mod
        ws = workspace_for(user)
        reached, problems = await alerts_mod.deliver(
            settings, ws, "Test alert from BB",
            "This is how you will hear that your mailbox stopped sending or needs signing in again. Nothing is wrong now.",
            ok=True)
        if not reached and not problems:
            return _redirect("/mailbox", error="Nothing is switched on: add an alert address or a webhook first.")
        return _redirect("/mailbox", msg=("Test alert sent to " + " and ".join(reached) + ".") if reached else "",
                         error="; ".join(problems))

    @app.post("/mailbox/m365")
    async def mailbox_m365_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if not ws.is_main:
            return _redirect("/mailbox", error=f"{ws.campaign_name()} sends from an account on the Accounts page; change it below.")
        form = dict(await request.form())
        current = ws.m365_settings()
        m365 = {}
        for key in current:
            value = str(form.get(key, "") or "").strip()
            m365[key] = value or (current[key] if key == "m365_client_secret" else "")
        m365["m365_mailbox"] = m365["m365_mailbox"].lower()
        taken = campaign_taken(ws, m365["m365_mailbox"])
        if taken:
            return _redirect("/mailbox", error=f"Not saved: that mailbox already sends for the campaign {taken} (one campaign per account).")
        if not all(m365.values()):
            return _redirect("/mailbox", error="Tenant ID, client ID, client secret and mailbox are all required.")
        ok, detail = await test_m365(m365)
        if not ok:
            return _redirect("/mailbox", error=f"Not saved: {detail}")
        ws.save_secrets(**m365)
        ws.set_mail_provider("microsoft_app")
        ws.clear_mailbox_problem()
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
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
        ws.set_mail_provider("gmail" if ws.gmail_connected() else ("microsoft" if ws.microsoft_connected() else "smtp"))
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
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
        if not ws.is_main:
            return _redirect("/mailbox", error=f"{ws.campaign_name()} sends from an account on the Accounts page; change it below.")
        ready = {"gmail": ws.gmail_connected(), "smtp": ws.smtp_configured(), "microsoft": ws.microsoft_connected(),
                 "microsoft_app": ws.m365_configured()}
        if provider in ready and not ready[provider]:
            needs = {"gmail": "Connect Gmail first (below).", "smtp": "Save your mailbox's SMTP settings first (below).",
                     "microsoft": "Sign in with Microsoft first (below).", "microsoft_app": "Save the Microsoft 365 app settings first (below)."}
            return _redirect("/mailbox", error=f"Nothing changed: that mailbox is not connected. {needs[provider]}")
        try:
            ws.set_mail_provider(provider)
        except ValueError as e:
            return _redirect("/mailbox", error=str(e))
        ws.clear_mailbox_problem()
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
        label = {"gmail": "Gmail", "microsoft": "your Microsoft account", "microsoft_app": "the Microsoft 365 app"
                 }.get(provider, "your mailbox's SMTP/IMAP settings")
        return _redirect("/mailbox", msg=f"Emails will go through {label}."
                         + (" The worker was restarted with it." if ws.worker_running() else ""))

    @app.post("/mailbox/smtp")
    async def mailbox_smtp_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if not ws.is_main:
            return _redirect("/mailbox", error=f"{ws.campaign_name()} sends from an account on the Accounts page; change it below.")
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
        taken = campaign_taken(ws, smtp.get("from_email") or smtp["smtp_username"])
        if taken:
            return _redirect("/mailbox", error=f"Not saved: that account already sends for the campaign {taken} (one campaign per account).")
        ok, detail = await test_smtp(smtp)
        if not ok:
            return _redirect("/mailbox", error=f"Not saved: {detail}")
        ws.save_secrets(**smtp)
        ws.set_mail_provider("smtp")
        ws.clear_mailbox_problem()
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
        return _redirect("/mailbox", msg=f"Mailbox saved. {detail}")

    @app.post("/mailbox/login")
    async def mailbox_login(request: Request, user: dict = Depends(current_user)):
        """Email + password: the servers are worked out from the address."""
        ws = workspace_for(user)
        if not ws.is_main:
            return _redirect("/mailbox", error=f"{ws.campaign_name()} sends from an account on the Accounts page; change it below.")
        form = dict(await request.form())
        email = str(form.get("email", "") or "").strip().lower()
        password = str(form.get("password", "") or "")
        if not email or not password:
            return _redirect("/mailbox", error="Enter the email address and its password.")
        taken = campaign_taken(ws, email)
        if taken:
            return _redirect("/mailbox", error=f"That account already sends for the campaign {taken} (one campaign per account).")
        guess, note = guess_settings(email)
        if guess is None:
            return _redirect("/mailbox", error=note)
        smtp = {**guess, "smtp_password": password}
        ok, detail = await test_smtp(smtp)
        if not ok:
            hint = ""
            if smtp["smtp_host"] == "smtp.gmail.com":
                hint = " Gmail needs an app password (myaccount.google.com/apppasswords), not your normal password."
            elif smtp["smtp_host"] == "smtp.office365.com":
                hint = " Microsoft 365 needs Authenticated SMTP switched on by your admin, or use Option C."
            elif smtp["smtp_host"].startswith("smtp.mail.yahoo") or smtp["smtp_host"].startswith("smtp.mail.me"):
                hint = " This provider needs an app password from its security settings."
            return _redirect("/mailbox", error=f"Could not sign in: {detail}.{hint} {note}")
        ws.save_secrets(**smtp)
        ws.set_mail_provider("smtp")
        ws.clear_mailbox_problem()
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
        return _redirect("/mailbox", msg=f"Signed in to {email}. {detail}")

    @app.post("/mailbox/paste")
    async def mailbox_paste(request: Request, user: dict = Depends(current_user)):
        """Paste the mailbox login (a settings block, a connection URL, JSON
        or one line); it is read, the servers filled from the address, then
        tested and saved like the typed form."""
        ws = workspace_for(user)
        if not ws.is_main:
            return _redirect("/mailbox", error=f"{ws.campaign_name()} sends from an account on the Accounts page; change it below.")
        form = dict(await request.form())
        parsed, note = parse_pasted_login(str(form.get("pasted", "") or ""))
        if parsed is None:
            return _redirect("/mailbox", error=note)
        email = parsed["email"]
        taken = campaign_taken(ws, email)
        if taken:
            return _redirect("/mailbox", error=f"That account already sends for the campaign {taken} (one campaign per account).")
        try:
            smtp, _ = to_smtp_fields(parsed, guess_settings)
        except ValueError as e:
            return _redirect("/mailbox", error=str(e))
        ok, detail = await test_smtp(smtp)
        if not ok:
            hint = ""
            if smtp["smtp_host"] == "smtp.gmail.com":
                hint = " Gmail needs an app password (myaccount.google.com/apppasswords), not your normal password."
            elif smtp["smtp_host"] == "smtp.office365.com":
                hint = " Microsoft 365 needs Authenticated SMTP switched on by your admin, or sign in with Microsoft."
            elif smtp["smtp_host"].startswith("smtp.mail.yahoo") or smtp["smtp_host"].startswith("smtp.mail.me"):
                hint = " This provider needs an app password from its security settings."
            return _redirect("/mailbox", error=f"Could not sign in: {detail}.{hint} {note}")
        ws.save_secrets(**smtp)
        ws.set_mail_provider("smtp")
        ws.clear_mailbox_problem()
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
        return _redirect("/mailbox", msg=f"Signed in to {email} from the login you pasted. {note} {detail}")

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
        await ws.restart_worker_if_running("the new mailbox settings")
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
        if not workspace_for(user).is_main:
            return _redirect("/mail/accounts", error="This campaign sends from an account on the Accounts page; connect or reconnect it there.")
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
        ws = base_workspace(user)      # the Mailbox page's own sign-in is the main campaign's
        try:
            data = await gmail_oauth.exchange_code(settings, code)
            email = await gmail_oauth.account_email(data.get("access_token", ""))
        except Exception as e:
            return _redirect("/mailbox", error=str(e))
        taken = campaign_taken(ws, email)
        if taken:
            return _redirect("/mailbox", error=f"{email} already sends for the campaign {taken} (one campaign per account).")
        gmail_oauth.save_token(ws, data, email)
        ws.set_mail_provider("gmail")
        ws.clear_mailbox_problem()
        ws.rewrite_config()  # the persona sends from the connected mailbox
        await ws.restart_worker_if_running("the new mailbox settings")
        response = _redirect("/mailbox", msg=f"Gmail connected as {email}.")
        response.delete_cookie(GMAIL_NONCE_COOKIE, path="/gmail")
        return response

    @app.post("/gmail/disconnect")
    async def gmail_disconnect(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        was = ws.mail_provider()
        if ws.worker_running() and was == "gmail":
            await ws.stop_worker("Gmail was disconnected")
        ws.disconnect_gmail()
        if was == "gmail":      # only the mailbox in use needs a replacement
            if ws.microsoft_connected():
                ws.set_mail_provider("microsoft")
            elif ws.smtp_configured():
                ws.set_mail_provider("smtp")
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
        return _redirect("/mailbox", msg="Gmail disconnected.")

    @app.post("/gmail/test")
    async def gmail_test(user: dict = Depends(current_user)):
        ok, detail = await gmail_oauth.test_connection(settings, workspace_for(user))
        return _redirect("/mailbox", msg=detail if ok else "", error="" if ok else detail)

    # ── Microsoft sign-in (the site's Entra app): Outlook, Hotmail, Microsoft 365 ──

    @app.post("/microsoft/connect")
    async def microsoft_connect(user: dict = Depends(current_user)):
        if not workspace_for(user).is_main:
            return _redirect("/mail/accounts", error="This campaign sends from an account on the Accounts page; connect or reconnect it there.")
        if not settings.ms_client_id:
            return _redirect("/mailbox", error="The site has no Microsoft app registration configured yet. "
                                               "Use the Microsoft 365 admin app or your mailbox's SMTP settings instead.")
        nonce = secrets.token_urlsafe(16)
        state = microsoft_oauth.sign_state(settings.secret_key(), user["id"], nonce)
        ws = workspace_for(user)
        hint = ws.microsoft_account().get("email", "") or ws.profile().get("email", "")
        response = RedirectResponse(microsoft_oauth.auth_url(settings, state, login_hint=hint), status_code=303)
        response.set_cookie(MS_NONCE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
                            secure=settings.secure_cookies, path="/microsoft")
        return response

    @app.get("/microsoft/callback")
    async def microsoft_callback(request: Request, user: dict = Depends(current_user),
                                 code: str = "", state: str = "", error: str = "", error_description: str = ""):
        nonce = request.cookies.get(MS_NONCE_COOKIE, "")
        if error:
            return _redirect("/mailbox", error=f"Microsoft said: {error_description or error}")
        if not microsoft_oauth.verify_state(settings.secret_key(), user["id"], nonce, state):
            return _redirect("/mailbox", error="The sign-in did not match this session. Try again.")
        ws = base_workspace(user)      # the Mailbox page's own sign-in is the main campaign's
        try:
            data = await microsoft_oauth.exchange_code(settings, code)
            email = await microsoft_oauth.account_email(data.get("access_token", ""))
        except Exception as e:
            return _redirect("/mailbox", error=str(e))
        taken = campaign_taken(ws, email)
        if taken:
            return _redirect("/mailbox", error=f"{email} already sends for the campaign {taken} (one campaign per account).")
        if not email:
            return _redirect("/mailbox", error="Microsoft did not say which mailbox this is. Try again.")
        microsoft_oauth.save_token(ws, data, email)
        ws.set_mail_provider("microsoft")
        ws.clear_mailbox_problem()
        ws.rewrite_config()  # the persona sends from the connected mailbox
        await ws.restart_worker_if_running("the new mailbox settings")
        response = _redirect("/mailbox", msg=f"Microsoft connected as {email}.")
        response.delete_cookie(MS_NONCE_COOKIE, path="/microsoft")
        return response

    @app.post("/microsoft/disconnect")
    async def microsoft_disconnect(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        was = ws.mail_provider()
        if ws.worker_running() and was == "microsoft":
            await ws.stop_worker("the Microsoft account was disconnected")
        ws.disconnect_microsoft()
        if was == "microsoft":  # only the mailbox in use needs a replacement
            if ws.gmail_connected():
                ws.set_mail_provider("gmail")
            elif ws.smtp_configured():
                ws.set_mail_provider("smtp")
        ws.rewrite_config()
        await ws.restart_worker_if_running("the new mailbox settings")
        return _redirect("/mailbox", msg="Microsoft account disconnected.")

    @app.post("/microsoft/test")
    async def microsoft_test(user: dict = Depends(current_user)):
        ok, detail = await microsoft_oauth.test_connection(settings, workspace_for(user))
        return _redirect("/mailbox", msg=detail if ok else "", error="" if ok else detail)

    # ── recipients ──

    @app.get("/recipients", response_class=HTMLResponse)
    async def recipients_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        excluded = await ws.excluded_recipients()
        gone = {e["email"] for e in excluded}
        recent = [r for r in await ws.recent_recipients() if r["email"] not in gone]
        return await render(request, "recipients.html", user, verifier=bool(os.environ.get("REOON_API_KEY") or os.environ.get("ZEROBOUNCE_API_KEY") or os.environ.get("HUNTER_API_KEY")), recent=recent,
                            excluded=excluded, sectors=await ws.sector_breakdown(), max_rows=recipients_mod.MAX_ROWS)

    @app.post("/recipients/clean")
    async def recipients_clean(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        report = await ws.clean_list()
        checked, moved = report.get("checked", 0), report.get("excluded", 0)
        if moved:
            msg = (f"Checked {checked} and moved {moved} invalid address(es) to the Excluded list "
                   "(dead domains, disposable or malformed). Undo any below.")
        else:
            msg = f"Checked {checked}. No invalid addresses found — nothing to clean."
        return _redirect("/recipients", msg=msg)

    @app.post("/recipients/{prospect_id}/restore")
    async def recipients_restore(prospect_id: str, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        await ws.restore_recipient(prospect_id)
        return _redirect("/recipients", msg="Restored to the sending list.")

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
        from bb.campaign_routes import import_for
        result = await import_for(ws, rows)
        msg = f"Added {result['added']} recipient(s)."
        if result["duplicates"]:
            msg += f" {result['duplicates']} already on your list."
        if result.get("elsewhere"):
            msg += f" {result['elsewhere']} already on another campaign's list (each account writes only to its own leads)."
        if result.get("suppressed"):
            msg += f" {result['suppressed']} asked never to be contacted."
        if result["invalid"]:
            msg += f" {result['invalid']} skipped (no valid email)."
        if result["too_many"]:
            msg += f" {result['too_many']} beyond the {recipients_mod.MAX_ROWS:,} limit were ignored."
        return _redirect("/recipients", msg=msg)

    # ── outbox ──

    @app.get("/outbox", response_class=HTMLResponse)
    async def outbox_page(request: Request, user: dict = Depends(current_user),
                          status: str = "pending_review"):
        if status not in ("pending_review", "approved", "sent", "failed", "bounced", "rejected"):
            status = "pending_review"
        ws = workspace_for(user)
        from bb.mail.sending import preview_document
        items = await ws.outbox(status)
        labels = ws.link_names()
        from harvey.integrations.mail_provider import embed_links
        for it in items:
            # What the recipient sees: every link on the words that name it.
            shown = ws.as_sent(it)
            it["as_sent"] = {**shown, "doc": preview_document(shown.get("html") or "")}
            it["body_md"] = embed_links(it.get("body") or "", labels)
            it["default_link"] = next(iter(labels), "")
        return await render(request, "outbox.html", user, items=items, status=status)

    @app.post("/outbox/rewrite-unsent")
    async def outbox_rewrite_unsent(user: dict = Depends(current_user)):
        ws = workspace_for(user)
        done, restarted = await ws.rewrite_unsent_now()
        return _redirect("/", msg=f"Cancelled {done['cancelled']} unsent email(s); {done['prospects']} recipient(s) will be "
                                  f"written to again with the current brief."
                                  + (" The worker was restarted." if restarted else ""))

    @app.post("/outbox/approve-all")
    async def outbox_approve_all(user: dict = Depends(current_user)):
        state = workspace_for(user).state()
        await state.init_db()
        n = await state.approve_outbox()
        return _redirect("/outbox", msg=f"Approved {n} email(s).")

    @app.post("/outbox/reject-all")
    async def outbox_reject_all(user: dict = Depends(current_user), status: str = Form("pending_review")):
        state = workspace_for(user).state()
        await state.init_db()
        n = await state.reject_all_outbox(status)
        verb = "Cancelled" if status == "approved" else "Rejected"
        return _redirect(f"/outbox?status={status}", msg=f"{verb} {n} email(s); none will be sent.")

    @app.post("/outbox/{item_id}/{action}")
    async def outbox_action(request: Request, item_id: str, action: str, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        state = ws.state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item:
            raise HTTPException(status_code=404)
        if action == "preview":
            from bb.mail.sending import preview_document
            form = dict(await request.form())
            shown = ws.as_sent(item, str(form.get("body", "") or ""))
            return JSONResponse({"doc": preview_document(shown.get("html") or ""), "plain": shown.get("plain", False)})
        back = f"/outbox?status={item['status'] if item['status'] in ('pending_review', 'approved') else 'pending_review'}"
        if action == "approve":
            await state.approve_outbox(item_id)
            return _redirect("/outbox", msg="Approved.")
        if action == "reject":
            await state.update_outbox_item(item_id, status="rejected", error="rejected in the web app")
            return _redirect(back, msg="It will not be sent.")
        if action in ("edit", "rewrite") and item["status"] not in ("pending_review", "approved"):
            return _redirect(back, msg="Only a queued or held email can be changed; this one has already gone.")
        if action == "edit":
            form = await request.form()
            subject = str(form.get("subject") or "").strip()[:200]
            from harvey.integrations.mail_provider import normalize_newlines
            body = normalize_newlines(str(form.get("body") or "")).strip()
            if not subject or not body:
                return _redirect(back, msg="A subject and a body are needed.")
            fields = {"subject": subject, "body": body, "error": ""}
            if form.get("approve") and item["status"] == "pending_review":
                fields["status"] = "approved"
            await state.update_outbox_item(item_id, **fields)
            return _redirect(f"/outbox?status={fields.get('status', item['status'])}",
                             msg="Saved." + (" It is scheduled." if fields.get("status") == "approved" else ""))
        if action == "rewrite":
            form = await request.form()
            instruction = str(form.get("instruction") or "").strip()[:500]
            try:
                result = await ws.rewrite_item(item, instruction)
            except Exception as e:
                return _redirect(back, msg=f"Could not rewrite it: {e}")
            return _redirect(back, msg="Rewritten. " + result["note"])
        raise HTTPException(status_code=404)

    # ── replies ──

    @app.get("/replies", response_class=HTMLResponse)
    async def replies_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "replies.html", user, conversations=await ws.conversations())

    # ── the mail command center ──
    from bb.mail.routes import register_mail_routes
    register_mail_routes(app, settings, current_user, workspace_for, render, _redirect, templates)
    from bb.campaign_routes import register_campaign_routes
    register_campaign_routes(app, settings, current_user, base_workspace, render, _redirect)

    # ── inbox auto-reply ──

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        return await render(request, "inbox.html", user, inbox=ws.inbox_settings(), scope_links=ws.scope_links(),
                            replies=await ws.inbox_replies(), max_links=MAX_INBOX_LINKS,
                            attachments=ws.attachments(), configured=ws.is_configured(),
                            backlog_choices=Workspace.BACKLOG_CHOICES,
                            require_approval=ws.profile().get("require_approval", False))

    @app.post("/inbox")
    async def inbox_save(request: Request, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        form = dict(await request.form())
        try:
            ws.write_inbox(form)
        except ValidationError as e:
            first = e.errors()[0]
            return _redirect("/inbox", error=f"Could not save: {'.'.join(str(p) for p in first['loc'])}: {first['msg']}")
        except ValueError as e:
            return _redirect("/inbox", error=str(e))
        restarted = await ws.restart_worker_if_running("the inbox settings")
        on = bool(form.get("inbox_autoreply"))
        return _redirect("/inbox", msg=("Inbox auto-reply is on." if on else "Inbox auto-reply is off.")
                         + (" The worker was restarted." if restarted else ""))

    # ── admin ──

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request, admin: dict = Depends(current_admin)):
        rows = []
        for u in accounts.list_users():
            ws = Workspace(settings, u)
            rows.append({**u, "running": any(one.worker_running() for one in ws.all_campaigns()), "configured": ws.is_configured(),
                         "gmail": ws.gmail_connected()})
        return await render(request, "admin_users.html", admin, users=rows, problems=settings.problems(),
                            tunnel=tunnel.describe(), public_url=settings.base_url,
                            link=await check_public_url(settings.base_url, use_cache="recheck" not in request.query_params))

    @app.get("/admin/signin", response_class=HTMLResponse)
    async def admin_signin_page(request: Request, admin: dict = Depends(current_admin)):
        from bb import signin_setup
        base = settings.base_url
        from bb import signin_auto
        job = signin_auto.job_for(admin["id"])
        return await render(request, "admin_signin.html", admin, st=signin_setup.status(settings), base_url=base,
                            ms_job=job.view() if job else None,
                            quick_tunnel="trycloudflare.com" in base,
                            google_uris=[base + "/gmail/callback", base + "/mail/gmail/callback"],
                            ms_uris=[base + "/microsoft/callback", base + "/mail/microsoft/callback"])

    @app.post("/admin/signin")
    async def admin_signin_save(request: Request, admin: dict = Depends(current_admin)):
        from bb import signin_setup
        form = dict(await request.form())
        provider = str(form.get("provider") or "")
        clear = [str(form["clear"])] if form.get("clear") in ("google", "microsoft") else []
        fields = {k: v for k, v in form.items() if k in signin_setup.FIELDS and
                  ((provider == "google" and k.startswith("gmail_")) or (provider == "microsoft" and k.startswith("ms_")))}
        warnings = []
        upload = form.get("client_json")
        if provider == "google" and upload is not None and getattr(upload, "filename", ""):
            # The file Google offers when the client is made: no copying by hand.
            from bb.signin_auto import read_google_client
            raw = await upload.read(65537)
            if len(raw) > 65536:
                return _redirect("/admin/signin", error="That file is far bigger than the one Google gives for a client "
                                                        "(client_secret_….json, a few hundred bytes).")
            values, bad, warnings = read_google_client(raw, settings.base_url)
            if bad:
                return _redirect("/admin/signin", error=" ".join(bad))
            fields.update(values)
        problems = signin_setup.save(settings, {**fields, "clear": clear})
        if problems:
            return _redirect("/admin/signin", error=" ".join(problems))
        restarted = await _restart_for_signin()
        if provider == "microsoft":
            from bb import signin_auto
            signin_auto.forget(admin["id"])       # a finished automatic setup's report no longer holds
        name = "Google" if provider == "google" else "Microsoft"
        if clear:
            msg = f"Sign in with {name} is off."
        else:
            on = settings.gmail_client_id if provider == "google" else settings.ms_client_id
            msg = (f"Sign in with {name} is on: the button works on every Mailbox and Accounts page now."
                   if on else f"Saved. Sign in with {name} is still off: it needs the client ID.")
        return _redirect("/admin/signin", msg=msg + (f" {restarted} background worker(s) restarted to use it." if restarted else "")
                         + ("" if not warnings else " " + " ".join(warnings)))

    async def _restart_for_signin() -> int:
        """Workers hold the sign-in settings from when they started: the ones
        that use them start again with the new ones."""
        restarted = 0
        for ws in all_workspaces(settings, accounts):
            try:
                if await ws.restart_mail_worker_if_running():
                    restarted += 1
                for one in ws.all_campaigns():
                    if one.worker_running() and one.mail_provider() in ("gmail", "microsoft"):
                        if await one.restart_worker_if_running("the site's new sign-in settings"):
                            restarted += 1
            except Exception as e:
                logger.warning("restart after sign-in setup failed for %s: %s", ws.user["email"], e)
        return restarted

    @app.post("/admin/signin/microsoft/auto")
    async def admin_signin_ms_auto(admin: dict = Depends(current_admin)):
        """Set Microsoft sign-in up from the admin's own Microsoft sign-in."""
        from bb import signin_auto, signin_setup
        if {"ms_client_id", "ms_client_secret", "ms_tenant"} & signin_setup.from_server(settings):
            return _redirect("/admin/signin", error="Microsoft sign-in is set in the server's own settings (bb.env); change it there.")

        async def save(client_id, secret, tenant):
            problems = signin_setup.save(settings, {"ms_client_id": client_id, "ms_client_secret": secret, "ms_tenant": tenant})
            if not problems:
                await _restart_for_signin()
            return problems

        # The app this site uses now, if any, is updated rather than replaced.
        current = settings.ms_client_id if signin_setup.stored(settings).get("ms_client_id") == settings.ms_client_id else ""
        job = await signin_auto.start(admin["id"], settings.base_url, save, current_client_id=current)
        if job.state == "failed":
            signin_auto.cancel(admin["id"])
            return _redirect("/admin/signin", error=job.error)
        return RedirectResponse("/admin/signin#microsoft", status_code=303)

    @app.get("/admin/signin/microsoft/auto")
    async def admin_signin_ms_auto_status(admin: dict = Depends(current_admin)):
        from bb import signin_auto
        job = signin_auto.job_for(admin["id"])
        return JSONResponse(job.view() if job else {"state": "none"})

    @app.post("/admin/signin/microsoft/auto/cancel")
    async def admin_signin_ms_auto_cancel(admin: dict = Depends(current_admin)):
        from bb import signin_auto
        if not signin_auto.cancel(admin["id"]):
            return _redirect("/admin/signin", msg="It is setting things up now and finishes by itself in a moment.")
        return RedirectResponse("/admin/signin#microsoft", status_code=303)

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
            for one in ws.all_campaigns():  # every campaign of theirs, and the mail worker
                if one.worker_running():
                    await one.stop_worker("the account was disabled by the admin")
            await ws.stop_mail_worker()
            return _redirect("/admin/users", msg=f"Disabled {target['email']} and stopped all their workers.")
        if action == "enable":
            accounts.set_disabled(user_id, False)
            return _redirect("/admin/users", msg=f"Enabled {target['email']}.")
        raise HTTPException(status_code=404)

    return app
