"""The pages under /mail: overview, accounts, per-mailbox settings, jobs,
the scope selector, Gmail OAuth for mailboxes, and the live event stream."""

import asyncio
import json
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import ValidationError

from bb import gmail_oauth
from bb.mail import service
from bb.mail.jobs import Jobs
from bb.mail.models import COLORS, MailboxSettings
from bb.mail.providers import AuthError, TransientError
from bb.mail_autoconfig import guess_settings

GMAIL_STATE_COOKIE = "bb_mail_gmail_state"


def register_mail_routes(app, settings, current_user, workspace_for, render, _redirect, templates):

    async def ctx(user: dict) -> dict:
        """Everything the mail pages share: the user's mailboxes, the scope,
        worker state. Also runs the one-time migration and revives a dead
        worker."""
        ws = workspace_for(user)
        db = ws.mail_db()
        await db.init()
        await service.migrate_from_workspace(db, settings.secret_key(), ws)
        revived = ws.revive_mail_worker()
        boxes = await db.mailboxes()
        for m in boxes:
            m["settings_model"] = service.settings_of(m)
            m["ready"], m["ready_why"] = service.ready_to_send(m)
        ids = await service.scope(db)
        selected = boxes if ids is None else [m for m in boxes if m["id"] in ids]
        return {"ws": ws, "db": db, "mailboxes": boxes, "scope_ids": ids, "selected": selected,
                "mail_worker": ws.mail_worker_alive(), "mail_worker_pid": ws.mail_worker_running(),
                "revived": revived, "colors": COLORS}

    def page_ctx(c: dict) -> dict:
        return {k: v for k, v in c.items() if k not in ("ws", "db")}

    # ── overview ──

    @app.get("/mail", response_class=HTMLResponse)
    async def mail_overview(request: Request, user: dict = Depends(current_user)):
        c = await ctx(user)
        activity = await c["db"].activity(limit=40, mailbox_ids=c["scope_ids"])
        jobs = Jobs(c["db"])
        return await render(request, "mail_overview.html", user, **page_ctx(c), activity=activity,
                            job_counts=await jobs.counts(), msg=c["revived"] or request.query_params.get("msg", ""))

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
        else:
            raise HTTPException(404)
        return _redirect("/mail", msg=detail if ok else "", error="" if ok else detail)

    # ── accounts ──

    @app.get("/mail/accounts", response_class=HTMLResponse)
    async def mail_accounts(request: Request, user: dict = Depends(current_user)):
        c = await ctx(user)
        return await render(request, "mail_accounts.html", user, **page_ctx(c),
                            gmail_available=bool(settings.gmail_client_id and settings.gmail_client_secret),
                            redirect_uri=settings.base_url + "/mail/gmail/callback")

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
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        return _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} (limited mode). {detail} "
                                                   "Now add the signature and project link.")

    @app.get("/mail/accounts/{mailbox_id}", response_class=HTMLResponse)
    async def mail_account(request: Request, mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        m["settings_model"] = service.settings_of(m)
        m["ready"], m["ready_why"] = service.ready_to_send(m)
        return await render(request, "mail_account.html", user, **page_ctx(c), box=m,
                            creds=service.masked_creds(settings.secret_key(), m),
                            gmail_available=bool(settings.gmail_client_id and settings.gmail_client_secret))

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
                daily_cap=form.get("daily_cap", 50), excluded=form.get("excluded", ""),
                hours_start=form.get("hours_start", ""), hours_end=form.get("hours_end", ""),
                color=form.get("color", "blue"),
            )
        except ValidationError as e:
            first = e.errors()[0]
            return _redirect(f"/mail/accounts/{mailbox_id}", error=str(first["msg"]).replace("Value error, ", ""))
        await c["db"].update_mailbox(mailbox_id, settings=new.model_dump())
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
        await c["db"].log("info", f"Removed {m['address']}.")
        return _redirect("/mail/accounts", msg=f"Removed {m['address']}.")

    @app.post("/mail/accounts/{mailbox_id}/enable")
    async def mail_account_enable(mailbox_id: str, user: dict = Depends(current_user)):
        c = await ctx(user)
        m = await c["db"].mailbox(mailbox_id)
        if not m:
            raise HTTPException(404)
        await c["db"].update_mailbox(mailbox_id, enabled=0 if m["enabled"] else 1)
        return _redirect(f"/mail/accounts/{mailbox_id}", msg="Disabled." if m["enabled"] else "Enabled.")

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
        if not c["ws"].mail_worker_running():
            c["ws"].start_mail_worker()
        response = _redirect(f"/mail/accounts/{mid}", msg=f"Connected {email} through Google. {detail}")
        response.delete_cookie(GMAIL_STATE_COOKIE, path="/")
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
        return await render(request, "mail_thread.html", user, **page_ctx(c), thread=t, box=box, messages=messages)

    @app.post("/mail/threads/{thread_id}/{action}")
    async def mail_thread_action(thread_id: str, action: str, user: dict = Depends(current_user)):
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
                for row in reversed(rows):
                    last = max(last, row["id"])
                    yield f"event: activity\ndata: {json.dumps(row)}\n\n"
                running = await db.query(
                    "SELECT id, kind, mailbox_id, progress_json, status FROM jobs WHERE status = 'running'")
                boxes = [{"id": m["id"], "address": m["address"], "status": m["status"],
                          "detail": m["status_detail"], "counts": m["counts"]} for m in await db.mailboxes()]
                yield "event: state\ndata: " + json.dumps({
                    "jobs": [{**r, "progress": json.loads(r.get("progress_json") or "{}")} for r in running],
                    "mailboxes": boxes, "worker": ws.mail_worker_alive()}) + "\n\n"
                ticks += 1
                if ticks % 10 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
