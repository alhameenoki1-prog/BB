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

import httpx
from urllib.parse import urlparse

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

def _mail_reconnects(ws) -> tuple[dict[str, str], set[str]]:
    """Command-center mailboxes that need signing in again, and the ids of
    every mailbox there is (read straight from the user's mail.db; nothing
    is created when there is none)."""
    path = ws.mail_db_path
    if not path.exists():
        return {}, set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}, set()
    try:
        rows = con.execute("SELECT id, address, status, status_detail, enabled FROM mailboxes").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    alerts = {f"mail:{mid}": f"{address} needs reconnecting in the mail command center: {(detail or 'the sign-in was refused')[:240]}"
              for mid, address, status, detail, enabled in rows if status == "reconnect" and enabled}
    return alerts, {r[0] for r in rows}


FROZEN_ALERT_SECONDS = 900   # a worker that has not answered for this long, restarts included, is reported


async def observe(ws) -> dict:
    """What is wrong now and what is being watched at all:
    {alerts: {key: sentence}, kinds: {key: kind}, watched: {keys}, mailboxes: {ids}}.
    A worker the user stopped or paused is not watched: its last alert is
    neither news nor cleared (Resume picks it up again)."""
    alerts: dict[str, str] = {}
    kinds: dict[str, str] = {}
    watched: set[str] = set()
    campaigns = ws.all_campaigns() if hasattr(ws, "all_campaigns") else [ws]
    for one in campaigns:
        main = getattr(one, "is_main", True)
        key = "mailbox" if main else f"campaign:{one.campaign_id}"
        if not (one.worker_running() or one.wants_autostart()):
            continue
        watched.add(key)
        try:
            health = await one.mailbox_health()
        except Exception:
            health = {}
        if health.get("alert"):
            reconnect = one.needs_reconnect(health)
            where = "" if main else f"{one.campaign_name()} ({one.sending_address()}): "
            alerts[key] = where + health["alert"] + (" The mailbox needs signing in again: open the Mailbox page and press "
                                                     "Reconnect." if reconnect else "")
            kinds[key] = "reconnect" if reconnect else ("login" if not health.get("ok", True) else "stall")
        elif hasattr(one, "worker_beat"):
            # Frozen: the process is there but has not answered for a quarter
            # of an hour, so the site's own restart did not bring it back.
            # (Its own health check cannot say so: it runs inside the worker.)
            beat = one.worker_beat()
            stuck = one.worker_stuck() if beat and beat.get("age", 0) >= FROZEN_ALERT_SECONDS else ""
            if stuck:
                where = "" if main else f"{one.campaign_name()} ({one.sending_address()}): "
                alerts[key] = where + (f"The worker stopped answering ({stuck}), and restarting it did not help. "
                                       "Open the dashboard and press Wake up now, or look at the raw log there.")
                kinds[key] = "frozen"
    mail_alerts, mailboxes = _mail_reconnects(ws)
    if ws.mail_worker_running() or ws.mail_autostart_file.exists():
        watched.add("mail:*")
        alerts.update(mail_alerts)
        kinds.update({k: "reconnect" for k in mail_alerts})
    return {"alerts": alerts, "kinds": kinds, "watched": watched, "mailboxes": mailboxes}


async def current_alerts(ws) -> dict[str, str]:
    """{key: plain sentence} for every problem the user should hear about."""
    return (await observe(ws))["alerts"]


# ── where it goes ──

def _addresses(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"), ipaddress.ip_network("::/96"))


def _public(ip) -> bool:
    """On the public internet, including the IPv4 address an IPv6 one
    carries (mapped, 6to4, NAT64): none of them may lead inside."""
    if not ip.is_global:
        return False
    if ip.version == 6:
        if ip.teredo:
            return False
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and any(ip in net for net in _NAT64):
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None and not inner.is_global:
            return False
    return True


def _resolve(url: str) -> tuple[str, str, str]:
    """(problem or "", the host as it is sent, the checked address to
    connect to). The host is IDNA-encoded the way httpx encodes it, and the
    request then goes to the one address that was checked, so a name that
    answers differently the second time cannot lead inside."""
    raw = (url or "").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "The webhook address has a hidden character in it (a tab or a line break); paste it again.", "", ""
    try:
        u = httpx.URL(raw)
        host = u.raw_host.decode("ascii")
        port = u.port or 443
    except Exception:
        return "That webhook address is not a valid web address.", "", ""
    if u.scheme != "https" or not host:
        return "The webhook address must start with https://", "", ""
    if not 0 < port <= 65535:
        return "That webhook address is not a valid web address.", "", ""
    try:
        addresses = _addresses(host.strip("[]"), port)
    except (OSError, UnicodeError, ValueError):
        return f"{host} could not be found.", "", ""
    if not addresses:
        return f"{host} could not be found.", "", ""
    for a in addresses:
        try:
            ip = ipaddress.ip_address(a.split("%", 1)[0])
        except ValueError:
            return f"{host} resolves to an address that cannot be checked.", "", ""
        if not _public(ip):
            return f"{host} points inside a private network, which is not allowed.", "", ""
    return "", host, addresses[0].split("%", 1)[0]


def webhook_problem(url: str) -> str:
    """Why this webhook address cannot be used, or "". Only https, and only
    hosts on the public internet: the site must not be made to post into
    its own network."""
    if not (url or "").strip():
        return ""
    return _resolve(url)[0]


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
    problem, host, address = await asyncio.to_thread(_resolve, url)
    if problem:
        return problem
    try:
        u = httpx.URL(url.strip())
        target = u.copy_with(host=address)
        req = _webhook_request(url, title, text, link, ok, account)
        headers = {**req.pop("headers", {}), "Host": host if u.port in (None, 443) else f"{host}:{u.port}"}
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT, follow_redirects=False) as client:
            r = await client.post(str(target), headers=headers, extensions={"sni_hostname": host}, **req)
    except Exception:
        return "the webhook could not be reached"
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
    # Implicit TLS on 465/2465, STARTTLS required on 587/2587, and on any
    # other port STARTTLS whenever the server offers it: a password never
    # crosses in clear text because of the port number.
    use_tls = port in (465, 2465)
    start_tls = False if use_tls else (True if port in (587, 2587) else None)
    try:
        await aiosmtplib.send(msg, hostname=settings.alert_smtp_host, port=port,
                              username=settings.alert_smtp_username or None, password=settings.alert_smtp_password or None,
                              start_tls=start_tls, use_tls=use_tls, timeout=30)
    except Exception as e:
        return f"the alert email could not be sent: {type(e).__name__}"
    return ""


async def deliver(settings, ws, title: str, text: str, ok: bool = False) -> tuple[list[str], list[str]]:
    """Send one message to every channel the user turned on. Returns
    (channels reached, problems)."""
    prefs = ws.alert_settings()
    link = settings.base_url + "/mailbox" + ("" if ok else "#health")
    reached, problems = [], []
    if prefs["email_on"] and prefs["email"]:
        if settings.alert_email_ready:
            try:
                why = await _send_email(settings, prefs["email"], title, text, link)
            except Exception as e:   # one channel's failure never undoes the other's delivery
                why = f"the alert email could not be sent: {type(e).__name__}"
            (problems.append(why) if why else reached.append(f"email to {prefs['email']}"))
        else:
            problems.append("the site has no alert email server set up (BB_ALERT_SMTP_HOST)")
    if prefs["webhook"]:
        try:
            why = await _send_webhook(prefs["webhook"], title, text, link, ok, ws.user.get("email", ""))
        except Exception:
            why = "the webhook could not be reached"
        (problems.append(why) if why else reached.append("the webhook"))
    return reached, problems


def _real(problems: list[str]) -> list[str]:
    """Problems with a channel that is set up (not "nothing set up")."""
    return [p for p in problems if not p.startswith("the site has no")]


# ── once per problem ──

async def check_workspace(settings, ws, now: datetime | None = None) -> list[str]:
    """Compare what is wrong now with what was already said, send what is
    new, due again, or cleared. Returns a line per message sent."""
    now = now or _now()
    state = ws.alert_state()
    incidents: dict = state.setdefault("incidents", {})
    obs = await observe(ws)
    active, kinds, watched = obs["alerts"], obs.get("kinds", {}), obs.get("watched", set())
    sent: list[str] = []
    for key, text in active.items():
        kind = kinds.get(key, "")
        inc = incidents.get(key)
        if inc is None or inc.get("clearing"):
            inc = incidents[key] = {"since": now.isoformat(), "notified_at": "", "reminders": 0, "kind": kind}
        elif inc.get("kind", kind) != kind:
            # A different problem on the same mailbox (a stall that became a
            # refused sign-in): news now, not in a day.
            inc.update(kind=kind, notified_at="", reminders=0)
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
        for p in _real(problems):   # a channel never set up is not a failure
            logger.warning("Alert for %s not delivered: %s", ws.user.get("email"), p)
        state["last_problem"] = "; ".join(_real(problems))[:300]
    for key in [k for k in incidents if k not in active]:
        inc = incidents[key]
        if key.startswith("mail:") and key[5:] not in obs.get("mailboxes", set()):
            incidents.pop(key)                 # the account was removed: nothing to report
            continue
        if ("mail:*" if key.startswith("mail:") else key) not in watched:
            continue                           # not being watched (paused or stopped): neither news nor cleared
        if not inc.get("notified_at"):
            incidents.pop(key)                 # never sent, so no "working again" either
            continue
        reached, problems = await deliver(settings, ws, "The mailbox is connected again" if key.startswith("mail:")
                                          else "Your mailbox is working again",
                                          "The problem we wrote to you about has cleared: " + (inc.get("text") or "")[:300], ok=True)
        if reached or not _real(problems):
            incidents.pop(key)
            if reached:
                sent.append(f"{key} cleared: {', '.join(reached)}")
        else:
            inc["clearing"] = True             # tried again on the next pass
            state["last_problem"] = "; ".join(_real(problems))[:300]
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
