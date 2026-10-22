"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import asyncio
import json
import re
import logging
from datetime import datetime, timezone

from harvey import enquiry, names
from harvey.brain import Brain
from harvey.config import HarveyConfig
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import StateManager

logger = logging.getLogger("harvey.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 40   # personal drafts per segment per cycle; model-bound, and the room() ceiling applies
LEGACY_BATCH_CAP = 50
# Segment mode (outreach.personalize_each: false): one sequence per
# sector × role segment, sent to everyone in it, so a segment can be large.
SEGMENT_BATCH_CAP = 250
SEGMENT_ROOM = 500
NEW_PROSPECTS_PAGE = 2000  # 'new' rows loaded per cycle; the rest wait their turn
# Team pages fetched per cycle to recover names the upload left out; the
# rest wait for the next cycle, so a 1-minute heartbeat stays quick.
MAX_NAME_LOOKUPS = 15  # contacts considered per cycle in segment mode
CLASSIFY_BATCH = 25  # contacts per classification call
MODEL_CONCURRENCY = 4  # model calls in flight at once (classify, sequences, personal drafts)
LOOKUP_CONCURRENCY = 5  # team pages fetched at once

# The sector vocabulary the classifier sorts contacts into. Closed, so
# segments line up across uploads; "other" is the catch-all.
SECTORS = (
    "construction", "real_estate", "architecture_engineering", "financial_advisory", "banking",
    "insurance", "accounting", "legal", "healthcare", "education", "hr_recruiting",
    "marketing_agency", "software", "it_services", "manufacturing", "logistics", "retail",
    "ecommerce", "hospitality", "nonprofit", "government", "media", "energy", "consulting",
    "agriculture", "automotive", "other",
)
TIERS = ("executive", "manager", "individual", "admin")
DEPARTMENTS = ("leadership", "finance", "hr", "operations", "sales", "marketing", "engineering",
               "admin", "other")
# How the position changes the message: the same ask is phrased for who reads it.
TIER_GUIDANCE = {
    "executive": ("Executives, owners and partners decide. Be brief, lead with the specific "
                  "thing you want from their business, and ask for one decision or a short call."),
    "manager": ("Managers run the work. Be practical: what you need, roughly when, and what "
                "you'd give them; ask one concrete question about how they'd take it on."),
    "individual": ("Specialists do the work. Speak to their craft, say what you need, and ask "
                   "how their team usually takes on a request like yours."),
    "admin": ("Assistants, office managers and shared inboxes route mail. Keep it to a few "
              "lines: say what you need and ask whether they can take it on or pass it to the "
              "person who does. Never remark on the inbox or on their role; just ask."),
}
_plain_signoff = enquiry.plain_signoff   # a customer signs with a first name alone


def _term_pattern(term: str) -> tuple[str, int]:
    """How a forbidden term is recognised: (regex source, flags).

    A domain, a multi-word name, a name with digits or a long name is
    caught anywhere, whatever the case. A short ordinary-looking word
    ("Build", "Zenith", however the upload cased it) is caught only
    written as a name, capitalised or in capitals, so "build" and
    "builder" the words are left alone."""
    if not term.isalpha() or len(term) >= 8:
        return re.escape(term), re.I
    low = term.lower()
    return rf"(?:{re.escape(low.capitalize())}|{re.escape(low.upper())})", 0


def _term_re(term: str) -> re.Pattern:
    src, flags = _term_pattern(term)
    return re.compile(rf"(?<![\w@.]){src}(?!\w)(?!\.\w)", flags)


def _mentions(text: str, terms: set[str]) -> bool:
    text = text or ""
    return any(_term_re(t).search(text) for t in terms if t)


def _scrub(text: str, terms: set[str]) -> str:
    """Replace company names and domains with neutral wording, so an email
    never names the recipient's business or the sender's. Longest terms
    first so "Acme Roofing" goes before "Acme"."""
    if not text or not terms:
        return text
    for term in sorted((t for t in terms if t), key=len, reverse=True):
        if not _term_re(term).search(text):
            continue      # not named (a plain word that is also a company's name is left alone)
        esc, flags = _term_pattern(term)
        flag = "(?i)" if flags & re.I else ""
        # "Avrum here from Wandlp." → "Avrum here."; "the team at Acme" → "the team".
        tail = r"(?!\w)(?!\.\w)"   # not inside a longer word, not the start of a domain
        text = re.sub(rf"{flag}\s+\b(?:at|from|of)\s+{esc}{tail}", "", text)
        # "working with Acme" → "working with you"; "a quote for Acme" → "a quote for you".
        text = re.sub(rf"{flag}\b(with|for|to)\s+{esc}{tail}", r"\1 you", text)
        text = re.sub(rf"{flag}\b{esc}['’]s{tail}", "your", text)
        text = re.sub(rf"{flag}(?<![\w@.]){esc}{tail}", "you", text)
    # Any web address left over (a dot in a word that looks like a host) goes too.
    text = re.sub(r"(?i)\b(?:https?://|www\.)\S+", "your site", text)
    text = re.sub(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|ng|uk|de|fr|ca|au|us|biz|info|app|dev|me)\b(?:/\S*)?",
                  "your site", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"\byour your\b", "your", text)
    return text.strip()


# Titles that really are admin / gatekeeping roles.
_ADMIN_TITLE_RE = re.compile(
    r"assistant|secretary|reception|front desk|office manager|office admin|administrator|"
    r"admin\b|coordinator|pa\b|ea\b|clerk|dispatcher", re.I)
# Scout-sourced seniority values map onto the four tiers.
_SENIORITY_TO_TIER = {
    "c_suite": "executive", "vp": "executive", "director": "executive", "owner": "executive",
    "founder": "executive", "partner": "executive", "executive": "executive",
    "manager": "manager", "admin": "admin", "individual": "individual",
}


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # A writer that died mid-personalisation left campaigns in 'writing':
        # one writer runs at a time, so they are the previous run's.
        try:
            freed = await self.state.release_writing_campaigns()
            if freed:
                logger.info(f"Writer: {freed} campaign(s) from an interrupted run handed to the sender.")
        except Exception as e:
            logger.debug(f"Writer: release_writing_campaigns failed: {e}")

        # An uploaded list is checked before anything is written to it, when
        # a verifier key is available: a guessed address bounces, and a few
        # bounces at one company get the rest of that company held.
        # (The list check itself rides along the heartbeat on its own clock
        # (main._bounded_listcheck) so it never delays the writing; the
        # sender re-reads the address status before every send.)

        # Get prospects that haven't been contacted yet (a bounded page: a
        # 10,000-row upload is not loaded whole every cycle)
        new_prospects = await self.state.get_prospects_by_status(
            "new", limit=NEW_PROSPECTS_PAGE, email_statuses=("verified", "provided", "risky"))
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        deliverable = {"verified", "provided", "risky"}
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in deliverable
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Native mode drafts cost a model call each, so never write more
        # than about two days of sends ahead of the sender: a 10,000-row
        # upload is worked through at the daily limit, not drafted at once.
        # Segment mode writes one sequence per segment, so it can look
        # further ahead.
        if self.is_native:
            room, ahead, backlog = await self.room()
            if room == 0:
                max_daily = int(self.config.channels.email.max_daily_sends or 0)
                logger.info(
                    f"Writer: {backlog} email(s) already written and waiting; "
                    f"drafting more once the sender catches up "
                    f"({f'{max_daily}/day' if max_daily else 'no daily limit'})."
                )
                return
            prospects_with_email = prospects_with_email[:room]

        # Work out who the nameless addresses belong to before anything is
        # written, so the greeting is "Hi Jane," and not "Hi there,".
        try:
            await self._find_missing_names(prospects_with_email)
        except Exception as e:
            logger.warning(f"Writer: name lookup skipped: {e}")

        # The operator's own message: no model call, one campaign, the
        # sender fills in {{first_name}} and the greeting per person.
        if self.use_template:
            await self._queue_template_campaign(prospects_with_email)
            return

        # Sort newcomers into sector × role before grouping, so every
        # campaign is one sector and one tier and the writing can speak to
        # what that business does and who is reading.
        await self._classify(prospects_with_email)

        # Batch prospects into campaign groups (by sector and role tier)
        batches = self._group_prospects(prospects_with_email)

        todo = [(name, ps) for name, ps in batches.items() if ps]
        gate = asyncio.Semaphore(self._concurrency)

        async def write(ps, name):
            # A segment written in the last day keeps its sequence: the next
            # batch of the same sector and tier costs no model call.
            try:
                earlier = await self.state.latest_campaign_named(name)
            except Exception:
                earlier = None
            if earlier and earlier.sequence and self._written_after_config_change(earlier):
                logger.info(f"Writer: reusing today's sequence for '{name}'.")
                return list(earlier.sequence)
            async with gate:
                try:
                    return await self._write_sequence(ps)
                except Exception as e:
                    logger.warning(f"Writer: sequence failed: {e}")
                    return []

        for name, ps in todo:
            logger.info(f"Writer: Creating campaign '{name}' for {len(ps)} prospects.")
        sequences = await asyncio.gather(*(write(ps, name) for name, ps in todo))

        for (batch_name, prospects), sequence in zip(todo, sequences):
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign. While first emails are being personalised
            # it is 'writing', invisible to the sender, so the sender cannot
            # stage the segment template over the personal drafts.
            personalising = self.is_native and self.personalize_each
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="writing" if personalising else "draft",
            )
            # Creating the campaign and marking its people as queued is one
            # step: cut between the two, the same people would be written
            # for again next cycle and get two emails.
            campaign_id = await asyncio.shield(self._commit_campaign(campaign, prospects))

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts —
            # unless the operator chose one email per segment.
            if personalising:
                try:
                    await self._personalize_first_emails(campaign_id, prospects)
                finally:
                    await asyncio.shield(self.state.update_campaign(campaign_id, status="draft"))

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    def _written_after_config_change(self, campaign) -> bool:
        """A sequence is reused only if it was written after the last edit of
        harvey.yaml AND of the writing brief itself: a changed request, style,
        ask or prompt must not ride on yesterday's words (an update that
        rewrites the brief makes every older sequence stale)."""
        try:
            from datetime import datetime, timezone
            from pathlib import Path
            from harvey import paths
            from harvey.config import _find_config_file
            stamps = [Path(_find_config_file()).stat().st_mtime]
            for name in ("inquiry_writer.md", "writer.md"):
                prompt_file = paths.PROMPTS_DIR / name
                if prompt_file.exists():
                    stamps.append(prompt_file.stat().st_mtime)
            mtime = datetime.fromtimestamp(max(stamps), tz=timezone.utc).replace(tzinfo=None)
            created = campaign.created_at
            if created.tzinfo is not None:
                created = created.astimezone(timezone.utc).replace(tzinfo=None)
            return created >= mtime
        except Exception:
            return False   # cannot tell: write fresh rather than reuse stale words

    async def _commit_campaign(self, campaign, prospects: list) -> str:
        return await self.state.commit_campaign(campaign, [p.id for p in prospects])

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        drafted = 0
        gate = asyncio.Semaphore(self._concurrency)

        async def write(prospect):
            async with gate:
                try:
                    return await self._write_personal_email(prospect)
                except Exception as e:
                    logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                    return None

        drafts = await asyncio.gather(*(write(p) for p in prospects))
        for prospect, draft in zip(prospects, drafts):
            if not draft:
                continue
            held = draft.get("spam_reasons") or []
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status="pending_review" if held else status,
                provider=provider,
            )
            if item_id and held:
                from harvey.spamcheck import describe
                await self.state.update_outbox_item(
                    item_id, error="Needs review: a spam filter would likely flag "
                    + describe(held) + ". Edit the profile's request or approve it as is.")
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    def _ask_block(self) -> str:
        """The closing ask the user chose (a call, a coffee, a visit...).
        Blank means the email ends on an easy question instead."""
        from harvey.config import ASK_THEMES
        outreach = getattr(self.config, "outreach", None)
        theme = (getattr(outreach, "ask", "") or "").strip()
        if theme not in ASK_THEMES:
            return ""
        persona = self.config.persona
        detail = (getattr(outreach, "ask_detail", "") or "").strip()
        offer = getattr(self.config.product, "offer", None)
        link = (getattr(offer, "booking_url", "") or "").strip() if theme in ("zoom", "phone") else ""
        lines = [f"THE ASK: what {persona.name} would like to happen next is {ASK_THEMES[theme]}."]
        if self.inquiry:
            first = self._persona_first
            if theme == "email":
                lines.append("- A reply by email is what the question itself asks for, so nothing is added: no call, "
                             "meeting or visit is mentioned anywhere in the email.")
            else:
                how = ("a short call" if theme in ("zoom", "phone") else
                       "meeting in person" if theme in ("coffee", "in_person") else "coming to see it")
                lines.append("- The ONE question of the first email stays the request itself (can you take this on, can "
                             f"you supply this). THE ASK is one plain extra sentence before that question, in {first}'s "
                             f"own words, written fresh each time (never a sentence copied from this brief or the examples), "
                             f"saying that {how} would be the easiest way to explain it. Never \"walk you through\", never "
                             "\"glad to\" or \"happy to\", never \"at a time that suits you\", never \"Zoom or Google "
                             "Meet\", never an alternative way to reply, never an easy out.")
            link = ""  # the booking link belongs in replies, not in a first enquiry
        else:
            lines.append("- The first email ends by proposing exactly that, in one plain sentence, as the ONE question of the email. "
                         "Low pressure: suggest it, give them an easy out, and say a one-line reply is fine if they would rather.")
        if theme in ("coffee", "in_person", "visit"):
            lines.append("- Only propose it as something practical for them; never invent a place, date or travel plan that is not in the facts or below.")
        if detail:
            lines.append(f"- Specifics from {persona.name} to use when they fit, in your own words: {detail}")
        if link:
            lines.append(f"- Booking link to include once, on its own line, after the ask: {link}")
        lines.append("- Follow-ups repeat the ask once, briefly, and otherwise add something new.")
        return "\n\n" + "\n".join(lines)

    async def _find_missing_names(self, prospects) -> int:
        """Fill in names the upload left blank: from the LinkedIn URL or a
        first.last address first (free), then from the company's team page
        (one fetch per domain, at most MAX_NAME_LOOKUPS pages a cycle),
        matched against the address. Shared inboxes are left alone. Returns
        how many names were found."""
        found = 0
        pages: dict[str, list] = {}
        # Team pages first, several at once: one fetch per domain, at most
        # MAX_NAME_LOOKUPS domains a cycle, in list order.
        need: list[str] = []
        for prospect in prospects:
            if prospect.full_name() or not prospect.email or names.is_role_address(prospect.email):
                continue
            if names.resolve_name("", "", prospect.email, prospect.linkedin_url)["source"] in ("linkedin", "email"):
                continue
            domain = prospect.email.rsplit("@", 1)[-1].lower()
            if domain not in names.PUBLIC_DOMAINS and domain not in need and len(need) < MAX_NAME_LOOKUPS:
                need.append(domain)
        if need:
            from harvey.agents.scout import Scout
            scout = Scout(self.brain, self.state, self.config, self.env)
            gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)

            async def fetch(domain):
                async with gate:
                    try:
                        return await scout._scrape_team_page(domain)
                    except Exception as e:
                        logger.debug(f"Writer: team page for {domain} unreadable: {e}")
                        return []
            for domain, page in zip(need, await asyncio.gather(*(fetch(d) for d in need))):
                pages[domain] = page or []
        for prospect in prospects:
            if prospect.full_name() or not prospect.email or names.is_role_address(prospect.email):
                continue
            resolved = names.resolve_name("", "", prospect.email, prospect.linkedin_url)
            note = ""
            if resolved["source"] == "linkedin":
                note = "Name taken from the LinkedIn URL."
            elif resolved["source"] == "email":
                note = "Name read from the email address (first.last pattern)."
            else:
                domain = prospect.email.rsplit("@", 1)[-1].lower()
                if domain not in pages:
                    continue
                member = names.match_team_member(prospect.email, pages[domain])
                if not member:
                    continue
                resolved = {"first_name": member["first_name"], "last_name": member["last_name"]}
                note = f"Name matched to {domain}'s team page" + (
                    f" ({member['title']})." if member.get("title") else ".")
                if member.get("title") and not prospect.title:
                    prospect.title = member["title"]
            if not resolved["first_name"]:
                continue
            prospect.first_name = resolved["first_name"]
            prospect.last_name = resolved["last_name"]
            await self.state.update_prospect_name(prospect.id, prospect.first_name,
                                                  prospect.last_name, note, title=prospect.title)
            found += 1
        if found:
            logger.info(f"Writer: found names for {found} recipient(s) the upload left blank.")
        return found

    @property
    def use_template(self) -> bool:
        outreach = getattr(self.config, "outreach", None)
        return bool(getattr(outreach, "use_template", False)
                    and (getattr(outreach, "template_body", "") or "").strip())

    def template_steps(self, message=None) -> list[EmailStep]:
        """The operator's own message as a sequence: the first email, then
        whichever follow-ups were written (3 and 7 days on). ``message`` is
        a sector version; None means the default."""
        outreach = self.config.outreach
        if message is None:
            subject, body = outreach.template_subject, outreach.template_body
            followups = (outreach.template_followup_1, outreach.template_followup_2)
        else:
            subject, body = message.subject or outreach.template_subject, message.body
            followups = (message.followup_1, message.followup_2)
        subject = (subject or "").strip() or "Following up"
        steps = [EmailStep(step=1, subject=subject, body=(body or "").strip(), delay_days=0)]
        for delay, text in zip((3, 4), followups):
            text = (text or "").strip()
            if text:
                steps.append(EmailStep(step=len(steps) + 1, subject=subject, body=text, delay_days=delay))
        return steps

    def _message_for_sector(self, sector: str):
        """The sector version that covers ``sector``, or None for the default."""
        for message in getattr(self.config.outreach, "sector_messages", None) or []:
            if (message.body or "").strip() and sector in message.sectors:
                return message
        return None

    async def _queue_template_campaign(self, prospects: list) -> list[str]:
        """One campaign per version of the operator's message. With sector
        versions, contacts are sorted into sectors first (one model call per
        25); anyone unsorted or in an uncovered sector gets the default."""
        versions = [m for m in (getattr(self.config.outreach, "sector_messages", None) or [])
                    if (m.body or "").strip() and m.sectors]
        if versions:
            try:
                await self._classify(prospects)
            except Exception as e:
                logger.warning(f"Writer: sorting into sectors failed ({e}); using the default message.")
        groups: dict[str, tuple] = {}
        for p in prospects:
            message = self._message_for_sector((p.industry or "").strip().lower()) if versions else None
            key = "your message" if message is None else "your message · " + " / ".join(message.sectors[:3])
            groups.setdefault(key, (message, []))[1].append(p)
        ids = []
        for name, (message, group) in groups.items():
            campaign = Campaign(
                id="", name=name, channel="email", sequence=self.template_steps(message),
                prospect_ids=[p.id for p in group], status="draft",
            )
            campaign_id = await asyncio.shield(self._commit_campaign(campaign, group))
            ids.append(campaign_id)
            await self.state.log_action(
                action_type="write_campaign", agent="writer",
                details={"campaign_id": campaign_id, "campaign_name": name,
                         "prospect_count": len(group), "steps": len(campaign.sequence),
                         "template": True})
            logger.info(f"Writer: queued '{name}' for {len(group)} recipient(s).")
        return ids

    def _risk(self, draft: dict, need: str = "") -> tuple[int, list[str]]:
        """Spam risk plus, for an enquiry, the vagueness that makes it read
        as a campaign: both on the same scale, held at REVIEW."""
        from harvey.spamcheck import spam_risk, vague_risk

        subject, body = str(draft.get("subject", "")), str(draft.get("body", ""))
        score, reasons = spam_risk(subject, body)
        if self.inquiry:
            v, vr = vague_risk(subject, names.strip_greeting(body), need)
            score, reasons = score + v, reasons + vr
        return score, reasons

    async def _spam_safe(self, prompt: str, result: dict, task: str, need: str = "") -> tuple[dict, list[str]]:
        """Score a draft; if it reads like spam (or, for an enquiry, like a
        campaign), ask once for a rewrite with the findings spelled out.
        Returns (best draft, reasons left)."""
        from harvey.spamcheck import REVIEW, describe

        score, reasons = self._risk(result, need)
        if score < REVIEW:
            return result, []
        retry = await self.brain.think_json(
            prompt + "\n\nYour previous draft would land in the junk folder or read as a campaign. A filter flagged: "
            + describe(reasons) + ". Rewrite it in the first person, in plain, specific words a real customer "
            "would use, with the thing itself (what, how much, where, when) from the NEED in the first two "
            "sentences, one request question, none of those phrases, same facts, same JSON.",
            session_id="harvey-writer", agent="writer", task=task,
        )
        if isinstance(retry, dict) and retry.get("body"):
            score2, reasons2 = self._risk(retry, need)
            if score2 < score:
                result, score, reasons = retry, score2, reasons2
        return result, (reasons if score >= REVIEW else [])

    def _forbidden_terms(self, prospects: list) -> set[str]:
        """Names that must not appear in an inquiry email: the recipients'
        companies and domains, and the sender's own company."""
        if not self.inquiry:
            return set()
        # The sender may say who they are (half a line, no pitch); only the
        # recipient's names and domains are kept out.
        terms = set()
        for p in prospects:
            domain = (p.email or "").rsplit("@", 1)[-1].lower()
            if domain and domain not in names.PUBLIC_DOMAINS:
                terms.add(domain)
                label = names.company_display_name("", domain)
                if len(label) >= 3 and not label.isdigit():
                    terms.add(label)
            for raw in (p.company or "", names.company_display_name(p.company, domain)):
                raw = raw.strip()
                if len(raw) >= 3 and raw.lower() not in {"your team", "the company", "unknown"}:
                    terms.add(raw)
        return terms

    @property
    def inquiry(self) -> bool:
        outreach = getattr(self.config, "outreach", None)
        return getattr(outreach, "mode", "offer") == "inquiry"

    @property
    def _persona_first(self) -> str:
        name = (self.config.persona.name or "").strip()
        return name.split()[0] if name else "me"

    def _need(self, sector: str = "") -> dict:
        """The sender's need for this kind of business (see enquiry.need_for)."""
        return enquiry.need_for(getattr(self.config, "outreach", None), sector)

    def _base_prompt(self, sector: str = "") -> str:
        """The system brief. Inquiry mode gets its own prompt (who is
        writing, the need for this kind of business, worked examples) and
        none of the sales skill files; offer mode keeps the cold-email
        prompt plus skills."""
        persona = self.config.persona
        if self.inquiry:
            need = self._need(sector)
            intent = need["need"] or (self.config.outreach.intent or "").strip() or "to work with them as a client or partner"
            if need["question"]:
                intent += f"\nThe question to end on, in {persona.name}'s words: {need['question']}"
            product = self.config.product
            description = (product.description or "").strip()
            context = (f"BACKGROUND, in {persona.name}'s words (a fact from it may help them answer; never paste it, "
                       f"never a pitch, never benefits): {description}" if description else "")
            prompt = self.brain.load_prompt(
                "inquiry_writer",
                persona_name=persona.name, persona_first=self._persona_first, persona_tone=persona.tone,
                sender_line=enquiry.sender_line(self.config), intent=intent, request_context=context,
                sector_ask=enquiry.sector_ask(sector) if sector else
                "Decide from the FACTS what this business does and ask for that; how much, where and when come from the NEED.",
            )
            if prompt:
                return prompt + self._ask_block() + self._goal_block()
            return (f"You are writing enquiry emails for {persona.name} ({enquiry.sender_line(self.config)}), a "
                    f"prospective customer or partner of the recipient. What {persona.name} needs: {intent}. "
                    f"Tone: {persona.tone}. Never pitch; first person; one request question.") + self._ask_block() + self._goal_block()
        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=persona.name, persona_company=persona.company,
            persona_role=persona.role, persona_tone=persona.tone,
        ) or (f"You are {persona.name}, {persona.role} at {persona.company}.\n"
              f"Your tone is: {persona.tone}\n\nProduct: {self.config.product.name}\n"
              f"Description: {self.config.product.description}")
        if self.skills:
            prompt += "\n\n" + self.skills
        return prompt + self._ask_block() + self._goal_block()

    def _goal_block(self) -> str:
        """The campaign's goal in the sender's words, when they gave one:
        every email, follow-up and rewrite is written to serve it."""
        outreach = getattr(self.config, "outreach", None)
        goal = (getattr(outreach, "goal", "") or "").strip()
        if not goal:
            return ""
        first = self._persona_first
        return ("\n\nCAMPAIGN GOAL, in " + self.config.persona.name + "'s words (every email serves this; when it says "
                "how an email should end or what it should ask, do exactly that, in " + first + "'s own words rather than "
                "copying the wording, and it outranks the shape of the examples):\n" + goal)

    @property
    def _subject_rule(self) -> str:
        if self.inquiry:
            return ("- Subject: sentence case, 3 to 10 words, the thing itself as a customer would type it, in the "
                    "email's own words (see SUBJECT LINE above): what, how much, where or when. No company name, "
                    "domain or person's name; never \"quick question\", \"opportunity\", \"proposal\", "
                    "\"introduction\", \"following up\" or anything about the act of writing.")
        return ("- Subject: 3 to 8 words, sentence case, the thing the email is about in its own words; no prospect "
                "name, no \"quick question\", \"opportunity\", \"proposal\", \"partnership\" or \"following up\".")

    def _fix_subject(self, subject: str, body: str, need: str = "") -> str:
        """A subject that fails the subject check is replaced by a plain one
        built from the sender's need (or the email's first sentence)."""
        from harvey.spamcheck import SUBJECT_REVIEW, describe, subject_from_need, subject_risk
        score, reasons = subject_risk(subject, body, need)
        if score < SUBJECT_REVIEW:
            return subject
        plain = subject_from_need(need, body)
        if plain and subject_risk(plain, body, need)[0] < SUBJECT_REVIEW:
            logger.info(f"Writer: subject \"{subject}\" replaced ({describe(reasons)}) with \"{plain}\".")
            return plain
        return subject

    @property
    def greeting_word(self) -> str:
        outreach = getattr(self.config, "outreach", None)
        return getattr(outreach, "greeting", "") or "Hi"

    @property
    def style(self) -> str:
        outreach = getattr(self.config, "outreach", None)
        return (getattr(outreach, "style", "") or "concise").strip().lower()

    def _style_block(self) -> str:
        """How much room and warmth the writer gets. 'concise' is the
        classic sub-75-word cold email; 'engaging' is a longer, more human
        note built to earn a reply."""
        if self.inquiry or self.style != "engaging":
            return ""
        return """

STYLE: ENGAGING. Write like a real person who is genuinely curious about this
recipient, not like a template. The reader should feel the email was written for
them alone and want to answer it.
- Open with the one most specific, true thing you know about THEM (from the
  facts), said in your own words. Never open with "I hope this finds you well",
  "My name is" or "I came across".
- Show you understood what they do and why it matters, in one or two sentences.
  Make a small, concrete connection to what the sender wants.
- Ask ONE easy question that is quick to answer and invites a conversation
  (an opinion, a yes/no about fit, how they usually start). Not "are you free
  for a call" unless THE ASK below says that is what the sender wants.
- Warm, direct, a touch of personality. Short paragraphs. No bullet points,
  no bold, no exclamation marks, no flattery.
- Make it easy to reply: tell them a one-line answer is fine."""

    @property
    def _length_rule(self) -> str:
        if self.inquiry:
            return "Between 35 and 90 words, as short as the need allows"
        if self.style == "engaging":
            return "Between 90 and 130 words"
        return "Under 75 words"

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        if names.is_role_address(prospect.email):
            name_fact = ("- Name: unknown — this is a shared inbox; write to the team, "
                         "do not invent a person")
        elif prospect.full_name():
            name_fact = f"- Name: {prospect.full_name()}"
        else:
            name_fact = "- Name: unknown (do not guess one)"
        company_name = names.company_display_name(
            prospect.company, (prospect.email or "").rsplit("@", 1)[-1])
        facts = [
            name_fact,
            f"- Title: {prospect.title or 'unknown'}",
            (f"- Company: {company_name or 'unknown'}" if not self.inquiry else
             "- Their company: (not to be named in the email; write to what it does)"),
        ]
        if prospect.industry and not self.inquiry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if self.inquiry:
            # A customer writes to what the business does, nothing else.
            if company and company.description:
                facts.append(f"- What this business does: {company.description}")
            else:
                facts.append(f"- What this business does: {enquiry.sector_noun(prospect.industry)}; nothing more "
                             "is known, so do not describe it")
            facts.append(f"- Who reads it: {enquiry.INQUIRY_TIER_NOTES[self.tier_of(prospect)]}")
            notes = enquiry.clean_notes(prospect.personalization_notes)
            if notes:
                facts.append(f"- Your notes on them: {notes}")
        else:
            if company:
                if company.description:
                    facts.append(f"- What the company says about itself: {company.description}")
                if company.tech_stack:
                    facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
                for signal in company.signals[:3]:
                    facts.append(
                        f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                    )
            if prospect.personalization_notes:
                facts.append(f"- Research notes: {prospect.personalization_notes}")
            if prospect.linkedin_url:
                facts.append(f"- LinkedIn: {prospect.linkedin_url}")

        prompt = self._base_prompt(sector=prospect.industry or "")
        prompt += self._style_block()

        prompt += f"""

Write ONE {'enquiry email' if self.inquiry else 'cold email'} (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect in
your email must come from these facts. If a fact isn't listed here, you
don't know it — do not invent funding rounds, mutual contacts, metrics,
or anything else:
{chr(10).join(facts)}

Requirements:
- {self._length_rule}. {("Say what " + self.config.persona.name + " needs from this business in the first two sentences, with the concrete details from the NEED, then half a line on who is writing, then ONE question that is a request. Follow the four examples, not the two wrong ones.") if self.inquiry else "One specific observation from the FACTS above, one question. No pitch, no product name."}
- Open with the greeting line "{names.greeting_for(prospect.first_name, prospect.email, prospect.company, self.greeting_word)}"
  on its own line, then a blank line. Use the person's first name only if it is
  in the FACTS; never invent or guess one.
{('- Sign off with "' + self._persona_first + '" alone on the last line.' + chr(10)) if self.inquiry else ''}- Write the actual text (no merge variables — you know their name/company).
- Follow every rule above.

{self._subject_rule}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        need = self._need(prospect.industry or "") if self.inquiry else {"need": ""}
        if self.inquiry:
            leaked = self._copied(f"{result.get('subject', '')} {result.get('body', '')}", [prospect])
            if leaked:
                # The model copied a fact or a sentence from the worked examples. Once more, pointed out.
                retry = await self.brain.think_json(
                    prompt + "\n\nYour previous draft copied from the examples (" + "; ".join(f'"{x}"' for x in leaked[:4])
                    + "). Those are other people's facts and sentences. Say the same things in your own words, from the "
                    "NEED, WHO IS WRITING and the FACTS only. Same JSON.",
                    session_id="harvey-writer", agent="writer", task="personal_email",
                )
                if isinstance(retry, dict) and retry.get("body"):
                    result = retry
        forbidden = self._forbidden_terms([prospect])
        if forbidden and _mentions(f"{result.get('subject', '')} {result.get('body', '')}", forbidden):
            # Once more, with the slip pointed out; then whatever is left is scrubbed.
            retry = await self.brain.think_json(
                prompt + "\n\nYour previous draft named a company or web address on their side (" +
                ", ".join(sorted(forbidden)[:4]) + "). Rewrite it without naming the recipient's company "
                "and without any web address. Same JSON.",
                session_id="harvey-writer", agent="writer", task="personal_email",
            )
            if isinstance(retry, dict) and retry.get("body"):
                result = retry
        result, spam_reasons = await self._spam_safe(prompt, result, "personal_email", need=need["need"])
        subject = _scrub(str(result.get("subject") or "").strip(), forbidden)
        body = _scrub(str(result.get("body") or "").strip(), forbidden)
        if self.inquiry:
            body = _plain_signoff(body, self._persona_first)
        body = names.ensure_greeting(body, prospect.first_name, prospect.email, prospect.company, self.greeting_word)
        subject = self._fix_subject(subject, body, need["need"])
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000], "spam_reasons": spam_reasons}

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        if self.inquiry:
            # Company names are never written, so they are not shown either.
            prospect_summary = "\n".join(
                f"- {p.full_name() or 'name unknown'}, {p.title or 'title unknown'}"
                + (f" | Notes: {enquiry.clean_notes(p.personalization_notes)}" if enquiry.clean_notes(p.personalization_notes) else "")
                for p in prospects[:5])
        else:
            prospect_summary = "\n".join(
                f"- {p.full_name() or 'name unknown'}, {p.title or 'title unknown'} at "
                f"{names.company_display_name(p.company, (p.email or '').rsplit('@', 1)[-1]) or 'a company'}"
                + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
                for p in prospects[:5]  # Show sample for context
            )

        prompt = self._base_prompt(sector=(prospects[0].industry or "") if prospects else "")
        prompt += self._style_block()
        prompt += self._segment_block(prospects)

        if self.inquiry:
            steps_brief = f"""- Email 1: the enquiry itself, exactly like the four examples. {self._length_rule}. The need in the
  first two sentences with its concrete details, half a line on who is writing, ONE question that is a request.
- Email 2: 3 days later, under 50 words, same voice. One line naming the thing again (the block, the rebar,
  the savings), one line that it is still open, one question: can you take it on. New subject in the same style.
- Email 3: 4 days after that, under 35 words. A plain close in the first person: it is still open, and a
  line back is welcome if it is something you do. No new ask, no question needed.
  (Rules 5 and 6 of the brief are about email 1; emails 2 and 3 follow the lines above.)"""
        else:
            steps_brief = f"""- Email 1: Personalized cold observation + one question. {self._length_rule}. No pitch.
- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. {self._length_rule}.
- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping."""

        prompt += f"""

Write a 3-email {'enquiry' if self.inquiry else 'cold outreach'} sequence for people like these:
{prospect_summary}

Requirements:
{steps_brief}
- Every email opens with the greeting line "{self.greeting_word} {{{{first_name}}}}," on its
  own line, then a blank line. Use {{{{first_name}}}} ONLY there (it is swapped for
  "there" or the team when a name is unknown).
{("- Do NOT use {{company}} or {{title}} anywhere: the recipient's company is never named. "
   "Email 1 must still read as written for people in this line of work (use the segment above).")
  if self.inquiry else
  "- Use {{company}} and {{title}} as merge variables where natural — every email\n"
  "  must use at least one, and email 1 must reference something specific to\n"
  "  these prospects' industry or role (use the notes above)."}
- {"Write as the customer, never as a seller. Sign each email off with " + self._persona_first + " alone on its last line." if self.inquiry else "Never be pushy or salesy."}
{self._subject_rule}
- Follow every rule above. No exceptions.

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        steps = self._parse_sequence(result, forbidden=self._forbidden_terms(prospects))
        if steps:
            first = {"subject": steps[0].subject, "body": steps[0].body}
            single = (prompt.split("Return ONLY a JSON array")[0]
                      + "Write ONLY email 1 (step 1) of that sequence. Return ONLY a JSON object: "
                        '{"subject": "...", "body": "..."}')
            fixed, reasons = await self._spam_safe(single, first, "sequence",
                                                   need=self._need(prospects[0].industry or "")["need"] if self.inquiry else "")
            if fixed is not first:
                steps[0].subject = _scrub(str(fixed.get("subject") or steps[0].subject), self._forbidden_terms(prospects))
                body = _scrub(str(fixed.get("body") or steps[0].body), self._forbidden_terms(prospects))
                steps[0].body = _plain_signoff(body, self._persona_first) if self.inquiry else body
            seq_need = self._need(prospects[0].industry or "")["need"] if self.inquiry else ""
            for st in steps:
                # Every step's subject names the thing; a follow-up that fails
                # keeps the first email's subject so the thread stays one thread.
                fixed_subject = self._fix_subject(st.subject, st.body, seq_need)
                st.subject = fixed_subject if (st is steps[0] or fixed_subject != st.subject and fixed_subject) else st.subject
                if st is not steps[0] and st.subject != fixed_subject:
                    st.subject = steps[0].subject
            if self.inquiry:
                leaked = self._copied(f"{steps[0].subject} {steps[0].body}", prospects)
                if leaked:
                    # The segment's email copied a fact or a sentence from the worked examples: once more, pointed out.
                    retry = await self.brain.think_json(
                        single + "\n\nYour previous draft copied from the examples (" + "; ".join(f'"{x}"' for x in leaked[:4])
                        + "). Those are other people's facts and sentences. Say the same things in your own words, from "
                        "the NEED, WHO IS WRITING and the SEGMENT only. Same JSON.",
                        session_id="harvey-writer", agent="writer", task="sequence")
                    if isinstance(retry, dict) and retry.get("body") and not self._copied(
                            f"{retry.get('subject', '')} {retry.get('body', '')}", prospects):
                        steps[0].subject = _scrub(str(retry.get("subject") or steps[0].subject), self._forbidden_terms(prospects))[:120]
                        steps[0].body = _plain_signoff(_scrub(str(retry.get("body")), self._forbidden_terms(prospects)), self._persona_first)
                    else:
                        reasons = reasons + [f"copied from the examples: {', '.join(leaked[:3])}"]
                        logger.warning(f"Writer: the first email still carries a fact from the examples ({', '.join(leaked[:3])}); "
                                       "the sender will hold it for review.")
            if reasons:
                logger.warning("Writer: the first email still reads like spam to a filter "
                               f"({'; '.join(reasons[:4])}); the sender will hold it for review.")
            if self.inquiry and not self.personalize_each and not reasons:
                await self._add_variants(steps[0], single, prospects)
        return steps

    VARIANTS = 2  # other wordings of email 1 per segment, on top of the original

    def _copied(self, text: str, prospects: list) -> list[str]:
        """What a draft took from the brief's worked examples: their facts
        and their sentences (six words in a row). The sender's own facts
        never count."""
        return enquiry.example_copying(text, self._leak_allowed(prospects), "inquiry_writer")

    @property
    def _concurrency(self) -> int:
        """Model calls in flight at once (brain.concurrency in harvey.yaml)."""
        try:
            return max(1, min(16, int(getattr(self.config.brain, "concurrency", MODEL_CONCURRENCY) or MODEL_CONCURRENCY)))
        except (TypeError, ValueError):
            return MODEL_CONCURRENCY

    async def room(self) -> tuple[int, int, int]:
        """(room, ahead, backlog): how many more first emails the writer may
        draft now. It stays far enough ahead that the sender never waits on
        it (two days of the daily limit, twenty send batches, never under a
        hundred in per-person mode) without drafting a 10,000-row upload at
        once; segment mode is cheap and looks further ahead."""
        email = self.config.channels.email
        max_daily = int(email.max_daily_sends or 0)
        per_cycle = int(getattr(email, "sends_per_cycle", 0) or 0)
        ahead = 2 * max(1, max_daily) if max_daily else 200
        ahead = max(ahead, 20 * per_cycle, 100)
        if not self.personalize_each:
            ahead = max(ahead, SEGMENT_ROOM, 10 * per_cycle)
        approval = bool(getattr(email, "require_approval", True))
        backlog = await self.state.count_outbox_queued(
            statuses=("pending_review", "approved") if approval else ("approved",))
        return max(0, ahead - backlog), ahead, backlog

    def _leak_allowed(self, prospects: list) -> str:
        """Facts that are the sender's own (the need, who they are, their
        background) and therefore never count as copied from the examples."""
        need = self._need(prospects[0].industry or "") if prospects else {"need": "", "question": ""}
        description = (getattr(self.config.product, "description", "") or "")
        return " ".join([need["need"], need.get("question", ""), enquiry.sender_line(self.config), description])

    async def _add_variants(self, step: EmailStep, single_prompt: str, prospects: list) -> None:
        """Two more wordings of the first email, same facts, so a segment
        of a hundred people does not receive one identical body (which
        mail providers read as bulk). Each must pass the same checks."""
        forbidden = self._forbidden_terms(prospects)
        need = self._need(prospects[0].industry or "")["need"] if prospects else ""
        subjects, bodies = [], []
        for n in range(self.VARIANTS):
            prior = "\n\n".join(f"Subject: {s}\n{b}" for s, b in zip([step.subject] + subjects, [step.body] + bodies))
            prompt = (single_prompt.replace("Write ONLY email 1 (step 1) of that sequence.",
                                            "Write email 1 again in different wording.")
                      + "\n\nIt must say the same things from the NEED, to the same kind of reader, but in different "
                        "words: a different opening sentence, a different order, a different subject line of the same "
                        "kind, the same length. Not a paraphrase line by line; a note the same person might have "
                        "typed on another day. These are the wordings already written, do not reuse their sentences:\n\n"
                      + prior)
            try:
                result = await self.brain.think_json(prompt, session_id="harvey-writer", agent="writer", task="sequence_variant")
            except Exception as e:
                logger.debug(f"Writer: variant {n + 1} failed: {e}")
                break
            if not isinstance(result, dict):
                break
            subject = _scrub(str(result.get("subject") or "").strip().replace("{{company}}", "your team"), forbidden)
            body = _plain_signoff(_scrub(str(result.get("body") or "").strip().replace("{{company}}", "your team"), forbidden),
                                  self._persona_first)
            if not subject or not body or body == step.body:
                continue
            if self._copied(f"{subject} {body}", prospects):
                continue
            score, _why = self._risk({"subject": subject, "body": body}, need)
            if score >= 4:
                continue
            subject = self._fix_subject(subject, body, need)
            subjects.append(subject[:120])
            bodies.append(body[:2000])
        if bodies:
            step.subjects, step.bodies = subjects, bodies
            logger.info(f"Writer: email 1 has {len(bodies) + 1} wordings; the sender rotates through them.")

    def _parse_sequence(self, result, forbidden: set | None = None) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            if self.inquiry:
                # No company on either side, ever: merge fields and names alike.
                subject = _scrub(subject.replace("{{company}}", "your team").replace("{{title}}", ""),
                                 forbidden or set())
                body = _plain_signoff(_scrub(body.replace("{{company}}", "your team").replace("{{title}}", ""),
                                             forbidden or set()), self._persona_first)
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    @property
    def personalize_each(self) -> bool:
        outreach = getattr(self.config, "outreach", None)
        return bool(getattr(outreach, "personalize_each", True))

    @staticmethod
    def tier_of(prospect) -> str:
        """The role tier a contact reads as: executive | manager | individual | admin."""
        seniority = (prospect.seniority or "").strip().lower()
        if seniority in _SENIORITY_TO_TIER:
            return _SENIORITY_TO_TIER[seniority]
        title = (prospect.title or "").lower()
        local = (prospect.email or "").split("@")[0].lower()
        if any(w in title for w in ("assistant", "receptionist", "front desk", "office manager",
                                    "secretary", "coordinator", "administrator")) \
                or local in ("info", "hello", "contact", "admin", "office", "enquiries", "inquiries"):
            return "admin"
        if any(w in title for w in ("chief", "ceo", "cfo", "coo", "cto", "founder", "owner", "partner",
                                    "president", "vp", "vice president", "director", "head of",
                                    "managing", "principal")):
            return "executive"
        if "manager" in title or "lead" in title or "supervisor" in title:
            return "manager"
        return "individual"

    async def _classify(self, prospects: list):
        """Ask the brain which sector and role tier each unsorted contact
        belongs to, in batches, and record it on the prospect (and the
        company's industry when it has none)."""
        unsorted = [p for p in prospects if not p.industry or not p.seniority]
        if not unsorted:
            return
        sorted_count = 0
        batches = [unsorted[i:i + CLASSIFY_BATCH] for i in range(0, len(unsorted), CLASSIFY_BATCH)]
        gate = asyncio.Semaphore(self._concurrency)

        async def ask(batch):
            async with gate:
                return await self._classify_prompt(batch)

        results = await asyncio.gather(*(ask(b) for b in batches), return_exceptions=True)
        for batch, result in zip(batches, results):
            if isinstance(result, Exception):
                logger.warning(f"Writer: classification failed for a batch: {result}; will retry next cycle.")
                continue
            if not isinstance(result, list):
                logger.warning("Writer: classification returned no usable list; will retry next cycle.")
                continue
            by_id = {p.id: p for p in batch}
            seen = set()
            for item in result:
                if not isinstance(item, dict):
                    continue
                p = by_id.get(str(item.get("id") or ""))
                if not p:
                    continue
                sector = str(item.get("sector") or "other").strip().lower().replace(" ", "_").replace("-", "_")
                tier = str(item.get("tier") or "").strip().lower()
                dept = str(item.get("department") or "other").strip().lower().replace(" ", "_")
                sector = sector if sector in SECTORS else "other"
                tier = tier if tier in TIERS else self.tier_of(p)
                if names.is_role_address(p.email):
                    tier = "admin"
                elif tier == "admin" and not _ADMIN_TITLE_RE.search(p.title or ""):
                    tier = "manager"  # a person, not a shared inbox
                dept = dept if dept in DEPARTMENTS else "other"
                await self.state.update_prospect_segment(p.id, industry=sector, seniority=tier, department=dept)
                if p.company_id:
                    await self.state.update_company_industry(p.company_id, sector)
                p.industry, p.seniority, p.department = sector, tier, dept
                seen.add(p.id)
                sorted_count += 1
            # Anyone the model skipped gets a deterministic fallback so the
            # same contacts are not re-sent for classification every cycle.
            for p in batch:
                if p.id not in seen:
                    tier = self.tier_of(p)
                    await self.state.update_prospect_segment(p.id, industry=p.industry or "other",
                                                             seniority=tier)
                    p.industry, p.seniority = p.industry or "other", tier
        if sorted_count:
            logger.info(f"Writer: sorted {sorted_count} contact(s) into sectors and role tiers.")
            await self.state.log_action(action_type="classify", agent="writer",
                                        details={"sorted": sorted_count})

    async def _classify_prompt(self, batch: list):
        """One classification call for one batch of contacts."""
        rows = []
        for p in batch:
            company = None
            if p.company_id:
                try:
                    company = await self.state.get_company(p.company_id)
                except Exception:
                    company = None
            rows.append({
                "id": p.id, "name": p.full_name(), "title": p.title,
                "company": p.company or (company.name if company else ""),
                "domain": company.domain if company else (p.email.split("@")[-1] if p.email else ""),
                "about": ((company.description or "") if company else "")[:300],
                "notes": (p.personalization_notes or "")[:300],
            })
        prompt = (
            "Sort these contacts by the SECTOR their company works in and the ROLE TIER of the "
            "person, from the facts given (title, company, domain, what the company says about "
            "itself, notes). Use the domain and company name when the title is missing. Never "
            "invent facts; when unsure, pick the closest sector and the tier the title implies.\n\n"
            f"SECTORS (pick exactly one): {', '.join(SECTORS)}\n"
            f"TIERS (pick exactly one): {', '.join(TIERS)} — executive = owners, partners, "
            "C-level, VPs, directors; manager = runs a team or function; individual = a "
            "specialist doing the work; admin = ONLY assistants, office managers, reception, or "
            "a shared inbox like info@ or office@. A personal address (a name before the @) with "
            "no title is NOT admin: choose manager.\n"
            f"DEPARTMENTS (pick exactly one): {', '.join(DEPARTMENTS)}\n\n"
            f"CONTACTS (JSON): {json.dumps(rows, ensure_ascii=False)}\n\n"
            'Return ONLY a JSON array like [{"id": "...", "sector": "...", "tier": "...", '
            '"department": "..."}] with one entry per contact id.'
        )
        return await self.brain.think_json(
            prompt, session_id="harvey-writer-classify", agent="writer", task="classify")

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches: one sector, one role tier."""
        batches: dict[str, list] = {}
        for prospect in prospects:
            sector = (prospect.industry or "other").lower()
            key = f"{sector} · {self.tier_of(prospect)}"
            batches.setdefault(key, []).append(prospect)

        # Cap batch size. Per-person mode stays small because email 1 is
        # drafted per prospect (one model call each); leftovers ride the
        # next cycle. Segment mode writes once per segment, so it can be big.
        if not self.is_native:
            cap = LEGACY_BATCH_CAP
        elif self.personalize_each:
            cap = NATIVE_BATCH_CAP
        else:
            cap = SEGMENT_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}

    def _segment_block(self, prospects: list) -> str:
        """Who this campaign is for: the sector and the role tier, and how
        the position changes the message."""
        if not prospects:
            return ""
        raw = prospects[0].industry or "other"
        sector = raw.replace("_", " ")
        tier = self.tier_of(prospects[0])
        if self.inquiry:
            return (
                f"\n\nSEGMENT: everyone this goes to works in {sector}, and the readers are {tier}s. "
                f"Write it to a single reader, {enquiry.sector_noun(raw)}, as if {self._persona_first} had picked that one today; "
                "it must read as a letter to one person, and it will be sent to others like them one at a time. "
                f"{enquiry.INQUIRY_TIER_NOTES[tier]} Shape the request for what a {sector} business does, using "
                "WHAT A CUSTOMER ASKS THIS KIND OF BUSINESS FOR above, without describing their business to them. "
                "The only merge field is {{first_name}} in the greeting line."
            )
        return (
            f"\n\nSEGMENT: everyone in this campaign works in {sector}, and the readers are "
            f"{tier}s. {TIER_GUIDANCE[tier]} Write for what a {sector} business does. "
            "The merge fields fill in the person and company."
        )
