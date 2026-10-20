"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import re
import logging
from datetime import datetime, timezone

from harvey import names
from harvey.brain import Brain
from harvey.config import HarveyConfig
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import StateManager

logger = logging.getLogger("harvey.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50
# Segment mode (outreach.personalize_each: false): one sequence per
# sector × role segment, sent to everyone in it, so a segment can be large.
SEGMENT_BATCH_CAP = 250
SEGMENT_ROOM = 500
# Team pages fetched per cycle to recover names the upload left out; the
# rest wait for the next cycle, so a 1-minute heartbeat stays quick.
MAX_NAME_LOOKUPS = 15  # contacts considered per cycle in segment mode
CLASSIFY_BATCH = 25  # contacts per classification call

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
def _mentions(text: str, terms: set[str]) -> bool:
    low = (text or "").lower()
    return any(t.lower() in low for t in terms if t)


def _scrub(text: str, terms: set[str]) -> str:
    """Replace company names and domains with neutral wording, so an email
    never names the recipient's business or the sender's. Longest terms
    first so "Acme Roofing" goes before "Acme"."""
    if not text or not terms:
        return text
    for term in sorted((t for t in terms if t), key=len, reverse=True):
        esc = re.escape(term)
        # "Avrum here from Wandlp." → "Avrum here."; "the team at Acme" → "the team".
        tail = r"(?!\w)(?!\.\w)"   # not inside a longer word, not the start of a domain
        text = re.sub(rf"(?i)\s+\b(?:at|from|of)\s+{esc}{tail}", "", text)
        # "working with Acme" → "working with you"; "a quote for Acme" → "a quote for you".
        text = re.sub(rf"(?i)\b(with|for|to)\s+{esc}{tail}", r"\1 you", text)
        text = re.sub(rf"(?i)\b{esc}['’]s{tail}", "your", text)
        text = re.sub(rf"(?i)(?<![\w@.]){esc}{tail}", "you", text)
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

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
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
            max_daily = int(self.config.channels.email.max_daily_sends or 0)
            # No daily limit: keep roughly a day of 15-minute cycles ahead.
            ahead = 2 * max(1, max_daily) if max_daily else 200
            if not self.personalize_each:
                ahead = max(ahead, SEGMENT_ROOM)
            backlog = await self.state.count_outbox_queued()
            room = max(0, ahead - backlog)
            if room == 0:
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

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            sequence = await self._write_sequence(prospects)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.update_prospect_status(p.id, "queued")

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts —
            # unless the operator chose one email per segment.
            if self.is_native and self.personalize_each:
                await self._personalize_first_emails(campaign_id, prospects)

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
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
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
                    item_id, error="Held for your review: a spam filter would likely flag "
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

    def _outreach_block(self) -> str:
        """Inquiry mode: the sender is the prospective client or partner,
        and each email asks for what THIS recipient's business does."""
        outreach = getattr(self.config, "outreach", None)
        if not outreach or outreach.mode != "inquiry":
            return ""
        persona = self.config.persona
        intent = outreach.intent.strip() or "to work with them as a client or partner"
        return f"""

OUTREACH MODE: INQUIRY. You are NOT selling anything. {persona.name} is writing as a
prospective client or partner. What {persona.name} wants from the recipient: {intent}.
- Work out from the facts what this person's business actually does, and ask for THAT.
  A financial advisor is asked about taking {persona.name} on as a client. A construction
  company is asked about a job {persona.name} wants done. A supplier, agency or studio is
  asked about working together. If the facts don't say what they do, ask what they handle
  before asking for anything.
- Say the specific thing you want in one plain sentence, then ask ONE practical question
  (availability, how they usually start, what they'd need from you).
- Do not describe or pitch "{self.config.product.name}" as a product. Who you are takes
  half a sentence at most.
- Never invent a project detail, budget, timeline or location that isn't in the facts or
  in what {persona.name} wants."""

    async def _find_missing_names(self, prospects) -> int:
        """Fill in names the upload left blank: from the LinkedIn URL or a
        first.last address first (free), then from the company's team page
        (one fetch per domain, at most MAX_NAME_LOOKUPS pages a cycle),
        matched against the address. Shared inboxes are left alone. Returns
        how many names were found."""
        found = 0
        lookups = 0
        pages: dict[str, list] = {}
        scout = None
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
                if domain in names.PUBLIC_DOMAINS:
                    continue
                if domain not in pages:
                    if lookups >= MAX_NAME_LOOKUPS:
                        continue
                    lookups += 1
                    if scout is None:
                        from harvey.agents.scout import Scout
                        scout = Scout(self.brain, self.state, self.config, self.env)
                    try:
                        pages[domain] = await scout._scrape_team_page(domain)
                    except Exception as e:
                        logger.debug(f"Writer: team page for {domain} unreadable: {e}")
                        pages[domain] = []
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
            campaign_id = await self.state.add_campaign(campaign)
            ids.append(campaign_id)
            for p in group:
                await self.state.update_prospect_status(p.id, "queued")
            await self.state.log_action(
                action_type="write_campaign", agent="writer",
                details={"campaign_id": campaign_id, "campaign_name": name,
                         "prospect_count": len(group), "steps": len(campaign.sequence),
                         "template": True})
            logger.info(f"Writer: queued '{name}' for {len(group)} recipient(s).")
        return ids

    async def _spam_safe(self, prompt: str, result: dict, task: str) -> tuple[dict, list[str]]:
        """Score a draft; if it reads like spam, ask once for a rewrite with
        the findings spelled out. Returns (best draft, reasons left)."""
        from harvey.spamcheck import REVIEW, describe, spam_risk

        score, reasons = spam_risk(str(result.get("subject", "")), str(result.get("body", "")))
        if score < REVIEW:
            return result, []
        retry = await self.brain.think_json(
            prompt + "\n\nYour previous draft would land in the junk folder. A filter flagged: "
            + describe(reasons) + ". Rewrite it in plain, specific words a real customer would use, "
            "with none of those, same facts, same JSON.",
            session_id="harvey-writer", agent="writer", task=task,
        )
        if isinstance(retry, dict) and retry.get("body"):
            score2, reasons2 = spam_risk(str(retry.get("subject", "")), str(retry.get("body", "")))
            if score2 < score:
                result, score, reasons = retry, score2, reasons2
        return result, (reasons if score >= REVIEW else [])

    def _forbidden_terms(self, prospects: list) -> set[str]:
        """Names that must not appear in an inquiry email: the recipients'
        companies and domains, and the sender's own company."""
        if not self.inquiry:
            return set()
        terms = set()
        own = (self.config.persona.company or "").strip()
        if len(own) >= 3:
            terms.add(own)
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

    def _base_prompt(self) -> str:
        """The system brief. Inquiry mode gets its own prompt and none of
        the sales skill files; offer mode keeps the cold-email prompt plus
        skills."""
        persona = self.config.persona
        if self.inquiry:
            outreach = self.config.outreach
            intent = (outreach.intent or "").strip() or "to work with them as a client or partner"
            product = self.config.product
            context = ""
            if product.description and product.description.strip() and product.name != persona.company:
                context = f"More about the request, in {persona.name}'s words: {product.description.strip()}"
            elif product.description and product.description.strip():
                context = f"Background: {product.description.strip()}"
            prompt = self.brain.load_prompt(
                "inquiry_writer",
                persona_name=persona.name, persona_company=persona.company,
                persona_role=persona.role or "", persona_tone=persona.tone,
                intent=intent, request_context=context,
            )
            if prompt:
                return prompt + self._ask_block()
            return (f"You are writing enquiry emails for {persona.name} ({persona.role} at "
                    f"{persona.company}), a prospective customer or partner of the recipient. "
                    f"What {persona.name} wants: {intent}. Tone: {persona.tone}. Never pitch.") + self._ask_block()
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
        return prompt + self._outreach_block() + self._ask_block()

    @property
    def _subject_rule(self) -> str:
        if self.inquiry:
            return ("- Subject: sentence case, 3 to 8 words, the request itself as a customer would "
                    "type it (see SUBJECT LINE above). No company name, domain or person's name.")
        return "- Subject lines: lowercase, 2-5 words, no prospect name, no \"quick question\"."

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
        if self.style != "engaging":
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
            return "Between 70 and 120 words" if self.style == "engaging" else "Between 45 and 80 words"
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
             "- Company: (not to be named in the email; the facts below say what it does)"),
        ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
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

        prompt = self._base_prompt()
        prompt += self._style_block()

        prompt += f"""

Write ONE {'enquiry email' if self.inquiry else 'cold email'} (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect in
your email must come from these facts. If a fact isn't listed here, you
don't know it — do not invent funding rounds, mutual contacts, metrics,
or anything else:
{chr(10).join(facts)}

Requirements:
- {self._length_rule}. {("Say what " + self.config.persona.name + " needs from this business, shaped to what the FACTS say it does, and ask ONE easy question.") if self.inquiry else "One specific observation from the FACTS above, one question. No pitch, no product name."}
- Open with the greeting line "{names.greeting_for(prospect.first_name, prospect.email, prospect.company, self.greeting_word)}"
  on its own line, then a blank line. Use the person's first name only if it is
  in the FACTS; never invent or guess one.
- Write the actual text (no merge variables — you know their name/company).
- Follow every rule above.

{self._subject_rule}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        forbidden = self._forbidden_terms([prospect])
        if forbidden and _mentions(f"{result.get('subject', '')} {result.get('body', '')}", forbidden):
            # Once more, with the slip pointed out; then whatever is left is scrubbed.
            retry = await self.brain.think_json(
                prompt + "\n\nYour previous draft named a company or web address (" +
                ", ".join(sorted(forbidden)[:4]) + "). Rewrite it without naming any company on "
                "either side and without any web address. Same JSON.",
                session_id="harvey-writer", agent="writer", task="personal_email",
            )
            if isinstance(retry, dict) and retry.get("body"):
                result = retry
        result, spam_reasons = await self._spam_safe(prompt, result, "personal_email")
        subject = _scrub(str(result.get("subject") or "").strip(), forbidden)
        body = names.ensure_greeting(
            _scrub(str(result.get("body") or "").strip(), forbidden), prospect.first_name,
            prospect.email, prospect.company, self.greeting_word)
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000], "spam_reasons": spam_reasons}

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name() or 'name unknown'}, {p.title or 'title unknown'} at "
            f"{names.company_display_name(p.company, (p.email or '').rsplit('@', 1)[-1]) or 'a company'}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )

        prompt = self._base_prompt()
        prompt += self._style_block()
        prompt += self._segment_block(prospects)

        if self.inquiry:
            steps_brief = f"""- Email 1: the enquiry itself. {self._length_rule}. What {self.config.persona.name} needs,
  shaped to what this kind of business does, one or two details that let them answer, ONE easy question.
- Email 2: 3 days later, under 60 words. A short nudge: one line restating the need, then ask
  whether they can take it on or who could. New subject, same request.
- Email 3: 4 days after that, under 40 words. A courteous close: if it is not something they
  handle, no reply needed; if it is, a line back is plenty."""
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
- Never be pushy or salesy.
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
            fixed, reasons = await self._spam_safe(prompt, first, "sequence")
            if fixed is not first:
                steps[0].subject = _scrub(str(fixed.get("subject") or steps[0].subject), self._forbidden_terms(prospects))
                steps[0].body = _scrub(str(fixed.get("body") or steps[0].body), self._forbidden_terms(prospects))
            if reasons:
                logger.warning("Writer: the first email still reads like spam to a filter "
                               f"({'; '.join(reasons[:4])}); the sender will hold it for review.")
        return steps

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
                body = _scrub(body.replace("{{company}}", "your team").replace("{{title}}", ""),
                              forbidden or set())
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
        for start in range(0, len(unsorted), CLASSIFY_BATCH):
            batch = unsorted[start:start + CLASSIFY_BATCH]
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
            result = await self.brain.think_json(
                prompt, session_id="harvey-writer-classify", agent="writer", task="classify")
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
        sector = (prospects[0].industry or "other").replace("_", " ")
        tier = self.tier_of(prospects[0])
        shaping = (
            f"Shape the request for what a {sector} business does (a builder is asked about the job, "
            f"an advisor about taking {self.config.persona.name} on), without describing their business "
            "to them." if self.inquiry else
            f"Write for what a {sector} business does.")
        return (
            f"\n\nSEGMENT: everyone in this campaign works in {sector}, and the readers are "
            f"{tier}s. {TIER_GUIDANCE[tier]} {shaping} The merge fields fill in the person and company."
        )
