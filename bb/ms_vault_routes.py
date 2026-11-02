"""Microsoft Graph vault and device-code authorization UI.

All operations are tied to the signed-in BB user's own workspace. The browser
never sees Microsoft access or refresh tokens; device user_code is not a token.
"""
import re
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from bb import microsoft_device, microsoft_oauth, microsoft_device_store
from bb.mail import crypto, service
from bb.mail.providers import AuthError, TransientError
from bb.mail.providers.graph import GraphAccount

_ID = re.compile(r"^[a-zA-Z0-9.!_~-]{1,180}$")
_MAX_FILE = 4 * 1024 * 1024


def register_ms_vault_routes(app, settings, current_user, base_workspace, render, _redirect, accounts):
    def requires_mfa(user):
        if not accounts.totp_enabled(user["id"]):
            raise HTTPException(
                303, headers={"Location": "/security/mfa"},
                detail="Enable two-factor authentication before opening the token vault.",
            )
        return user

    def owner_ws(user):
        requires_mfa(user)
        return base_workspace(user)

    async def mailbox_ctx(user):
        ws = owner_ws(user)
        db = ws.mail_db()
        await db.init()
        await migrate_legacy_microsoft(ws, db)
        return ws, db

    async def get_account(user, mailbox_id):
        ws, db = await mailbox_ctx(user)
        box = await db.mailbox(mailbox_id)
        if not box or box.get("provider") != "graph":
            raise HTTPException(404, "Microsoft mailbox not found.")
        creds = service.open_creds(settings.secret_key(), box)
        if not creds.get("refresh_token"):
            raise HTTPException(404, "Microsoft delegated mailbox not found.")
        return ws, db, box, creds

    async def graph_call(user, mailbox_id, method, path, **kwargs):
        ws, db, box, creds = await get_account(user, mailbox_id)
        account = GraphAccount(box, creds, settings.worker_env())
        try:
            value = await account._call(method, path, **kwargs)
        except AuthError as exc:
            await db.update_mailbox(mailbox_id, status="reconnect", status_detail=str(exc)[:250])
            raise HTTPException(409, "Microsoft authorization needs reconnection.") from exc
        except TransientError as exc:
            raise HTTPException(502, str(exc)[:180]) from exc
        finally:
            await service.persist_creds(db, settings.secret_key(), box, account)
        return value

    async def migrate_legacy_microsoft(ws, db):
        """Move an old single-mailbox plaintext refresh token into the encrypted DB."""
        path = ws.microsoft_token_file
        if not path.exists():
            return
        data = ws.microsoft_token()
        mailbox = ws.microsoft_account().get("email", "")
        if not mailbox or not data.get("refresh_token"):
            return
        await service.connect_microsoft(
            db, settings.secret_key(), address=mailbox,
            refresh_token=data["refresh_token"], env=settings.worker_env(),
            scopes=str(data.get("scope") or ""), verify=False,
        )
        path.unlink()  # remove only after the encrypted transaction succeeded
        ws.microsoft_account_file.unlink(missing_ok=True)

    @app.get("/vault", response_class=HTMLResponse)
    async def vault_home(request: Request, mailbox: str = "", user: dict = Depends(current_user)):
        ws, db = await mailbox_ctx(user)
        boxes = [b for b in await db.mailboxes() if b["provider"] == "graph"
                 and service.signed_in(settings.secret_key(), b)]
        selected = next((b for b in boxes if b["id"] == mailbox), boxes[0] if boxes else None)
        messages, drive, directory = [], [], {}
        problems = []
        if selected and selected.get("status") != "reconnect":
            mid = selected["id"]
            try:
                msgs = await graph_call(user, mid, "GET", "/me/messages", params={
                    "$top": "25", "$orderby": "receivedDateTime desc",
                    "$select": "id,subject,from,bodyPreview,receivedDateTime",
                })
                messages = msgs.get("value", [])
            except HTTPException as exc:
                problems.append("Mail: " + str(exc.detail))
            try:
                files = await graph_call(user, mid, "GET", "/me/drive/root/children",
                                         params={"$top": "40", "$select": "id,name,size,folder,file,webUrl"})
                drive = files.get("value", [])
            except HTTPException as exc:
                problems.append("OneDrive: " + str(exc.detail))
        return await render(
            request, "ms_vault.html", user, vault_mailboxes=boxes, vault_selected=selected,
            vault_messages=messages, vault_drive=drive, vault_problems=problems,
            mfa_enabled=True,
        )

    @app.get("/vault/mailbox/{mailbox_id}/message/{message_id}", response_class=HTMLResponse)
    async def vault_message(request: Request, mailbox_id: str, message_id: str,
                            user: dict = Depends(current_user)):
        if not _ID.fullmatch(message_id):
            raise HTTPException(400, "Invalid message ID.")
        _, _, box, _ = await get_account(user, mailbox_id)
        result = await graph_call(user, mailbox_id, "GET",
                                  "/me/messages/" + quote(message_id, safe=""),
                                  params={"$select": "id,subject,from,toRecipients,body,receivedDateTime"})
        from harvey.integrations.mail_provider import html_to_text
        body = (result.get("body") or {}).get("content") or ""
        if (result.get("body") or {}).get("contentType", "").lower() == "html":
            body = html_to_text(body)
        return await render(request, "ms_vault_message.html", user, box=box,
                            message=result, body=body)

    @app.post("/vault/mailbox/{mailbox_id}/send")
    async def vault_send(request: Request, mailbox_id: str, user: dict = Depends(current_user)):
        form = await request.form()
        to = str(form.get("to") or "").strip()
        subject = str(form.get("subject") or "").strip()[:250]
        body = str(form.get("body") or "").strip()
        if len(to) > 320 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            return _redirect("/vault?mailbox=" + mailbox_id, error="Enter one valid recipient email.")
        if not subject or not body or len(body) > 100000:
            return _redirect("/vault?mailbox=" + mailbox_id, error="Enter a subject and message.")
        await graph_call(user, mailbox_id, "POST", "/me/sendMail", json={
            "message": {
                "subject": subject, "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            }, "saveToSentItems": True,
        })
        return _redirect("/vault?mailbox=" + mailbox_id, msg="Message sent through Microsoft Graph.")

    @app.post("/vault/mailbox/{mailbox_id}/message/{message_id}/reply")
    async def vault_reply(request: Request, mailbox_id: str, message_id: str,
                          user: dict = Depends(current_user)):
        if not _ID.fullmatch(message_id):
            raise HTTPException(400, "Invalid message ID.")
        body = str((await request.form()).get("body") or "").strip()
        if not body or len(body) > 100000:
            raise HTTPException(400, "Reply must contain a message.")
        await graph_call(user, mailbox_id, "POST",
                         "/me/messages/" + quote(message_id, safe="") + "/reply",
                         json={"comment": body})
        return _redirect("/vault?mailbox=" + mailbox_id, msg="Reply sent through Microsoft Graph.")

    @app.post("/vault/mailbox/{mailbox_id}/drive/upload")
    async def vault_upload(request: Request, mailbox_id: str, user: dict = Depends(current_user)):
        import httpx
        from bb.mail.providers.graph import GRAPH
        _, db, box, creds = await get_account(user, mailbox_id)
        upload = (await request.form()).get("file")
        if not upload or not hasattr(upload, "read"):
            raise HTTPException(400, "Select a file to upload.")
        name = str(upload.filename or "").replace("\\", "/").split("/")[-1]
        if not name or name in (".", "..") or len(name) > 120:
            raise HTTPException(400, "Invalid file name.")
        data = await upload.read(_MAX_FILE + 1)
        if len(data) > _MAX_FILE:
            raise HTTPException(413, "Files over 4 MB require a resumable upload session.")
        account = GraphAccount(box, creds, settings.worker_env())
        try:
            token = await account._token()
            url = GRAPH + "/me/drive/root:/" + quote(name, safe="") + ":/content"
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.put(
                    url, content=data, headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/octet-stream",
                    })
            if response.status_code in (401, 403):
                await db.update_mailbox(mailbox_id, status="reconnect",
                                        status_detail="Microsoft authorization needs reconnection or Files.ReadWrite permission.")
                raise HTTPException(409, "Microsoft refused OneDrive authorization.")
            if response.status_code >= 400:
                raise HTTPException(502, "OneDrive upload failed.")
        finally:
            await service.persist_creds(db, settings.secret_key(), box, account)
        return _redirect("/vault?mailbox=" + mailbox_id, msg="File uploaded to OneDrive.")

    @app.get("/vault/mailbox/{mailbox_id}/drive/{item_id}/download")
    async def vault_download(mailbox_id: str, item_id: str, user: dict = Depends(current_user)):
        import httpx
        from bb.mail.providers.graph import GRAPH
        if not _ID.fullmatch(item_id):
            raise HTTPException(400, "Invalid OneDrive item ID.")
        _, db, box, creds = await get_account(user, mailbox_id)
        meta = await graph_call(user, mailbox_id, "GET",
                                "/me/drive/items/" + quote(item_id, safe=""))
        name = str(meta.get("name") or "download.bin").replace("\\", "/").split("/")[-1][:120]
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "download.bin"
        account = GraphAccount(box, service.open_creds(settings.secret_key(), await db.mailbox(mailbox_id)),
                               settings.worker_env())
        try:
            token = await account._token()
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                async with client.stream(
                    "GET", GRAPH + "/me/drive/items/" + quote(item_id, safe="") + "/content",
                    headers={"Authorization": "Bearer " + token},
                ) as response:
                    if response.status_code >= 400:
                        raise HTTPException(502, "OneDrive download failed.")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 10 * 1024 * 1024:
                            raise HTTPException(413, "OneDrive file exceeds the 10 MB download limit.")
                        chunks.append(chunk)
            return Response(b"".join(chunks), media_type="application/octet-stream",
                            headers={"Content-Disposition": 'attachment; filename="' + name + '"',
                                     "Cache-Control": "no-store"})
        finally:
            await service.persist_creds(db, settings.secret_key(), box, account)

    @app.post("/vault/mailbox/{mailbox_id}/disconnect")
    async def vault_disconnect(request: Request, mailbox_id: str,
                               user: dict = Depends(current_user)):
        """Remove one account's sealed tokens after rechecking the owner's TOTP."""
        ws, db, box, creds = await get_account(user, mailbox_id)
        code = str((await request.form()).get("code") or "")
        if not accounts.verify_totp(user["id"], settings.secret_key(), code):
            return _redirect("/vault?mailbox=" + mailbox_id,
                             error="Enter the current authenticator code to disconnect.")
        await db.remove_mailbox(mailbox_id)
        return _redirect("/vault", msg="Microsoft account disconnected from BB. "
                         "You can also revoke the application from your Microsoft account.")

    @app.get("/vault/mailbox/{mailbox_id}/organization", response_class=HTMLResponse)
    async def vault_organization(request: Request, mailbox_id: str, user: dict = Depends(current_user)):
        _, _, box, _ = await get_account(user, mailbox_id)
        try:
            data = await graph_call(user, mailbox_id, "GET", "/organization",
                                    params={"$select": "id,displayName,verifiedDomains"})
            orgs = data.get("value", [])
            err = ""
        except HTTPException as exc:
            orgs, err = [], str(exc.detail) + " (Organization.Read.All may require admin consent.)"
        return await render(request, "ms_vault_org.html", user, box=box,
                            organizations=orgs, org_error=err)

    @app.post("/l/{token}/device")
    @app.post("/microsoft/authorize/l/{token}/device")
    async def vault_start_device(token: str, user: dict = Depends(current_user)):
        from bb.microsoft_authorization_links import link_for_token
        ws, db = await mailbox_ctx(user)
        if not link_for_token(ws.user_data_dir, token):
            raise HTTPException(404, "Authorization link expired, revoked, or belongs to another BB account.")
        try:
            grant = await microsoft_device.start(settings)
        except (ValueError, RuntimeError) as exc:
            return _redirect("/l/" + token, error=str(exc)[:230])
        ident = microsoft_device_store.save(ws.user_data_dir, settings.secret_key(), grant)
        return _redirect("/vault/device/" + ident)

    @app.get("/vault/device/{ident}", response_class=HTMLResponse)
    async def vault_device_page(request: Request, ident: str, user: dict = Depends(current_user)):
        ws = owner_ws(user)
        grant = microsoft_device_store.read(ws.user_data_dir, ident)
        if not grant:
            raise HTTPException(404, "Microsoft device authorization has expired.")
        return await render(
            request, "ms_vault_device.html", user, device_id=ident,
            user_code=grant["user_code"], verification_uri=grant["verify_uri"],
            expires_at=grant["expires_at"], poll_interval=grant["interval_seconds"],
        )

    @app.post("/vault/device/{ident}/poll")
    async def vault_device_poll(ident: str, user: dict = Depends(current_user)):
        ws, db = await mailbox_ctx(user)
        grant = microsoft_device_store.claim_poll(ws.user_data_dir, ident)
        if not grant:
            if not microsoft_device_store.read(ws.user_data_dir, ident):
                return JSONResponse({"status": "failed", "message": "Authorization expired or already completed."})
            return JSONResponse({"status": "pending"})
        device_code = crypto.open_sealed(settings.secret_key(), grant["code_ciphertext"])
        try:
            status, data = await microsoft_device.poll(settings, device_code, grant["client_id"])
            if status == "slow_down":
                microsoft_device_store.slow_down(ws.user_data_dir, ident)
            if status in ("pending", "authorization_pending", "slow_down"):
                return JSONResponse({"status": "pending"})
            if status == "failed":
                microsoft_device_store.remove(ws.user_data_dir, ident)
                return JSONResponse({"status": "failed", "message": data.get("message", "Authorization failed.")})
            email = await microsoft_oauth.account_email(data["access_token"])
            mid, _ = await service.connect_microsoft(
                db, settings.secret_key(), address=email,
                refresh_token=data["refresh_token"], env=settings.worker_env(),
                scopes=str(data.get("scope") or ""), expires_in=int(data.get("expires_in") or 0),
                access_token=data["access_token"], client_id=grant["client_id"],
                public_client=True, verify=False,
            )
            microsoft_device_store.remove(ws.user_data_dir, ident)
            return JSONResponse({"status": "connected", "url": "/vault?mailbox=" + mid,
                                 "mailbox": email})
        except Exception:
            # Never serialize OAuth tokens or HTTP errors in browser responses.
            return JSONResponse({"status": "failed",
                                 "message": "Could not finalize Microsoft authorization. Please start again."},
                                status_code=502)
