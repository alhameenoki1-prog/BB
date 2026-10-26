"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import threading
import logging
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

from harvey import names
from harvey.brain import Brain
from harvey.config import HarveyConfig, EnvConfig
from harvey.gate import pre_send_check
from harvey.integrations.instantly import InstantlyClient
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, bounce_kind, bounce_reason, get_mail_provider
from harvey.integrations.precleared import PreclearedClient
from harvey.spamcheck import REVIEW as SPAM_REVIEW, describe, spam_risk
from harvey.state import StateManager

logger = logging.getLogger("harvey.sender")

# Merge fields a template may carry; a template rewrite must keep exactly these.
_MERGE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
# A template whose rewrite still fails for a copy gets at most this many
# per-copy rewrites in one staging pass; the rest wait under Needs review.
PER_COPY_REWRITES = 3

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
REPLIES_PER_PASS = 200      # replies have no limit; this only bounds one pass (the next pass takes the rest)
PASS_SECONDS = 420          # a pass takes nothing new after this, well inside the cycle's 600 s; the rest goes next pass
# Warm-up ramp for a mailbox new to cold outreach: this many on day one,
# then WARMUP_STEP more each day until the configured daily cap (or, with
# no cap, until the ramp no longer binds). Mailbox providers watch a
# sudden jump in volume more closely than anything else.
WARMUP_START = 20
WARMUP_STEP = 10


def _attachment_paths(raw) -> list:
    """Outbox attachments_json → existing files under HARVEY_HOME/attachments.
    Names only, never paths, so a row can't point outside that folder."""
    import json as _json
    from pathlib import Path as _Path

    from harvey.paths import ATTACHMENTS_DIR

    try:
        names = _json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    paths = []
    for name in names if isinstance(names, list) else []:
        name = _Path(str(name)).name
        candidate = ATTACHMENTS_DIR / name
        if name and candidate.is_file():
            paths.append(candidate)
        else:
            logger.warning(f"Sender: attachment {name!r} is missing; sending without it.")
    return paths
SEND_JITTER_SECONDS = (4, 15)
# A temporary refusal (throttling, a 4xx, the network) puts the email back
# in the queue for later, a few times, before it counts as failed.
MAX_SEND_RETRIES = 5
RETRY_DELAY_SECONDS = 600


def _retry_count(error: str) -> int:
    m = re.match(r"retry (\d+)/", error or "")
    return int(m.group(1)) if m else 0

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"
# Set by the handler when a receiving server (or our own provider) blocks
# an email as spam: an ISO time until which nothing is sent, so the
# mailbox's reputation is not burned further by the next batch.
COOLOFF_KEY = "sender_cooloff_until"
COOLOFF_REASON_KEY = "sender_cooloff_reason"
SKIP_REASON_KEY = "sender_skip_reason"
LAST_SENT_KEY = "mailbox_last_sent_at"     # when the mailbox last delivered an email

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
# "provided" = supplied by the operator (web upload): sends like verified.
SENDABLE_EMAIL_STATUSES = {"verified", "provided"}
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "provided", "risky"}



def daily_allowance(first_sent_at, now, max_daily: int, ramp: bool) -> int:
    """How many emails may go out today. 0 means no limit.

    With the warm-up ramp on, a mailbox starts at WARMUP_START a day and
    gains WARMUP_STEP a day, counted from its first send. The ramp never
    raises the configured cap; with no cap it stops binding once it has
    climbed past a few hundred a day (by then the mailbox has a history).
    """
    if not ramp:
        return max(0, int(max_daily))
    if not first_sent_at:
        day = 0
    else:
        try:
            first = datetime.fromisoformat(str(first_sent_at).replace("Z", ""))
        except ValueError:
            first = now
        if first.tzinfo is not None:
            first = first.astimezone(timezone.utc).replace(tzinfo=None)
        day = max(0, (now.date() - first.date()).days)
    ramped = WARMUP_START + WARMUP_STEP * day
    if max_daily > 0:
        return min(max_daily, ramped)
    return 0 if ramped >= 500 else ramped


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.instantly = InstantlyClient(env.instantly_api_key)
        self.provider = get_mail_provider(config, env)
        # Optional per-recipient compliance check; None unless enabled.
        self.precleared = PreclearedClient.from_config(config, env)
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True
        # One of several campaigns on the same account (started by the site):
        # every send takes a slot of the account's shared daily limit.
        from harvey import ledger as ledger_mod
        self.ledger, self.ledger_account, self.ledger_campaign, self.ledger_label = ledger_mod.from_env()

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self, first_only: bool = False):
        """Deploy campaigns — natively through the outbox, or via Instantly.
        ``first_only``: send only first emails (step 1), the ones nobody can
        have replied to yet — what is safe while replies cannot be read."""
        self.auth_refused = False   # set when the mailbox refuses the login this pass
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native(first_only=first_only)
            return
        if first_only:
            # Instantly runs the whole sequence once a campaign is deployed:
            # there is no first-emails-only, so it waits as before.
            logger.info("Sender: replies can't be read right now; campaigns are deployed once they can.")
            return

        logger.info("Sender: Checking for campaigns to deploy...")
        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": step.body.strip(),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self, first_only: bool = False):
        """Stage new campaigns into the outbox, then drain what's due."""
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `harvey sending resume` once the cause is fixed."
            )
            return

        autopilot = not getattr(self.config.channels.email, "require_approval", True)
        waiting = 0
        for campaign in await self.state.get_campaigns_by_status("draft"):
            if first_only and autopilot and not campaign.name.startswith("your message"):
                # Staging an AI-written campaign on autopilot runs the spam
                # check, whose fix is a model call; with the model out of
                # reach a failing copy would be parked for a person. It
                # stays a draft and is staged (and fixed) once the model is back.
                waiting += 1
                continue
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")
        if waiting:
            logger.info(f"Sender: {waiting} AI-written campaign(s) wait for the model before they are staged; "
                        "your own message and emails already in the outbox still go.")

        await self._drain_due(first_only=first_only)

    def _render(self, text: str, prospect, *, body: bool = False) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers.
        A body also gets its greeting line fixed to what is known about the
        recipient ("Hi Jane," / "Hello team," / "Hi,")."""
        if body:
            text = names.strip_greeting(text)
        first = (prospect.first_name or "").strip()
        company = names.company_display_name(
            prospect.company, (prospect.email or "").rsplit("@", 1)[-1]) or "your team"
        replacements = {
            "first_name": first or ("your team" if names.is_role_address(prospect.email) else "there"),
            "last_name": prospect.last_name,
            "company": company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        for key, value in replacements.items():
            text = re.sub(r"\{\{\s*" + key + r"\s*\}\}", lambda _m, v=(value or ""): v, text)
        text = text.strip()
        if body:
            outreach = getattr(self.config, "outreach", None)
            text = names.ensure_greeting(text, first, prospect.email, prospect.company,
                                         getattr(outreach, "greeting", "") or "Hi")
        return text

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        held = 0
        staged = 0
        skipped = 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seen_emails: set[str] = set()
        # Slots already filled (a personal first email the writer drafted):
        # the template copy for them would be thrown away, so it is neither
        # rendered nor checked, and costs no model call.
        try:
            taken = await self.state.outbox_slots(campaign.id)
        except Exception:
            taken = set()
        # One rewrite per template wording, shared by every copy of it (a
        # campaign of one person is simply its own copy).
        fit_cache: dict | None = {} if len(campaign.prospect_ids) > 1 else None

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            if self.ledger is not None and await self._led(self.ledger.is_suppressed, email):
                # They asked another of this user's campaigns never to be contacted.
                await self.state.update_prospect_status(prospect.id, "opted_out")
                continue
            seen_emails.add(email)

            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                if (prospect.id, int(step.step)) in taken:
                    continue
                # A stable pick among the wordings the writer produced, so a
                # segment does not send one identical body a hundred times.
                raw_subject, raw_body = step.variant(prospect.id)
                subject = self._render(raw_subject, prospect)
                body = self._render(raw_body, prospect, body=True)
                status, note = initial_status, ""
                if status == "approved" and not campaign.name.startswith("your message"):
                    # Autopilot never sends what a filter would junk; the
                    # operator's own words are theirs to send. What fails is
                    # rewritten here first, and only what still fails waits
                    # for a person, without holding anything else up.
                    subject, body, reasons = await self._fit_for_sending(
                        subject, body, prospect, campaign, step,
                        template=(raw_subject, raw_body) if fit_cache is not None else None, cache=fit_cache)
                    if reasons:
                        status = "pending_review"
                        note = ("Needs review: even after a rewrite a spam filter would likely flag "
                                + describe(reasons) + ". Edit it, approve it as is, or reject it; everything else keeps sending.")
                        held += 1
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=subject,
                    body=body,
                    send_at=send_at.isoformat(),
                    status=status,
                    provider=self.provider.name,
                )
                if item_id:
                    staged += 1
                    if note:
                        await self.state.update_outbox_item(item_id, error=note)
            await self.state.update_prospect_status(prospect.id, "queued")

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if held:
            logger.warning(
                f"Sender: {held} email(s) from '{campaign.name}' read like spam to a filter "
                "and wait in the Outbox for your approval instead of going out.")
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    async def _drain_due(self, first_only: bool = False):
        """Send due, approved outbox items through the provider. With
        ``first_only``, follow-ups (step 2 and on) are left for a pass that
        can read replies first; first emails and answers still go."""
        # (allowance and per-cycle size come from channels.email)
        if not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return

        email_cfg = self.config.channels.email
        if await self._in_cooloff():
            return
        max_daily = email_cfg.max_daily_sends
        # Every limit below is for first emails and follow-ups. Replies (an
        # answer to someone who wrote) have none: they go whatever the day's
        # count, the warm-up or the account's share, and do not use them up.
        sent_today = await self.state.count_outbox_sent_today(kind="sequence")
        per_cycle = int(getattr(email_cfg, "sends_per_cycle", MAX_SENDS_PER_CYCLE) or
                        MAX_SENDS_PER_CYCLE)
        ceiling = int(getattr(email_cfg, "mailbox_daily_ceiling", 0) or 0)
        if ceiling:
            max_daily = min(max_daily, ceiling) if max_daily else ceiling
        allowance = daily_allowance(
            await self.state.first_outbox_sent_at(),
            datetime.now(timezone.utc).replace(tzinfo=None),
            max_daily, bool(getattr(email_cfg, "warmup_ramp", False)),
        )
        # 0 means no daily limit: the provider's own ceiling is the only one.
        budget = per_cycle if allowance == 0 else min(per_cycle, allowance - sent_today)
        if budget <= 0:
            budget = 0
            logger.info(
                f"Sender: daily send allowance reached ({sent_today}/{allowance}"
                f"{' warm-up' if allowance != max_daily else ''}); only replies go.")
        # The mailbox's own maximum counts everything, replies included: past
        # it the provider refuses mail. First emails stop a tenth short of it,
        # so that last tenth is always there for replies.
        reply_room = REPLIES_PER_PASS
        if ceiling:
            everything = await self.state.count_outbox_sent_today()
            reply_room = min(REPLIES_PER_PASS, max(0, ceiling - everything))
            if budget and everything >= ceiling - ceiling // 10:
                budget = 0
                logger.info(f"Sender: {everything} sent today against the mailbox's own maximum of {ceiling}; "
                            f"the last {ceiling // 10} are kept for replies.")
            budget = min(budget, max(0, ceiling - ceiling // 10 - everything))
            if not reply_room:
                logger.warning(f"Sender: the mailbox's own daily maximum of {ceiling} is reached, replies included; "
                               "everything waits for tomorrow (the provider would refuse it).")

        if self.ledger is not None and budget > 0:
            # Other campaigns send from this account too: this one keeps to
            # its part of the account's daily limit.
            try:
                pic = await self._led(self.ledger.allowance, self.ledger_account, self.ledger_campaign, "outreach", self.ledger_label)
            except Exception as e:
                logger.warning(f"Sender: the shared send ledger could not be read ({e}); first emails wait for the next pass.")
                pic = {"ok": False, "limited": True, "room": 0, "reason": ""}
            if not pic["ok"]:
                if pic.get("reason"):
                    logger.info(f"Sender: {pic['reason']}; only replies go.")
                    await self.state.set_setting(SKIP_REASON_KEY, f"daily allowance shared with other campaigns: {pic['reason']}")
                budget = 0
            else:
                # Room again: the wait is over, so the dashboard (and the stall
                # check, which ignores a deliberate wait) stop reading it.
                if ((await self.state.get_setting(SKIP_REASON_KEY)) or "").startswith("daily allowance shared with other campaigns"):
                    await self.state.set_setting(SKIP_REASON_KEY, "")
                if pic["limited"]:
                    budget = min(budget, pic["room"])

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        stale = await self.state.release_stale_sending()
        if stale:
            logger.warning(f"Sender: {stale} email(s) were left mid-send by an earlier worker; marked failed.")
        # With first_only (the model cannot run: out of allowance,
        # rate-limited, replies unread) a follow-up could land on someone who
        # has answered, so the query leaves them out; first emails need no
        # model and go now.
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3, first_only=first_only,
        ) if budget > 0 else []
        # Replies on their own query, every pass: cold emails held at the
        # front of the queue (no allowance left, or an unavailable
        # compliance check) never keep them waiting.
        seen = {item["id"] for item in due}
        replies = await self.state.get_outbox(
            status="approved", due_before=now, limit=reply_room,
            exclude_kind="sequence",
        ) if reply_room else []
        extra = [item for item in replies if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        if not due:
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        # Set when Precleared can't be asked (allowance used up, network):
        # cold emails wait for a later cycle, replies still go out.
        compliance_paused = False
        ready: list[tuple[dict, object, str]] = []   # (item, prospect, receipt_id)
        cold = 0
        for item in due:
            if item["kind"] == "sequence" and cold >= budget:
                continue                  # the limit is for these; replies after them still go
            needs_check = self.precleared is not None and item["kind"] == "sequence"
            if needs_check and compliance_paused:
                continue

            prospect = await self.state.get_prospect(item["prospect_id"])
            if prospect is None:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error="prospect missing"
                )
                continue
            if (prospect.email_status or "") == "invalid":
                # The list check (which runs beside the writer) found the
                # address dead after this email was staged.
                await self.state.update_outbox_item(
                    item["id"], status="cancelled", error="address marked invalid before sending")
                continue
            if self.ledger is not None and await self._led(self.ledger.is_suppressed, item["to_email"]):
                # They asked another of this user's campaigns never to be contacted.
                await self.state.update_outbox_item(
                    item["id"], status="cancelled", error="asked not to be contacted (in another campaign)")
                if prospect.status != "opted_out":
                    await self.state.update_prospect_status(prospect.id, "opted_out")
                continue
            if self.ledger is not None and item["kind"] != "sequence" and item.get("in_reply_to"):
                try:
                    by = await self._led(self.ledger.answered_by, item["in_reply_to"])
                except Exception:
                    by = None
                if by and by != self.ledger_campaign:
                    # A reply campaign on this account answered that message
                    # while this campaign was stopped: this would be a second answer.
                    await self.state.update_outbox_item(
                        item["id"], status="cancelled", error="already answered by another campaign on this account")
                    continue

            # A follow-up goes only after its first email did: one held for
            # review waits with it, one rejected or failed takes it along.
            if item["kind"] == "sequence" and int(item.get("step") or 1) > 1 and item.get("campaign_id"):
                first = await self.state.sequence_step_status(item["campaign_id"], item["prospect_id"], 1)
                if first in ("pending_review", "approved", "sending"):
                    continue
                if first in ("rejected", "cancelled", "failed"):
                    await self.state.update_outbox_item(
                        item["id"], status="cancelled", error="the first email of this sequence never went out")
                    continue

            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if item["kind"] == "sequence" and prospect.status in STOP_STATUSES:
                await self.state.update_outbox_item(
                    item["id"], status="cancelled",
                    error=f"prospect status '{prospect.status}'",
                )
                continue

            gate = pre_send_check(
                item["to_email"], item["subject"], item["body"],
                prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            )
            if not gate:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error="gate: " + "; ".join(gate.reasons)[:300],
                )
                logger.warning(
                    f"Sender: gate blocked email to {item['to_email']}: "
                    f"{gate.reasons}"
                )
                continue

            # Optional compliance check (compliance.precleared): one per
            # address, before the first cold email. Replies are never checked.
            receipt_id = ""
            if needs_check:
                clearance = await self.precleared.clear(item["to_email"], self.state)
                if clearance.held:
                    compliance_paused = True
                    logger.warning(
                        f"Sender: compliance check unavailable, holding cold "
                        f"email until a later cycle: {clearance.reason}"
                    )
                    continue
                if not clearance.allowed:
                    await self.state.update_outbox_item(
                        item["id"], status="failed",
                        error=f"precleared: {clearance.verdict}: {clearance.reason}"[:300],
                    )
                    logger.warning(
                        f"Sender: Precleared held back email to {item['to_email']}: "
                        f"{clearance.verdict} ({clearance.reason})"
                    )
                    continue
                receipt_id = clearance.receipt_id
            ready.append((item, prospect, receipt_id))
            if item["kind"] == "sequence":
                cold += 1

        # Send: several at once when the channel allows it, each lane pausing
        # a little between its sends. With send_concurrency 1 this is the
        # one-at-a-time, few-seconds-apart behaviour a personal mailbox needs.
        lanes = max(1, int(getattr(email_cfg, "send_concurrency", 1) or 1))
        jitter_max = float(getattr(email_cfg, "send_jitter_seconds", SEND_JITTER_SECONDS[1]) or 0)
        queue = list(ready)
        sent = 0

        self._throttled = False
        self._cold_held = False
        self._auth_logged = False

        started = time.monotonic()

        async def lane():
            nonlocal sent
            first = True
            while queue and not self._throttled:
                if time.monotonic() - started > PASS_SECONDS:
                    break             # the rest stays approved for the next pass
                item, prospect, receipt_id = queue.pop(0)
                if self._cold_held and item["kind"] == "sequence":
                    continue          # the shared limit ran out this pass
                if self.send_pacing and jitter_max > 0 and not first:
                    await asyncio.sleep(random.uniform(jitter_max / 4, jitter_max))
                first = False
                if await self._send_one(item, prospect, receipt_id):
                    sent += 1

        await asyncio.gather(*(lane() for _ in range(min(lanes, len(queue)) or 1)))

        if sent:
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"(first emails and follow-ups today: {await self.state.count_outbox_sent_today(kind='sequence')}"
                        f"/{max_daily or 'no limit'}; replies have no limit).")

    async def _led(self, fn, *args, **kw):
        """A call on the shared ledger, off the event loop: the file is shared
        with other processes, and a write can wait seconds for its turn while
        the heartbeat, the send loop and the beat must keep running."""
        return await asyncio.to_thread(fn, *args, **kw)

    async def _in_cooloff(self) -> bool:
        """True while a spam block has the sender standing down. The reason
        is kept where the dashboard reads it; it is cleared when the time
        has passed."""
        until = await self.state.get_setting(COOLOFF_KEY)
        if not until:
            return False
        try:
            end = datetime.fromisoformat(until)
        except ValueError:
            await self.state.set_setting(COOLOFF_KEY, "")
            return False
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        reason = await self.state.get_setting(COOLOFF_REASON_KEY) or "an email was blocked as spam"
        if end > now:
            mins = int((end - now).total_seconds() // 60)
            text = f"{reason}; sending stands down for another {mins // 60}h {mins % 60:02d}m so the mailbox's reputation recovers"
            logger.warning(f"Sender: {text}.")
            await self.state.set_setting(SKIP_REASON_KEY, text)
            return True
        await self.state.set_setting(COOLOFF_KEY, "")
        skip = await self.state.get_setting(SKIP_REASON_KEY) or ""
        if "stands down" in skip:
            await self.state.set_setting(SKIP_REASON_KEY, "")
        logger.info("Sender: the cool-off after a spam block is over; sending resumes at the configured pace.")
        return False

    async def _fit_for_sending(self, subject: str, body: str, prospect, campaign, step, *,
                               template: tuple[str, str] | None = None, cache: dict | None = None) -> tuple[str, str, list[str]]:
        """The pre-send check and its own fix. The subject must be the
        thing the email is about and the whole must pass the spam check;
        what fails is rewritten once by the model (the goal and the rules
        in front of it) and checked again. Returns (subject, body, reasons
        still standing).

        With ``template`` (the unrendered wording) and a ``cache`` shared
        across one staging pass, the fix is made once to the template and
        every copy of it reuses that, instead of one model call per
        recipient; a copy the shared fix does not cure gets its own rewrite,
        a few per template at most."""
        from harvey.spamcheck import SUBJECT_REVIEW, subject_from_need, subject_risk
        need = ""
        try:
            from harvey import enquiry
            if getattr(getattr(self.config, "outreach", None), "mode", "offer") == "inquiry":
                need = enquiry.need_for(self.config.outreach, getattr(prospect, "industry", "") or "")["need"]
        except Exception:
            need = ""
        score, reasons = spam_risk(subject, body)
        s_score, s_reasons = subject_risk(subject, body, need)
        if score < SPAM_REVIEW and s_score < SUBJECT_REVIEW:
            return subject, body, []
        if s_score >= SUBJECT_REVIEW and score < SPAM_REVIEW:
            # Only the subject is wrong: a plain one from the sender's own need, no model call.
            plain = subject_from_need(need, body)
            if plain and subject_risk(plain, body, need)[0] < SUBJECT_REVIEW:
                return plain, body, []
        if template is not None and cache is not None:
            key = (campaign.id, int(step.step), template[0], template[1])
            if key not in cache:
                cache[key] = await self._rewrite_template_for_filter(template[0], template[1], reasons + s_reasons, need)
                if cache[key]:
                    logger.info(f"Sender: email {step.step} of '{campaign.name}' read like spam to a filter; "
                                "rewritten once for every copy.")
            fixed = cache[key]
            if fixed:
                t_subject = self._render(fixed[0], prospect)
                t_body = self._render(fixed[1], prospect, body=True)
                cured = self._passes(t_subject, t_body, need)
                if cured:
                    return cured[0], cured[1], []
            budget_key = ("per_copy", key)
            if cache.get(budget_key, 0) >= PER_COPY_REWRITES:
                if fixed:
                    t_reasons = spam_risk(t_subject, t_body)[1] + subject_risk(t_subject, t_body, need)[1]
                    return t_subject, t_body, t_reasons
                return subject, body, reasons + s_reasons
            cache[budget_key] = cache.get(budget_key, 0) + 1
        rewritten = await self._rewrite_for_filter(subject, body, reasons + s_reasons, prospect, need)
        if rewritten:
            new_subject, new_body = rewritten
            score2, reasons2 = spam_risk(new_subject, new_body)
            s_score2, s_reasons2 = subject_risk(new_subject, new_body, need)
            if s_score2 >= SUBJECT_REVIEW:
                plain = subject_from_need(need, new_body)
                if plain and subject_risk(plain, new_body, need)[0] < SUBJECT_REVIEW:
                    new_subject, s_score2, s_reasons2 = plain, 0, []
            if score2 < SPAM_REVIEW and s_score2 < SUBJECT_REVIEW:
                return new_subject, new_body, []
            return new_subject, new_body, reasons2 + s_reasons2
        return subject, body, reasons + s_reasons

    @staticmethod
    def _passes(subject: str, body: str, need: str) -> tuple[str, str] | None:
        """(subject, body) when the pair passes both checks, the subject
        swapped for a plain one from the need if only it fails; else None."""
        from harvey.gate import MERGE_TAG_RE
        from harvey.spamcheck import SUBJECT_REVIEW, subject_from_need, subject_risk
        if MERGE_TAG_RE.search(subject + " " + body):
            return None        # a field nobody filled in would be refused at the gate
        if spam_risk(subject, body)[0] >= SPAM_REVIEW:
            return None
        if subject_risk(subject, body, need)[0] < SUBJECT_REVIEW:
            return subject, body
        plain = subject_from_need(need, body)
        if plain and subject_risk(plain, body, need)[0] < SUBJECT_REVIEW:
            return plain, body
        return None

    async def _rewrite_template_for_filter(self, subject: str, body: str, reasons: list[str], need: str) -> tuple[str, str] | None:
        """One model pass over a segment's shared wording (merge fields and
        all) so every copy of it passes. None when the model fails or does
        not keep the merge fields exactly."""
        outreach = getattr(self.config, "outreach", None)
        goal = (getattr(outreach, "goal", "") or "").strip()
        bare = names.strip_greeting(body)
        fields = set(_MERGE_RE.findall(subject + "\n" + bare))
        prompt = (
            f"This email template is about to be sent by {self.config.persona.name} to many people, and a spam "
            f"filter would likely flag it: {describe(reasons)}.\n\nRewrite it so it passes, keeping every fact, the "
            "sign-off and the same request. Keep every merge field such as {{first_name}} or {{company}} exactly "
            "as written, add no new ones, and write no greeting line (one is added per person). Plain words a "
            "person would type; no marketing vocabulary, no capitals, no exclamation marks, no links. The subject "
            "names the thing the email is about, in the email's own words, 3 to 10 words, sentence case; never "
            "\"quick question\", \"opportunity\", \"proposal\", \"partnership\", \"following up\" or anything about "
            "the act of writing.\n"
            + (f"\nTHE NEED THIS EMAIL IS ABOUT: {need}\n" if need else "")
            + (f"\nCAMPAIGN GOAL (the email serves this): {goal}\n" if goal else "")
            + f"\nSUBJECT: {subject}\n\nBODY:\n{bare}\n\n"
            'Return ONLY JSON: {"subject": "...", "body": "..."}'
        )
        try:
            result = await self.brain.think_json(prompt, session_id="harvey-sender", agent="sender", task="fit_template")
        except Exception as e:
            logger.debug(f"Sender: template rewrite failed: {e}")
            return None
        if not isinstance(result, dict) or not str(result.get("body") or "").strip():
            return None
        # One spelling for every merge field ({{ company }} → {{company}}), the one _render fills.
        new_subject = _MERGE_RE.sub(lambda m: "{{" + m.group(1) + "}}", str(result.get("subject") or subject).strip()[:120])
        new_body = _MERGE_RE.sub(lambda m: "{{" + m.group(1) + "}}", names.strip_greeting(str(result["body"]).strip()))
        if set(_MERGE_RE.findall(new_subject + "\n" + new_body)) != fields:
            logger.info("Sender: the template rewrite changed its merge fields; each copy is checked on its own instead.")
            return None
        return new_subject, new_body

    async def _rewrite_for_filter(self, subject: str, body: str, reasons: list[str], prospect, need: str) -> tuple[str, str] | None:
        """One model pass to fix a rendered email a filter would junk,
        keeping its facts, its greeting line and its sign-off."""
        outreach = getattr(self.config, "outreach", None)
        goal = (getattr(outreach, "goal", "") or "").strip()
        persona = self.config.persona
        prompt = (
            f"This email is about to be sent by {persona.name} and a spam filter would likely flag it: "
            f"{describe(reasons)}.\n\nRewrite it so it passes, keeping every fact, the greeting line exactly as it "
            "is, the sign-off, and the same request. Plain words a person would type; no marketing vocabulary, no "
            "capitals, no exclamation marks, no links. The subject names the thing the email is about, in the "
            "email's own words, 3 to 10 words, sentence case; never \"quick question\", \"opportunity\", "
            "\"proposal\", \"partnership\", \"following up\" or anything about the act of writing.\n"
            + (f"\nTHE NEED THIS EMAIL IS ABOUT: {need}\n" if need else "")
            + (f"\nCAMPAIGN GOAL (the email serves this): {goal}\n" if goal else "")
            + f"\nSUBJECT: {subject}\n\nBODY:\n{body}\n\n"
            'Return ONLY JSON: {"subject": "...", "body": "..."}'
        )
        try:
            result = await self.brain.think_json(prompt, session_id="harvey-sender", agent="sender", task="fit_for_sending")
        except Exception as e:
            logger.debug(f"Sender: pre-send rewrite failed: {e}")
            return None
        if not isinstance(result, dict) or not str(result.get("body") or "").strip():
            return None
        new_body = str(result["body"]).strip()
        first_line = (body or "").strip().split("\n", 1)[0]
        if first_line and not new_body.startswith(first_line):
            head = new_body.split("\n\n", 1)
            # The model wrote its own greeting: swap it for the right one. It
            # wrote none: put the greeting above what it wrote, losing nothing.
            if len(head) == 2 and re.match(r"(?i)^(hi|hello|hey|dear|good (morning|afternoon|evening))\b.{0,60}$", head[0].strip()):
                new_body = first_line + "\n\n" + head[1]
            else:
                new_body = first_line + "\n\n" + new_body
        return str(result.get("subject") or subject).strip()[:120], new_body

    async def _record_bad_address(self, item: dict, prospect, error: str) -> None:
        """A permanent refusal of the address at send time is a bounce
        that never had to travel: the address is marked invalid, the
        person's queued emails are cancelled, a dead domain holds the
        company's other first emails, and the bounce rate counts it."""
        reason_text, hint = bounce_reason(error)
        kind = bounce_kind(error) or "no such address (refused by the mail server)"
        await self.state.update_outbox_item(item["id"], status="failed", error=f"Bounced: {error[:220]}. {hint}"[:600])
        if prospect:
            try:
                await self.state.update_prospect_email(prospect.id, prospect.email, "invalid")
                cancelled = await self.state.cancel_pending_outbox_for_prospect(prospect.id, reason="bounced at send")
            except Exception as e:
                logger.debug(f"Sender: could not mark {item['to_email']} invalid: {e}")
                cancelled = 0
            logger.warning(f"Sender: {item['to_email']} was refused by the mail server ({kind}); marked invalid, "
                           f"{cancelled} queued email(s) cancelled. {hint}")
            if "domain does not exist" in kind:
                domain = (prospect.email or "").rsplit("@", 1)[-1].lower()
                try:
                    held = await self.state.hold_domain_sends(
                        domain, prospect.id,
                        f"Held: the mail server says the domain {domain} does not exist ({prospect.email} was refused). "
                        "Approve to send anyway, or reject.")
                    if held:
                        logger.warning(f"Sender: {held} queued email(s) to other people at {domain} now wait for your review.")
                except Exception as e:
                    logger.debug(f"Sender: domain hold failed: {e}")
        try:
            bounces = await self.state.increment_setting(BOUNCE_COUNT_KEY)
            await self.state.log_action(action_type="bounce", agent="sender",
                                        details={"prospect": item["to_email"], "bounces": bounces, "reason": reason_text,
                                                 "at": "send"})
            # The same bad-list switch the handler trips on bounces that travelled.
            total_sent = await self.state.count_outbox_sent()
            max_rate = getattr(self.config.channels.email, "max_bounce_rate", 0.05)
            if max_rate > 0 and total_sent >= 50 and bounces / max(total_sent, 1) > max_rate:
                reason = f"bounce rate {bounces}/{total_sent} exceeded {max_rate:.0%} — check list quality before resuming"
                await self.state.set_setting(KILL_SWITCH_KEY, reason)
                logger.error(f"Sender: KILL SWITCH ENGAGED — {reason}")
        except Exception:
            pass

    def _plain(self, item: dict) -> bool:
        """A first email goes as plain text (no HTML, a links-free signature)
        unless the operator switched that off."""
        if not bool(getattr(self.config.channels.email, "plain_first_email", True)):
            return False
        try:
            return (item.get("kind") or "sequence") == "sequence" and int(item.get("step") or 1) == 1
        except (TypeError, ValueError):
            return False

    async def _send_one(self, item: dict, prospect, receipt_id: str) -> bool:
        """Send one gated outbox item and record the outcome. True when it went."""
        if not await self.state.claim_outbox_item(item["id"]):
            return False   # another pass took it, or it was approved no more
        send_kwargs = {}
        files = _attachment_paths(item.get("attachments_json"))
        if files:
            send_kwargs["attachments"] = files
        if self._plain(item):
            send_kwargs["plain"] = True
        slot = None
        if self.ledger is not None:
            # The slot is taken on a thread (the shared file can be busy for
            # seconds). If this pass is cut off meanwhile, the slot goes back
            # (the thread gives it back itself when it sees that, or the
            # callback does) and the email returns to the queue untouched.
            gave_up = threading.Event()
            ledger, kind = self.ledger, item.get("kind") or "sequence"

            def _take():
                # An answer is written down but never held back: replies have no limit.
                got, picture = ledger.reserve(self.ledger_account, self.ledger_campaign, "outreach", self.ledger_label,
                                              kind=kind, free=kind != "sequence")
                if got and gave_up.is_set():
                    ledger.release(got)
                    return None, picture
                return got, picture
            taking = asyncio.ensure_future(asyncio.to_thread(_take))
            try:
                slot, pic = await asyncio.shield(taking)
            except asyncio.CancelledError:
                gave_up.set()

                def _give_back(f, _ledger=ledger):
                    if not f.cancelled() and f.exception() is None and f.result()[0]:
                        _ledger.release(f.result()[0])
                taking.add_done_callback(_give_back)
                try:
                    await asyncio.shield(self.state.update_outbox_item(item["id"], status="approved"))
                except Exception:
                    pass        # the stale-send sweep puts it back later in any case
                raise
            except Exception as e:
                slot, pic = None, {"reason": f"the shared send ledger could not be read ({e})", "unread": True}
            if slot is None:
                # Another campaign on this account took the last of the shared
                # limit in the meantime: the email waits, and so do the other
                # first emails of this pass; replies (no limit) still go. A
                # ledger that cannot be read ends the pass.
                await self.state.update_outbox_item(item["id"], status="approved")
                if pic.get("unread") or (item.get("kind") or "sequence") != "sequence":
                    self._throttled = True      # a reply held back means the mailbox's own maximum: nothing more goes
                else:
                    self._cold_held = True
                logger.info(f"Sender: {pic.get('reason')}; the rest waits.")
                return False
        inner = asyncio.ensure_future(self.provider.send_email(
            item["to_email"], item["subject"], item["body"],
            thread_ref=item.get("thread_ref", ""),
            in_reply_to=item.get("in_reply_to", ""),
            **send_kwargs,
        ))
        if self.ledger is not None and slot:
            # The slot goes back only when the email did not leave, decided
            # by the send itself: a pass cut off while it finishes keeps it.
            loop = asyncio.get_running_loop()

            def _settle(task, _slot=slot, _ledger=self.ledger, _loop=loop):
                try:
                    failed = task.cancelled() or task.exception() is not None or not task.result().ok
                except Exception:
                    failed = True
                if failed:
                    _loop.run_in_executor(None, _ledger.release, _slot)   # off the loop: the file may be busy
            inner.add_done_callback(_settle)
        result = await asyncio.shield(inner)
        if not result.ok:
            if getattr(result, "auth_failed", False):
                # The mailbox refused the login: nothing can leave until it is
                # reconnected. The row goes back untouched; the pass ends.
                await self.state.update_outbox_item(item["id"], status="approved",
                                                    error=f"waiting: {result.error[:200]}")
                self._throttled = True
                self.auth_refused = True
                if not self._auth_logged:
                    self._auth_logged = True
                    logger.error(f"Sender: the mailbox refused the login; nothing is sent until it is reconnected ({result.error[:160]}).")
                    try:
                        await self.state.set_setting("sender_skip_reason", f"the mailbox refused the login: {result.error[:160]}")
                    except Exception:
                        pass
                return False
            if getattr(result, "bad_address", False):
                await self._record_bad_address(item, prospect, result.error)
                return False
            if getattr(result, "retryable", False):
                daily = bool(getattr(result, "daily_limit", False))
                previous = _retry_count(item.get("error") or "")
                tries = previous if daily else previous + 1   # a full mailbox is not the email's fault
                if daily or tries <= MAX_SEND_RETRIES:
                    delay = 24 * 3600 if daily else RETRY_DELAY_SECONDS * tries
                    later = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)).isoformat()
                    note = (f"retry {tries}/{MAX_SEND_RETRIES}: " if tries else "") + result.error[:200]
                    await self.state.update_outbox_item(item["id"], status="approved", send_at=later, error=note)
                    if getattr(result, "scope", "mailbox") == "mailbox":
                        self._throttled = True   # the provider is asking for a pause: this pass ends
                    logger.warning(f"Sender: temporary refusal for {item['to_email']}; trying again in "
                                   f"{delay // 60} min" + (f" ({tries}/{MAX_SEND_RETRIES})" if tries else "") + "."
                                   + (" The mailbox's daily sending limit is reached." if daily else ""))
                    return False
            await self.state.update_outbox_item(
                item["id"], status="failed", error=result.error[:300]
            )
            return False
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        await asyncio.shield(self.state.update_outbox_item(
            item["id"], status="sent", sent_at=now_iso,
            message_id=result.message_id, thread_ref=result.thread_ref,
        ))
        if prospect.status in ("new", "queued"):
            await self.state.update_prospect_status(prospect.id, "contacted")
        if self.ledger is not None and item.get("kind") != "sequence" and item.get("in_reply_to"):
            try:
                await self._led(self.ledger.mark_answered, self.ledger_account, self.ledger_campaign, [item["in_reply_to"]])
            except Exception:
                pass
        try:
            await self.state.set_setting(LAST_SENT_KEY, now_iso)   # the mailbox health check reads this
        except Exception:
            pass

        # A sent reply belongs in its conversation thread.
        if item["kind"] == "reply" and item.get("conversation_id"):
            try:
                convo = await self.state.get_conversation(item["conversation_id"])
                if convo:
                    from harvey.models.conversation import Message
                    convo.thread.append(
                        Message(sender="harvey", content=item["body"])
                    )
                    await self.state.update_conversation(
                        convo.id, thread_json=convo.thread_json()
                    )
            except Exception as e:
                logger.debug(f"Sender: convo append failed: {e}")
        details = {
            "to": item["to_email"], "step": item["step"],
            "kind": item["kind"], "provider": self.provider.name,
        }
        if receipt_id:
            details["precleared_receipt"] = receipt_id
        await self.state.log_action(
            action_type="email_sent",
            agent="sender",
            details=details,
        )
        logger.info(
            f"Sender: sent step {item['step']} to {item['to_email']} "
            f"via {self.provider.name}."
        )
        return True
