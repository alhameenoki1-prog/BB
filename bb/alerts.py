"""Telling a user, away from the site, that their mailbox stopped.

The worker's health check (``harvey.health``) and the command center's
connection checks write what is wrong into each workspace; the dashboard
and the Mailbox page show it. Nobody sees those pages at three in the
morning, so this loop, in the site process, reads the same facts every
few minutes and sends each new problem to the person once: by email
through the site's own alert mailbox (``BB_ALERT_SMTP_*``, never the
user's mailbox, which may be the thing that broke) and/or to a webhook
they chose (ntfy for a phone notification, Slack, Discord, anything that
takes a POST). A problem that is still there a day later is sent again;
when it clears, one "working again" message follows.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("bb.alerts")

CHECK_EVERY_SECONDS = 300      # how often every workspace is looked at
FIRST_CHECK_AFTER_SECONDS = 60  # let the site and the workers settle first
REMIND_HOURS = 24              # a problem still there is sent again after this long
WEBHOOK_TIMEOUT = 15


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# ── what is wrong right now ──

def _mail_reconnects(ws) -> dict[str, str]:
    """Command-center mailboxes that need signing in again (read straight
    from the user's mail.db; nothing is created when there is none)."""
    path = ws.mail_db_path
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute("SELECT id, address, status_detail FROM mailboxes WHERE status = 'reconnect' AND enabled = 1").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return {f"mail:{mid}": f"{address} needs reconnecting in the mail command center: {(detail or 'the sign-in was refused')[:240]}"
            for mid, address, detail in rows}


async def current_alerts(ws) -> dict[str, str]:
    """{key: plain sentence} for every problem the user should hear about.
    The worker's mailbox counts only while the worker runs or is meant to
    (an alert left behind by a worker the user stopped is history)."""
    out: dict[str, str] = {}
    campaigns = ws.all_campaigns() if hasattr(ws, "all_campaigns") else [ws]
    for one in campaigns:
        if not (one.worker_running() or one.wants_autostart()):
            continue
        try:
            health = await one.mailbox_health()
        except Exception:
            health = {}
        if health.get("alert"):
            reconnect = one.needs_reconnect(health)
            where = "" if getattr(one, "is_main", True) else f"{one.campaign_name()} ({one.sending_address()}): "
            key = "mailbox" if getattr(one, "is_main", True) else f"campaign:{one.campaign_id}"
            out[key] = where + health["alert"] + (" The mailbox needs signing in again: open the Mailbox page and press "
                                                  "Reconnect." if reconnect else "")
    if ws.mail_worker_running() or ws.mail_autostart_file.exists():
        out.update(_mail_reconnects(ws))
    return out


# ── where it goes ──

def _addresses(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def webhook_problem(url: str) -> str:
    """Why this webhook address cannot be used, or "". Only https, and only
    hosts on the public internet: the site must not be made to post into
    its own network."""
    url = (url or "").strip()
    if not url:
        return ""
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return "The webhook address must start with https://"
    try:
        addresses = _addresses(u.hostname, u.port or 443)
    except (socket.gaierror, UnicodeError, OSError):
        return f"{u.hostname} could not be found."
    for a in addresses:
        try:
            ip = ipaddress.ip_address(a.split("%", 1)[0])
        except ValueError:
            return f"{u.hostname} resolves to an address that cannot be checked."
        if not ip.is_global:
            return f"{u.hostname} points inside a private network, which is not allowed."
    return ""


def _webhook_request(url: str, title: str, text: str, link: str, ok: bool, account: str) -> dict:
    host = (urlparse(url).hostname or "").lower()
    if "ntfy" in host:
        return {"content": text.encode(), "headers": {"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                                                       "Tags": "white_check_mark" if ok else "warning",
                                                       "Priority": "default" if ok else "high", "Click": link}}
    if host.endswith("slack.com"):
        return {"json": {"text": f"*{title}*\n{text}\n<{link}|Open the Mailbox page>"}}
    if host.endswith(("discord.com", "discordapp.com")):
        return {"json": {"content": f"**{title}**\n{text}\n{link}"[:1900]}}
    return {"json": {"title": title, "text": text, "link": link, "status": "ok" if ok else "alert", "account": account}}


async def _send_webhook(url: str, title: str, text: str, link: str, ok: bool, account: str) -> str:
    problem = await asyncio.to_thread(webhook_problem, url)
    if problem:
        return problem
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT, follow_redirects=False) as client:
            r = await client.post(url, **_webhook_request(url, title, text, link, ok, account))
    except httpx.HTTPError as e:
        return f"the webhook could not be reached: {e}"
    if r.status_code >= 400:
        return f"the webhook answered {r.status_code}"
    return ""


async def _send_email(settings, to: str, title: str, text: str, link: str) -> str:
    import aiosmtplib

    msg = EmailMessage()
    msg["From"] = settings.alert_from
    msg["To"] = to
    msg["Subject"] = title
    msg.set_content(f"{text}\n\nOpen the Mailbox page: {link}\n\n"
                    f"You get this because mailbox alerts are on for {to}. Turn them off on the Mailbox page.")
    port = settings.alert_smtp_port
    try:
        await aiosmtplib.send(msg, hostname=settings.alert_smtp_host, port=port,
                              username=settings.alert_smtp_username or None, password=settings.alert_smtp_password or None,
                              start_tls=(port == 587), use_tls=(port == 465), timeout=30)
    except (aiosmtplib.SMTPException, OSError) as e:
        return f"the alert email could not be sent: {str(e)[:160]}"
    return ""


async def deliver(settings, ws, title: str, text: str, ok: bool = False) -> tuple[list[str], list[str]]:
    """Send one message to every channel the user turned on. Returns
    (channels reached, problems)."""
    prefs = ws.alert_settings()
    link = settings.base_url + "/mailbox" + ("" if ok else "#health")
    reached, problems = [], []
    if prefs["email_on"] and prefs["email"]:
        if settings.alert_email_ready:
            why = await _send_email(settings, prefs["email"], title, text, link)
            (problems.append(why) if why else reached.append(f"email to {prefs['email']}"))
        else:
            problems.append("the site has no alert email server set up (BB_ALERT_SMTP_HOST)")
    if prefs["webhook"]:
        why = await _send_webhook(prefs["webhook"], title, text, link, ok, ws.user.get("email", ""))
        (problems.append(why) if why else reached.append("the webhook"))
    return reached, problems


# ── once per problem ──

async def check_workspace(settings, ws, now: datetime | None = None) -> list[str]:
    """Compare what is wrong now with what was already said, send what is
    new, due again, or cleared. Returns a line per message sent."""
    now = now or _now()
    state = ws.alert_state()
    incidents: dict = state.setdefault("incidents", {})
    active = await current_alerts(ws)
    sent: list[str] = []
    for key, text in active.items():
        inc = incidents.setdefault(key, {"since": now.isoformat(), "notified_at": "", "reminders": 0})
        inc["text"] = text
        last = _parse(inc.get("notified_at", ""))
        if last and now - last < timedelta(hours=REMIND_HOURS):
            continue
        again = bool(last)
        title = ("Still not working: " if again else "") + ("a mailbox needs reconnecting" if key.startswith("mail:")
                                                           else "your mailbox needs attention")
        reached, problems = await deliver(settings, ws, title[:1].upper() + title[1:], text)
        if reached:
            inc["notified_at"] = now.isoformat()
            inc["reminders"] = int(inc.get("reminders", 0)) + (1 if again else 0)
            sent.append(f"{key}: {', '.join(reached)}")
        for p in problems:
            if not p.startswith("the site has no"):   # a channel never set up is not a failure
                logger.warning("Alert for %s not delivered: %s", ws.user.get("email"), p)
        state["last_problem"] = "; ".join(p for p in problems if not p.startswith("the site has no"))[:300]
    for key in [k for k in incidents if k not in active]:
        inc = incidents.pop(key)
        if inc.get("notified_at"):
            reached, _ = await deliver(settings, ws, "The mailbox is connected again" if key.startswith("mail:")
                                       else "Your mailbox is working again",
                                       "The problem we wrote to you about has cleared: " + (inc.get("text") or "")[:300], ok=True)
            if reached:
                sent.append(f"{key} cleared: {', '.join(reached)}")
    if sent:
        state["last_sent_at"] = now.isoformat()
        state["last_sent"] = sent[-1][:300]
    ws.save_alert_state(state)
    return sent


async def run_once(settings, accounts) -> int:
    from bb.workspaces import all_workspaces
    total = 0
    for ws in all_workspaces(settings, accounts):
        if ws.user.get("disabled"):
            continue
        try:
            total += len(await check_workspace(settings, ws))
        except Exception as e:  # one broken workspace must not stop the others
            logger.warning("Alert check failed for %s: %s", ws.user.get("email"), e)
    return total


async def loop(settings, accounts):
    await asyncio.sleep(FIRST_CHECK_AFTER_SECONDS)
    while True:
        try:
            await run_once(settings, accounts)
        except Exception as e:
            logger.warning("Alert loop: %s", e)
        await asyncio.sleep(CHECK_EVERY_SECONDS)
