"""Is the mailbox still sending, and still delivering?

A connected mailbox can stop quietly: the password changes, the provider
suspends the account, the SMTP server starts refusing logins, or every
send fails and nothing leaves. The worker checks on a clock (login to
SMTP and IMAP, as ``harvey status --check`` does) and watches delivery
(the last email that went out against the emails that are due), and
writes one plain sentence into settings that the dashboard and the
Mailbox page show in red, and the log repeats, until it clears.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("harvey.health")

CHECK_EVERY_MINUTES = 30      # how often the login check runs
STALL_MINUTES = 90            # due emails, nothing delivered for this long: an alert
CHECK_TIMEOUT_SECONDS = 60

CHECK_AT_KEY = "mailbox_check_at"
CHECK_OK_KEY = "mailbox_check_ok"
CHECK_ERROR_KEY = "mailbox_check_error"
LAST_SENT_KEY = "mailbox_last_sent_at"
ALERT_KEY = "mailbox_alert"
ALERT_SINCE_KEY = "mailbox_alert_since"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


def _when(dt: datetime | None) -> str:
    return dt.strftime("%H:%M UTC on %d %b") if dt else "an unknown time"


async def run_check(state, provider, config, *, quiet: bool = False, force: bool = False) -> dict:
    """One health pass. Logs in to the mailbox when the last check is old
    enough (or ``force``), then judges delivery. Returns the current
    picture: {ok, checked_at, error, last_sent_at, alert}."""
    out = {"ok": True, "checked_at": "", "error": "", "last_sent_at": "", "alert": ""}
    test = getattr(provider, "test_connection", None)
    if test is None or not getattr(provider, "is_configured", lambda: True)():
        return out
    now = _now()
    last_check = _parse(await state.get_setting(CHECK_AT_KEY))
    ok_setting = await state.get_setting(CHECK_OK_KEY)
    error = await state.get_setting(CHECK_ERROR_KEY) or ""
    ok = ok_setting != "0"
    if force or last_check is None or now - last_check >= timedelta(minutes=CHECK_EVERY_MINUTES):
        try:
            ok, msg = await asyncio.wait_for(test(), timeout=CHECK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            ok, msg = False, f"no answer from the mail server within {CHECK_TIMEOUT_SECONDS} seconds"
        except Exception as e:  # the check must never take the worker down
            ok, msg = False, str(e)[:200]
        error = "" if ok else (msg or "the mailbox refused the connection")[:300]
        await state.set_setting(CHECK_AT_KEY, now.isoformat())
        await state.set_setting(CHECK_OK_KEY, "1" if ok else "0")
        await state.set_setting(CHECK_ERROR_KEY, error)
        last_check = now
    out.update(ok=ok, checked_at=last_check.isoformat() if last_check else "", error=error)

    alert = ""
    if not ok:
        alert = f"The mailbox check at {_when(last_check)} failed: {error}. Nothing is sent until it works again."
    elif not quiet:
        alert = await _stall_alert(state, now)
    out["last_sent_at"] = await state.get_setting(LAST_SENT_KEY) or ""
    previous = await state.get_setting(ALERT_KEY) or ""
    if alert != previous:
        await state.set_setting(ALERT_KEY, alert)
        await state.set_setting(ALERT_SINCE_KEY, now.isoformat() if alert else "")
        if alert:
            logger.error(f"Mailbox: {alert}")
        elif previous:
            logger.info("Mailbox: the earlier alert has cleared; sending and delivery look normal again.")
    elif alert:
        logger.warning(f"Mailbox: {alert}")
    out["alert"] = alert
    return out


async def _stall_alert(state, now: datetime) -> str:
    """Emails are due and approved, nothing has left for a long while,
    and no deliberate pause explains it: say so."""
    try:
        due = [i for i in await state.get_outbox(status="approved", limit=500)
               if (_parse(i.get("send_at")) or now) <= now]
    except Exception:
        return ""
    if not due:
        return ""
    last_sent = _parse(await state.get_setting(LAST_SENT_KEY))
    oldest_due = min((_parse(i.get("send_at")) or now) for i in due)
    since = max(filter(None, [last_sent, oldest_due])) if (last_sent or oldest_due) else now
    if now - since < timedelta(minutes=STALL_MINUTES):
        return ""
    # A pause the operator chose, or one the sender took on purpose, is not a stall.
    if await state.get_setting("sending_paused"):
        return ""
    cooloff = _parse(await state.get_setting("sender_cooloff_until"))
    if cooloff and cooloff > now:
        return ""
    skip = await state.get_setting("sender_skip_reason") or ""
    if "quiet" in skip.lower() or "warm" in skip.lower() or "daily" in skip.lower() or "cap" in skip.lower():
        return ""
    reason = f" The sender's last reason: {skip}." if skip else ""
    return (f"Nothing has been delivered since {_when(last_sent) if last_sent else 'the worker started'} although "
            f"{len(due)} approved email{'s are' if len(due) != 1 else ' is'} due.{reason} Check the Mailbox page and the worker log.")


async def picture(state) -> dict:
    """What the pages show: the last check, its result, the last delivery
    and the alert, all from settings (no mailbox call)."""
    return {
        "ok": (await state.get_setting(CHECK_OK_KEY)) != "0",
        "checked_at": await state.get_setting(CHECK_AT_KEY) or "",
        "error": await state.get_setting(CHECK_ERROR_KEY) or "",
        "last_sent_at": await state.get_setting(LAST_SENT_KEY) or "",
        "alert": await state.get_setting(ALERT_KEY) or "",
        "alert_since": await state.get_setting(ALERT_SINCE_KEY) or "",
    }
