"""Configuration loader for Harvey. Reads harvey.yaml + .env."""

import logging
import os
import re
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from harvey.paths import CONFIG_FILE, LOCAL_CONFIG_FILE, PROJECT_ROOT

logger = logging.getLogger("harvey.config")


class ConfigError(Exception):
    """Raised when Harvey's configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Let the heartbeat go looking for new businesses on its own when the
    # pipeline runs low. Off when the operator supplies every recipient
    # (the hosted web app's upload flow) so Harvey only writes to them.
    auto_prospect: bool = True
    # Role keywords that indicate a company is in-market right now (a company
    # hiring a "Head of Growth" is buying growth tooling). Empty → falls back
    # to `titles`. Used for careers-page scanning and job-board discovery.
    hiring_signals: list[str] = []
    # Discovery needs a radius, not a place name. Maps each entry in
    # `geography` to "lat,lng,radius_km" — e.g.
    #   "Denver, CO": "39.7392,-104.9903,50"
    # Without one, listings providers can only match on the business name.
    geo_coordinates: dict[str, str] = {}


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "instantly" (legacy), "gmail" (Gmail/Workspace via API — recommended),
    # "microsoft" (Microsoft 365 / Outlook via Graph), or "smtp" (any
    # SMTP+IMAP mailbox: AgentMail, Fastmail, ...)
    provider: str = "instantly"
    max_daily_sends: int = 50
    # When True, also send to catch-all ("risky") domains, not just verified
    # mailboxes. Off by default — catch-alls accept everything, so a bad
    # guess still bounces.
    send_to_risky: bool = False
    # Copilot mode (native providers): every outgoing email waits in the
    # outbox for your approval (dashboard → Outbox, or `harvey outbox`).
    # Set false for full autopilot once you trust the output.
    require_approval: bool = True
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (measured over the trailing sends). 0 disables the switch.
    max_bounce_rate: float = 0.05
    # How many emails one heartbeat may send (with a few seconds between
    # each). Together with usage.heartbeat_interval_minutes this is the speed.
    sends_per_cycle: int = 8
    # How many emails are in flight at once (SMTP relays take several
    # connections; a personal Gmail or Outlook mailbox should stay at 1) and
    # the longest pause between two sends in seconds (the shortest is a
    # quarter of it). Together these are the pace inside one heartbeat.
    send_concurrency: int = 1
    send_jitter_seconds: float = 15.0
    # Native providers: drain the outbox (after reading replies) this often,
    # on its own clock, instead of once per heartbeat. 0 = once per heartbeat,
    # after research and writing, as before.
    send_every_seconds: int = 0
    # What the mailbox itself allows per day when the operator knows it (a
    # gmail.com address: 500; Google Workspace: 2,000; Microsoft 365: 10,000
    # recipients). 0 = unknown. Applied on top of max_daily_sends.
    mailbox_daily_ceiling: int = 0
    # Warm-up: a mailbox that has never sent cold email starts at 20 a day
    # and may send 10 more each day, up to max_daily_sends. Off = full speed
    # from day one (fine for a mailbox that already sends a lot).
    warmup_ramp: bool = False
    # The first email to each person goes as plain text, with a signature
    # stripped of links and the image: a text-only note from a stranger is
    # what mail providers accept; HTML, links and inline images in a first
    # contact are what they block as bulk. Replies and follow-ups carry the
    # full signature.
    plain_first_email: bool = True
    # Add a List-Unsubscribe header (mailto) so Gmail/Yahoo show "Unsubscribe"
    # and count the mail as compliant bulk rather than suspicious.
    list_unsubscribe: bool = True

    @field_validator("send_concurrency")
    @classmethod
    def _concurrency(cls, v: int) -> int:
        return max(1, min(int(v or 1), 16))

    @field_validator("send_every_seconds")
    @classmethod
    def _send_every(cls, v: int) -> int:
        v = int(v or 0)
        return 0 if v <= 0 else max(10, min(v, 3600))

    @field_validator("send_jitter_seconds")
    @classmethod
    def _jitter(cls, v: float) -> float:
        return max(0.0, min(float(v or 0), 120.0))

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v

    @field_validator("sends_per_cycle")
    @classmethod
    def _per_cycle_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("sends_per_cycle must be between 1 and 100")
        return v


class LinkedInChannelConfig(BaseModel):
    enabled: bool = True
    max_daily_connections: int = 20
    max_daily_messages: int = 10


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()


PRECLEARED_ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")


class PreclearedConfig(BaseModel):
    """Optional per-recipient cold-email check through Precleared.

    Off by default. When on, each new recipient is checked once, before the
    first cold email, against UK PECR, US CAN-SPAM, Canada's CASL, Germany's
    UWG and France's rules. Any verdict other than ok_to_email holds the
    email back with the reason. See harvey/integrations/precleared.py.
    """

    enabled: bool = False
    # Where your business is based: GB or US. Required when enabled.
    sender_country: str = ""
    # Where prospect addresses come from, in Precleared's terms. Harvey
    # builds most addresses from a learned pattern, so the honest default
    # is "unknown"; use "published" only if yours are copied from the
    # businesses' own websites.
    address_source: str = "unknown"

    @field_validator("sender_country", mode="before")
    @classmethod
    def _upper_country(cls, v) -> str:
        return str(v or "").strip().upper()

    @field_validator("address_source", mode="before")
    @classmethod
    def _valid_source(cls, v) -> str:
        v = str(v or "unknown").strip().lower()
        if v not in PRECLEARED_ADDRESS_SOURCES:
            raise ValueError(
                f"'{v}' is not a valid address_source. "
                f"Use one of: {', '.join(PRECLEARED_ADDRESS_SOURCES)}."
            )
        return v

    @model_validator(mode="after")
    def _country_when_enabled(self):
        if self.enabled and self.sender_country not in ("GB", "US"):
            raise ValueError(
                "compliance.precleared.sender_country must be GB or US "
                "when the check is enabled."
            )
        return self


class ComplianceConfig(BaseModel):
    precleared: PreclearedConfig = PreclearedConfig()


OUTREACH_MODES = ("offer", "inquiry")
ATTACH_POLICIES = ("first_reply", "every_reply", "never")


class SectorMessage(BaseModel):
    """One of the operator's own messages, for the sectors listed. Contacts
    in any other sector get the default message."""

    sectors: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    followup_1: str = ""
    followup_2: str = ""

    @field_validator("sectors", mode="before")
    @classmethod
    def _clean_sectors(cls, v) -> list[str]:
        if isinstance(v, str):
            v = [x for x in v.replace(",", " ").split() if x]
        out = []
        for x in v or []:
            x = str(x).strip().lower().replace(" ", "_").replace("-", "_")
            if x and x not in out:
                out.append(x)
        return out


class SectorNeed(BaseModel):
    """What the sender needs from one kind of business, in their own
    words, with the link to the full scope of it. The writer picks the
    row whose sectors contain the recipient's classified industry; the
    handler and the inbox auto-reply share the same row's link."""

    sectors: list[str] = Field(default_factory=list)
    need: str = ""
    question: str = ""        # optional: the one question to end on
    details_url: str = ""     # the full scope (a doc, a folder, a page)
    details_label: str = ""   # what to call it: "the project brief"

    @field_validator("sectors", mode="before")
    @classmethod
    def _clean_sectors(cls, v) -> list[str]:
        if isinstance(v, str):
            v = [x for x in re.split(r"[,\s]+", v) if x]
        return [str(x).strip().lower().replace(" ", "_") for x in (v or []) if str(x).strip()]

    @field_validator("details_url", mode="before")
    @classmethod
    def _http_url(cls, v) -> str:
        v = str(v or "").strip()
        if v and not v.lower().startswith(("http://", "https://")):
            v = "https://" + v
        return v


class SharedLink(BaseModel):
    """A document or folder the inbox auto-reply may send: its name, the
    link, and when it is the right thing to share."""

    label: str = ""
    url: str = ""
    when: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def _http_url(cls, v) -> str:
        v = str(v or "").strip()
        if v and not v.lower().startswith(("http://", "https://")):
            v = "https://" + v
        return v


# The closing ask a user can choose for every email, and how to phrase it.
ASK_THEMES = {
    "zoom": "a short video call",
    "phone": "a quick phone call",
    "coffee": "a coffee, if they are nearby",
    "in_person": "meeting in person at their office or on site",
    "visit": "a visit to their place, to see it first-hand",
    "email": "a short reply by email with the details, no call needed",
}


class OutreachConfig(BaseModel):
    """What kind of email this is, and how replies are answered.

    ``offer`` (the default): you have something to sell or propose; email 1
    makes an observation and asks a question, later emails introduce it.
    ``inquiry``: you are the prospective client or partner. Each email is
    written from what the recipient's business does: a financial advisor is
    asked about becoming a client, a builder about a job, an agency about a
    partnership. ``intent`` says in your words what you want from them.
    """

    mode: str = "offer"
    intent: str = ""
    # The campaign's goal in the sender's own words ("the first email is to
    # get permission to send the project brief; end on an open question
    # offering it"). Every first email, follow-up, rewrite and reply is
    # written to serve it.
    goal: str = ""
    # Inquiry mode: who is writing, in one line, as they would say it to a
    # stranger ("I run Greystone Development, a residential developer in
    # Baltimore"). Half a line of it goes into every email.
    sender_line: str = ""
    # The need per kind of business (the writer matches the recipient's
    # sector); ``intent`` is the need for everyone not covered. Each row
    # may carry the link to the full scope; ``details_url`` is the general
    # one, sent once per conversation when someone asks for details.
    sector_needs: list[SectorNeed] = Field(default_factory=list)
    details_url: str = ""
    details_label: str = ""
    # Standing instructions for every auto-reply ("offer a 20-minute call
    # on Tuesdays", "never discuss budget by email", ...).
    reply_instructions: str = ""
    # File names under HARVEY_HOME/attachments sent with replies.
    reply_attachments: list[str] = []
    attach_on: str = "first_reply"  # first_reply | every_reply | never
    # True: one model-written email per person (slower, most personal).
    # False: contacts are sorted into sector × role segments and one email
    # is written per segment, then sent to everyone in it with merge fields.
    personalize_each: bool = True
    # "concise": the classic under-75-word observation + question.
    # "engaging": 90–130 words with a hook, one useful detail, an
    # easy-to-answer question and a PS — written to earn a reply.
    style: str = "concise"
    # Signature appended to every outgoing email. The HTML version (with
    # links, and {{logo}} for the image) makes the mail multipart with a
    # plain-text alternative; the text version is used when HTML is empty.
    signature_html: str = ""
    signature_text: str = ""
    signature_image: str = ""  # file name under HARVEY_HOME/signature
    greeting: str = "Hi"       # the opening word: "Hi Jane," / "Hello team,"
    # What the sender would like to happen next, if anything: the closing
    # ask of every first email and where replies steer. One of ASK_THEMES
    # ("zoom", "phone", "coffee", "in_person", "visit", "email") or blank
    # for a plain question that invites a reply. ``ask_detail`` is a line
    # of specifics ("I'm in Lagos the week of the 20th", "Tuesdays after 2").
    ask: str = ""
    ask_detail: str = ""
    # Your own message instead of AI writing: subject + body for the first
    # email ({{first_name}} allowed; the greeting line is added if missing),
    # and optional follow-ups 3 and 7 days later (blank = none).
    use_template: bool = False
    template_subject: str = ""
    template_body: str = ""
    template_followup_1: str = ""
    template_followup_2: str = ""
    # Different versions of your own message for particular sectors; the
    # fields above are the default for everyone else.
    sector_messages: list[SectorMessage] = Field(default_factory=list)
    # Inbox auto-reply: answer new mail from people who are not on the
    # list, following inbox_instructions, sharing the right inbox_links.
    inbox_autoreply: bool = False
    inbox_instructions: str = ""
    inbox_links: list[SharedLink] = Field(default_factory=list)
    inbox_attach_files: bool = False
    # Also answer mail already in the inbox from the last N days that nobody
    # replied to (0 = only mail that arrives from now on).
    inbox_backlog_days: int = 0

    @field_validator("mode", mode="before")
    @classmethod
    def _valid_mode(cls, v) -> str:
        v = str(v or "offer").strip().lower()
        if v not in OUTREACH_MODES:
            raise ValueError(f"'{v}' is not an outreach mode. Use one of: {', '.join(OUTREACH_MODES)}.")
        return v

    @field_validator("style", mode="before")
    @classmethod
    def _valid_style(cls, v) -> str:
        v = str(v or "concise").strip().lower()
        if v not in ("concise", "engaging"):
            raise ValueError("outreach.style must be concise or engaging.")
        return v

    @field_validator("inbox_backlog_days", mode="before")
    @classmethod
    def _backlog_range(cls, v) -> int:
        try:
            v = int(v or 0)
        except (TypeError, ValueError):
            v = 0
        return max(0, min(v, 365))

    @field_validator("greeting", mode="before")
    @classmethod
    def _valid_greeting(cls, v) -> str:
        from harvey.names import GREETING_WORDS
        v = str(v or "Hi").strip()
        match = next((w for w in GREETING_WORDS if w.lower() == v.lower()), "")
        if not match:
            raise ValueError(f"'{v}' is not a greeting. Use one of: {', '.join(GREETING_WORDS)}.")
        return match

    @field_validator("signature_image", mode="before")
    @classmethod
    def _plain_image_name(cls, v) -> str:
        v = str(v or "").strip()
        return "" if ("/" in v or "\\" in v or v.startswith(".")) else v

    @field_validator("attach_on", mode="before")
    @classmethod
    def _valid_attach_on(cls, v) -> str:
        v = str(v or "first_reply").strip().lower()
        if v not in ATTACH_POLICIES:
            raise ValueError(f"'{v}' is not an attachment policy. Use one of: {', '.join(ATTACH_POLICIES)}.")
        return v

    @field_validator("reply_attachments", mode="before")
    @classmethod
    def _plain_names(cls, v) -> list[str]:
        names = []
        for name in (v or []):
            name = str(name).strip()
            if name and "/" not in name and "\\" not in name and not name.startswith("."):
                names.append(name)
        return names


class QuietHoursConfig(BaseModel):
    # enabled False = send at any hour; start/end are then ignored.
    enabled: bool = True
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    # Codex only: share of the ChatGPT plan's rate-limit windows Harvey may
    # use (the name predates Codex support). Claude runs on the API, which
    # has no subscription quota, so it is capped in dollars instead.
    max_daily_claude_percent: float = 80.0
    # Claude only: estimated API spend per day (UTC) before Harvey pauses
    # until tomorrow. 0 = no cap (only max_daily_calls applies).
    max_daily_cost_usd: float = 0.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("max_daily_cost_usd")
    @classmethod
    def _valid_cost(cls, v: float) -> float:
        if v < 0:
            raise ValueError("max_daily_cost_usd must be 0 (no cap) or positive")
        return float(v)

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


BRAIN_PROVIDERS = ("claude", "codex", "openai")
CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
OPENAI_EFFORTS = ("low", "medium", "high")
REASONING_EFFORTS = {"claude": CLAUDE_EFFORTS, "codex": CODEX_REASONING_EFFORTS,
                     "openai": OPENAI_EFFORTS}

# Model names go into an API request or a CLI, so they must look like a
# model name and never like a flag ("-x") or anything a shell would treat
# specially.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")


class BrainConfig(BaseModel):
    """Which model provider Harvey thinks with.

    ``claude`` (default) calls Claude through the Anthropic API with the key
    in ``ANTHROPIC_API_KEY`` (billed per token; the model defaults to
    ``claude-opus-5-5``). ``codex`` runs Codex on your ChatGPT plan.
    ``openai`` talks to any OpenAI-compatible chat-completions endpoint
    (``base_url``: OpenAI, Google Gemini, Groq, OpenRouter, Together, a local
    Ollama, ...) with the key in ``LLM_API_KEY``; several of those have free
    tiers. There is never an automatic fallback between providers, and
    ``model`` only affects Harvey — your own Codex settings are never read
    or changed.
    """

    provider: str = "claude"
    model: str = ""  # empty = the provider's default
    # openai only: the API root, e.g. https://api.groq.com/openai/v1
    base_url: str = ""
    # Thinking depth. Claude: low | medium | high | xhigh | max (empty = the
    # model's default). Codex: minimal | low | medium | high | xhigh.
    reasoning_effort: str = ""
    # Claude only: let the API re-run a request a safety classifier declined
    # on a fallback model, inside the same call, instead of returning nothing.
    fallbacks: bool = True

    @field_validator("provider", mode="before")
    @classmethod
    def _valid_provider(cls, v) -> str:
        v = str(v or "claude").strip().lower()
        if v not in BRAIN_PROVIDERS:
            raise ValueError(
                f"'{v}' is not a supported brain provider. "
                f"Use one of: {', '.join(BRAIN_PROVIDERS)}."
            )
        return v

    @field_validator("model", mode="before")
    @classmethod
    def _valid_model(cls, v) -> str:
        v = str(v or "").strip()
        if v and not _MODEL_NAME_RE.match(v):
            raise ValueError(f"'{v}' is not a valid model name.")
        return v

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _valid_effort(cls, v) -> str:
        return str(v or "").strip().lower()

    @field_validator("base_url", mode="before")
    @classmethod
    def _valid_base_url(cls, v) -> str:
        v = str(v or "").strip().rstrip("/")
        if v and not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("base_url must start with https:// (or http:// for a local server).")
        return v

    @model_validator(mode="after")
    def _effort_matches_provider(self):
        allowed = REASONING_EFFORTS[self.provider]
        if self.reasoning_effort and self.reasoning_effort not in allowed:
            raise ValueError(
                f"'{self.reasoning_effort}' is not a valid reasoning effort for "
                f"{self.provider}. Use one of: {', '.join(allowed)}."
            )
        if self.provider == "openai" and not self.base_url:
            raise ValueError("brain.base_url is required for provider openai "
                             "(e.g. https://api.openai.com/v1).")
        return self


class HarveyConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()
    brain: BrainConfig = BrainConfig()
    compliance: ComplianceConfig = ComplianceConfig()
    outreach: OutreachConfig = OutreachConfig()


class EnvConfig(BaseModel):
    # Brain (brain.provider: claude). Read by the Anthropic SDK from the
    # environment; kept here so setup and status can report on it.
    anthropic_api_key: str = ""
    anthropic_workspace_id: str = ""  # only for keys not scoped to a workspace (sk-ant-usr-…)
    # Brain (brain.provider: openai): key for the OpenAI-compatible endpoint.
    llm_api_key: str = ""
    instantly_api_key: str = ""
    # Discovery providers
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_sandbox: str = ""   # any truthy value routes to the free sandbox
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""
    reoon_api_key: str = ""
    zerobounce_api_key: str = ""
    # Native mail providers (channels.email.provider: gmail | smtp)
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_reply_to: str = ""  # optional Reply-To header on outgoing SMTP mail
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # Microsoft 365 (channels.email.provider: microsoft — signed-in user, or
    # microsoft_app — an admin-consented Azure app sending as one mailbox)
    m365_client_id: str = ""
    m365_tenant_id: str = ""
    m365_client_secret: str = ""
    m365_mailbox: str = ""
    # Optional: only used when compliance.precleared.enabled is true.
    precleared_api_key: str = ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> HarveyConfig:
    """Load Harvey configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create one from harvey.yaml.example or run 'harvey setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'harvey setup' to configure Harvey.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        return HarveyConfig(**data)
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'harvey setup'."
        )
        raise


def load_brain_config(config_path: str | None = None) -> BrainConfig:
    """Read just the ``brain:`` section, without validating the rest.

    Training runs before a product is configured (the template harvey.yaml
    still has placeholder values), but it must still think with the
    provider the user chose. A missing file or section means the Claude
    default; an invalid section raises rather than silently switching
    providers.
    """
    if config_path is None:
        try:
            config_path = _find_config_file()
        except ConfigFileNotFoundError:
            return BrainConfig()
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return BrainConfig()
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Could not read the brain settings in {config_path}: {e}")
    section = data.get("brain") if isinstance(data, dict) else None
    if section is None:
        return BrainConfig()
    if not isinstance(section, dict):
        raise ConfigError(f"'brain' in {config_path} must be a mapping.")
    try:
        return BrainConfig(**section)
    except ValidationError as e:
        raise ConfigError(
            f"Invalid brain settings in {config_path}:\n{_format_validation_error(e)}"
        )


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip(),
        llm_api_key=(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", "").strip(),
        dataforseo_login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=os.getenv("DATAFORSEO_SANDBOX", "").strip(),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        reoon_api_key=os.getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=os.getenv("ZEROBOUNCE_API_KEY", "").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_reply_to=os.getenv("SMTP_REPLY_TO", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=os.getenv("IMAP_USERNAME", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        m365_client_id=os.getenv("M365_CLIENT_ID", "").strip(),
        m365_tenant_id=os.getenv("M365_TENANT_ID", "").strip(),
        m365_client_secret=os.getenv("M365_CLIENT_SECRET", "").strip(),
        m365_mailbox=os.getenv("M365_MAILBOX", "").strip().lower(),
        precleared_api_key=os.getenv("PRECLEARED_API_KEY", "").strip(),
    )
    return env


def _find_config_file() -> str:
    """Search for Harvey's config.

    ``harvey.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``harvey.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.
    """
    # HARVEY_HOME first (it is the checkout unless the env var says
    # otherwise), then the working directory, then the checkout itself.
    candidates = [
        LOCAL_CONFIG_FILE,
        Path.cwd() / "harvey.local.yaml",
        PROJECT_ROOT / "harvey.local.yaml",
        CONFIG_FILE,
        Path.cwd() / "harvey.yaml",
        Path.cwd().parent / "harvey.yaml",
        PROJECT_ROOT / "harvey.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "harvey.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from harvey.yaml.example or run 'harvey setup'."
    )
