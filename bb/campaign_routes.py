"""The Campaigns page: every outreach, reply and mailbox-scan campaign of a
user in one view, with its account, progress and its part of the
account's daily limit, start / pause / stop on each, and the form that
starts one campaign per chosen account in one go."""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from bb import campaigns as campaigns_mod
from bb import recipients as recipients_mod

logger = logging.getLogger("bb.campaigns")

FEATURE_LABELS = {"outreach": "Outreach", "reply": "Reply campaign", "scan": "Mailbox scan", "reply-run": "Reply run"}


def split_leads(rows: list[dict], campaigns: list, how: str) -> tuple[dict, int]:
    """{campaign id: rows}: by the upload's account column when asked (rows
    for an account not among them are left out and counted), else dealt
    out in turn so each account gets its own share and nobody is on two
    lists."""
    out = {ws.campaign_id: [] for ws in campaigns}
    if not campaigns:
        return out, len(rows)
    left_out = 0
    if how == "column":
        by_account = {(ws.sending_address() or "").lower(): ws.campaign_id for ws in campaigns}
        for row in rows:
            cid = by_account.get((row.get("account") or "").strip().lower())
            if cid:
                out[cid].append(row)
            else:
                left_out += 1
        return out, left_out
    for i, row in enumerate(rows):
        out[campaigns[i % len(campaigns)].campaign_id].append(row)
    return out, 0


async def import_for(ws, rows: list[dict]) -> dict:
    """Import rows into one campaign's list, leaving out anyone already on
    another campaign's list or suppressed anywhere."""
    taken, suppressed = ws.leads_elsewhere([r.get("email", "") for r in rows])
    keep = [r for r in rows if (r.get("email") or "").strip().lower() not in taken | suppressed]
    result = await recipients_mod.import_rows(ws.state(), keep) if keep else {"added": 0, "duplicates": 0, "invalid": 0, "too_many": 0}
    result["elsewhere"] = sum(1 for r in rows if (r.get("email") or "").strip().lower() in taken)
    result["suppressed"] = sum(1 for r in rows if (r.get("email") or "").strip().lower() in suppressed)
    return result


def describe_import(result: dict) -> str:
    parts = [f"{result['added']} added"]
    if result.get("duplicates"):
        parts.append(f"{result['duplicates']} already on this list")
    if result.get("elsewhere"):
        parts.append(f"{result['elsewhere']} already on another campaign's list")
    if result.get("suppressed"):
        parts.append(f"{result['suppressed']} asked never to be contacted")
    if result.get("invalid"):
        parts.append(f"{result['invalid']} with no valid email")
    return ", ".join(parts)


def register_campaign_routes(app, settings, current_user, base_workspace, render, _redirect):
    from bb.mail import runs as runs_mod
    from bb.mail import service as mail_service
    from bb.workspaces import Workspace, _profile_form

    async def _mail(base):
        db = base.mail_db()
        await db.init()
        return db

    async def _outreach_rows(base, led) -> list[dict]:
        rows = []
        for ws in base.all_campaigns():
            if not ws.is_main and not ws.campaign_row():
                continue
            summ = await ws.summary()
            box = ws.bound_mailbox() if not ws.is_main else None
            account = ((box or {}).get("address") or (ws.sending_address() if ws.mailbox_connected() else "")).lower()
            if summ["worker_running"]:
                status = "running"
            elif summ["worker_paused"]:
                status = "paused"
            elif ws.is_configured() and account:
                status = "stopped"
            else:
                status = "not set up"
            share = {}
            if account:
                pic = led.summary(account)
                me = next((m for m in pic["members"] if m["campaign"] == ws.campaign_id), None)
                share = {"limit": pic["limit"], "total": pic["total"], "share": pic["share"] if me else 0,
                         "used": led.used_by(ws.campaign_id), "members": len(pic["members"])}
            cfg = ws.config() or {}
            rows.append({"id": ws.campaign_id, "name": ws.campaign_name(), "account": account, "status": status,
                         "goal": ((cfg.get("outreach") or {}).get("goal") or "")[:160], "summary": summ, "share": share,
                         "main": ws.is_main, "account_missing": (not ws.is_main and not box)})
        return rows

    async def _run_rows(db, led, boxes: dict) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        out = []
        for run in await db.query("SELECT * FROM reply_runs WHERE status IN ('queued', 'running', 'paused') OR updated_at >= ? "
                                  "ORDER BY CASE WHEN status IN ('queued', 'running', 'paused') THEN 0 ELSE 1 END, created_at DESC LIMIT 100",
                                  (since,)):
            ids = json.loads(run.get("mailbox_ids_json") or "[]")
            addresses = [boxes[i]["address"] for i in ids if i in boxes]
            summary = await runs_mod.run_summary(db, run["id"])
            kind = run.get("kind") or "reply"
            feature = runs_mod.FEATURES.get(kind, "reply-run")
            out.append({**run, "kind": kind, "feature": feature, "feature_label": FEATURE_LABELS.get(feature, "Run"),
                        "addresses": addresses, "progress": summary, "label": runs_mod.run_label(run),
                        "live": run["status"] in runs_mod.LIVE,
                        "used": led.used_by(run["id"]) if addresses else 0})
        return out

    @app.get("/campaigns", response_class=HTMLResponse)
    async def campaigns_page(request: Request, user: dict = Depends(current_user)):
        base = base_workspace(user)
        base.sync_main_registry()
        db = await _mail(base)
        await mail_service.migrate_from_workspace(db, settings.secret_key(), base)
        mailboxes = await db.mailboxes()
        boxes = {m["id"]: m for m in mailboxes}
        led = base.ledger()
        outreach = await _outreach_rows(base, led)
        taken_accounts = {r["account"] for r in outreach if r["account"]}
        accounts = []
        for m in mailboxes:
            m["ready"], m["ready_why"] = mail_service.ready_to_send(m)
            pic = led.summary(m["address"])
            accounts.append({**m, "ledger": pic, "has_outreach": m["address"].lower() in taken_accounts,
                             "limit": mail_service.account_daily_limit(m)})
        return await render(request, "campaigns.html", user, outreach=outreach, runs=await _run_rows(db, led, boxes),
                            accounts=accounts, profiles=[(w.campaign_id, w.campaign_name()) for w in base.all_campaigns()],
                            months=runs_mod.RECONNECT_MONTHS, backlog_choices=runs_mod.BACKLOG_CHOICES,
                            brain_ok=base.llm_configured(), mail_worker=base.mail_worker_alive())

    @app.post("/campaigns/select")
    async def campaigns_select(request: Request, user: dict = Depends(current_user)):
        form = await request.form()
        base = base_workspace(user)
        if not base.select(str(form.get("campaign", "") or "")):
            return _redirect("/campaigns", error="That campaign no longer exists.")
        nxt = str(form.get("next", "") or "/")
        return _redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")

    @app.post("/campaigns/new")
    async def campaigns_new(request: Request, user: dict = Depends(current_user)):
        form = await request.form()
        base = base_workspace(user)
        db = await _mail(base)
        feature = str(form.get("feature", "") or "")
        ids = [str(x) for x in form.getlist("mailbox_ids")]
        boxes = [m for m in await db.mailboxes() if m["id"] in ids]
        if not boxes:
            return _redirect("/campaigns", error="Choose at least one account.")
        if not base.llm_configured():
            return _redirect("/campaigns", error="Set up your AI provider first; every campaign writes with it.")
        name = str(form.get("name", "") or "").strip()[:80]
        mode = str(form.get("mode", "") or "")
        instructions = str(form.get("instructions", "") or "")
        profile_choice = str(form.get("profile_id", "") or "")
        notes: list[str] = []

        if feature == "outreach":
            return await _new_outreach(request, user, base, boxes, form, name)

        if feature not in ("reply", "scan"):
            return _redirect("/campaigns", error="Choose what the campaigns do.")
        if mode not in runs_mod.MODES:
            mode = "auto" if feature == "reply" else "review"
        if mode == "auto":
            for m in boxes:
                ok, why = mail_service.ready_to_send(m)
                if not ok:
                    notes.append(f"{m['address']}: {why}")
            boxes = [m for m in boxes if mail_service.ready_to_send(m)[0]]
            if not boxes:
                return _redirect("/campaigns", error="None of those accounts can send on its own yet: " + "; ".join(notes))
        started = []
        for m in boxes:
            pid = profile_choice if profile_choice and base.campaign(profile_choice) else \
                campaigns_mod.profile_for_account(base.user_root, m["address"])
            if feature == "reply":
                try:
                    backlog = int(form.get("backlog_days", 0) or 0)
                except (TypeError, ValueError):
                    backlog = 0
                run = await runs_mod.create_reply_campaign(db, m, mode=mode, instructions=instructions, profile_id=pid,
                                                           backlog_days=backlog, created_by=user.get("email", ""), name=name)
                if run.get("busy"):
                    notes.append(f"{m['address']} already has a reply campaign")
                else:
                    started.append(m["address"])
            else:
                try:
                    months = int(form.get("months", runs_mod.RECONNECT_DEFAULT_MONTHS))
                except (TypeError, ValueError):
                    months = runs_mod.RECONNECT_DEFAULT_MONTHS
                res = (await runs_mod.create_reconnect_runs(db, [m], months=months, mode=mode, instructions=instructions,
                                                            created_by=user.get("email", ""), all_mailboxes=await db.mailboxes(),
                                                            profile_id=pid, name=name))[0]
                if res.get("busy"):
                    notes.append(f"{m['address']} already has a scan going")
                elif not res.get("id"):
                    notes.append(f"{m['address']}: no past conversations to continue in the last {months} months")
                else:
                    started.append(m["address"])
        if started and not base.mail_worker_running():
            base.start_mail_worker()
        what = "reply campaign" if feature == "reply" else "mailbox scan"
        msg = (f"Started {len(started)} {what}{'s' if len(started) != 1 else ''}: {', '.join(started)}." if started else "")
        return _redirect("/campaigns", msg=msg, error="; ".join(notes))

    async def _new_outreach(request, user, base, boxes, form, name):
        """One outreach campaign per chosen account, each its own workspace:
        the profile copied from a campaign you pick (the goal replaced if you
        typed one), the brief files and signature image with it, and the
        uploaded leads dealt out between them."""
        source = base.campaign(str(form.get("profile_id", "") or "")) or base.main()
        goal = str(form.get("goal", "") or "").strip()
        taken = {(w.sending_address() or "").lower() for w in base.all_campaigns() if w.mailbox_connected()}
        taken |= {(r.get("account") or "").lower() for r in campaigns_mod.load(base.user_root) if r.get("account")}
        created, notes = [], []
        for m in boxes:
            address = m["address"].lower()
            if address in taken:
                notes.append(f"{address} already has an outreach campaign (one per account)")
                continue
            cid = campaigns_mod.new_id()
            label = name if (name and len(boxes) == 1) else (f"{name} · {address}" if name else f"Outreach from {address}")
            campaigns_mod.upsert(base.user_root, {"id": cid, "name": label[:100], "mailbox_id": m["id"], "account": address})
            ws = Workspace(settings, user, cid).ensure()
            problem = ws.bind_account()
            if problem:
                campaigns_mod.remove(base.user_root, cid)
                shutil.rmtree(ws.root, ignore_errors=True)
                notes.append(f"{address}: {problem}")
                continue
            if source.is_configured():
                try:
                    values = _profile_form(source.profile())
                    if goal:
                        values["campaign_goal"] = goal
                    for f in source.attachments():
                        shutil.copy2(source.attachments_dir / f["name"], ws.attachments_dir / f["name"])
                    if source.signature_image():
                        shutil.copy2(source.signature_dir / source.signature_image(), ws.signature_dir / source.signature_image())
                    ws.write_profile(values)
                except Exception as e:
                    notes.append(f"{address}: the profile could not be copied ({e}); fill it in on its Profile page")
            created.append(ws)
            taken.add(address)
        lead_note = ""
        upload = form.get("leads")
        text = str(form.get("pasted", "") or "")
        if hasattr(upload, "read") and getattr(upload, "filename", ""):
            raw = await upload.read()
            text = raw.decode("utf-8-sig", errors="replace") if len(raw) <= 20 * 1024 * 1024 else ""
        rows = recipients_mod.parse_rows(text) if text.strip() else []
        if rows and created:
            how = "column" if str(form.get("split", "") or "") == "column" else "turns"
            dealt, left_out = split_leads(rows, created, how)
            lines = []
            for ws in created:
                result = await import_for(ws, dealt[ws.campaign_id])
                lines.append(f"{ws.sending_address()}: {describe_import(result)}")
            lead_note = " Leads: " + "; ".join(lines) + "." + (f" {left_out} row(s) named no account of these campaigns." if left_out else "")
        started = []
        if created and form.get("start"):
            for ws in created:
                ok, detail = ws.start_worker()
                (started.append(ws.sending_address()) if ok else notes.append(f"{ws.sending_address()}: {detail}"))
        if not created:
            return _redirect("/campaigns", error="; ".join(notes) or "No campaign was created.")
        if len(created) == 1:
            base.select(created[0].campaign_id)
        msg = (f"Created {len(created)} outreach campaign{'s' if len(created) != 1 else ''}: "
               + ", ".join(w.sending_address() for w in created) + "." + lead_note
               + (f" Started: {', '.join(started)}." if started else " Each starts from its row below once its profile and leads look right."))
        return _redirect("/campaigns", msg=msg, error="; ".join(notes))

    @app.post("/campaigns/outreach/{campaign_id}/{action}")
    async def campaigns_outreach_action(campaign_id: str, action: str, user: dict = Depends(current_user)):
        base = base_workspace(user)
        ws = base.campaign(campaign_id)
        if ws is None:
            raise HTTPException(404)
        name = ws.campaign_name()
        if action == "start":
            ok, detail = ws.start_worker()
            ws.paused_file.unlink(missing_ok=True) if ok else None
        elif action == "pause":
            ok, detail = await ws.pause_worker()
        elif action == "resume":
            ok, detail = ws.resume_worker()
        elif action == "stop":
            ws.paused_file.unlink(missing_ok=True)
            ok, detail = await ws.stop_worker(f"Stop pressed for {name} on the Campaigns page")
            detail = "Stopped. Its leads, emails and replies are kept; Start picks up where it left off." if ok else detail
        elif action == "open":
            base.select(ws.campaign_id)
            return _redirect("/")
        elif action == "remove":
            if ws.is_main:
                return _redirect("/campaigns", error="The main campaign stays; stop it instead.")
            if ws.worker_running():
                return _redirect("/campaigns", error="Stop the campaign before removing it.")
            ws._leave_ledger()
            campaigns_mod.remove(base.user_root, ws.campaign_id)
            # Kept, not deleted: a removed campaign's folder can be put back by hand.
            dest = base.user_root / "campaigns" / ".removed" / f"{ws.campaign_id}-{int(time.time())}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(ws.root), str(dest))
            except OSError as e:
                logger.warning("could not move %s aside: %s", ws.root, e)
            if base.selected().campaign_id == ws.campaign_id:
                base.select("main")
            return _redirect("/campaigns", msg=f"Removed {name}. Its folder is kept under campaigns/.removed.")
        else:
            raise HTTPException(404)
        return _redirect("/campaigns", msg=f"{name}: {detail}" if ok else "", error="" if ok else f"{name}: {detail}")

    @app.post("/campaigns/run/{run_id}/{action}")
    async def campaigns_run_action(run_id: str, action: str, user: dict = Depends(current_user)):
        base = base_workspace(user)
        db = await _mail(base)
        env = {"HARVEY_HOME": str(base.user_root)}
        if action == "pause":
            ok = await runs_mod.pause_run(db, run_id, env)
        elif action == "resume":
            ok = await runs_mod.resume_run(db, run_id)
            if ok and not base.mail_worker_running():
                base.start_mail_worker()
        elif action == "stop":
            ok = await runs_mod.cancel_run(db, run_id, env)
        else:
            raise HTTPException(404)
        verb = {"pause": "Paused", "resume": "Resumed", "stop": "Stopped"}[action]
        return _redirect("/campaigns", msg=f"{verb}." if ok else "", error="" if ok else "That run cannot do that in its current state.")

    @app.post("/campaigns/{campaign_id}/account")
    async def campaigns_rebind(request: Request, campaign_id: str, user: dict = Depends(current_user)):
        """Give an outreach campaign another account (not one that already has a campaign)."""
        base = base_workspace(user)
        ws = base.campaign(campaign_id)
        if ws is None or ws.is_main:
            raise HTTPException(404)
        form = await request.form()
        db = await _mail(base)
        box = await db.mailbox(str(form.get("mailbox_id", "") or ""))
        if not box:
            return _redirect("/mailbox", error="Choose an account.")
        others = {(r.get("account") or "").lower() for r in campaigns_mod.load(base.user_root) if r["id"] != ws.campaign_id}
        if box["address"].lower() in others:
            return _redirect("/mailbox", error=f"{box['address']} already has an outreach campaign.")
        was_running = bool(ws.worker_running())
        if was_running:
            await ws.stop_worker("the campaign was given another account")
        campaigns_mod.upsert(base.user_root, {**ws.campaign_row(), "mailbox_id": box["id"], "account": box["address"].lower()})
        ws.binding_file.unlink(missing_ok=True)
        problem = ws.bind_account()
        if problem:
            return _redirect("/mailbox", error=problem)
        ws.rewrite_config()
        if was_running:
            ws.start_worker()
        return _redirect("/mailbox", msg=f"{ws.campaign_name()} now sends from {box['address']}.")
