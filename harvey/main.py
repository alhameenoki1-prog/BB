"""Harvey's main heartbeat loop. Always Be Closing."""

import asyncio
import logging
import signal
import sys
import time as _time
from datetime import datetime, time, timedelta, timezone

import pytz

from harvey.brain import Brain
from harvey.config import ConfigError, load_config, load_env, HarveyConfig
from harvey.pipeline import run_profile_stage
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("harvey")

# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900

# Hard ceiling on one cycle's parallel agents. return_exceptions=True on
# gather() only isolates a task that raises — a task that hangs (a socket or
# lock that never resolves) blocks gather() forever, taking the whole
# heartbeat loop down silently with it. This is the only thing standing
# between one stuck agent and Harvey freezing for hours with no agent
# knowing it's dead — found in production after exactly that happened.
CYCLE_TIMEOUT_SECONDS = 600
# Research (website profiling) per heartbeat: how many businesses, and how
# long before it yields to the next cycle. It runs beside the writer and
# sender, never ahead of them.
PROFILE_PER_CYCLE = 200
PROFILE_BUDGET_SECONDS = 120
# While recipients are still waiting to be written and the writer has room,
# the next cycle follows this soon instead of a full heartbeat later.
BUSY_SLEEP_SECONDS = 45

# Cap on a single quiet-hours sleep chunk. See the quiet-hours block in
# heartbeat() for why this exists — a system sleep/wake cycle during one
# multi-hour asyncio.wait_for left Harvey silently stalled well past when
# quiet hours actually ended.
QUIET_HOURS_POLL_SECONDS = 300

# Phase-1 tasks that read replies; sending waits for all of them to succeed.
REPLY_READERS = ("handle_replies", "read_replies")

# Replies are read and mail is sent by one task at a time: the send loop and
# the heartbeat share the mailbox, and two drains at once would send twice.
_mail_lock = asyncio.Lock()


async def _mail_pass(handler, sender, brain, config, max_calls: int) -> str:
    """Read replies, then send what is due, in that order, under the lock.
    Returns "" when mail went out (or there was nothing to send) and a
    reason when sending was skipped."""
    if in_quiet_hours(config):
        return "quiet hours"
    # Sending needs no model, but reading replies may: a human reply is
    # classified and answered by the brain. Over the daily spend cap that
    # must not happen, and sending waits for replies to be handled first.
    try:
        within = await brain.is_within_budget(max_calls, max_percent=config.usage.max_daily_claude_percent,
                                              max_daily_cost_usd=config.usage.max_daily_cost_usd)
    except Exception:
        within = True
    if not within:
        if getattr(brain, "paused_reason", ""):
            # The provider said it is out of credit or quota: an hour's pause, not a day's.
            return f"{brain.paused_reason}; replies wait until it resets (it is checked again every hour), so sending waits too"
        return "the daily AI spend cap is reached; replies wait for tomorrow, so sending waits too"
    async with _mail_lock:
        read = await asyncio.wait_for(handler.run(backlog=False), timeout=CYCLE_TIMEOUT_SECONDS)
        if read is not True:
            return "replies couldn't all be read and handled"
        await asyncio.wait_for(sender.run(), timeout=CYCLE_TIMEOUT_SECONDS)
    return ""


SKIP_REASON_KEY = "sender_skip_reason"   # settings row the dashboard reads
SEND_LOOP_STOP_GRACE = 25   # seconds a stopping worker gives the send loop to finish its pass


async def _send_loop(handler, sender, brain, config, stop_event: asyncio.Event, every: int, max_calls: int, state=None):
    """Native providers with channels.email.send_every_seconds: drain the
    outbox every ``every`` seconds on its own clock, so sending never waits
    for research or writing. Stop-on-reply order is kept: every pass reads
    replies first and sends nothing if that failed."""
    logger.info(f"Sender: draining the outbox every {every}s, independently of the heartbeat.")
    last_skip = None   # the first pass always records its state: a stale reason from an earlier worker must not linger
    while not stop_event.is_set():
        try:
            skipped = await _mail_pass(handler, sender, brain, config, max_calls)
            if skipped and skipped != last_skip:
                logger.info(f"Sender: not sending right now: {skipped}.")
            if skipped != last_skip and state is not None:
                try:
                    await state.set_setting(SKIP_REASON_KEY, skipped)
                except Exception:
                    pass
            last_skip = skipped
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error("Sender: a send pass timed out; trying again next time.")
        except Exception as e:
            logger.error(f"Sender: send pass failed: {e}")
        from harvey import wake
        if await wake.sleep(every, stop_event, "send", show=False) == "stop":
            break


def in_quiet_hours(config: HarveyConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    if not getattr(qh, "enabled", True):
        return False
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now < end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now < end


def seconds_until_quiet_hours_end(config: HarveyConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: HarveyConfig,
    summary: dict | None = None,
) -> str:
    """Decide what Harvey should do next based on current state.

    Uses deterministic priority rules (handle_replies > send_campaign >
    write_campaign > prospect > idle) instead of burning a Claude call on a
    decision that is fully derivable from pipeline counts. This saves budget
    every cycle and removes a fragile LLM string-parsing step.
    """
    if summary is None:
        summary = await state.get_state_summary()

    prospects = summary.get("prospects") or {}
    new_prospects = prospects.get("new", 0) if isinstance(prospects, dict) else 0
    draft_campaigns = summary.get("draft_campaigns", 0) or 0
    open_conversations = summary.get("open_conversations", 0) or 0

    # Native mail providers read the mailbox every cycle regardless of the
    # action chosen here (the heartbeat adds the handler alongside whatever
    # runs), so an open conversation must not stop the writer for everyone
    # else: the first warm reply would otherwise end new outreach for good.
    native = config.channels.email.provider in NATIVE_PROVIDERS
    if open_conversations > 0 and not native:
        action, reason = "handle_replies", f"{open_conversations} open conversation(s) waiting"
    elif draft_campaigns > 0 and not native:
        action, reason = "send_campaign", f"{draft_campaigns} draft campaign(s) ready to deploy"
    elif new_prospects > 0:
        action, reason = "write_campaign", f"{new_prospects} new prospect(s) with no drafts ready"
    elif new_prospects < 20 and config.icp.auto_prospect:
        action, reason = "prospect", f"only {new_prospects} new prospect(s); pipeline needs leads"
    else:
        action, reason = "idle", "pipeline is healthy; running analysis"

    logger.info(f"Decision: {action} ({reason})")
    return action


async def _bounded_profile(state, limit: int = PROFILE_PER_CYCLE):
    """Research with its own clock. Slow websites must never run the whole
    cycle into the timeout and take the writer down with them: past the
    budget this stage stops and the rest of the backlog waits for the next
    heartbeat. What was read before the cut-off is already saved."""
    try:
        return await asyncio.wait_for(run_profile_stage(state, limit=limit), timeout=PROFILE_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.info(f"profile: stopped after {PROFILE_BUDGET_SECONDS}s this cycle; the rest waits for the next heartbeat.")
        return None


async def _bounded_listcheck(state, env):
    """The uploaded list's domain and verifier checks, under their own time
    budget, beside the writer rather than in front of it."""
    from harvey import listcheck
    try:
        return await listcheck.run_all(state, env)
    except Exception as e:
        logger.debug(f"listcheck: skipped this cycle: {e}")
        return None


async def _more_to_write(state, writer, brain) -> bool:
    """Should the next cycle follow quickly? Yes when deliverable recipients
    still have no email, the writer has room for more, and the brain is
    usable."""
    if getattr(brain, "paused_reason", ""):
        return False
    try:
        waiting = await state.get_prospects_by_status("new", limit=1, email_statuses=("verified", "provided", "risky"))
        if not waiting:
            return False
        room, _ahead, _backlog = await writer.room()
        return room > 0
    except Exception:
        return False


async def _locked(coro):
    async with _mail_lock:
        return await coro


async def _run_agents(tasks: list) -> dict:
    """Run (name, coroutine) pairs in parallel; return {name: outcome}.

    Errors are caught per task so one bad agent never takes down the cycle,
    and the whole batch is bounded so one HUNG agent (no exception, just
    never returns) can't either. A task's outcome is what it returned, or
    the exception it raised; a task cut off by the bound has no outcome.
    """
    if not tasks:
        return {}
    if len(tasks) > 1:
        logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[t[1] for t in tasks], return_exceptions=True),
            timeout=CYCLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Cycle timed out after {CYCLE_TIMEOUT_SECONDS}s with "
            f"{[t[0] for t in tasks]} still running — cancelled, "
            "moving on to the next heartbeat instead of hanging forever."
        )
        results = []
    outcomes = {}
    for (name, _), result in zip(tasks, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.error(f"Agent {name} failed: {result}", exc_info=result)
        outcomes[name] = result
    return outcomes


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event, state: str = "sleeping",
                               detail: str = "") -> bool:
    """Sleep up to `seconds`, waking at once on shutdown or when someone
    presses Wake up now (harvey/wake.py), and by the wall clock, so a
    machine that slept in between does not stretch it.

    Returns True if a shutdown was requested during the sleep.
    """
    from harvey import wake
    return await wake.sleep(seconds, stop_event, "heartbeat", state=state, detail=detail) == "stop"


# Why the loop ended, for the last log line: set by the signal handler
# ("SIGTERM from the dashboard or the system"), else worked out below.
_stop_reason = {"text": ""}


async def heartbeat(stop_event: asyncio.Event | None = None):
    """Harvey's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()
    _stop_reason["text"] = ""

    logger.info("=" * 60)
    logger.info("Harvey is online. Always Be Closing.")
    logger.info("=" * 60)

    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start — configuration error:\n{e}")
        return
    env = load_env()
    state = StateManager()
    brain = Brain(state, config.brain)
    logger.info(
        f"Thinking with {brain.provider_label}"
        + (f" ({brain.model})" if brain.model else "")
        + ". Change brain.provider in harvey.yaml to switch."
    )

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from harvey.agents.scout import Scout
    from harvey.agents.writer import Writer
    from harvey.agents.sender import Sender
    from harvey.agents.handler import Handler
    from harvey.agents.analyst import Analyst

    scout = Scout(brain, state, config, env)
    writer = Writer(brain, state, config, env)
    sender = Sender(brain, state, config, env)
    handler = Handler(brain, state, config, env)
    analyst = Analyst(state)

    interval = config.usage.heartbeat_interval_minutes * 60
    # The daily call counter stands in for the ChatGPT plan's quota when it
    # cannot be read (Codex). An API key is bounded by usage.max_daily_cost_usd
    # (Claude) or the provider's own limits: 160 calls a day would stop a
    # 10,000-row list after the first few batches.
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1) if brain.provider == "codex" else 10 ** 9
    consecutive_errors = 0

    # Native providers can send on their own clock (see _send_loop); the
    # heartbeat then only researches, writes and reads replies.
    send_every = int(getattr(config.channels.email, "send_every_seconds", 0) or 0)
    send_task = None
    if sender.is_native and send_every > 0:
        send_task = asyncio.create_task(_send_loop(handler, sender, brain, config, stop_event, send_every, max_calls, state))
    # The beat: what the worker is doing, for the dashboard, and how a press
    # of Wake up now reaches it (harvey/wake.py).
    from harvey import wake
    wake.set_state("working")
    beat_task = asyncio.create_task(wake.beat_loop(stop_event))

    try:
        await _heartbeat_cycles(stop_event, config, state, brain, scout, writer, sender, handler, analyst,
                                interval, max_calls, consecutive_errors, send_task is not None)
    finally:
        beat_task.cancel()
        wake.stop_beat()
        if send_task is not None:
            # The loop ends on its own after the current pass (stop_event is
            # set); give it time to finish an email in flight before cancelling.
            try:
                await asyncio.wait_for(asyncio.shield(send_task), timeout=SEND_LOOP_STOP_GRACE)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                send_task.cancel()
                try:
                    await send_task
                except (asyncio.CancelledError, Exception):
                    pass


async def _ledger_checkin(sender, state=None):
    """Tell the account's shared ledger this campaign is still working on
    it. Sends check in by themselves; a night of quiet hours (longer than
    the ledger's six hours) would otherwise read as a stopped campaign, and
    the others would take its share before it wakes. Only a membership that
    is still active is extended, and not while this campaign's sending is
    paused or standing down after a spam block: one that cannot send in
    the morning must not hold a part of the limit all night."""
    led = getattr(sender, "ledger", None)
    if led is None:
        return
    try:
        if state is not None:
            from harvey.agents.sender import COOLOFF_KEY, KILL_SWITCH_KEY
            if await state.get_setting(KILL_SWITCH_KEY):
                return
            until = await state.get_setting(COOLOFF_KEY)
            if until:
                try:
                    if datetime.fromisoformat(until) > datetime.now(timezone.utc).replace(tzinfo=None):
                        return
                except ValueError:
                    pass
        await asyncio.to_thread(led.refresh, sender.ledger_account, sender.ledger_campaign)
    except Exception as e:
        logger.debug(f"ledger check-in failed: {e}")


async def _heartbeat_cycles(stop_event, config, state, brain, scout, writer, sender, handler, analyst,
                            interval, max_calls, consecutive_errors, has_send_loop):
    last_wake_seen = 0.0
    while not stop_event.is_set():
        try:
            # 1. Check quiet hours
            if in_quiet_hours(config):
                sleep_for = seconds_until_quiet_hours_end(config)
                logger.info(f"Quiet hours until {config.usage.quiet_hours.end} {config.usage.quiet_hours.timezone} "
                            f"({sleep_for // 60} minutes; checked every {QUIET_HOURS_POLL_SECONDS // 60} minutes). "
                            "Untick \"Only send during these hours\" on the profile to send around the clock.")
                # Sleep in short polled chunks rather than one multi-hour
                # wait: found in production that a system sleep/wake cycle
                # during a long asyncio.wait_for left the process silently
                # stalled for the rest of the quiet-hours window (11+ hours
                # in one case) instead of resuming once the Mac woke up.
                # Re-checking wall-clock quiet hours every few minutes
                # bounds the worst case to QUIET_HOURS_POLL_SECONDS instead
                # of however long the missed timer would have run.
                shutting_down = False
                from harvey import wake
                wake.set_state("quiet", _time.time() + sleep_for)
                while in_quiet_hours(config):
                    await _ledger_checkin(sender, state)   # still a campaign on the account: its share waits for the morning
                    how = await wake.sleep(QUIET_HOURS_POLL_SECONDS, stop_event, "heartbeat", show=False)
                    if how == "stop":
                        shutting_down = True
                        break
                    if how == "wake" and in_quiet_hours(config):
                        logger.info("Woken up during quiet hours: nothing is sent until they end "
                                    f"({config.usage.quiet_hours.end} {config.usage.quiet_hours.timezone}).")
                if shutting_down:
                    break
                wake.set_state("working")
                continue

            # Woken up by hand since the last look: a provider problem or a
            # usage pause is checked again now, not after its own wait.
            from harvey import wake
            if wake.last_woken() > last_wake_seen:
                last_wake_seen = wake.last_woken()
                try:
                    await brain.recheck()
                except Exception as e:
                    logger.debug(f"brain re-check after a wake failed: {e}")

            # 2. Is the brain usable at all? (Codex: CLI version, isolation,
            # login, account and model checked without a prompt.) If not, do nothing
            # this cycle — no replies consumed, nothing written or sent —
            # and say why, rather than running agents that would each fail.
            problem = await brain.preflight()
            if problem:
                logger.error(
                    f"Harvey is paused: {problem} Nothing was sent or changed. "
                    f"Re-checking in {config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event, "paused", str(problem)[:200]):
                    break
                continue

            # 3. Check usage budget: Claude's daily spend cap, Codex's real
            # plan quota when readable, else Harvey's own call counter.
            if not await brain.is_within_budget(
                max_calls, max_percent=config.usage.max_daily_claude_percent,
                max_daily_cost_usd=config.usage.max_daily_cost_usd,
            ):
                cap = (f"${config.usage.max_daily_cost_usd:.2f}/day"
                       if brain.provider == "claude" and config.usage.max_daily_cost_usd
                       else f"{config.usage.max_daily_claude_percent}% of quota")
                if brain.paused_reason:
                    # The provider is out of credit or quota (not the daily cap).
                    logger.info(f"{brain.paused_reason}. Sleeping 1h, then re-checking (Wake up now checks at once).")
                else:
                    logger.info(
                        f"{brain.provider_label} usage limit reached "
                        f"({cap} or {max_calls} calls). Sleeping 1h, then re-checking."
                    )
                await _ledger_checkin(sender, state)   # still this account's campaign: its part waits for the reset
                if await _interruptible_sleep(3600, stop_event, "paused", f"the AI usage limit ({cap}) is reached"):
                    break
                continue

            # 4. Decide what to do
            logger.info("Checking pipeline state...")
            summary = await state.get_state_summary()
            action = await decide_next_action(brain, state, config, summary=summary)

            # 5. Execute — run independent agents in parallel where possible
            # Handler is always safe to run alongside other agents
            tasks = []
            has_open_convos = summary.get("open_conversations", 0) > 0

            if action == "handle_replies":
                tasks.append(("handle_replies", handler.run()))
            elif action == "prospect":
                tasks.append(("prospect", scout.run()))
                # Also handle replies in parallel if needed
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
                # Analyst is cheap (no Claude calls) — keep analytics fresh
                tasks.append(("analyze", analyst.run()))
            elif action == "write_campaign":
                tasks.append(("write_campaign", writer.run()))
                if has_open_convos:
                    tasks.append(("handle_replies", handler.run()))
            elif action == "idle":
                tasks.append(("analyze", analyst.run()))
            # Writing never waits its turn behind replies or sending: while
            # recipients are waiting for an email, the writer runs every
            # cycle beside whatever else the cycle does (it keeps its own
            # ceiling, so it costs nothing when the outbox is full).
            prospects = summary.get("prospects") or {}
            if (isinstance(prospects, dict) and prospects.get("new", 0) > 0
                    and not any(n == "write_campaign" for n, _ in tasks) and sender.is_native):
                tasks.append(("write_campaign", writer.run()))

            # Sending happens in a second phase, after every agent above has
            # finished: the handler's stop-on-reply must cancel follow-ups to
            # people who just replied before anything is sent. Native mail
            # providers drain the outbox every cycle, so their mailbox is
            # read every cycle too (handle_replies above only runs when a
            # conversation is already open).
            send_tasks = []
            if action == "send_campaign":
                send_tasks.append(("send_campaign", sender.run))
            elif sender.is_native and not has_send_loop:
                send_tasks.append(("send_outbox", sender.run))
            if sender.is_native and not any(n == "handle_replies" for n, _ in tasks):
                # Also with the send loop: this is the pass that sweeps older
                # unanswered inbox mail (backlog); the loop's passes skip it.
                tasks.append(("read_replies", handler.run()))
            # The send loop owns the mailbox: anything here that reads replies
            # or sends takes the same lock so the two never overlap.
            tasks = [(n, _locked(c) if n in REPLY_READERS else c) for n, c in tasks]

            # Profiling rides along every cycle. It is three HTTP requests per
            # business with no model call, so it costs nothing against the
            # Claude budget the rest of this loop is rationing — and it is what
            # turns a name and a domain into something worth writing about.
            if summary.get("unprofiled", 0):
                tasks.append(("profile", _bounded_profile(state)))
            # So does the uploaded list's domain check (and the verifier when
            # a key is set): beside the writer, never in front of it.
            if sender.is_native and (summary.get("prospects") or {}).get("new", 0) > 0:
                tasks.append(("listcheck", _bounded_listcheck(state, getattr(writer, "env", None))))

            outcomes = await _run_agents(tasks)

            # Phase 2: send — only after every reply-reading task this cycle
            # finished reading and handling replies (it returns True; a
            # failure, exception or timeout doesn't), and only if the brain
            # didn't stop being usable meanwhile. Otherwise a reply may be
            # unread, so its stop-on-reply hasn't run: queued emails wait.
            if send_tasks:
                unread = [n for n, _ in tasks
                          if n in REPLY_READERS and outcomes.get(n) is not True]
                paused = brain.paused_reason
                if paused or unread:
                    reason = paused or "replies couldn't all be read and handled."
                    logger.error(
                        f"Not sending this cycle: {reason} Queued emails wait "
                        "until replies can be read again."
                    )
                else:
                    await _run_agents([(name, _locked(run())) for name, run in send_tasks])

            # 6. Log the action (best-effort; never kills the loop)
            try:
                await state.log_action(action_type=action, agent="main")
            except Exception as e:
                logger.warning(f"Failed to log action '{action}': {e}")

            consecutive_errors = 0

            # 6b. Is the mailbox still sending? A login check on its own
            # clock, and a look at whether due emails actually leave.
            if sender.is_native and getattr(sender, "provider", None) is not None:
                try:
                    from harvey import health
                    await health.run_check(state, sender.provider, config, quiet=in_quiet_hours(config))
                except Exception as e:
                    logger.debug(f"Mailbox health check skipped: {e}")

            # 7. Sleep until next heartbeat: a short pause while there is
            # still writing to do, the full heartbeat otherwise.
            sleep_for = interval
            if sender.is_native and interval > BUSY_SLEEP_SECONDS and await _more_to_write(state, writer, brain):
                sleep_for = BUSY_SLEEP_SECONDS
                logger.info(f"Cycle complete. Recipients are still waiting for an email: next cycle in {BUSY_SLEEP_SECONDS} seconds.")
            else:
                logger.info(
                    f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
                )
            if await _interruptible_sleep(sleep_for, stop_event):
                break

        except (KeyboardInterrupt, asyncio.CancelledError):
            _stop_reason["text"] = _stop_reason["text"] or "interrupted from the keyboard"
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(
                ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                ERROR_BACKOFF_CAP,
            )
            logger.error(
                f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                exc_info=True,
            )
            logger.info(f"Recovering... sleeping {backoff}s before retry.")
            if await _interruptible_sleep(backoff, stop_event, "backoff", str(e)[:200]):
                break

    reason = _stop_reason["text"] or (
        "asked to stop" if stop_event.is_set() else "the loop ended")
    logger.info(
        f"Harvey stopped: {reason}. This is a normal stop, not an error — nothing "
        "is lost. Start it again and it carries on where it left off."
    )


def _needs_setup() -> bool:
    """Check if Harvey needs first-time setup."""
    import os
    from harvey.paths import ENV_FILE
    from harvey.config import _find_config_file, ConfigFileNotFoundError

    # Credentials come from .env, or straight from the environment when a
    # supervisor (the hosted web app) starts this process with them set.
    has_env = ENV_FILE.exists() or any(
        os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GMAIL_CLIENT_ID",
                                    "SMTP_HOST", "M365_CLIENT_ID", "INSTANTLY_API_KEY"))
    if not has_env:
        return True

    # harvey.local.yaml wins over the tracked harvey.yaml template, same
    # resolution order the real config loader uses (see config.py).
    try:
        config_file = _find_config_file()
    except ConfigFileNotFoundError:
        return True

    try:
        with open(config_file) as f:
            import yaml
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            return True
        company = (config.get("persona") or {}).get("company", "")
        if company in ("Your Company", ""):
            return True
    except Exception:
        return True

    return False


async def _run_with_signals():
    """Run the heartbeat with SIGINT/SIGTERM wired to a graceful shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        source = {
            "SIGTERM": "a stop request (the dashboard's Stop button, a settings change "
                       "that needs a restart, or the system)",
            "SIGINT": "Ctrl+C in the terminal",
            "SIGHUP": "the terminal closing",
        }.get(sig_name, sig_name)
        _stop_reason["text"] = f"{sig_name}, {source}"
        logger.info(f"Received {sig_name} ({source}). Finishing current work, then stopping...")
        stop_event.set()

    # When the site finds this worker stuck it asks, before restarting it,
    # where every thread was (written to the log): the cause can be read later.
    if hasattr(signal, "SIGUSR2"):
        try:
            import faulthandler
            faulthandler.register(signal.SIGUSR2, all_threads=True)
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback
            signal.signal(sig, lambda s, f: _request_shutdown(signal.Signals(s).name))

    await heartbeat(stop_event)


def main():
    """Entry point."""
    # Check for first-time setup
    if _needs_setup():
        print("\n  First time running Harvey? Let's get you set up.\n")
        from harvey.setup import run_setup
        asyncio.run(run_setup())
        return

    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
