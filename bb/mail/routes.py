"""The pages under /mail: overview, accounts, per-mailbox settings, jobs,
the scope selector, Gmail OAuth for mailboxes, and the live event stream."""

import asyncio
import json
import logging
import re
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError

from bb import gmail_oauth, microsoft_oauth
from bb.mail import service
from bb.mail.jobs import Jobs
from bb.mail import scope as scope_mod
from bb.mail.models import COLORS, MailboxSettings
from bb.mail.providers import AuthError, TransientError
from bb.mail_autoconfig import guess_settings
from bb.mail_paste import parse_pasted_login, to_smtp_fields

logger = logging.getLogger("bb.mail.routes")

GMAIL_STATE_COOKIE = "bb_mail_gmail_state"
BULK_LIMIT = 50


def parse_bulk_accounts(text: str) -> list[dict]:
    """One account per line: ``address password``, ``address,password`` or
    ``address<TAB>password``, optionally followed by the SMTP server and
    the IMAP server (``host`` or ``host:port``) for a mailbox whose servers
    cannot be worked out from the address. A password with spaces or
    commas goes in a tab-separated line. Lines starting with # are
    ignored; the same address twice counts once."""
    out, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([^\s,;:\t]+@[^\s,;:\t]+)[\s,;:\t]+(.+)$", line)
        if not m:
            continue
        email, rest = m.group(1).strip().lower(), m.group(2)
        # The servers, if any, are the last one or two fields and look like
        # host[:port]; everything between the address and them is the
        # password, commas and spaces included.
        servers = []
        for _ in range(2):
            tail = re.search(r"[\s,;\t]+([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d{1,5})?)\s*$", rest)
            if not tail or "." not in tail.group(1).split(":")[0]:
                break
            servers.insert(0, tail.group(1))
            rest = rest[:tail.start()]
        password = rest.strip()
        if not password:
            continue
        if email in seen:
            continue
        seen.add(email)
        entry = {"email": email, "password": password}
        for key, value in zip(("smtp", "imap"), servers):
            host, _, port = value.partition(":")
            entry[f"{key}_host"] = host
            entry[f"{key}_port"] = port
        if "smtp_host" in entry and "imap_host" not in entry:
            entry["imap_host"] = entry["smtp_host"]
        out.append(entry)
    return out
MS_STATE_COOKIE = "bb_mail_ms_state"


def register_mail_routes(app, settings, current_user, workspace_for, render, _redirect, templates):

    async def ctx(user: dict) -> dict:
        """Everything the mail pages share: the user's mailboxes, the scope,
        worker state. Also runs the one-time migration and revives a dead
        worker."""
        ws = workspace_for(user)
        db = ws.mail_db()
        await db.init()
        await service.migrate_from_workspace(db, settings.secret_key(), ws.main())
        revived = ws.revive_mail_worker()
        boxes = await db.mailboxes()
        for m in boxes:
            m["settings_model"] = service.settings_of(m)
            m["ready"], m["ready_why"] = service.ready_to_send(m)
            m["signin"] = service.signed_in(settings.secret_key(), m)
        ids = await service.scope(db)
        selected = boxes if ids is None else [m for m in boxes if m["id"] in ids]
        return {"ws": ws, "db": db, "mailboxes": boxes, "scope_ids": ids, "selected": selected,
                "mail_worker": ws.mail_worker_alive(), "mail_worker_pid": ws.mail_worker_running(),
                "revived": revived, "colors": COLORS}

    def page_ctx(c: dict) -> dict:
        return {k: v for k, v in c.items() if k not in ("ws", "db")}

    def _scope_for(c: dict, address: str = "") -> dict:
        """The goal and brief an account writes with: the outreach campaign
        that sends from it, else the main profile."""
        from bb import campaigns as campaigns_mod
        main = c["ws"].main()
        target = main.campaign(campaigns_mod.profile_for_account(main.user_root, address)) if address else main
        return scope_mod.from_config((target or main).config())

    # ── overview ──

    @app.get("/mail", response_class=HTMLResponse)
    async def mail_overview(request: Request, user: dict = Depends(current_user)):
        c = await ctx(user)
        activity = [a for a in await c["db"].activity(limit=60, mailbox_ids=c["scope_ids"])
                    if not a["text"].startswith("Background worker ")][:40]
        jobs = Jobs(c["db"])
        active_runs = await c["db"].query("SELECT * FROM reply_runs WHERE status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 5")
        from bb.mail import commands
        brain = _brain(c["ws"])
        return await render(request, "mail_overview.html", user, **page_ctx(c), activity=activity, active_runs=active_runs,
                            job_counts=await jobs.counts(), msg=c["revived"] or request.query_params.get("msg", ""),
                            today=service.now_iso()[:10], command_history=list(reversed(await commands.history(c["db"]))), command_pending=await commands.pending(c["db"]),
                            brain_ok=brain.configured, brain_tools=brain.supports_tools, brain_label=brain.label)

    # ── the command bar ──

    def _center(c, user):
        from bb.mail.commands import CommandCenter
        ws = c["ws"]
        return CommandCenter(c["db"], _brain(ws), c["mailboxes"], c["selected"], settings.secret_key(), {},
                             user.get("email", ""), on_worker_needed=lambda: (ws.mail_worker_running() or ws.start_mail_worker()),
                             scope=_scope_for(c), scope_for=lambda address: _scope_for(c, address))

    @app.post("/mail/command")
    async def mail_command(request: Request, user: dict = Depends(current_user)):
        from bb.mail import commands
        c = await ctx(user)
        form = await request.form()
        text = str(form.get("q", "") or "").strip()[:1000]
        if await commands.pending(c["db"]):
            return _redirect("/mail", error="Confirm or cancel the pending action first.")
        center = _center(c, user)
        result = await center.run(text)
        await commands.remember(c["db"], text, result["reply"], result["steps"])
        await commands.set_pending(c["db"], result["pending"])
        return _redirect("/mail#command")

    @app.post("/mail/command/{action}")
    async def mail_command_action(action: str, user: dict = Depends(current_user)):
        from bb.mail import commands
        from bb.mail.brain import BrainError
        c = await ctx(user)
        if action == "clear":
            await c["db"].set_setting(commands.HISTORY_KEY, "[]")
            await commands.set_pending(c["db"], None)
            return _redirect("/mail")
        pend = await commands.pending(c["db"])
        if not pend:
            return _redirect("/mail", error="Nothing is waiting for confirmation.")
        if action == "cancel":
            await commands.set_pending(c["db"], None)
            await commands.remember(c["db"], "(cancelled)", f"Not done: {pend['description']}", [])
            return _redirect("/mail#command")
        if action != "confirm":
            raise HTTPException(404)
        try:
            line = await commands.confirm(_center(c, user), pend)
        except BrainError as e:
            await commands.set_pending(c["db"], None)
            return _redirect("/mail#command", error=str(e))
        await commands.set_pending(c["db"], None)
        await commands.remember(c["db"], "(confirmed)", f"Done: {pend['description']}. {line}", [pend["kind"]])
        return _redirect("/mail#command", msg="Confirmed.")


    @app.post("/mail/scope")
    async def mail_scope(request: Request, user: dict = Depends(current_user)):
        c = await ctx(user)
        form = await request.form()
        chosen = [str(x) for x in form.getlist("mailbox")]
        if form.get("all") or not chosen:
            await service.set_scope(c["db"], None)
        else:
            await service.set_scope(c["db"], chosen)
        back = str(form.get("back") or "/mail")
        return _redirect(back if back.startswith("/mail") else "/mail")

    @app.post("/mail/worker/{action}")
    async def mail_worker(action: str, user: dict = Depends(current_user)):
        ws = workspace_for(user)
        if action == "start":
            ok, detail = ws.start_mail_worker()
        elif action == "stop":
            ok, detail = await ws.stop_mail_worker()
        elif action == "restart":
            ok, detail = await ws.restart_mail_worker("Restart pressed on the Mail page")
        else:
            raise HTTPException(404)
        return _redirect("/mail", msg=detail if ok else "", error="" if ok else detail)

    # ── accounts ──

    @app.get("/mail/accounts", response_class=HTMLResponse)
    async def mail_accounts(request: Request, user: dict = Depends(current_user)):
        c = await ctx(user)
        return await render(request, "mail_accounts.html", user, **page_ctx(c),
                            gmail_available=bool(settings.gmail_client_id and settings.gmail_client_secret),
                            ms_available=bool(settings.ms_client_id),
                            redirect_uri=settings.base_url + "/mail/gmail/callback",
                            ms_redirect_uri=settings.base_url + "/mail/microsoft/callback")

    @app.post("/mail/accounts/graph")
    async def mail_connect_graph(request: Request, user: dict = Depends(current_user)):
        """Microsoft 365 through an app registration: tenant, client id, secret."""
        c = await ctx(user)
        form = dict(await request.form())
        email = str(form.get("email", "") or "").strip().lower()
        try:
            mid, detail = await service.connect_graph(
                c["db"], settings.secret_key(), address=email, tenant_id=str(form.get("tenant_id", "") or ""),
                client_id=str(form.get("client_id", "") or ""), client_secret=str(form.get("client_secret", "") or ""),
                env=settings.worker_env())
        except AuthError as e:
            return _redirect("/mail/accounts", error=f"Microsoft refused: {e}")
        except TransientError as e:
            return _redirect("/mail/accounts", error=f"Could not connect: {e}")
        except ValueError as e:
            return _redirect("/mail/accounts", error=str(e))
        await c["ws"].main().account_changed(mid)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} through Microsoft 365. {detail} "
                                                     "Now set its signature and project link below.")

    @app.post("/mail/accounts/bulk")
    async def mail_connect_bulk(request: Request, user: dict = Depends(current_user)):
        """Several mailboxes at once, one per line: address and password
        (an app password where the provider wants one), servers worked out
        from each address unless given. Each is tested and stored on its
        own; one that fails is reported and the rest are kept."""
        c = await ctx(user)
        form = dict(await request.form())
        lines = parse_bulk_accounts(str(form.get("lines", "") or ""))
        if not lines:
            return _redirect("/mail/accounts", error="Put one account per line: the address, then its password "
                                                      "(separated by a comma, a tab or a space).")
        if len(lines) > BULK_LIMIT:
            return _redirect("/mail/accounts", error=f"Up to {BULK_LIMIT} accounts at a time, please.")
        gate = asyncio.Semaphore(4)

        async def one(entry):
            async with gate:
                email, password = entry["email"], entry["password"]
                if entry.get("smtp_host"):
                    smtp = {"smtp_host": entry["smtp_host"], "smtp_port": entry.get("smtp_port") or "587",
                            "imap_host": entry.get("imap_host") or entry["smtp_host"], "imap_port": entry.get("imap_port") or "993",
                            "imap_username": "", "imap_password": "", "smtp_username": email}
                else:
                    smtp, note = await asyncio.to_thread(guess_settings, email)      # DNS off the event loop
                    if smtp is None:
                        return email, False, note
                smtp["smtp_password"] = password
                try:
                    mid, _ = await service.connect_imap(c["db"], settings.secret_key(), address=email, smtp=smtp, env=settings.worker_env())
                    await c["ws"].main().account_changed(mid)
                    return email, True, ""
                except AuthError:
                    hint = " (an app password is needed)" if any(h in smtp["smtp_host"] for h in ("gmail", "yahoo", "me.com")) else ""
                    return email, False, f"sign-in refused{hint}"
                except TransientError:
                    return email, False, "the mail server could not be reached"
                except ValueError as e:
                    return email, False, str(e)[:120]
                except Exception as e:      # one odd server never sinks the batch
                    return email, False, f"could not be checked ({type(e).__name__})"

        results = await asyncio.gather(*(one(e) for e in lines))
        ok = [e for e, good, _ in results if good]
        bad = [f"{e}: {why}" for e, good, why in results if not good]
        if ok and not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        msg = (f"Connected {len(ok)} account{'s' if len(ok) != 1 else ''}: {', '.join(ok)}. Give each its signature and project "
               "link here for replies and scans (an outreach campaign signs with its own Profile), then start campaigns on them "
               "from the Campaigns page." if ok else "")
        return _redirect("/mail/accounts", msg=msg, error=("Not connected: " + "; ".join(bad)) if bad else "")

    @app.post("/mail/accounts/graph/bulk")
    async def mail_connect_graph_bulk(request: Request, user: dict = Depends(current_user)):
        """Several mailboxes of one Microsoft 365 tenant through the same app
        registration: one address per line."""
        c = await ctx(user)
        form = dict(await request.form())
        addresses = [a.strip().lower() for a in str(form.get("addresses", "") or "").replace(",", "\n").splitlines() if "@" in a]
        if not addresses:
            return _redirect("/mail/accounts", error="Put one mailbox address per line.")
        if len(addresses) > BULK_LIMIT:
            return _redirect("/mail/accounts", error=f"Up to {BULK_LIMIT} mailboxes at a time, please.")
        ok, bad = [], []
        gate = asyncio.Semaphore(4)

        async def one(email):
            async with gate:
                try:
                    mid, _ = await service.connect_graph(c["db"], settings.secret_key(), address=email,
                                                         tenant_id=str(form.get("tenant_id", "") or ""),
                                                         client_id=str(form.get("client_id", "") or ""),
                                                         client_secret=str(form.get("client_secret", "") or ""), env=settings.worker_env())
                    await c["ws"].main().account_changed(mid)
                    return email, ""
                except (AuthError, TransientError, ValueError) as e:
                    return email, str(e)[:140]
                except Exception as e:
                    return email, f"could not be checked ({type(e).__name__})"

        for email, why in await asyncio.gather(*(one(e) for e in dict.fromkeys(addresses))):
            (bad.append(f"{email}: {why}") if why else ok.append(email))
        if ok and not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect("/mail/accounts", msg=(f"Connected {len(ok)} through Microsoft 365: {', '.join(ok)}." if ok else ""),
                         error=("Not connected: " + "; ".join(bad)) if bad else "")

    @app.post("/mail/accounts/imap")
    async def mail_connect_imap(request: Request, user: dict = Depends(current_user)):
        """Email + password → servers worked out → tested → stored sealed."""
        c = await ctx(user)
        form = dict(await request.form())
        email = str(form.get("email", "") or "").strip().lower()
        password = str(form.get("password", "") or "")
        if not email or not password:
            return _redirect("/mail/accounts", error="Enter the email address and its password.")
        if form.get("smtp_host"):
            smtp = {k: str(form.get(k, "") or "").strip() for k in
                    ("smtp_host", "smtp_port", "imap_host", "imap_port", "imap_username", "imap_password")}
            smtp["smtp_username"] = str(form.get("username", "") or "").strip() or email
            note = "custom servers"
        else:
            smtp, note = guess_settings(email)
            if smtp is None:
                return _redirect("/mail/accounts", error=note)
        smtp["smtp_password"] = password
        try:
            mid, detail = await service.connect_imap(c["db"], settings.secret_key(), address=email, smtp=smtp,
                                                     env=settings.worker_env())
        except AuthError as e:
            hint = (" Gmail, Yahoo and iCloud need an app password, not the normal one."
                    if any(h in smtp["smtp_host"] for h in ("gmail", "yahoo", "me.com")) else "")
            return _redirect("/mail/accounts", error=f"Sign-in refused: {e}.{hint} ({note})")
        except TransientError as e:
            return _redirect("/mail/accounts", error=f"Could not connect: {e}. ({note})")
        except ValueError as e:
            return _redirect("/mail/accounts", error=str(e))
        await c["ws"].main().account_changed(mid)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} (limited mode). {detail} "
                                                   "Now add the signature and project link.")

    @app.post("/mail/accounts/paste")
    async def mail_connect_paste(request: Request, user: dict = Depends(current_user)):
        """Paste the mailbox's login in whatever shape (a labelled settings
        block, a connection URL, JSON, or one address-and-password line);
        it is read, the servers filled from the address, then tested and
        stored like the typed form."""
        c = await ctx(user)
        form = dict(await request.form())
        parsed, note = parse_pasted_login(str(form.get("pasted", "") or ""))
        if parsed is None:
            return _redirect("/mail/accounts", error=note)
        try:
            smtp, _ = to_smtp_fields(parsed, guess_settings)
        except ValueError as e:
            return _redirect("/mail/accounts", error=str(e))
        email = parsed["email"]
        try:
            mid, detail = await service.connect_imap(c["db"], settings.secret_key(), address=email, smtp=smtp,
                                                     env=settings.worker_env())
        except AuthError as e:
            hint = (" Gmail, Yahoo and iCloud need an app password, not the normal one."
                    if any(h in smtp["smtp_host"] for h in ("gmail", "yahoo", "me.com")) else "")
            return _redirect("/mail/accounts", error=f"Sign-in refused: {e}.{hint} ({note})")
        except TransientError as e:
            return _redirect("/mail/accounts", error=f"Could not connect: {e}. ({note})")
        except ValueError as e:
            return _redirect("/mail/accounts", error=str(e))
        await c["ws"].main().account_changed(mid)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} from the login you pasted. {note} {detail} "
                                                   "Now add the signature and project link.")

    @app.get("/mail/accounts/{mailbox_id}", response_class=HTMLResponse)
    async def mail_account(request: Request, mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        m["settings_model"] = service.settings_of(m)
        m["ready"], m["ready_why"] = service.ready_to_send(m)
        m["signin"] = service.signed_in(settings.secret_key(), m)
        m["provider_limit"] = service.provider_daily_limit(m, service.open_creds(settings.secret_key(), m))
        return await render(request, "mail_account.html", user, **page_ctx(c), box=m,
                            creds=service.masked_creds(settings.secret_key(), m),
                            gmail_available=bool(settings.gmail_client_id and settings.gmail_client_secret),
                            ms_available=bool(settings.ms_client_id))

    @app.post("/mail/accounts/{mailbox_id}")
    async def mail_account_save(request: Request, mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        form = dict(await request.form())
        try:
            new = MailboxSettings(
                display_name=form.get("display_name", ""), signature_html=form.get("signature_html", ""),
                project_link=form.get("project_link", ""), reply_to=form.get("reply_to", ""),
                persona_notes=form.get("persona_notes", ""), auto_send=bool(form.get("auto_send")),
                daily_cap=form.get("daily_cap", 50), account_limit=form.get("account_limit", m["settings"].get("account_limit", 0)),
                excluded=form.get("excluded", ""),
                hours_start=form.get("hours_start", ""), hours_end=form.get("hours_end", ""),
                color=form.get("color", "blue"),
            )
        except ValidationError as e:
            first = e.errors()[0]
            return _redirect(f"/mail/accounts/{mailbox_id}", error=str(first["msg"]).replace("Value error, ", ""))
        # The list as this page showed it: an address added since (someone
        # asked not to be contacted while the page was open) is kept, and
        # only what the person took off here is lifted.
        current = [str(x).strip().lower() for x in (m["settings"].get("excluded") or [])]
        shown_raw = form.get("excluded_shown")
        shown = set(MailboxSettings(excluded=shown_raw or "").excluded) if shown_raw is not None else set(current)
        typed = list(new.excluded)
        added_since = [x for x in current if x not in shown and x not in typed]
        if added_since:
            new = new.model_copy(update={"excluded": typed + added_since})
        await c["db"].update_mailbox(mailbox_id, settings=new.model_dump())
        # An address taken off the list on purpose is no longer held back by
        # the shared opt-out list either (every campaign reads that one),
        # unless the list still covers it (its domain is on it).
        from bb.mail.runs import is_excluded
        lifted = sorted(e for e in (shown & set(current)) - set(new.excluded)
                        if "@" in e and not is_excluded(new, e)) if shown_raw is not None else []
        if lifted:
            try:
                led = c["ws"].ledger()
                for email in lifted:
                    led.unsuppress(email)
            except Exception:
                pass
        if (m["settings"].get("reply_to") or "") != new.reply_to:
            await c["ws"].main().account_changed(mailbox_id)       # the outreach campaign sends with the new Reply-To
        try:   # campaigns on this account share the new limit at once
            service.set_ledger_limit(c["ws"].ledger(), {**m, "settings": new.model_dump()},
                                     service.open_creds(settings.secret_key(), m))
        except Exception:
            pass
        ok, why = new.ready_to_send()
        msg = "Settings saved." + ("" if ok else f" {why}")
        return _redirect(f"/mail/accounts/{mailbox_id}", msg=msg)

    @app.post("/mail/accounts/{mailbox_id}/check")
    async def mail_account_check(mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        status = await service.check_mailbox(c["db"], settings.secret_key(), m, settings.worker_env())
        m = await c["db"].mailbox(mailbox_id)
        return _redirect(f"/mail/accounts/{mailbox_id}", msg=m["status_detail"] if status == "ok" else "",
                         error="" if status == "ok" else m["status_detail"])

    @app.post("/mail/accounts/{mailbox_id}/remove")
    async def mail_account_remove(mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        await c["db"].remove_mailbox(mailbox_id)
        await c["ws"].main().account_changed(mailbox_id, "removed")
        from bb.mail import runs as runs_mod
        from bb.mail.jobs import Jobs
        await runs_mod.reconcile(c["db"], Jobs(c["db"]))      # runs that were working on it finish or move on
        await c["db"].log("info", f"Removed {m['address']}.")
        return _redirect("/mail/accounts", msg=f"Removed {m['address']}.")

    @app.post("/mail/accounts/{mailbox_id}/enable")
    async def mail_account_enable(mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        await c["db"].update_mailbox(mailbox_id, enabled=0 if m["enabled"] else 1)
        touched = await c["ws"].main().account_changed(mailbox_id, "disabled" if m["enabled"] else "reconnected")
        note = (f" The outreach campaign on it ({', '.join(touched)}) was stopped." if touched and m["enabled"] else "")
        return _redirect(f"/mail/accounts/{mailbox_id}", msg=("Disabled." if m["enabled"] else "Enabled.") + note)

    # ── Gmail OAuth for mailboxes ──

    @app.post("/mail/gmail/connect")
    async def mail_gmail_connect(request: Request, user: dict = Depends(current_user)):
        if not (settings.gmail_client_id and settings.gmail_client_secret):
            return _redirect("/mail/accounts", error="The site has no Google OAuth client yet; use the password "
                                                      "sign-in (limited mode) for now.")
        form = dict(await request.form())
        nonce = secrets.token_urlsafe(16)
        state = gmail_oauth.sign_state(settings.secret_key(), user["id"], nonce)
        url = gmail_oauth.auth_url(settings, state, login_hint=str(form.get("email", "") or ""))
        url = url.replace(gmail_oauth.redirect_uri(settings), settings.base_url + "/mail/gmail/callback")
        response = _redirect(url)
        response.set_cookie(GMAIL_STATE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
                            secure=settings.base_url.startswith("https://"), path="/")
        return response

    @app.get("/mail/gmail/callback")
    async def mail_gmail_callback(request: Request, user: dict = Depends(current_user),
                                  code: str = "", state: str = "", error: str = ""):
        nonce = request.cookies.get(GMAIL_STATE_COOKIE, "")
        if error:
            return _redirect("/mail/accounts", error=f"Google said: {error}")
        if not gmail_oauth.verify_state(settings.secret_key(), user["id"], nonce, state):
            return _redirect("/mail/accounts", error="The sign-in did not match this session. Try again.")
        c = await ctx(user)
        try:
            data = await gmail_oauth.exchange_code(settings, code, redirect_uri=settings.base_url + "/mail/gmail/callback")
            email = await gmail_oauth.account_email(data["access_token"])
            mid, detail = await service.connect_gmail(c["db"], settings.secret_key(), address=email,
                                                      refresh_token=data["refresh_token"], env=settings.worker_env())
        except (AuthError, TransientError, KeyError, RuntimeError) as e:
            return _redirect("/mail/accounts", error=f"Google sign-in failed: {e}")
        await c["ws"].main().account_changed(mid)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        response = _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} through Google. {detail}")
        response.delete_cookie(GMAIL_STATE_COOKIE, path="/")
        return response

    # ── Microsoft sign-in for mailboxes (Outlook, Hotmail, Microsoft 365) ──

    @app.post("/mail/microsoft/connect")
    async def mail_microsoft_connect(request: Request, user: dict = Depends(current_user)):
        if not settings.ms_client_id:
            return _redirect("/mail/accounts", error="The site has no Microsoft app registration yet; use the "
                                                      "Microsoft 365 admin app or the password sign-in for now.")
        form = dict(await request.form())
        nonce = secrets.token_urlsafe(16)
        state = microsoft_oauth.sign_state(settings.secret_key(), user["id"], nonce)
        redirect = settings.base_url + "/mail/microsoft/callback"
        url = microsoft_oauth.auth_url(settings, state, login_hint=str(form.get("email", "") or ""), redirect=redirect)
        response = _redirect(url)
        response.set_cookie(MS_STATE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
                            secure=settings.base_url.startswith("https://"), path="/")
        return response

    @app.get("/mail/microsoft/callback")
    async def mail_microsoft_callback(request: Request, user: dict = Depends(current_user),
                                      code: str = "", state: str = "", error: str = "", error_description: str = ""):
        nonce = request.cookies.get(MS_STATE_COOKIE, "")
        if error:
            return _redirect("/mail/accounts", error=f"Microsoft said: {error_description or error}")
        if not microsoft_oauth.verify_state(settings.secret_key(), user["id"], nonce, state):
            return _redirect("/mail/accounts", error="The sign-in did not match this session. Try again.")
        c = await ctx(user)
        redirect = settings.base_url + "/mail/microsoft/callback"
        try:
            data = await microsoft_oauth.exchange_code(settings, code, redirect=redirect)
            email = await microsoft_oauth.account_email(data["access_token"])
            mid, detail = await service.connect_microsoft(c["db"], settings.secret_key(), address=email,
                                                          refresh_token=data["refresh_token"], env=settings.worker_env())
        except (AuthError, TransientError, KeyError, RuntimeError, ValueError) as e:
            return _redirect("/mail/accounts", error=f"Microsoft sign-in failed: {e}")
        await c["ws"].main().account_changed(mid)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        response = _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} through Microsoft. {detail}")
        response.delete_cookie(MS_STATE_COOKIE, path="/")
        return response

    @app.post("/mail/accounts/{mailbox_id}/sync")
    async def mail_account_sync(mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        jobs = Jobs(c["db"])
        kind = "sync_incremental" if (m.get("sync") or {}).get("backfill_done") else "sync_backfill"
        await jobs.enqueue(kind, mailbox_id, dedupe=True, max_attempts=8)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect("/mail", msg=f"Sync queued for {m['address']}.")

    @app.post("/mail/accounts/{mailbox_id}/resync")
    async def mail_account_resync(mailbox_id: str, user: dict = Depends(current_user)):
        """Forget the sync position and read the mailbox again (messages
        already stored are kept; nothing is duplicated)."""
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        await c["db"].update_mailbox(mailbox_id, sync={})
        await Jobs(c["db"]).enqueue("sync_backfill", mailbox_id, dedupe=True, max_attempts=8)
        return _redirect(f"/mail/accounts/{mailbox_id}", msg="A full re-sync is queued.")

    # ── threads ──

    @app.get("/mail/threads", response_class=HTMLResponse)
    async def mail_threads(request: Request, user: dict = Depends(current_user), state: str = "needs_reply",
                           mailbox: str = "", q: str = "", page: int = 1):
        c = await ctx(user)
        boxes = {m["id"]: m for m in c["selected"]}
        if mailbox and mailbox in {m["id"] for m in c["mailboxes"]}:
            boxes = {mailbox: await c["db"].mailbox(mailbox)}
            boxes[mailbox]["settings_model"] = service.settings_of(boxes[mailbox])
        where, params = ["mailbox_id IN (%s)" % (",".join("?" for _ in boxes) or "''")], list(boxes)
        if state and state != "all":
            where.append("state = ?"); params.append(state)
        if q:
            like = f"%{q.strip()}%"
            where.append("(subject LIKE ? OR last_from LIKE ? OR snippet LIKE ? OR participants_json LIKE ?)")
            params += [like, like, like, like]
        page = max(1, page)
        rows = await c["db"].query(
            f"SELECT * FROM threads WHERE {' AND '.join(where)} ORDER BY last_message_at DESC LIMIT 51 OFFSET ?",
            (*params, (page - 1) * 50))
        more = len(rows) > 50
        rows = rows[:50]
        for r in rows:
            r["box"] = boxes.get(r["mailbox_id"])
        state_counts = {}
        for r in await c["db"].query(
                f"SELECT state, COUNT(*) AS n FROM threads WHERE mailbox_id IN ({','.join('?' for _ in boxes) or "''"}) GROUP BY state",
                list(boxes)):
            state_counts[r["state"]] = r["n"]
        return await render(request, "mail_threads.html", user, **page_ctx(c), threads=rows, state=state,
                            mailbox=mailbox, q=q, page=page, more=more, state_counts=state_counts)

    @app.get("/mail/threads/{thread_id}", response_class=HTMLResponse)
    async def mail_thread(request: Request, thread_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        t = await c["db"].one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not t:
            raise HTTPException(404)
        box = await c["db"].mailbox(t["mailbox_id"])
        if box:
            box["settings_model"] = service.settings_of(box)
        messages = await c["db"].query(
            "SELECT id, direction, sent_by, from_email, from_name, to_json, cc_json, subject, body, snippet, sent_at, automated, folder "
            "FROM messages WHERE thread_id = ? ORDER BY sent_at, created_at", (thread_id,))
        for m in messages:
            m["to"] = json.loads(m.get("to_json") or "[]")
        try:
            t["participants"] = json.loads(t.get("participants_json") or "[]")
        except ValueError:
            t["participants"] = []
        draft_id = request.query_params.get("draft", "")
        draft = await _draft_row(c["db"], draft_id) if draft_id else None
        if draft is None:
            draft = await c["db"].one("SELECT * FROM drafts WHERE thread_id = ? AND status = 'pending_review' ORDER BY created_at DESC LIMIT 1", (thread_id,))
            if draft:
                draft["to"] = json.loads(draft.get("to_json") or "[]"); draft["qa"] = json.loads(draft.get("qa_json") or "{}")
        in_flight = await c["db"].query("SELECT * FROM drafts WHERE thread_id = ? AND status IN ('approved', 'failed') ORDER BY updated_at DESC LIMIT 3", (thread_id,))
        box_ready = service.ready_to_send(box) if box else (False, "")
        draft_preview = ""
        if draft:
            await _for_editor(c, draft)
            draft_preview = await _preview_doc(c, draft, draft.get("body") or "")
        return await render(request, "mail_thread.html", user, **page_ctx(c), thread=t, box=box, messages=messages,
                            draft=draft, in_flight=in_flight, box_ready=box_ready, draft_preview=draft_preview)

    @app.post("/mail/threads/{thread_id}/snooze")
    @app.post("/mail/threads/{thread_id}/dismiss")
    @app.post("/mail/threads/{thread_id}/reopen")
    async def mail_thread_action(request: Request, thread_id: str, user: dict = Depends(current_user)):
        action = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        c = await ctx(user)
        t = await c["db"].one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not t:
            raise HTTPException(404)
        from bb.mail.ingest import Ingest
        box = await c["db"].mailbox(t["mailbox_id"])
        if action in ("snooze", "dismiss"):
            await c["db"].execute("UPDATE threads SET state = ?, state_reason = 'set by hand', updated_at = ? WHERE id = ?",
                                  ("snoozed" if action == "snooze" else "dismissed", service.now_iso(), thread_id))
        elif action == "reopen":
            await c["db"].execute("UPDATE threads SET state = 'unknown', updated_at = ? WHERE id = ?",
                                  (service.now_iso(), thread_id))
            await Ingest(c["db"], box).refresh_thread(thread_id)
        else:
            raise HTTPException(404)
        await Ingest(c["db"], box).refresh_counts()
        return _redirect(f"/mail/threads/{thread_id}", msg={"snooze": "Snoozed.", "dismiss": "Dismissed.", "reopen": "Back in the queue."}[action])

    # ── drafts: the composer, the review tray, compose ──

    def _brain(ws):
        from bb.mail.brain import MailBrain
        return MailBrain(ws.mail_brain_env())

    async def _for_editor(c, draft: dict):
        """The draft's text as the editor shows it: every link on its words."""
        from bb.mail.sending import editor_body
        try:
            draft["body_md"], draft["default_link"] = await editor_body(c["db"], {"HARVEY_HOME": str(c["ws"].user_root)}, draft)
        except Exception as e:
            logger.debug("editor body failed: %s", e)
            draft["body_md"], draft["default_link"] = draft.get("body") or "", ""

    async def _preview_doc(c, draft: dict, body: str) -> str:
        """The draft as it would go out (links on their words), as a page for the preview frame."""
        from bb.mail.sending import preview_document, preview_html
        try:
            return preview_document(await preview_html(c["db"], {"HARVEY_HOME": str(c["ws"].user_root)}, draft, body))
        except Exception as e:
            logger.debug("preview failed: %s", e)
            return ""

    @app.post("/mail/drafts/{draft_id}/preview")
    async def mail_draft_preview(request: Request, draft_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        d = await _draft_row(c["db"], draft_id)
        if not d:
            raise HTTPException(404)
        form = dict(await request.form())
        return JSONResponse({"doc": await _preview_doc(c, d, str(form.get("body", "") or ""))})

    async def _draft_row(db, draft_id: str) -> dict | None:
        d = await db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        if d:
            d["to"] = json.loads(d.get("to_json") or "[]")
            d["qa"] = json.loads(d.get("qa_json") or "{}")
        return d

    @app.post("/mail/threads/{thread_id}/draft")
    async def mail_thread_draft(request: Request, thread_id: str, user: dict = Depends(current_user)):
        """Ask the model for a reply to this thread; the draft lands in the
        composer on the thread page for editing before anything is sent."""
        from bb.mail.brain import BrainError
        from bb.mail.drafting import draft_reply
        c = await ctx(user)
        db = c["db"]
        t = await db.one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not t:
            raise HTTPException(404)
        box = await db.mailbox(t["mailbox_id"])
        form = dict(await request.form())
        messages = await db.query("SELECT * FROM messages WHERE thread_id = ? ORDER BY sent_at, created_at", (thread_id,))
        them = next((m for m in reversed(messages) if m["direction"] == "inbound"), None)
        if not them:
            return _redirect(f"/mail/threads/{thread_id}", error="There is no message from them to answer in this thread.")
        try:
            result = await draft_reply(_brain(c["ws"]), service.settings_of(box), box, t, messages,
                                       instructions=str(form.get("instructions", "") or ""),
                                       scope=_scope_for(c, box["address"]))
        except BrainError as e:
            return _redirect(f"/mail/threads/{thread_id}", error=str(e))
        subject = t["subject"] if t["subject"].lower().startswith("re:") else f"Re: {t['subject']}"
        refs = (them.get("references_text") or "")
        draft_id = service.new_id()
        now = service.now_iso()
        await db.execute(
            "INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, reply_to, subject, body, in_reply_to, references_text, "
            "status, qa_json, confidence, created_at, updated_at) VALUES (?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)",
            (draft_id, box["id"], thread_id, json.dumps([{"name": them["from_name"], "email": them["from_email"]}]),
             service.settings_of(box).reply_to, subject[:200], result["body"], them.get("message_id") or "", refs,
             json.dumps({**result["qa"], "attempts": result["attempts"], "confidence_reason": result["confidence_reason"]}),
             result["confidence"], now, now))
        return _redirect(f"/mail/threads/{thread_id}?draft={draft_id}", msg="Draft ready. Edit it, then send.")

    @app.post("/mail/drafts/{draft_id}/send")
    async def mail_draft_send(request: Request, draft_id: str, user: dict = Depends(current_user)):
        """Save the edited draft, approve it and queue the send (the worker
        sends within seconds; a dry run records instead)."""
        c = await ctx(user)
        db = c["db"]
        d = await _draft_row(db, draft_id)
        if not d:
            raise HTTPException(404)
        if d["status"] in ("sent", "dry_run"):
            return _redirect(f"/mail/threads/{d['thread_id']}" if d["thread_id"] else "/mail/review",
                             error="That draft was already sent.")
        form = dict(await request.form())
        from harvey.integrations.mail_provider import normalize_newlines
        body = normalize_newlines(str(form.get("body", "") or d["body"])).strip()
        subject = str(form.get("subject", "") or d["subject"]).strip()[:200]
        reply_to = str(form.get("reply_to", "") or "").strip().lower()
        box = await db.mailbox(d["mailbox_id"])
        ok, why = service.ready_to_send(box) if box else (False, "mailbox missing")
        if not ok:
            return _redirect(f"/mail/threads/{d['thread_id']}?draft={draft_id}" if d["thread_id"] else "/mail/review", error=why)
        if not body:
            return _redirect(f"/mail/threads/{d['thread_id']}?draft={draft_id}", error="The reply is empty.")
        dry = bool(form.get("dry_run"))
        await db.execute("UPDATE drafts SET body = ?, subject = ?, reply_to = ?, status = 'approved', error = '', updated_at = ? WHERE id = ?",
                         (body, subject, reply_to, service.now_iso(), draft_id))
        await Jobs(db).enqueue("send_draft", d["mailbox_id"], {"draft_id": draft_id, "dry_run": dry,
                                                               "saw_thread": bool(form.get("from_thread"))}, max_attempts=4)
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        back = f"/mail/threads/{d['thread_id']}" if d["thread_id"] else "/mail/review"
        return _redirect(back, msg="Dry run queued; nothing will be sent." if dry else "Sending…")

    @app.post("/mail/drafts/{draft_id}/reject")
    async def mail_draft_reject(draft_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        d = await _draft_row(c["db"], draft_id)
        if not d:
            raise HTTPException(404)
        await c["db"].execute("UPDATE drafts SET status = 'rejected', updated_at = ? WHERE id = ? AND status != 'sent'",
                              (service.now_iso(), draft_id))
        return _redirect(f"/mail/threads/{d['thread_id']}" if d["thread_id"] else "/mail/review", msg="Draft discarded.")

    @app.get("/mail/review", response_class=HTMLResponse)
    async def mail_review(request: Request, user: dict = Depends(current_user)):
        c = await ctx(user)
        ids = [m["id"] for m in c["selected"]]
        marks = ",".join("?" for _ in ids) or "''"
        rows = await c["db"].query(
            f"SELECT d.*, t.subject AS thread_subject, t.last_from FROM drafts d LEFT JOIN threads t ON t.id = d.thread_id "
            f"WHERE d.mailbox_id IN ({marks}) AND d.status = 'pending_review' ORDER BY d.created_at DESC", ids)
        boxes = {m["id"]: m for m in c["mailboxes"]}
        for r in rows:
            r["to"] = json.loads(r.get("to_json") or "[]")
            r["qa"] = json.loads(r.get("qa_json") or "{}")
            r["box"] = boxes.get(r["mailbox_id"])
            await _for_editor(c, r)
            r["preview"] = await _preview_doc(c, r, r.get("body") or "")
        recent = await c["db"].query(
            f"SELECT d.*, t.subject AS thread_subject FROM drafts d LEFT JOIN threads t ON t.id = d.thread_id "
            f"WHERE d.mailbox_id IN ({marks}) AND d.status IN ('sent', 'dry_run', 'approved', 'failed') ORDER BY d.updated_at DESC LIMIT 30", ids)
        for r in recent:
            r["to"] = json.loads(r.get("to_json") or "[]"); r["box"] = boxes.get(r["mailbox_id"])
        return await render(request, "mail_review.html", user, **page_ctx(c), drafts=rows, recent=recent)

    @app.post("/mail/review/approve-all")
    async def mail_review_approve_all(user: dict = Depends(current_user)):
        c = await ctx(user)
        ids = [m["id"] for m in c["selected"] if m["ready"]]
        marks = ",".join("?" for _ in ids) or "''"
        from bb.mail import runs as runs_mod
        await runs_mod.supersede_stale_followups(c["db"])     # a follow-up they wrote back to since is not sent blind
        rows = await c["db"].query(f"SELECT id, mailbox_id FROM drafts WHERE mailbox_id IN ({marks}) AND status = 'pending_review'", ids)
        jobs = Jobs(c["db"])
        for r in rows:
            await c["db"].execute("UPDATE drafts SET status = 'approved', error = '', updated_at = ? WHERE id = ?", (service.now_iso(), r["id"]))
            await jobs.enqueue("send_draft", r["mailbox_id"], {"draft_id": r["id"]}, max_attempts=4)
        if rows and not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect("/mail/review", msg=f"Approved {len(rows)}; sending.")

    @app.get("/mail/compose", response_class=HTMLResponse)
    async def mail_compose(request: Request, user: dict = Depends(current_user), draft: str = ""):
        c = await ctx(user)
        d = await _draft_row(c["db"], draft) if draft else None
        preview = ""
        if d:
            await _for_editor(c, d)
            preview = await _preview_doc(c, d, d.get("body") or "")
        return await render(request, "mail_compose.html", user, **page_ctx(c), draft=d, draft_preview=preview,
                            ready=[m for m in c["mailboxes"] if m["enabled"]])

    @app.post("/mail/compose")
    async def mail_compose_draft(request: Request, user: dict = Depends(current_user)):
        from bb.mail.brain import BrainError
        from bb.mail.drafting import draft_new
        c = await ctx(user)
        form = dict(await request.form())
        box = await c["db"].mailbox(str(form.get("mailbox", "")))
        to_email = str(form.get("to", "") or "").strip().lower()
        to_name = str(form.get("to_name", "") or "").strip()
        instruction = str(form.get("instruction", "") or "").strip()
        if not box or "@" not in to_email or len(instruction) < 5:
            return _redirect("/mail/compose", error="Pick a mailbox, enter the recipient, and say what the email should do.")
        try:
            result = await draft_new(_brain(c["ws"]), service.settings_of(box), box, to_name, to_email, instruction,
                                     scope=_scope_for(c, box["address"]))
        except BrainError as e:
            return _redirect("/mail/compose", error=str(e))
        draft_id = service.new_id(); now = service.now_iso()
        await c["db"].execute(
            "INSERT INTO drafts(id, mailbox_id, kind, to_json, reply_to, subject, body, status, qa_json, confidence, created_at, updated_at) "
            "VALUES (?, ?, 'new', ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)",
            (draft_id, box["id"], json.dumps([{"name": to_name, "email": to_email}]), service.settings_of(box).reply_to,
             result["subject"], result["body"], json.dumps({**result["qa"], "attempts": result["attempts"],
                                                          "confidence_reason": result["confidence_reason"]}),
             result["confidence"], now, now))
        return _redirect(f"/mail/compose?draft={draft_id}", msg="Draft ready. Edit it, then send.")

    # ── reply runs: pick the unreplied threads, draft them all, route by mode ──

    @app.get("/mail/runs", response_class=HTMLResponse)
    async def mail_runs(request: Request, user: dict = Depends(current_user), mailbox: str = ""):
        from bb.mail import runs as runs_mod
        c = await ctx(user)
        boxes = c["selected"]
        if mailbox and mailbox in {m["id"] for m in c["mailboxes"]}:
            boxes = [m for m in c["mailboxes"] if m["id"] == mailbox]
        cands = await runs_mod.candidates(c["db"], boxes)
        groups = []
        for m in boxes:
            mine = [t for t in cands if t["mailbox_id"] == m["id"]]
            groups.append({"box": m, "threads": mine, "excluded": sum(1 for t in mine if t["excluded"])})
        recent = await c["db"].query("SELECT * FROM reply_runs ORDER BY created_at DESC LIMIT 20")
        names = {m["id"]: m for m in c["mailboxes"]}
        for r in recent:
            r["summary"] = json.loads(r.get("summary_json") or "{}")
            r["boxes"] = [names[x] for x in json.loads(r.get("mailbox_ids_json") or "[]") if x in names]
        brain = _brain(c["ws"])
        try:
            months = int(request.query_params.get("months", runs_mod.RECONNECT_DEFAULT_MONTHS))
        except ValueError:
            months = runs_mod.RECONNECT_DEFAULT_MONTHS
        if months not in runs_mod.RECONNECT_MONTHS:
            months = runs_mod.RECONNECT_DEFAULT_MONTHS
        reconnect = await runs_mod.reconnect_candidates(c["db"], boxes, months, all_mailboxes=c["mailboxes"])
        backfill_days = min([int((m.get("settings") or {}).get("backfill_days", 365) or 0) or 10 ** 6 for m in boxes] or [10 ** 6])
        return await render(request, "mail_runs.html", user, **page_ctx(c), groups=groups, recent=recent,
                            total=sum(len(g["threads"]) for g in groups), mailbox=mailbox,
                            brain_ok=brain.configured, brain_label=brain.label,
                            reconnect=reconnect[:200], reconnect_total=len(reconnect), reconnect_months=months,
                            reconnect_choices=runs_mod.RECONNECT_MONTHS,
                            reconnect_window_short=backfill_days < 30 * months)

    @app.post("/mail/runs")
    async def mail_runs_create(request: Request, user: dict = Depends(current_user)):
        from bb.mail import runs as runs_mod
        c = await ctx(user)
        form = await request.form()
        thread_ids = [str(x) for x in form.getlist("thread")]
        mode = str(form.get("mode", "review") or "review")
        if mode not in runs_mod.MODES:
            mode = "review"
        reply_to = str(form.get("reply_to", "") or "").strip().lower()
        if reply_to and ("@" not in reply_to or " " in reply_to):
            return _redirect("/mail/runs", error="Reply-To must be an email address.")
        if not thread_ids:
            return _redirect("/mail/runs", error="Tick at least one thread.")
        if not _brain(c["ws"]).configured:
            return _redirect("/mail/runs", error="Set up an AI provider first; drafting needs a model.")
        boxes = list(c["mailboxes"])
        if mode == "auto":
            not_ready = [m["address"] for m in boxes if not m["ready"]]
            boxes = [m for m in boxes if m["ready"]]
            if not boxes:
                return _redirect("/mail/runs", error="None of the accounts can send yet: " + "; ".join(not_ready))
        run = await runs_mod.create_run(c["db"], boxes, thread_ids, mode=mode, reply_to=reply_to,
                                        instructions=str(form.get("instructions", "") or ""), created_by=user.get("email", ""))
        if not run:
            return _redirect("/mail/runs", error="None of those threads still needs a reply (or they are already in a run "
                                                 "that has not finished).")
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/runs/{run['id']}", msg=f"Run started: {run['total']} threads across {len(run['per_mailbox'])} account(s).")

    @app.post("/mail/runs/reconnect")
    async def mail_runs_reconnect(request: Request, user: dict = Depends(current_user)):
        from bb.mail import runs as runs_mod
        c = await ctx(user)
        form = await request.form()
        mode = str(form.get("mode", "review") or "review")
        if mode not in runs_mod.MODES:
            mode = "review"
        try:
            months = int(form.get("months", runs_mod.RECONNECT_DEFAULT_MONTHS))
        except (TypeError, ValueError):
            months = runs_mod.RECONNECT_DEFAULT_MONTHS
        if not _brain(c["ws"]).configured:
            return _redirect("/mail/runs", error="Set up an AI provider first; drafting needs a model.")
        boxes = list(c["selected"])
        if mode == "auto":
            not_ready = [m["address"] for m in boxes if not m["ready"]]
            boxes = [m for m in boxes if m["ready"]]
            if not boxes:
                return _redirect("/mail/runs", error="None of the accounts can send yet: " + "; ".join(not_ready))
        results = await runs_mod.create_reconnect_runs(c["db"], boxes, months=months, mode=mode,
                                                       instructions=str(form.get("instructions", "") or ""),
                                                       created_by=user.get("email", ""), all_mailboxes=c["mailboxes"])
        started = [r for r in results if r.get("id")]
        if len(results) > 1:
            if started and not c["ws"].mail_worker_running():
                c["ws"].start_mail_worker()
            by_id = {m["id"]: m for m in boxes}
            notes = [(f"{r['address']}: a scan is already going" if r.get("busy")
                      else runs_mod.nobody_note(by_id.get(r["mailbox_id"]) or {"address": r["address"]}, months))
                     for r in results if not r.get("id")]
            return _redirect("/campaigns", msg=(f"Started {len(started)} follow-up run{'s' if len(started) != 1 else ''}, one per account: "
                                                + ", ".join(r["address"] for r in started) + ".") if started else "",
                             error="; ".join(notes))
        run = results[0] if results and (results[0].get("id") or results[0].get("busy")) else {}
        if run.get("busy"):
            busy = await c["db"].one("SELECT status FROM reply_runs WHERE id = ?", (run["busy"],))
            what = ("is paused: resume it or cancel it" if (busy or {}).get("status") == "paused"
                    else "is still going: it has to finish (or be cancelled)")
            return _redirect(f"/mail/runs/{run['busy']}", error=f"A follow-up run over these accounts {what} before another starts.")
        if not run and boxes and all(m.get("status") == "reconnect" for m in boxes):
            return _redirect("/mail/runs", error="These accounts cannot read their mail until they are signed in again "
                                                 "(Reconnect on the Accounts page).")
        if not run and boxes and all(runs_mod.still_syncing(m) for m in boxes):
            return _redirect("/mail/runs", error="These accounts are still reading their past mail (the first sync). "
                                                 "Start the scan again once they show as synced.")
        if not run:
            return _redirect("/mail/runs", error=f"No past conversations from the last {months} months to follow up with "
                                                 "(two-way threads with people who are not colleagues, automated senders or already followed up).")
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/runs/{run['id']}", msg=f"Follow-up run started: {run['total']} past conversation(s) across "
                                                       f"{len(run['per_mailbox'])} account(s).")

    async def _run_page_data(c, run_id):
        from bb.mail import runs as runs_mod
        run = await c["db"].one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))
        if not run:
            raise HTTPException(404)
        run["summary"] = await runs_mod.run_summary(c["db"], run_id)
        run["done"] = sum(v for k, v in run["summary"].items() if k not in ("queued", "drafting"))
        items = await c["db"].query(
            "SELECT ri.*, t.subject, t.last_from, t.last_message_at, d.confidence, d.status AS draft_status "
            "FROM run_items ri JOIN threads t ON t.id = ri.thread_id LEFT JOIN drafts d ON d.id = ri.draft_id "
            "WHERE ri.run_id = ? ORDER BY ri.mailbox_id, t.last_message_at", (run_id,))
        names = {m["id"]: m for m in c["mailboxes"]}
        groups = []
        for mid in json.loads(run.get("mailbox_ids_json") or "[]"):
            groups.append({"box": names.get(mid, {"id": mid, "address": "(removed account)", "settings_model": MailboxSettings()}),
                           "rows": [i for i in items if i["mailbox_id"] == mid]})
        return run, groups, items

    @app.get("/mail/runs/{run_id}", response_class=HTMLResponse)
    async def mail_run(request: Request, run_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        run, groups, items = await _run_page_data(c, run_id)
        return await render(request, "mail_run.html", user, **page_ctx(c), run=run, groups=groups)

    @app.get("/mail/runs/{run_id}/items.json")
    async def mail_run_items(run_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        run, groups, items = await _run_page_data(c, run_id)
        return {"status": run["status"], "done": run["done"], "total": run["total"], "summary": run["summary"],
                "items": [{"id": i["id"], "status": i["status"], "detail": i["detail"], "draft_id": i["draft_id"],
                           "confidence": i.get("confidence") or ""} for i in items]}

    @app.post("/mail/runs/{run_id}/cancel")
    async def mail_run_cancel(run_id: str, user: dict = Depends(current_user)):
        from bb.mail import runs as runs_mod
        c = await ctx(user)
        ok = await runs_mod.cancel_run(c["db"], run_id, {"HARVEY_HOME": str(c["ws"].user_root)})
        return _redirect(f"/mail/runs/{run_id}", msg="Cancelled; threads not yet drafted are skipped." if ok else "",
                         error="" if ok else "That run is already finished.")

    @app.post("/mail/runs/{run_id}/pause")
    async def mail_run_pause(run_id: str, user: dict = Depends(current_user)):
        from bb.mail import runs as runs_mod
        c = await ctx(user)
        ok = await runs_mod.pause_run(c["db"], run_id, {"HARVEY_HOME": str(c["ws"].user_root)})
        return _redirect(f"/mail/runs/{run_id}", msg="Paused; nothing more is drafted or sent until you resume it." if ok else "",
                         error="" if ok else "Only a run that is going can be paused.")

    @app.post("/mail/runs/{run_id}/resume")
    async def mail_run_resume(run_id: str, user: dict = Depends(current_user)):
        from bb.mail import runs as runs_mod
        c = await ctx(user)
        ok = await runs_mod.resume_run(c["db"], run_id)
        if ok and not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/runs/{run_id}", msg="Resumed." if ok else "", error="" if ok else "Only a paused run can be resumed.")

    # ── audit log ──

    @app.get("/mail/audit", response_class=HTMLResponse)
    async def mail_audit(request: Request, user: dict = Depends(current_user), kind: str = "", mailbox: str = "",
                         page: int = 1):
        c = await ctx(user)
        ids = [m["id"] for m in c["selected"]]
        if mailbox and mailbox in {m["id"] for m in c["mailboxes"]}:
            ids = [mailbox]
        marks = ",".join("?" for _ in ids) or "''"
        where, params = [f"(mailbox_id = '' OR mailbox_id IN ({marks}))"], list(ids)
        if kind:
            where.append("kind = ?"); params.append(kind)
        page = max(1, page)
        rows = await c["db"].query(f"SELECT * FROM activity WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 101 OFFSET ?",
                                   (*params, (page - 1) * 100))
        more = len(rows) > 100
        rows = rows[:100]
        names = {m["id"]: m for m in c["mailboxes"]}
        for r in rows:
            r["meta"] = json.loads(r.pop("meta_json", "{}") or "{}")
            r["box"] = names.get(r["mailbox_id"])
        sends = await c["db"].query(
            f"SELECT d.id, d.mailbox_id, d.kind, d.to_json, d.subject, d.status, d.confidence, d.run_id, d.thread_id, d.sent_at, d.updated_at, d.error "
            f"FROM drafts d WHERE d.mailbox_id IN ({marks}) AND d.status IN ('sent', 'dry_run', 'failed', 'rejected') "
            f"ORDER BY d.updated_at DESC LIMIT 100", ids)
        for d in sends:
            d["to"] = json.loads(d.get("to_json") or "[]"); d["box"] = names.get(d["mailbox_id"])
        kinds = [r["kind"] for r in await c["db"].query("SELECT DISTINCT kind FROM activity ORDER BY kind")]
        return await render(request, "mail_audit.html", user, **page_ctx(c), rows=rows, sends=sends, kind=kind,
                            kinds=kinds, mailbox=mailbox, page=page, more=more)

    # ── jobs (dead-letter page) ──

    @app.get("/mail/jobs", response_class=HTMLResponse)
    async def mail_jobs(request: Request, user: dict = Depends(current_user), status: str = ""):
        c = await ctx(user)
        jobs = Jobs(c["db"])
        if status not in ("", "queued", "running", "done", "dead", "cancelled"):
            status = ""
        rows = await jobs.list(status or None, limit=200)
        names = {m["id"]: m["address"] for m in c["mailboxes"]}
        for r in rows:
            r["mailbox"] = names.get(r["mailbox_id"], "")
        return await render(request, "mail_jobs.html", user, **page_ctx(c), jobs=rows, status=status,
                            counts=await jobs.counts())

    @app.post("/mail/jobs/retry-all")
    async def mail_jobs_retry_all(user: dict = Depends(current_user)):
        c = await ctx(user)
        jobs = Jobs(c["db"])
        n = 0
        for j in await jobs.list("dead", limit=500):
            n += 1 if await jobs.retry(j["id"]) else 0
        if n and not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect("/mail/jobs", msg=f"Queued {n} failed job{'s' if n != 1 else ''} again.")

    @app.post("/mail/jobs/{job_id}/{action}")
    async def mail_job_action(job_id: str, action: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        jobs = Jobs(c["db"])
        if action == "retry":
            ok = await jobs.retry(job_id)
        elif action == "discard":
            ok = await jobs.discard(job_id)
        else:
            raise HTTPException(404)
        return _redirect("/mail/jobs", msg=("Queued again." if action == "retry" else "Discarded.") if ok else "",
                         error="" if ok else "That job cannot be changed in its current state.")

    # ── live events (server-sent) ──

    @app.get("/mail/events")
    async def mail_events(request: Request, user: dict = Depends(current_user), after: int = 0):
        ws = workspace_for(user)
        db = ws.mail_db()
        await db.init()

        async def stream():
            last = after
            ticks = 0
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                ids = await service.scope(db)
                rows = await db.activity(limit=20, after_id=last, mailbox_ids=ids)
                rows = [r for r in rows if not r["text"].startswith("Background worker ")]
                for row in reversed(rows):
                    last = max(last, row["id"])
                    yield f"event: activity\ndata: {json.dumps(row)}\n\n"
                running = await db.query(
                    "SELECT id, kind, mailbox_id, progress_json, status FROM jobs WHERE status = 'running'")
                boxes = [{"id": m["id"], "address": m["address"], "status": m["status"],
                          "detail": m["status_detail"], "counts": m["counts"]} for m in await db.mailboxes()]
                active = await db.query("SELECT id, status, total, done, mode FROM reply_runs WHERE status IN ('queued', 'running') "
                                        "ORDER BY created_at DESC LIMIT 10")
                yield "event: state\ndata: " + json.dumps({
                    "jobs": [{**r, "progress": json.loads(r.get("progress_json") or "{}")} for r in running],
                    "mailboxes": boxes, "worker": ws.mail_worker_alive(), "runs": active}) + "\n\n"
                ticks += 1
                if ticks % 10 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
