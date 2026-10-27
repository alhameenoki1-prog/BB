"""One isolated Harvey per user.

A workspace is a directory under ``BB_HOME/workspaces/<user id>`` that
serves as that user's ``HARVEY_HOME``: harvey.yaml (written from the profile
form), data/harvey.db, data/gmail_token.json, data/harvey.log, and a
skills/ folder holding the product knowledge the writer reads. The heartbeat
runs as a separate process started with ``HARVEY_HOME`` pointing here, so
Harvey's own code needs no notion of tenants.
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiosqlite
import yaml

from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager

logger = logging.getLogger("bb.workspaces")

TONES = ("plain and friendly", "friendly and direct", "warm and curious", "formal", "casual",
         "professional, consultative, confident")
GOALS = {"book_call": "Book a call", "get_reply": "Get a reply", "start_trial": "Start a trial"}
OUTREACH_MODES = {
    "inquiry": "I want something from them (hire them, buy from them, partner with them)",
    "offer": "I have something to offer them (a product, a service, a proposal)",
}
# AI providers a user can bring. Model names are suggestions the user can
# edit; the endpoint and key page are what matter.
LLM_PRESETS = {
    "gemini": {"label": "Google Gemini", "note": "Free tier, no card needed. Good default.",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "key_url": "https://aistudio.google.com/apikey", "free": True},
    "groq": {"label": "Groq", "note": "Free tier, very fast open models.",
             "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b",
             "key_url": "https://console.groq.com/keys", "free": True},
    "openrouter": {"label": "OpenRouter", "note": "Hundreds of models; the ones ending in :free cost nothing.",
                   "base_url": "https://openrouter.ai/api/v1",
                   "model": "meta-llama/llama-3.3-70b-instruct:free",
                   "key_url": "https://openrouter.ai/keys", "free": True},
    "ollama": {"label": "Ollama on this computer", "note": "Free and private; needs Ollama running where the site runs.",
               "base_url": "http://localhost:11434/v1", "model": "llama3.1",
               "key_url": "https://ollama.com/download", "free": True, "no_key": True},
    "openai": {"label": "OpenAI", "note": "Paid, billed to your OpenAI account.",
               "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "key_url": "https://platform.openai.com/api-keys", "free": False},
    "anthropic": {"label": "Anthropic Claude", "note": "Paid: needs prepaid credit on your Anthropic account (Console → Billing). Strongest writing.",
                  "base_url": "", "model": "", "key_url": "https://console.anthropic.com/settings/keys",
                  "free": False},
    "custom": {"label": "Another OpenAI-compatible API", "note": "Any endpoint that speaks /chat/completions.",
               "base_url": "", "model": "", "key_url": "", "free": False},
}

ATTACH_POLICIES = {"first_reply": "With my first reply to each person",
                   "every_reply": "With every reply", "never": "Never"}
MAX_ATTACHMENT_TOTAL = 20 * 1024 * 1024  # Gmail's hard limit is 25 MB per message
SMTP_FIELDS = ("smtp_host", "smtp_port", "smtp_username", "smtp_password",
               "imap_host", "imap_port", "imap_username", "imap_password", "from_email", "reply_to")
M365_FIELDS = ("m365_tenant_id", "m365_client_id", "m365_client_secret", "m365_mailbox")
# How fast the worker runs: minutes between cycles and emails per cycle.
# Each speed: how often the worker's heartbeat (research, writing) runs, and
# how the sender drains the outbox on its own clock: a batch of per_cycle
# every send_every seconds, through `concurrency` lanes, `jitter` seconds
# apart at most. Sending never waits for research or writing.
SPEEDS = {
    "gentle": {"label": "Gentle", "note": "8 emails every 5 minutes, one at a time with a pause between them (about 95 an hour)",
               "interval": 5, "per_cycle": 8, "send_every": 300, "concurrency": 1, "jitter": 15.0},
    "normal": {"label": "Normal", "note": "10 emails every 2 minutes, a few seconds apart (about 300 an hour)",
               "interval": 2, "per_cycle": 10, "send_every": 120, "concurrency": 1, "jitter": 6.0},
    "fast": {"label": "Fast", "note": "25 emails every 30 seconds, two at a time (about 3,000 an hour; a personal Gmail or Outlook mailbox is held to Gentle's pace automatically)",
             "interval": 1, "per_cycle": 25, "send_every": 30, "concurrency": 2, "jitter": 2.0},
    "relay": {"label": "Relay", "note": "100 emails every 30 seconds, eight at a time (about 12,000 an hour). For an SMTP sending service such as Amazon SES, Brevo, Postmark, Mailgun or SendGrid, never a personal mailbox",
              "interval": 1, "per_cycle": 100, "send_every": 30, "concurrency": 8, "jitter": 0.8},
    "custom": {"label": "Custom", "note": "Your own numbers: emails per batch, seconds between batches, the pause between two emails, and how many go at once. For an external SMTP whose limits you know",
               "interval": 1, "per_cycle": 25, "send_every": 60, "concurrency": 1, "jitter": 3.0},
}
# Google Workspace and Microsoft 365 on a company domain take more than a
# personal mailbox but still lock an account that bursts: Microsoft allows
# 30 messages a minute over SMTP, Google about 2,000 a day. This is the
# ceiling Fast, Relay and Custom are held to on them.
BUSINESS_PACE = {"per_cycle": 20, "send_every": 60, "concurrency": 2, "jitter": 2.0}
# Above this many cold emails a day from one mailbox, the dashboard says so:
# a provider's technical maximum (9,000 on Microsoft 365) is not a safe pace.
SAFE_COLD_PER_DAY = 100
CUSTOM_LIMITS = {"per_cycle": (1, 100), "send_every": (10, 3600), "jitter": (0.0, 120.0), "concurrency": (1, 16)}
# Mail hosts that belong to a personal or office mailbox, not a sending
# service: they throttle or lock an account that opens several connections
# or sends a burst, so every speed is held to Gentle's pace on them.
PERSONAL_SMTP_HOSTS = ("smtp.gmail.com", "smtp.googlemail.com", "smtp-mail.outlook.com", "smtp.office365.com",
                       "smtp.live.com", "smtp.mail.yahoo.com", "smtp.mail.me.com", "smtp.aol.com", "smtp.zoho.com",
                       "smtp.zoho.eu", "smtp.fastmail.com", "smtp.mail.com", "smtp.gmx.com", "smtp.gmx.net")
PERSONAL_SMTP_SUFFIXES = ("gmail.com", "googlemail.com", "outlook.com", "office365.com", "live.com", "hotmail.com",
                          "yahoo.com", "icloud.com", "me.com", "aol.com")


def is_personal_smtp_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host or host.startswith("smtp-relay."):   # Google Workspace's relay is a relay
        return False
    return host in PERSONAL_SMTP_HOSTS or host.endswith(PERSONAL_SMTP_SUFFIXES)
GREETINGS = ("Hi", "Hello", "Hey", "Dear", "Good day")
# Sectors the writer sorts contacts into, with labels for the form.
SECTOR_LABELS = {
    "construction": "Construction", "real_estate": "Real estate", "architecture_engineering": "Architecture & engineering",
    "financial_advisory": "Financial advisory", "banking": "Banking", "insurance": "Insurance", "accounting": "Accounting",
    "legal": "Legal", "healthcare": "Healthcare", "education": "Education", "hr_recruiting": "HR & recruiting",
    "marketing_agency": "Marketing agency", "software": "Software", "it_services": "IT services",
    "manufacturing": "Manufacturing", "logistics": "Logistics", "retail": "Retail", "ecommerce": "E-commerce",
    "hospitality": "Hospitality", "nonprofit": "Non-profit", "government": "Government", "media": "Media",
    "energy": "Energy", "consulting": "Consulting", "agriculture": "Agriculture", "automotive": "Automotive",
    "other": "Other",
}
MAX_SECTOR_MESSAGES = 12
MAX_INBOX_LINKS = 12


def _sector_messages_from_form(form) -> list[dict]:
    """Rows sm_sectors_N (several values) + sm_subject_N + sm_body_N +
    sm_followup_1_N + sm_followup_2_N. Empty rows are dropped."""
    out = []
    getlist = getattr(form, "getlist", None)
    for i in range(MAX_SECTOR_MESSAGES):
        body = str(form.get(f"sm_body_{i}", "") or "").strip()
        if getlist:
            sectors = [str(x) for x in getlist(f"sm_sectors_{i}")]
        else:
            raw = form.get(f"sm_sectors_{i}", [])
            sectors = list(raw) if isinstance(raw, (list, tuple)) else str(raw or "").split()
        sectors = [x.strip().lower() for x in sectors if x and x.strip().lower() in SECTOR_LABELS]
        if not body or not sectors:
            continue
        out.append({"sectors": sectors, "subject": str(form.get(f"sm_subject_{i}", "") or "").strip()[:120],
                    "body": body, "followup_1": str(form.get(f"sm_followup_1_{i}", "") or "").strip(),
                    "followup_2": str(form.get(f"sm_followup_2_{i}", "") or "").strip()})
    return out
MAX_SECTOR_NEEDS = 12


def _profile_form(prof: dict) -> dict:
    """A stored profile as the form that would produce it again: checkbox
    flags as "on"/"", the row-shaped settings flattened back into the
    keys their parsers read, and the "rewrite" flag so stored values are
    not re-validated against rules added since they were saved."""
    form = {**prof, "auto_prospect": "on" if prof.get("auto_prospect") else "",
            "require_approval": "on" if prof.get("require_approval") else "",
            "quiet_form": "1", "quiet_enabled": "on" if prof.get("quiet_enabled") else "",
            "warmup_ramp": "on" if prof.get("warmup_ramp") else "",
            "plain_first_email": "on" if prof.get("plain_first_email", True) else "",
            "template_fallback": "on" if prof.get("template_fallback") else "", "rewrite": "1"}
    _flatten_rows(form, "sm", prof.get("sector_messages"),
                  {"subject": "subject", "body": "body", "followup_1": "followup_1", "followup_2": "followup_2"})
    _flatten_rows(form, "sn", prof.get("sector_needs"),
                  {"need": "need", "question": "question", "url": "details_url", "label": "details_label"})
    return form


def _sector_needs_from_form(form) -> list[dict]:
    """Rows sn_sectors_N (several values) + sn_need_N + sn_question_N +
    sn_url_N + sn_label_N: what the sender needs from each kind of
    business, with the link to its full scope. Empty rows are dropped."""
    out = []
    getlist = getattr(form, "getlist", None)
    for i in range(MAX_SECTOR_NEEDS):
        need = str(form.get(f"sn_need_{i}", "") or "").strip()
        if getlist:
            sectors = [str(x) for x in getlist(f"sn_sectors_{i}")]
        else:
            raw = form.get(f"sn_sectors_{i}", [])
            sectors = list(raw) if isinstance(raw, (list, tuple)) else str(raw or "").split()
        sectors = [x.strip().lower() for x in sectors if x and x.strip().lower() in SECTOR_LABELS]
        if not need or not sectors:
            continue
        url = str(form.get(f"sn_url_{i}", "") or "").strip()[:500]
        if url and not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        out.append({"sectors": sectors, "need": need[:1200],
                    "question": str(form.get(f"sn_question_{i}", "") or "").strip()[:200],
                    "details_url": url,
                    "details_label": str(form.get(f"sn_label_{i}", "") or "").strip()[:80]})
    return out


def _flatten_rows(form: dict, prefix: str, rows: list, fields: dict) -> None:
    """Put stored rows back into the form keys the parsers read."""
    for i, row in enumerate(rows or []):
        form[f"{prefix}_sectors_{i}"] = list(row.get("sectors") or [])
        for key, field in fields.items():
            form[f"{prefix}_{key}_{i}"] = row.get(field, "") or ""


# "What you'd like to happen next": the closing ask of every email. Keys
# match harvey.config.ASK_THEMES; blank = an easy question, no ask.
ASKS = {
    "": ("Just a reply", "The email ends on an easy question; you take it from there."),
    "zoom": ("A video call", "After the request, one plain line says a short call would be easiest if it is something they do. Your booking link goes into replies, not the first email."),
    "phone": ("A phone call", "One plain line offering a quick call, after the request."),
    "coffee": ("Coffee", "Suggests meeting for a coffee if they are nearby."),
    "in_person": ("Meet in person", "Suggests meeting at their office or on site."),
    "visit": ("A visit or tour", "Asks to come by and see their place first-hand."),
    "email": ("Details by email", "Asks for a short written reply with the details; no call."),
}
STYLES = {
    "engaging": "Engaging: 90–130 words with a hook, one useful detail, an easy question and a PS",
    "concise": "Concise: under 75 words, one observation and one question",
}
MAIL_PROVIDERS = ("gmail", "smtp", "microsoft", "microsoft_app")
STOP_GRACE_SECONDS = 30   # how long a worker gets to finish after SIGTERM before SIGKILL
_LIFECYCLE_LOCKS: dict[str, asyncio.Lock] = {}


def _lifecycle_lock(key: str) -> asyncio.Lock:
    """One stop-and-start at a time for one worker: two restarts pressed
    together (or the site's watch and a button) must not start two."""
    key = f"{id(asyncio.get_running_loop())}:{key}"     # (a lock belongs to one event loop)
    lock = _LIFECYCLE_LOCKS.get(key)
    if lock is None:
        if len(_LIFECYCLE_LOCKS) > 4096:
            _LIFECYCLE_LOCKS.clear()
        lock = _LIFECYCLE_LOCKS[key] = asyncio.Lock()
    return lock


def _unlink_if_pid(path: Path, pid: int):
    """Clear a pid file only while it still names the process that was
    stopped: a worker started meanwhile keeps its record."""
    try:
        if int(path.read_text().strip()) == pid:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
WAKE_WAIT_SECONDS = 15    # how long Wake up now waits for the worker to answer before restarting it
STUCK_GRACE_SECONDS = 5   # a frozen worker cannot finish anything: it is ended sooner
STUCK_CYCLE_SECONDS = 45 * 60   # one cycle is cut off after 20 minutes at most; one running this long is stuck
WAKE_BUSY_SECONDS = 60    # a beat this fresh with no answer to a wake: busy (a slow step), not frozen
MAX_SIGNATURE_IMAGE = 1024 * 1024

# A hidden marker on a signature BB built itself (from the profile fields),
# so the profile form can tell it from HTML the user typed and rebuild it on
# every save. An HTML comment: invisible in the email, dropped from plain text.
AUTO_SIG_MARKER = "<!-- bb-auto-signature -->"


def build_auto_signature(name: str, role: str, company: str, linkedin: str) -> str:
    """A clean, email-safe signature from the profile fields, so nobody has to
    write HTML. The uploaded image is added underneath it by the send path
    (``render_parts``), so this is just the name, role, company and LinkedIn."""
    import html as _h

    parts = []
    name = (name or "").strip()
    if name:
        parts.append(f'<div style="font-weight:600;color:#1d1d1f">{_h.escape(name)}</div>')
    role_company = " · ".join(p for p in ((role or "").strip(), (company or "").strip()) if p)
    if role_company:
        parts.append(f"<div>{_h.escape(role_company)}</div>")
    linkedin = (linkedin or "").strip()
    if linkedin:
        url = linkedin if linkedin.lower().startswith(("http://", "https://")) else "https://" + linkedin.lstrip("/")
        parts.append(f'<div style="margin-top:2px"><a href="{_h.escape(url, quote=True)}" '
                     'style="color:#0a66c2;text-decoration:none">LinkedIn</a></div>')
    return AUTO_SIG_MARKER + "".join(parts)

COMMON_TIMEZONES = (
    "UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "Africa/Lagos", "Africa/Nairobi",
    "Africa/Johannesburg", "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo",
    "Australia/Sydney", "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Sao_Paulo",
)

# Prospect statuses, grouped the way a user thinks about them.
WAITING_STATUSES = ("new",)            # uploaded, no email written yet
QUEUED_STATUSES = ("queued",)          # written, waiting in the outbox
SENT_STATUSES = ("contacted",)
REPLIED_STATUSES = ("replied", "interested", "meeting", "not_interested", "closed", "lost",
                    "closed_won", "closed_lost")


def _split_lines(text: str) -> list[str]:
    return [line.strip(" -•\t") for line in (text or "").replace(",", "\n").splitlines()
            if line.strip(" -•\t")]


class Workspace:
    def __init__(self, settings, user: dict, campaign_id: str = ""):
        from bb import campaigns as campaigns_mod
        self.settings = settings
        self.user = user
        # The user's own folder holds what every campaign shares: the AI
        # provider, the accounts and the mail command center, the alerts.
        # An outreach campaign other than the main one is a Harvey
        # workspace of its own under campaigns/<id>/ in it.
        self.user_root = settings.workspaces_dir / user["id"]
        self.user_data_dir = self.user_root / "data"
        self.campaign_id = campaign_id if campaign_id and campaign_id != campaigns_mod.MAIN else campaigns_mod.MAIN
        self.root = campaigns_mod.campaign_root(self.user_root, self.campaign_id)
        self.data_dir = self.root / "data"
        self.skills_dir = self.root / "skills"
        self.config_file = self.root / "harvey.yaml"
        self.db_path = self.data_dir / "harvey.db"
        self.pid_file = self.data_dir / "harvey.pid"
        self.log_file = self.data_dir / "harvey.log"
        self.autostart_file = self.data_dir / "worker.autostart"
        # Present while the user has paused the worker to make edits: the
        # dashboard shows Resume, and nothing revives the worker meanwhile.
        self.paused_file = self.data_dir / "worker.paused"
        self.gmail_token_file = self.data_dir / "gmail_token.json"
        self.gmail_account_file = self.data_dir / "gmail_account.json"
        # Where harvey.integrations.microsoft reads the signed-in user's token.
        self.microsoft_token_file = self.data_dir / "m365_token.json"
        self.microsoft_account_file = self.data_dir / "microsoft_account.json"
        self.secrets_file = self.data_dir / "secrets.json"
        # Where mailbox alerts go (bb.alerts) and what was already said: per user.
        self.alerts_file = self.user_data_dir / "alerts.json"
        self.alerts_state_file = self.user_data_dir / "alerts_state.json"
        # Which campaign the pages show (per user, chosen in the nav).
        self.current_campaign_file = self.user_data_dir / "current_campaign"
        # The account an outreach campaign was given, and the credentials last copied from it.
        self.binding_file = self.data_dir / "account_binding.json"
        self.mailbox_file = self.data_dir / "mailbox.json"
        self.attachments_dir = self.root / "attachments"
        self.signature_dir = self.root / "signature"

    def ensure(self):
        for d in (self.user_data_dir, self.root, self.data_dir, self.skills_dir, self.attachments_dir, self.signature_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # ── signature image ──

    def signature_image(self) -> str:
        if not self.signature_dir.is_dir():
            return ""
        for path in sorted(self.signature_dir.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                return path.name
        return ""

    def set_signature_image(self, filename: str, data: bytes) -> str:
        name = self.safe_filename(filename)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
            raise ValueError("Use a PNG, JPG, GIF or WebP image.")
        if len(data) > MAX_SIGNATURE_IMAGE:
            raise ValueError("Keep the signature image under 1 MB; large images hurt deliverability.")
        self.ensure()
        self.remove_signature_image()
        (self.signature_dir / name).write_bytes(data)
        self.rewrite_config()
        return name

    def remove_signature_image(self):
        if self.signature_dir.is_dir():
            for path in self.signature_dir.iterdir():
                if path.is_file():
                    path.unlink()
        self.rewrite_config()

    # ── per-user secrets (their own Anthropic key, their mailbox password) ──

    def secrets(self) -> dict:
        try:
            data = json.loads(self.secrets_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_secrets(self, **updates):
        self.ensure()
        data = {**self.secrets(), **{k: v for k, v in updates.items() if v is not None}}
        self.secrets_file.write_text(json.dumps(data, indent=2))
        try:
            self.secrets_file.chmod(0o600)
        except OSError:
            pass

    def anthropic_key(self) -> str:
        """The Anthropic key when that is the chosen provider ("" otherwise)."""
        llm = self.llm()
        return llm["api_key"] if llm["provider"] == "anthropic" else ""

    # ── the user's AI provider ──

    @property
    def is_main(self) -> bool:
        return self.campaign_id == "main"

    def main(self) -> "Workspace":
        """The user's own workspace (the main campaign)."""
        return self if self.is_main else Workspace(self.settings, self.user)

    def llm(self) -> dict:
        s = self.main().secrets()       # one AI provider per user, for every campaign
        provider = str(s.get("llm_provider") or "")
        if not provider and s.get("anthropic_api_key"):  # saved before presets existed
            provider = "anthropic"
        preset = LLM_PRESETS.get(provider, LLM_PRESETS["custom"])
        api_key = str(s.get("llm_api_key") or (s.get("anthropic_api_key") if provider == "anthropic" else "") or "")
        return {
            "provider": provider,
            "label": preset["label"] if provider else "",
            "api_key": api_key,
            "workspace_id": str(s.get("anthropic_workspace_id") or ""),
            "base_url": str(s.get("llm_base_url") or preset["base_url"] or ""),
            "model": str(s.get("llm_model") or preset["model"] or ""),
            "free": bool(preset.get("free")),
        }

    def save_llm(self, provider: str, api_key: str, model: str = "", base_url: str = "",
                 workspace_id: str = ""):
        if provider not in LLM_PRESETS:
            raise ValueError("Choose a provider from the list.")
        preset = LLM_PRESETS[provider]
        self.main().save_secrets(
            llm_provider=provider, llm_api_key=api_key.strip(),
            llm_model=(model or preset["model"]).strip(),
            llm_base_url=(base_url or preset["base_url"]).strip().rstrip("/"),
            anthropic_api_key=api_key.strip() if provider == "anthropic" else "",
            anthropic_workspace_id=workspace_id.strip() if provider == "anthropic" else "",
        )
        for ws in self.all_campaigns():
            ws.rewrite_config()

    def clear_llm(self):
        self.main().save_secrets(llm_provider="", llm_api_key="", llm_model="", llm_base_url="",
                                 anthropic_api_key="", anthropic_workspace_id="")

    def llm_configured(self) -> bool:
        llm = self.llm()
        if not llm["provider"]:
            return False
        if llm["provider"] == "anthropic":
            return bool(llm["api_key"])
        needs_key = not LLM_PRESETS[llm["provider"]].get("no_key")
        return bool(llm["base_url"] and llm["model"] and (llm["api_key"] or not needs_key))

    def brain_section(self) -> dict:
        llm = self.llm()
        if llm["provider"] == "anthropic" or not llm["provider"]:
            section = {"provider": "claude"}
            model = llm["model"] or self.settings.default_model
            if model:
                section["model"] = model
            return section
        return {"provider": "openai", "base_url": llm["base_url"], "model": llm["model"]}

    @staticmethod
    def mask(secret: str) -> str:
        secret = secret or ""
        return (secret[:7] + "…" + secret[-4:]) if len(secret) > 14 else ("•" * len(secret))

    # ── mailbox: Gmail (OAuth) or the mailbox's own SMTP/IMAP ──

    def mail_provider(self) -> str:
        try:
            data = json.loads(self.mailbox_file.read_text())
            provider = str(data.get("provider") or "")
        except (OSError, ValueError, AttributeError):
            provider = ""
        usable = {"gmail": True, "smtp": True, "microsoft": self.microsoft_connected(),
                  "microsoft_app": self.m365_configured()}
        if provider in MAIL_PROVIDERS and usable[provider]:   # a disconnected sign-in is not a choice
            return provider
        if self.microsoft_connected() and not self.gmail_connected():
            return "microsoft"
        if self.m365_configured() and not self.gmail_connected():
            return "microsoft_app"
        return "smtp" if self.smtp_configured() and not self.gmail_connected() else "gmail"

    def set_mail_provider(self, provider: str):
        if provider not in MAIL_PROVIDERS:
            raise ValueError("Choose gmail, smtp, microsoft or microsoft_app.")
        self.ensure()
        self.mailbox_file.write_text(json.dumps({"provider": provider}))

    def smtp_settings(self) -> dict:
        secrets = self.secrets()
        return {k: secrets.get(k, "") for k in SMTP_FIELDS}

    @property
    def speed_file(self):
        return self.data_dir / "speed.txt"

    def _held_speed_marker(self) -> str:
        try:
            return self.speed_file.read_text().strip()
        except OSError:
            return ""

    @staticmethod
    def _custom_pace(form: dict) -> dict:
        """The Custom speed's numbers from the form, within sane bounds."""
        def num(key, default, lo, hi, cast=int):
            try:
                v = cast(str(form.get(key, "") or default).strip() or default)
            except (TypeError, ValueError):
                v = default
            return max(lo, min(v, hi))
        return {"per_cycle": num("custom_per_batch", 25, *CUSTOM_LIMITS["per_cycle"]),
                "send_every": num("custom_every", 60, *CUSTOM_LIMITS["send_every"]),
                "jitter": num("custom_delay", 3.0, *CUSTOM_LIMITS["jitter"], cast=float),
                "concurrency": num("custom_lanes", 1, *CUSTOM_LIMITS["concurrency"])}

    def mailbox_tier(self) -> tuple[str, str]:
        """(tier, label): "consumer" (a gmail.com / outlook.com / yahoo address),
        "business" (Google Workspace or Microsoft 365 on a company domain), or
        "" (an SMTP sending service or unknown)."""
        provider = self.mail_provider()
        address = (self.sending_address() or "").lower()
        domain = address.rsplit("@", 1)[-1] if "@" in address else ""
        consumer_domains = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
                            "yahoo.com", "icloud.com", "me.com", "aol.com")
        host = str(self.smtp_settings().get("smtp_host") or "").strip().lower() if provider == "smtp" else ""
        google = (provider == "gmail" and self.gmail_connected()) or host.endswith(("gmail.com", "googlemail.com"))
        microsoft = (provider in ("microsoft", "microsoft_app")
                     or host.endswith(("office365.com", "outlook.com", "live.com")))
        if host.startswith("smtp-relay."):
            return "", ""
        if google:
            return ("consumer", "a personal Gmail address") if domain in consumer_domains else ("business", "Google Workspace")
        if microsoft:
            return ("consumer", "a personal Outlook address") if domain in consumer_domains else ("business", "Microsoft 365")
        if provider == "smtp" and is_personal_smtp_host(host):
            return "consumer", f"{host}, a personal mailbox"
        return "", ""

    def pace_held_reason(self, speed: dict) -> str:
        """Why a chosen speed is held back on this mailbox, or "". Personal
        mailboxes are held to Gentle (cold mail from a gmail.com or
        outlook.com address is blocked as spam long before the account's
        own limit); Workspace and Microsoft 365 to the Business pace (20
        a minute, two at a time)."""
        tier, label = self.mailbox_tier()
        if tier == "consumer" and (speed.get("concurrency", 1) > 1 or speed.get("per_cycle", 0) > SPEEDS["gentle"]["per_cycle"]
                                   or speed.get("send_every", 0) < SPEEDS["gentle"]["send_every"]):
            return (f"{label} throttles or locks an account that sends in bursts, and cold mail from it is blocked as spam "
                    "well before that, so it is held to Gentle's pace")
        if tier == "business" and (speed.get("concurrency", 1) > BUSINESS_PACE["concurrency"]
                                   or speed.get("per_cycle", 0) > BUSINESS_PACE["per_cycle"]
                                   or speed.get("send_every", 0) < BUSINESS_PACE["send_every"]):
            return f"{label} allows about 20 emails a minute, so it is held to that (two at a time)"
        return ""

    def held_pace(self, speed: dict) -> dict:
        """The speed after the mailbox's own limits are applied."""
        out = dict(speed)
        tier, _ = self.mailbox_tier()
        cap = SPEEDS["gentle"] if tier == "consumer" else (BUSINESS_PACE if tier == "business" else None)
        if cap and self.pace_held_reason(speed):
            out.update(per_cycle=min(out["per_cycle"], cap["per_cycle"]), send_every=max(out["send_every"], cap["send_every"]),
                       concurrency=min(out["concurrency"], cap["concurrency"]), jitter=max(out["jitter"], cap["jitter"]))
        return out

    def mailbox_daily_ceiling(self) -> int:
        """What the mailbox itself allows per day, a little under the
        published limit so the last emails of the day are not refused:
        gmail.com 500 → 450, Google Workspace 2,000 → 1,800, outlook.com
        300 → 250, Microsoft 365 10,000 recipients → 9,000. 0 = unknown
        (a sending service). Written to harvey.yaml as its own setting and
        applied on top of the user's daily maximum."""
        tier, label = self.mailbox_tier()
        if not tier:
            return 0
        if "Gmail" in label or label == "Google Workspace":
            return 450 if tier == "consumer" else 1800
        if "Outlook" in label or label == "Microsoft 365":
            return 250 if tier == "consumer" else 9000
        return 450 if tier == "consumer" else 0

    def smtp_configured(self) -> bool:
        s = self.smtp_settings()
        return bool(s["smtp_host"] and s["smtp_username"] and s["smtp_password"])

    def m365_settings(self) -> dict:
        secrets = self.secrets()
        return {k: str(secrets.get(k, "") or "") for k in M365_FIELDS}

    def m365_configured(self) -> bool:
        return all(self.m365_settings().values())

    def mailbox_connected(self) -> bool:
        provider = self.mail_provider()
        if provider == "gmail":
            return self.gmail_connected()
        if provider == "microsoft":
            return self.microsoft_connected()
        if provider == "microsoft_app":
            return self.m365_configured()
        return self.smtp_configured()

    def sending_address(self) -> str:
        provider = self.mail_provider()
        if provider == "gmail":
            return self.gmail_account().get("email", "")
        if provider == "microsoft":
            return self.microsoft_account().get("email", "")
        if provider == "microsoft_app":
            return self.m365_settings()["m365_mailbox"].lower()
        s = self.smtp_settings()
        return s["from_email"] or s["smtp_username"]

    # ── attachments sent with replies ──

    @staticmethod
    def safe_filename(name: str) -> str:
        name = Path(str(name or "")).name
        name = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", name).strip(" ._")
        return name[:120]

    def attachments(self) -> list[dict]:
        if not self.attachments_dir.is_dir():
            return []
        out = []
        for path in sorted(self.attachments_dir.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                out.append({"name": path.name, "size": path.stat().st_size})
        return out

    def add_attachment(self, filename: str, data: bytes) -> str:
        name = self.safe_filename(filename)
        if not name:
            raise ValueError("That file has no usable name.")
        total = sum(a["size"] for a in self.attachments() if a["name"] != name) + len(data)
        if total > MAX_ATTACHMENT_TOTAL:
            raise ValueError("Attachments would exceed 20 MB in total; email providers reject that.")
        self.ensure()
        (self.attachments_dir / name).write_bytes(data)
        self._sync_attachments_into_config()
        return name

    def remove_attachment(self, name: str):
        safe = self.safe_filename(name)
        try:
            (self.attachments_dir / safe).unlink()
        except FileNotFoundError:
            pass
        self._sync_attachments_into_config()

    def _sync_attachments_into_config(self):
        """harvey.yaml lists the files to attach; keep it equal to the folder."""
        cfg = self.config()
        if not cfg:
            return
        cfg.setdefault("outreach", {})["reply_attachments"] = [a["name"] for a in self.attachments()]
        self.config_file.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))

    # ── configuration (harvey.yaml) ──

    def config(self) -> dict | None:
        try:
            data = yaml.safe_load(self.config_file.read_text())
        except FileNotFoundError:
            return None
        except (OSError, yaml.YAMLError) as e:
            logger.warning("Could not read %s: %s", self.config_file, e)
            return None
        return data if isinstance(data, dict) else None

    def is_configured(self) -> bool:
        cfg = self.config() or {}
        persona = cfg.get("persona") or {}
        return bool(persona.get("name") and persona.get("company"))

    def profile(self) -> dict:
        """The profile form's values, from harvey.yaml (defaults when none)."""
        cfg = self.config() or {}
        persona = cfg.get("persona") or {}
        product = cfg.get("product") or {}
        offer = product.get("offer") or {}
        icp = cfg.get("icp") or {}
        email = (cfg.get("channels") or {}).get("email") or {}
        usage = cfg.get("usage") or {}
        quiet = usage.get("quiet_hours") or {}
        outreach = cfg.get("outreach") or {}
        return {
            "name": persona.get("name", ""),
            "company": persona.get("company", ""),
            "role": persona.get("role", ""),
            "email": persona.get("email", "") or self.sending_address(),
            "linkedin": persona.get("linkedin", ""),
            "tone": persona.get("tone", TONES[0]),
            "offer_name": product.get("name", ""),
            "offer_description": product.get("description", ""),
            "pricing": product.get("pricing", ""),
            "benefits": "\n".join(product.get("key_benefits") or []),
            "goal": offer.get("goal", "get_reply"),
            "booking_url": offer.get("booking_url", ""),
            "meeting_duration": offer.get("meeting_duration", "15 minutes"),
            "industries": ", ".join(icp.get("industries") or []),
            "titles": ", ".join(icp.get("titles") or []),
            "company_size": icp.get("company_size", ""),
            "geography": ", ".join(icp.get("geography") or []),
            "auto_prospect": bool(icp.get("auto_prospect", False)),
            "max_daily_sends": int(email.get("max_daily_sends", 0)),
            "require_approval": bool(email.get("require_approval", False)),
            # A profile that exists keeps whatever it has (older files have no
            # flag and meant "on"); a brand-new profile starts with no quiet hours.
            "quiet_enabled": bool(quiet.get("enabled", True)) if cfg else False,
            "quiet_start": quiet.get("start", "20:00"),
            "quiet_end": quiet.get("end", "08:00"),
            "timezone": quiet.get("timezone", "UTC"),
            "max_daily_cost_usd": float(usage.get("max_daily_cost_usd",
                                                  self.settings.default_daily_cost_usd)),
            "mode": outreach.get("mode", "inquiry"),
            "intent": outreach.get("intent", ""),
            "campaign_goal": outreach.get("goal", ""),
            "sender_line": outreach.get("sender_line", ""),
            "details_url": outreach.get("details_url", ""),
            "details_label": outreach.get("details_label", ""),
            "sector_needs": [
                {"sectors": list(m.get("sectors") or []), "need": m.get("need", ""), "question": m.get("question", ""),
                 "details_url": m.get("details_url", ""), "details_label": m.get("details_label", "")}
                for m in (outreach.get("sector_needs") or []) if isinstance(m, dict)],
            "reply_instructions": outreach.get("reply_instructions", ""),
            "attach_on": outreach.get("attach_on", "first_reply"),
            "writing": ("template" if outreach.get("use_template") else
                        "person" if outreach.get("personalize_each", False) else "segment"),
            "template_subject": outreach.get("template_subject", ""),
            "template_body": outreach.get("template_body", ""),
            "template_fallback": bool(outreach.get("template_fallback")),
            "template_followup_1": outreach.get("template_followup_1", ""),
            "template_followup_2": outreach.get("template_followup_2", ""),
            "sector_messages": [
                {"sectors": list(m.get("sectors") or []), "subject": m.get("subject", ""),
                 "body": m.get("body", ""), "followup_1": m.get("followup_1", ""),
                 "followup_2": m.get("followup_2", "")}
                for m in (outreach.get("sector_messages") or []) if isinstance(m, dict)],
            "style": outreach.get("style", "engaging"),
            "greeting": outreach.get("greeting", "Hi"),
            "ask": outreach.get("ask", "") if outreach.get("ask", "") in ASKS else "",
            "ask_detail": outreach.get("ask_detail", ""),
            "signature_html": "" if (outreach.get("signature_html", "") or "").startswith(AUTO_SIG_MARKER)
                              else outreach.get("signature_html", ""),
            "speed": (self._held_speed_marker() if self._held_speed_marker() in SPEEDS else
                      next((k for k, v in SPEEDS.items()
                            if v["interval"] == usage.get("heartbeat_interval_minutes")
                            and v["per_cycle"] == email.get("sends_per_cycle")), "normal")),
            "custom_per_batch": int(email.get("sends_per_cycle") or SPEEDS["custom"]["per_cycle"]),
            "custom_every": int(email.get("send_every_seconds") or SPEEDS["custom"]["send_every"]),
            "custom_delay": float(email.get("send_jitter_seconds") if email.get("send_jitter_seconds") is not None else SPEEDS["custom"]["jitter"]),
            "custom_lanes": int(email.get("send_concurrency") or 1),
            "warmup_ramp": bool(email.get("warmup_ramp", True)),
            "plain_first_email": bool(email.get("plain_first_email", True)),
        }

    # ── inbox auto-reply ──

    INBOX_KEYS = ("inbox_autoreply", "inbox_instructions", "inbox_links", "inbox_attach_files",
                  "inbox_backlog_days")
    BACKLOG_CHOICES = ((0, "Only mail that arrives from now on"), (7, "Also unanswered mail from the last week"),
                       (30, "Also unanswered mail from the last 30 days"),
                       (90, "Also unanswered mail from the last 90 days"))

    def _inbox_section(self, form: dict) -> dict:
        """The inbox fields for harvey.yaml: from the form when the Inbox
        page posted them, else carried over from the current file."""
        if "inbox_form" in form:
            links = []
            for i in range(MAX_INBOX_LINKS):
                url = str(form.get(f"link_url_{i}", "") or "").strip()
                if not url:
                    continue
                links.append({"label": str(form.get(f"link_label_{i}", "") or "").strip()[:80],
                              "url": url[:500], "when": str(form.get(f"link_when_{i}", "") or "").strip()[:300]})
            return {
                "inbox_autoreply": bool(form.get("inbox_autoreply")),
                "inbox_instructions": str(form.get("inbox_instructions", "") or "").strip(),
                "inbox_links": links,
                "inbox_attach_files": bool(form.get("inbox_attach_files")),
                "inbox_backlog_days": int(str(form.get("inbox_backlog_days", "0") or "0").strip() or 0)
                if str(form.get("inbox_backlog_days", "0") or "0").strip().isdigit() else 0,
            }
        current = ((self.config() or {}).get("outreach") or {})
        defaults = {"inbox_links": [], "inbox_instructions": "", "inbox_backlog_days": 0}
        return {k: current.get(k, defaults.get(k, False)) for k in self.INBOX_KEYS}

    def scope_links(self) -> list[dict]:
        """The links to the full scope from the profile's needs, with the
        sectors they cover; the general one last."""
        outreach = ((self.config() or {}).get("outreach") or {})
        out, seen = [], set()
        for r in outreach.get("sector_needs") or []:
            url = (r.get("details_url") or "").strip() if isinstance(r, dict) else ""
            if url and url not in seen:
                seen.add(url)
                out.append({"url": url, "label": (r.get("details_label") or "").strip(),
                            "sectors": [SECTOR_LABELS.get(s, s) for s in (r.get("sectors") or [])],
                            "need": (r.get("need") or "").strip()})
        general = (outreach.get("details_url") or "").strip()
        if general and general not in seen:
            out.append({"url": general, "label": (outreach.get("details_label") or "").strip(), "sectors": [],
                        "need": (outreach.get("intent") or "").strip()})
        return out

    def inbox_settings(self) -> dict:
        out = self._inbox_section({})
        out["inbox_links"] = [l for l in (out.get("inbox_links") or []) if isinstance(l, dict)]
        return out

    def write_inbox(self, form: dict) -> dict:
        """Save the Inbox page: the rest of harvey.yaml is rewritten from the
        stored profile, with these fields replaced."""
        if not self.is_configured():
            raise ValueError("Fill in your profile first.")
        merged = {**_profile_form(self.profile()), **form, "inbox_form": "1"}
        return self.write_profile(merged)

    async def inbox_replies(self, limit: int = 100) -> list[dict]:
        """Replies drafted or sent to people who wrote in, newest first."""
        if not await self._table_exists("outbox"):
            return []
        return await self._query(
            "SELECT o.*, p.first_name, p.last_name, p.company FROM outbox o "
            "LEFT JOIN prospects p ON p.id = o.prospect_id WHERE o.kind = 'inbox_reply' "
            "ORDER BY o.created_at DESC LIMIT ?", (limit,))

    def template_warning(self) -> str:
        """What a spam filter would flag in the operator's own message, as a
        sentence for the page, or ""."""
        from harvey.spamcheck import REVIEW, describe, spam_risk

        prof = self.profile()
        if prof.get("writing") != "template" or not prof.get("template_body"):
            return ""
        score, reasons = spam_risk(prof.get("template_subject", ""), prof.get("template_body", ""))
        if score < REVIEW:
            return ""
        return ("Your message may land in junk folders. A filter would flag: " + describe(reasons)
                + ". Plain, specific words about the actual job pass; marketing words, capitals, "
                "links and exclamation marks do not.")

    def write_profile(self, form: dict) -> dict:
        """Build harvey.yaml from the profile form and write it (validated
        with Harvey's own config model first, so a bad value is rejected
        here rather than crashing the worker). Returns the config dict."""
        from harvey.config import HarveyConfig

        def text(key, default=""):
            return str(form.get(key, default) or default).strip()

        def flag(key):
            return str(form.get(key, "")).lower() in ("1", "true", "on", "yes")

        name = text("name")
        company = text("company")
        if not name or not company:
            raise ValueError("Your name and company are required.")
        # The address Harvey sends from must be the connected mailbox.
        persona_email = self.sending_address() or text("email")
        benefits = _split_lines(form.get("benefits", ""))
        mode = text("mode") or "inquiry"
        if text("writing") == "template" and len(str(form.get("template_body", "") or "").strip()) < 20:
            raise ValueError("Write the message you want sent (at least a few lines), or pick one of the AI options.")
        if mode == "inquiry" and text("writing") != "template" and len(text("intent")) < 15:
            raise ValueError(
                "Say what you want from the people you write to, in a sentence or two (for example "
                "\"quotes for a two-storey extension in Lagos, starting in March\"). Every email is "
                "built around it; without it they come out vague.")
        needs_rows = _sector_needs_from_form(form)
        if mode == "inquiry" and text("writing") != "template" and "rewrite" not in form:
            # A need nobody could repeat back produces the "our team has a
            # current need" email. Checked when a person saves, not when the
            # file is rewritten after a mailbox change.
            from harvey.enquiry import GENERIC_NEED_RE, need_is_concrete
            checks = [("What you need", text("intent"))] + [
                (", ".join(SECTOR_LABELS.get(s, s) for s in r["sectors"]), r["need"]) for r in needs_rows]
            for label, value in checks:
                if GENERIC_NEED_RE.search(value) and not need_is_concrete(value):
                    raise ValueError(
                        f"{label}: that says nothing a builder, adviser or supplier can answer. Say the thing itself: "
                        "what, roughly how much, where, when (for example \"about 40 tons of rebar a month from "
                        "spring, delivered in Baltimore\").")
            if re.search(r"https?://|www\.", text("sender_line"), re.I):
                raise ValueError("Leave web addresses out of the line about you; your signature carries them.")
        details_url = text("details_url")[:500]
        if details_url and not details_url.lower().startswith(("http://", "https://")):
            details_url = "https://" + details_url
        speed = dict(SPEEDS.get(text("speed") or "normal", SPEEDS["normal"]))
        if text("speed") == "custom":
            speed.update(self._custom_pace(form))
        try:
            self.ensure()
            self.speed_file.write_text(text("speed") if text("speed") in SPEEDS else "normal")
        except OSError:
            pass
        speed["held"] = self.pace_held_reason(speed)
        speed = self.held_pace(speed)
        custom_sig = str(form.get("signature_html", "") or "").strip()
        if custom_sig.startswith(AUTO_SIG_MARKER):
            custom_sig = ""   # an auto signature from a previous save: rebuild it from the current fields
        signature_html = custom_sig or build_auto_signature(name, text("role"), company, text("linkedin"))
        cfg = {
            "persona": {
                "name": name, "company": company, "role": text("role"),
                "email": persona_email, "linkedin": text("linkedin"),
                "tone": text("tone") or TONES[0],
            },
            "product": {
                "name": text("offer_name") or company,
                "description": text("offer_description"),
                "pricing": text("pricing") or "Not discussed in the first email",
                "key_benefits": benefits,
                "objection_responses": {},
                "offer": {
                    "primary": text("offer_name") or company,
                    "entry": "",
                    "goal": text("goal") if text("goal") in GOALS else "get_reply",
                    "booking_method": "calendar_link" if text("booking_url") else "ask_preference",
                    "booking_url": text("booking_url"),
                    "meeting_duration": text("meeting_duration") or "15 minutes",
                    "meeting_owner": name,
                },
            },
            "icp": {
                "industries": _split_lines(form.get("industries", "")),
                "company_size": text("company_size"),
                "titles": _split_lines(form.get("titles", "")),
                "geography": _split_lines(form.get("geography", "")),
                "auto_prospect": flag("auto_prospect"),
            },
            "channels": {
                "email": {
                    "enabled": True,
                    "provider": self.mail_provider(),
                    "max_daily_sends": max(0, int(text("max_daily_sends", "0") or 0)),  # 0 = no limit
                    "mailbox_daily_ceiling": self.mailbox_daily_ceiling(),
                    "require_approval": flag("require_approval"),
                    "send_to_risky": False,
                    # An uploaded list that was never verified bounces 5-15%;
                    # the switch is for a list that is clearly bad.
                    "max_bounce_rate": 0.15,
                    "sends_per_cycle": speed["per_cycle"],
                    "send_every_seconds": speed["send_every"],
                    "send_concurrency": speed["concurrency"],
                    "send_jitter_seconds": speed["jitter"],
                    # Checkbox: absent in the form means off; a brand-new form
                    # (no key at all) defaults to on.
                    "warmup_ramp": flag("warmup_ramp") if "warmup_ramp" in form or "speed" in form else True,
                    # The first email to each person as plain text with a
                    # links-free signature; same checkbox rule as above.
                    "plain_first_email": flag("plain_first_email") if "plain_first_email" in form or "speed" in form else True,
                    # A personal enquiry carries no bulk-mail headers; a sales
                    # sequence does, as Gmail and Yahoo require of bulk senders.
                    "list_unsubscribe": mode != "inquiry",
                },
                "linkedin": {"enabled": False},
            },
            "usage": {
                # No spend cap by default: the user's provider bills (or doesn't) them directly.
                "max_daily_cost_usd": max(0.0, float(text("max_daily_cost_usd", "0") or 0)),
                "max_daily_claude_percent": 80,
                "heartbeat_interval_minutes": speed["interval"],
                "quiet_hours": {
                    # The checkbox: absent in a posted form means off. A form
                    # that never had the box (an older page) keeps the old "on".
                    "enabled": flag("quiet_enabled") if "quiet_form" in form else True,
                    "start": text("quiet_start") or "20:00",
                    "end": text("quiet_end") or "08:00",
                    "timezone": text("timezone") or "UTC",
                },
            },
            "brain": self.brain_section(),
            "outreach": {
                "mode": mode,
                "intent": text("intent"),
                "goal": str(form.get("campaign_goal", "") or "").strip()[:2000],
                "sender_line": text("sender_line")[:300],
                "sector_needs": needs_rows,
                "details_url": details_url,
                "details_label": text("details_label")[:80],
                "reply_instructions": str(form.get("reply_instructions", "") or "").strip(),
                "reply_attachments": [a["name"] for a in self.attachments()],
                "attach_on": text("attach_on") or "first_reply",
                # Web default: one email per sector × role segment, sent in bulk.
                "personalize_each": text("writing") == "person",
                "use_template": text("writing") == "template",
                # Only meaningful in an AI mode: the message is the reserve, not the rule.
                "template_fallback": flag("template_fallback") and text("writing") != "template",
                "template_subject": text("template_subject")[:120],
                "template_body": str(form.get("template_body", "") or "").strip(),
                "template_followup_1": str(form.get("template_followup_1", "") or "").strip(),
                "template_followup_2": str(form.get("template_followup_2", "") or "").strip(),
                "sector_messages": _sector_messages_from_form(form),
                "style": text("style") if text("style") in STYLES else "engaging",
                "greeting": text("greeting") if text("greeting") in GREETINGS else "Hi",
                "ask": text("ask") if text("ask") in ASKS else "",
                "ask_detail": text("ask_detail")[:300],
                "signature_html": signature_html,
                "signature_image": self.signature_image(),
                **self._inbox_section(form),
            },
        }
        HarveyConfig(**cfg)  # raises pydantic.ValidationError with the field
        self.ensure()
        self.config_file.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
        self._write_product_knowledge(cfg)
        return cfg

    def rewrite_config(self) -> dict | None:
        """Re-run write_profile with the current values (after the mailbox or
        attachments change) so harvey.yaml stays in step. No-op until the
        profile has been filled in once."""
        if not self.is_configured():
            return None
        try:
            return self.write_profile(_profile_form(self.profile()))
        except Exception as e:  # a stale config must never break a mailbox change
            logger.warning("Could not rewrite harvey.yaml for %s: %s", self.user["email"], e)
            return None

    def _write_product_knowledge(self, cfg: dict):
        """What the writer and handler know about the sender. The trainer
        produces a richer version from a website; this one is built from
        the form so a user is never writing with an empty skill."""
        persona, product = cfg["persona"], cfg["product"]
        offer = product["offer"]
        outreach = cfg.get("outreach") or {}
        lines = [
            f"# {product['name']} — what {persona['name']} is reaching out about",
            "",
            f"**Sender:** {persona['name']}, {persona['role'] or 'contact'} at {persona['company']}.",
            f"**LinkedIn:** {persona['linkedin']}" if persona.get("linkedin") else "",
            "",
            ("## Who is writing, in their words\n"
             + (outreach.get("sender_line") or f"{persona['name']}, {persona['role'] or 'contact'} at {persona['company']}.")
             + "\n\n## What the sender needs (anyone not covered below)\n"
             + (outreach.get("intent") or "To work with them as a client or partner.")
             + (f"\nThe full scope: {outreach.get('details_label') or 'the brief'}: {outreach['details_url']}"
                if outreach.get("details_url") else "")
             + "\n\n## What the sender needs, by kind of business\n"
             + ("\n".join(
                 f"- {', '.join(SECTOR_LABELS.get(s, s) for s in (r.get('sectors') or []))}: {r.get('need', '')}"
                 + (f" (full scope: {r.get('details_label') or 'the brief'}: {r['details_url']})" if r.get("details_url") else "")
                 for r in (outreach.get("sector_needs") or [])) or "- (none listed; the line above applies to everyone)")
             + "\n\nThe sender is the customer. Each email asks for what THAT recipient's business does; nothing is "
               "being sold. Replies answer what was asked, send the matching full scope once when details are "
               "wanted, and never invent a number, date or commitment.")
            if outreach.get("mode") == "inquiry" else "",
            "",
            "## What it is",
            product["description"] or "(no description given)",
            "",
            "## Why it matters to the recipient",
        ]
        lines += [f"- {b}" for b in product["key_benefits"]] or ["- (no benefits listed)"]
        lines += [
            "",
            "## Pricing",
            product["pricing"],
            "",
            "## The ask",
            {"book_call": f"Book a {offer['meeting_duration']} call"
                          + (f" via {offer['booking_url']}" if offer["booking_url"] else ""),
             "start_trial": "Start a trial",
             "get_reply": "Get a reply and open a conversation"}.get(offer["goal"], "Get a reply"),
            "",
            "## Rules",
            "- Only use facts about the recipient that appear in their notes or signals; never invent any.",
            "- Short, plain, specific. One ask per email.",
        ]
        (self.skills_dir / "product_knowledge.md").write_text(
            "\n".join(line for line in lines if line is not None))

    # ── Gmail ──

    def gmail_connected(self) -> bool:
        try:
            data = json.loads(self.gmail_token_file.read_text())
            return bool(isinstance(data, dict) and data.get("refresh_token"))
        except (OSError, ValueError):
            return False

    def gmail_account(self) -> dict:
        try:
            data = json.loads(self.gmail_account_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def disconnect_gmail(self):
        for path in (self.gmail_token_file, self.gmail_account_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def microsoft_connected(self) -> bool:
        try:
            data = json.loads(self.microsoft_token_file.read_text())
            return bool(isinstance(data, dict) and data.get("refresh_token"))
        except (OSError, ValueError):
            return False

    def microsoft_account(self) -> dict:
        try:
            data = json.loads(self.microsoft_account_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def microsoft_token(self) -> dict:
        try:
            data = json.loads(self.microsoft_token_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def disconnect_microsoft(self):
        for path in (self.microsoft_token_file, self.microsoft_account_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def alert_settings(self) -> dict:
        """{email, email_on, webhook}: email defaults to the login address."""
        try:
            data = json.loads(self.alerts_file.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        return {"email": str(data.get("email") or self.user.get("email") or "").strip().lower(),
                "email_on": bool(data.get("email_on", True)),
                "webhook": str(data.get("webhook") or "").strip()}

    def save_alert_settings(self, *, email: str, email_on: bool, webhook: str):
        email = (email or "").strip().lower()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("That alert address does not look like an email address.")
        self.ensure()
        self.alerts_file.write_text(json.dumps({"email": email, "email_on": bool(email_on),
                                                "webhook": (webhook or "").strip()[:500]}))

    def alert_state(self) -> dict:
        try:
            data = json.loads(self.alerts_state_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_alert_state(self, state: dict):
        self.ensure()
        tmp = self.alerts_state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.alerts_state_file)

    def needs_reconnect(self, health: dict | None = None) -> str:
        """Why the connected mailbox must be signed in again, or "": the
        health check recorded that the mailbox refused the sign-in (not
        that it was unreachable), or the sender was refused the login."""
        from harvey import health as health_mod
        health = health or {}
        if not health.get("ok", True):
            flag = health.get("auth", "")
            if flag == "1" or (flag == "" and health_mod.is_auth_failure(health.get("error", ""))):
                return health.get("alert") or health.get("error") or "the mailbox refused the sign-in"
        try:
            skip = self._skip_reason_sync()
        except Exception:
            skip = ""
        if skip.startswith("the mailbox refused the login"):
            return skip
        return ""

    def clear_mailbox_problem(self):
        """A mailbox was just connected or signed in again: the old login
        failure and alert no longer describe it (the worker checks again
        when it starts)."""
        import sqlite3
        from harvey import health as health_mod
        if not self.db_path.exists():
            return
        try:
            con = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                con.execute("DELETE FROM settings WHERE key IN (?, ?, ?, ?, ?)",
                            (health_mod.CHECK_OK_KEY, health_mod.CHECK_ERROR_KEY, health_mod.CHECK_AUTH_KEY,
                             health_mod.ALERT_KEY, health_mod.ALERT_SINCE_KEY))
                con.execute("DELETE FROM settings WHERE key = 'sender_skip_reason' AND value LIKE 'the mailbox refused the login%'")
                con.commit()
            finally:
                con.close()
        except sqlite3.Error as e:
            logger.debug("could not clear the old mailbox problem: %s", e)

    def _skip_reason_sync(self) -> str:
        import sqlite3
        if not self.db_path.exists():
            return ""
        con = sqlite3.connect(str(self.db_path))
        try:
            row = con.execute("SELECT value FROM settings WHERE key = 'sender_skip_reason'").fetchone()
            return (row[0] or "") if row else ""
        except sqlite3.Error:
            return ""
        finally:
            con.close()

    # ── state ──

    def state(self) -> StateManager:
        self.ensure()
        return StateManager(str(self.db_path))

    async def _query(self, sql: str, params=()) -> list[dict]:
        if not self.db_path.exists():
            return []
        async with aiosqlite.connect(str(self.db_path), timeout=10) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _table_exists(self, name: str) -> bool:
        rows = await self._query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return bool(rows)

    async def summary(self) -> dict:
        """The numbers on the banner and the dashboard."""
        out = {
            "recipients": 0, "waiting": 0, "queued": 0, "sent": 0, "replied": 0,
            "bounced": 0, "outbox_pending": 0, "outbox_scheduled": 0, "outbox_sent": 0,
            "outbox_failed": 0, "outbox_rejected": 0, "replies": 0, "open_replies": 0,
            "sent_today": 0, "sending_paused": "", "worker_running": bool(self.worker_running()),
            "worker_paused": self.worker_paused(), "worker_doing": self.worker_doing(),
            "configured": self.is_configured(), "gmail": self.mailbox_connected(),
            "mailbox": self.mail_provider(), "api_key": self.llm_configured(),
            "llm": self.llm()["label"],
            "attachments": len(self.attachments()),
        }
        if not await self._table_exists("prospects"):
            out["allowance"] = await self._send_allowance(0)
            out["sending"] = await self._sending_status(out)
            return out
        for row in await self._query("SELECT status, COUNT(*) AS n FROM prospects "
                                     "WHERE source != 'inbox' OR source IS NULL GROUP BY status"):
            status, n = row["status"] or "", row["n"]
            out["recipients"] += n
            if status in WAITING_STATUSES:
                out["waiting"] += n
            elif status in QUEUED_STATUSES:
                out["queued"] += n
            elif status in SENT_STATUSES:
                out["sent"] += n
            elif status in REPLIED_STATUSES:
                out["replied"] += n
        rows = await self._query(
            "SELECT COUNT(*) AS n FROM prospects WHERE email_status = 'invalid'")
        out["bounced"] = rows[0]["n"] if rows else 0
        if await self._table_exists("outbox"):
            for row in await self._query("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"):
                key = {"pending_review": "outbox_pending", "approved": "outbox_scheduled", "sending": "outbox_scheduled",
                       "sent": "outbox_sent", "failed": "outbox_failed",
                       "rejected": "outbox_rejected", "cancelled": "outbox_rejected"}.get(row["status"])
                if key:
                    out[key] += row["n"]
            rows = await self._query(
                "SELECT COUNT(*) AS n FROM outbox WHERE status = 'sent' AND date(sent_at) = date('now')")
            out["sent_today"] = rows[0]["n"] if rows else 0
        if await self._table_exists("conversations"):
            rows = await self._query(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open FROM conversations")
            if rows:
                out["replies"] = rows[0]["n"] or 0
                out["open_replies"] = rows[0]["open"] or 0
        if await self._table_exists("settings"):
            rows = await self._query("SELECT value FROM settings WHERE key = 'sending_paused'")
            out["sending_paused"] = (rows[0]["value"] or "") if rows else ""
        out["not_sent"] = out["waiting"] + out["queued"]
        first_today = out["sent_today"]
        if out["sent_today"] and await self._table_exists("outbox"):
            # The daily limit counts first emails and follow-ups; replies have none.
            rows = await self._query("SELECT COUNT(*) AS n FROM outbox WHERE status = 'sent' AND date(sent_at) = date('now') "
                                     "AND COALESCE(kind, 'sequence') = 'sequence'")
            first_today = rows[0]["n"] if rows else 0
        out["allowance"] = await self._send_allowance(first_today)
        out["sending"] = await self._sending_status(out)
        return out

    async def stale_queued(self) -> int:
        """Unsent first emails whose wording reads like a campaign: written
        with an older brief, before an update, and still waiting to go out."""
        if not await self._table_exists("outbox"):
            return 0
        from harvey.names import strip_greeting
        from harvey.spamcheck import REVIEW, vague_risk
        rows = await self._query(
            "SELECT subject, body FROM outbox WHERE kind = 'sequence' AND step = 1 "
            "AND status IN ('pending_review', 'approved') LIMIT 400")
        return sum(1 for r in rows if vague_risk(r["subject"] or "", strip_greeting(r["body"] or ""))[0] >= REVIEW)

    # What shapes the words of an email: a change here makes every email
    # written before it stale. Speed, caps, the mailbox and the like do not.
    BRIEF_KEYS = ("mode", "intent", "goal", "sender_line", "sector_needs", "style", "ask", "ask_detail", "greeting",
                  "use_template", "template_subject", "template_body", "template_followup_1", "template_followup_2",
                  "sector_messages", "personalize_each")
    PERSONA_BRIEF_KEYS = ("name", "role", "company", "tone")

    def link_names(self) -> dict:
        """URL → what the profile calls it (the scope links, the inbox
        documents, the booking link): the words a link goes on."""
        cfg = self.config() or {}
        if not cfg:
            return {}
        try:
            from harvey.config import HarveyConfig
            from harvey.integrations.mail_provider import booking_url, link_labels
            model = HarveyConfig(**cfg)
            return link_labels(model.outreach, booking_url(model))
        except Exception:
            return {}

    def as_sent(self, item: dict, body: str | None = None) -> dict:
        """How an Outbox email goes out, for the page to show: {html} with
        every link on the words that name it and the signature, or {plain:
        True} for a first email sent as plain text (nothing to click there:
        the address shows as written)."""
        import html as _html
        cfg = self.config() or {}
        if not cfg:
            return {"html": "", "plain": False}
        try:
            from harvey.config import HarveyConfig
            from harvey.integrations.mail_provider import booking_url, render_parts
            model = HarveyConfig(**cfg)
            plain = (bool(getattr(model.channels.email, "plain_first_email", True))
                     and (item.get("kind") or "sequence") == "sequence" and int(item.get("step") or 1) == 1)
            if plain:
                return {"html": "", "plain": True}
            # The logo is shown from this site (the email carries it inline):
            # its place is marked before rendering, which would drop it here.
            sig = model.outreach.signature_html or ""
            placed = "{{logo}}" in sig
            outreach = model.outreach.model_copy(update={"signature_image": "", "signature_html": sig.replace("{{logo}}", "%%BB-LOGO%%")})
            text = item.get("body") or "" if body is None else body
            html = render_parts(text, outreach, booking=booking_url(model))[1]
        except Exception as e:
            logger.debug("as_sent failed: %s", e)
            return {"html": "", "plain": False}
        if not html:
            return {"html": "", "plain": False}
        logo = '<img src="/signature/image" alt="" style="max-width:240px;max-height:120px;height:auto;width:auto;display:block;margin-top:8px">' \
            if self.signature_image() else ""
        if logo and not placed and html.endswith("</div></div>"):
            html = html[:-len("</div></div>")] + logo + "</div></div>"     # after the signature, as it is sent
        html = html.replace("%%BB-LOGO%%", logo).replace(_html.escape("{{logo}}"), logo).replace("{{logo}}", logo)
        return {"html": html, "plain": False}

    def writing_brief(self) -> str:
        """A fingerprint of everything in harvey.yaml that decides what an
        email says ("" before the profile exists). Read through the config
        model, so a key an older file lacks counts as its default and a save
        that changes nothing about the words changes nothing here."""
        cfg = self.config() or {}
        if not cfg:
            return ""
        try:
            from harvey.config import HarveyConfig
            model = HarveyConfig(**cfg)
            outreach = model.outreach.model_dump(include=set(self.BRIEF_KEYS))
            product = model.product.model_dump()
            persona = model.persona.model_dump(include=set(self.PERSONA_BRIEF_KEYS))
        except Exception:
            outreach = {k: (cfg.get("outreach") or {}).get(k) for k in self.BRIEF_KEYS}
            product = cfg.get("product") or {}
            persona = {k: (cfg.get("persona") or {}).get(k) for k in self.PERSONA_BRIEF_KEYS}
        return json.dumps({"outreach": outreach, "product": product, "persona": persona}, sort_keys=True, default=str)

    async def rewrite_unsent_now(self, note: str = "rewritten with the new brief") -> tuple[dict, bool]:
        """rewrite_unsent with the worker stopped around it, so a writer in
        the middle of a batch cannot stage old words after the reset.
        Returns (counts, whether the worker was started again)."""
        was_running = bool(self.worker_running())
        if was_running:
            await self.stop_worker("rewriting the unsent emails")
        try:
            done = await self.rewrite_unsent(note)
        finally:
            restarted = self.start_worker()[0] if was_running else False
        return done, restarted

    async def rewrite_unsent(self, note: str = "rewritten with the new brief") -> dict:
        """Cancel every unsent first email (and the follow-ups behind it)
        of people not written to yet, put them back in line to be written
        to, and retire the campaigns they came from, so the worker writes
        them again with the current brief. Nothing already sent is touched,
        and someone who already has an email from us keeps the follow-ups of
        that thread: a new opening line in the middle of a conversation
        would read wrong."""
        out = {"cancelled": 0, "prospects": 0, "campaigns": 0}
        if not await self._table_exists("outbox"):
            return out
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        # Already written to: sent, being sent, or a send cut off that may
        # have left. Their thread goes on as it was.
        started = ("SELECT prospect_id FROM outbox WHERE prospect_id != '' AND (status IN ('sent', 'sending') "
                   "OR (status = 'failed' AND error LIKE 'the worker stopped while this was being sent%'))")
        # Held by the domain guard (a sister address bounced as nonexistent):
        # a person decides on these, and a rewrite must not slip past that.
        held = "SELECT prospect_id FROM outbox WHERE status = 'pending_review' AND error LIKE 'Held:%'"
        async with aiosqlite.connect(str(self.db_path), timeout=10) as db:
            cur = await db.execute(
                "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                f"WHERE kind = 'sequence' AND status IN ('pending_review', 'approved') AND prospect_id NOT IN ({started}) "
                f"AND prospect_id NOT IN ({held})",
                (note[:200], now))
            out["cancelled"] = cur.rowcount
            cur = await db.execute(
                "UPDATE prospects SET status = 'new', updated_at = ? WHERE status = 'queued' "
                f"AND id NOT IN ({started}) AND id NOT IN ({held})", (now,))
            out["prospects"] = cur.rowcount
            cur = await db.execute(
                "UPDATE campaigns SET status = 'failed' WHERE status IN ('draft', 'active', 'writing')")
            out["campaigns"] = cur.rowcount
            await db.commit()
        return out

    async def bounce_summary(self, limit: int = 500) -> list[dict]:
        """Why emails bounced, grouped by what the receiving server said:
        [{what, hint, count, example}], most common first. "No such
        address" is the list; rate, reputation and authentication are the
        mailbox."""
        if not await self._table_exists("outbox"):
            return []
        from harvey.integrations.mail_provider import BOUNCE_HINTS
        rows = await self._query(
            "SELECT error FROM outbox WHERE status IN ('sent', 'failed') AND error LIKE 'Bounced:%' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,))
        groups: dict[str, dict] = {}
        for r in rows:
            err = (r["error"] or "")[len("Bounced: "):]
            what, hint = "the receiving server rejected it (no reason given)", ""
            for _pattern, label, advice in BOUNCE_HINTS:
                if advice and advice in err:
                    what, hint = label, advice
                    break
            else:
                if ". " in err:
                    hint = err.rsplit(". ", 1)[-1]
            g = groups.setdefault(what, {"what": what, "hint": hint, "count": 0, "example": ""})
            g["count"] += 1
            if not g["example"]:
                g["example"] = (err[: len(err) - len(hint)] if hint and err.endswith(hint) else err).strip(" .")[:160]
        return sorted(groups.values(), key=lambda g: -g["count"])

    async def _sending_status(self, out: dict) -> dict:
        """What the sender is doing right now, in one sentence, with the
        thing to change when it is stuck: {state, text, link}. state is
        'sending', 'waiting' (nothing to send yet) or 'stuck'."""
        cfg = self.config() or {}
        email = (cfg.get("channels") or {}).get("email") or {}
        usage = cfg.get("usage") or {}
        speed = dict(SPEEDS.get(self._held_speed_marker() or "normal", SPEEDS["normal"]))
        held = self.pace_held_reason(speed)
        if not out["worker_running"]:
            if out["worker_paused"]:
                return {"state": "stuck", "text": "Paused for edits: nothing is written or sent until you press Resume.", "link": ""}
            return {"state": "stuck", "text": "The worker is not running, so nothing is written or sent.", "link": ""}
        if (out.get("worker_doing") or {}).get("state") == "stuck":
            return {"state": "stuck", "text": f"The worker stopped answering ({self.worker_stuck()}). It is restarted on its own "
                                               "within a few minutes; Wake up now does it at once.", "link": ""}
        if out["sending_paused"]:
            return {"state": "stuck", "text": f"All sending is paused ({out['sending_paused']}). Press Resume sending.", "link": ""}
        quiet = usage.get("quiet_hours") or {}
        if quiet.get("enabled", True) and quiet.get("start") and quiet.get("end"):
            try:
                from harvey.config import HarveyConfig
                from harvey.main import in_quiet_hours, seconds_until_quiet_hours_end
                hc = HarveyConfig(**cfg)
                if in_quiet_hours(hc):
                    mins = seconds_until_quiet_hours_end(hc) // 60
                    return {"state": "stuck", "text": f"Quiet hours: the worker sleeps for another {mins // 60}h {mins % 60}m "
                                                       f"(it sends only between {quiet.get('end')} and {quiet.get('start')} {quiet.get('timezone', '')}). "
                                                       "Untick \"Only send during these hours\" on the profile to send around the clock.",
                            "link": "/profile#sending"}
            except Exception:
                pass
        # The model (or the mailbox's inbox) is out of reach: first emails
        # need neither and still go; replies and follow-ups wait. That is a
        # note on whatever the sender is doing, not a state of its own.
        note, note_link = "", ""
        if await self._table_exists("settings"):
            rows = await self._query("SELECT value FROM settings WHERE key = 'sender_skip_reason'")
            skip = (rows[0]["value"] or "") if rows else ""
            if skip and "first emails still go out" in skip:
                cause = skip.rsplit("; follow-ups wait", 1)[0].strip()
                low = cause.lower()
                if "mailbox could not be read" in low:
                    note, note_link = f" Replies and follow-ups wait until the inbox can be read again: {cause}.", "/mailbox"
                else:
                    note = f" Replies and follow-ups wait until the AI can answer again: {cause}."
                    if "spend cap" in low:
                        note_link = "/profile"
                    elif any(w in low for w in ("usage limit", "credit", "rate limit", "rate-limited", "the ai ", "llm", "quota")):
                        note_link = "/apikey"
            elif skip and "quiet" not in skip:
                return {"state": "stuck", "text": f"The sender is skipping its passes: {skip}.", "link": ""}
            rows = await self._query("SELECT value FROM settings WHERE key = 'replies_waiting'")
            waiting = int((rows[0]["value"] or 0) if rows and str(rows[0]["value"] or "").isdigit() else 0)
            if waiting:
                # Read from the mailbox, not yet answered: they are there, the model is not.
                note = (f" {waiting} repl{'ies' if waiting != 1 else 'y'} came in and wait for the AI to answer "
                        f"(the worker reads them again every pass).") + note
        for line in ([] if note else reversed(self.log_tail(80))):
            low = line.lower()
            if "harvey is paused" in low or "usage limit reached" in low or "not sending" in low:
                text = line.split(" - ", 2)[-1] if " - " in line else line
                text = text.split("INFO:")[-1].split("ERROR:")[-1].strip()
                if "usage limit" in low and "/day" in low:
                    return {"state": "stuck", "text": f"The AI spend cap stopped the worker until tomorrow: {text[:160]} Raise the cap on the profile.", "link": "/profile"}
                if "usage limit" in low:
                    return {"state": "stuck", "text": f"The AI provider says the plan's usage or credit is used up; the worker checks again every hour: "
                                                       f"{text[:160]} Add credit with the provider, then press Wake up now.", "link": "/apikey"}
                if "paused" in low and "harvey is paused" in low:
                    return {"state": "stuck", "text": f"The AI provider is refusing calls: {text[:200]}", "link": "/apikey"}
                if "quiet hours" in low or "sleeping" in low:
                    continue
                return {"state": "stuck", "text": text[:220], "link": ""}
            if "sent step" in low or "sent this cycle" in low or "cycle complete" in low or "checking pipeline" in low:
                break
        a = out["allowance"]
        if a.get("reached"):
            # The day's count starts again at midnight UTC: say when, so a
            # full day does not read as a worker that went to sleep for good.
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            left = int(((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now).total_seconds())
            when = f" Sending starts again at 00:00 UTC, in {left // 3600}h {(left % 3600) // 60}m."
            return {"state": "stuck", "text": a["text"] + when + (" Switch off warm-up or raise the daily maximum on the profile." if a.get("warming") else " Raise the daily maximum on the profile.") + note, "link": "/profile#sending"}
        review = ""
        if out["outbox_pending"]:
            review = (f" {out['outbox_pending']} wait for your approval in the Outbox." if email.get("require_approval") else
                      f" {out['outbox_pending']} could not be fixed by the spam check and wait for your review in the Outbox; "
                      "nothing else waits on them.")
        if out["recipients"] == 0:
            return {"state": "waiting", "text": "No recipients yet. Upload a list and the worker starts writing.", "link": "/recipients"}
        if note:
            # Only what can leave now counts: approved first emails and replies.
            rows = await self._query("SELECT COUNT(*) AS n FROM outbox WHERE status IN ('approved', 'sending') "
                                     "AND (kind IS NULL OR kind != 'sequence' OR step IS NULL OR step <= 1)")
            going = rows[0]["n"] if rows else 0
            if going:
                pace = (f"{email.get('sends_per_cycle') or speed['per_cycle']} every "
                        f"{email.get('send_every_seconds') or speed['send_every']} seconds")
                return {"state": "sending", "text": f"Sending first emails: {going} are approved and leave at {pace}"
                                                     + (f" (held to this pace because {held})" if held else "")
                                                     + f"; {out['sent_today']} went out today." + note + review,
                        "link": note_link or ("/outbox" if review else "")}
            return {"state": "waiting", "text": ("No first emails are approved to go right now." + review if review else
                                                 "No first emails are waiting to go right now.") + note,
                    "link": note_link or ("/outbox" if review else "")}
        if out["outbox_scheduled"]:
            pace = (f"{email.get('sends_per_cycle') or speed['per_cycle']} every "
                    f"{email.get('send_every_seconds') or speed['send_every']} seconds")
            return {"state": "sending", "text": f"Sending: {out['outbox_scheduled']} emails are approved and leave at {pace}"
                                                 + (f" (held to this pace because {held})" if held else "") + f"; {out['sent_today']} went out today."
                                                 + review, "link": "/outbox" if review else ""}
        if out["outbox_pending"] and not out["waiting"]:
            return {"state": "waiting", "text": f"Everything else is sent or scheduled.{review} Rewrite, edit, approve or reject them there.",
                    "link": "/outbox"}
        if out["outbox_pending"]:
            return {"state": "waiting", "text": f"Writing: {out['waiting']} recipients still need an email; the next batch is written this cycle.{review}",
                    "link": "/outbox"}
        if out["waiting"]:
            return {"state": "waiting", "text": f"Writing: {out['waiting']} recipients still need an email; the next batch is written this cycle and sends right after.", "link": ""}
        return {"state": "waiting", "text": "Everything on the list has been sent or is scheduled as a follow-up.", "link": ""}

    async def _send_allowance(self, sent_today: int) -> dict:
        """Today's send limit as the sender computes it, with the reason in
        words, so the dashboard can say why sending stopped at 20."""
        from datetime import datetime, timezone
        from harvey.agents.sender import WARMUP_START, WARMUP_STEP, daily_allowance
        email = ((self.config() or {}).get("channels") or {}).get("email") or {}
        max_daily = int(email.get("max_daily_sends", 0) or 0)
        ceiling = int(email.get("mailbox_daily_ceiling", 0) or 0)
        if ceiling:
            max_daily = min(max_daily, ceiling) if max_daily else ceiling
        ramp = bool(email.get("warmup_ramp", True))
        first = None
        if await self._table_exists("outbox"):
            rows = await self._query("SELECT MIN(sent_at) AS first FROM outbox WHERE status = 'sent'")
            first = rows[0]["first"] if rows else None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        allowed = daily_allowance(first, now, max_daily, ramp)
        warming = ramp and (max_daily == 0 or allowed < max_daily)
        day = 0
        if warming and first:
            try:
                day = max(0, (now.date() - datetime.fromisoformat(str(first).replace("Z", "")).date()).days)
            except ValueError:
                day = 0
        out = {"allowed": allowed, "sent": sent_today, "warming": warming, "day": day + 1,
               "reached": bool(allowed and sent_today >= allowed), "max_daily": max_daily}
        replies = (f" Replies have no limit of their own: only the mailbox's maximum of {ceiling} a day, counting "
                   f"everything, stops them, and first emails leave its last {ceiling // 10} to replies."
                   if ceiling else " Replies have no limit.")
        if warming:
            out["text"] = (f"Warm-up day {day + 1}: {sent_today} of {allowed} first emails and follow-ups sent today. "
                           f"Starts at {WARMUP_START} a day and rises by {WARMUP_STEP} each day"
                           + (f" up to your limit of {max_daily}." if max_daily else ".") + replies)
        elif allowed:
            out["text"] = (f"{sent_today} of {allowed} first emails and follow-ups sent today "
                           + ("(what this mailbox allows per day)." if ceiling and allowed == ceiling else "(your daily limit).")
                           + replies)
        else:
            out["text"] = f"{sent_today} sent today, no daily limit set."
        if (not warming and (allowed == 0 or allowed > SAFE_COLD_PER_DAY)
                and (self.config() or {}).get("outreach", {}).get("use_template") is not None):
            # The mailbox's own maximum is a technical limit, not a safe pace:
            # Microsoft 365 and Google restrict a mailbox they see sending
            # bulk mail long before it, and then every email bounces.
            out["caution"] = (f"Cold email at more than {SAFE_COLD_PER_DAY} a day from one mailbox gets it blocked: "
                              "Microsoft 365 and Google restrict mailboxes they see sending bulk mail, and then every "
                              "email bounces until an admin unblocks it. 30 to 50 a day per mailbox, with the warm-up "
                              "ramp on, is the pace that lasts.")
            out["text"] += " " + out["caution"]
        return out

    async def sector_breakdown(self) -> dict:
        """Contacts per sector and role tier (the writer's classification),
        plus how many are still unsorted."""
        if not await self._table_exists("prospects"):
            return {"rows": [], "unsorted": 0, "total": 0}
        rows = await self._query(
            "SELECT industry, seniority, COUNT(*) AS n FROM prospects "
            "WHERE industry != '' GROUP BY industry, seniority ORDER BY n DESC")
        by_sector: dict[str, dict] = {}
        for r in rows:
            entry = by_sector.setdefault(r["industry"], {"sector": r["industry"].replace("_", " "),
                                                         "total": 0, "tiers": {}})
            entry["total"] += r["n"]
            tier = r["seniority"] or "individual"
            entry["tiers"][tier] = entry["tiers"].get(tier, 0) + r["n"]
        unsorted = await self._query("SELECT COUNT(*) AS n FROM prospects WHERE industry = '' OR industry IS NULL")
        total = await self._query("SELECT COUNT(*) AS n FROM prospects")
        return {"rows": sorted(by_sector.values(), key=lambda e: -e["total"]),
                "unsorted": unsorted[0]["n"] if unsorted else 0,
                "total": total[0]["n"] if total else 0}

    async def recent_recipients(self, limit: int = 50) -> list[dict]:
        if not await self._table_exists("prospects"):
            return []
        return await self._query(
            "SELECT first_name, last_name, email, company, title, status, email_status, created_at "
            "FROM prospects ORDER BY created_at DESC LIMIT ?", (limit,))

    async def clean_list(self) -> dict:
        """Verify every recipient with free checks (syntax, live domain,
        disposable, role) and move the provably-invalid ones to the Excluded
        list. Reversible — nothing is deleted. Returns the sweep report."""
        from harvey import listclean
        st = self.state()
        await st.init_db()
        return await listclean.clean_prospects(st, apply=True)

    async def excluded_recipients(self, limit: int = 500) -> list[dict]:
        """Recipients the list check moved to the Excluded list: out of every
        send, kept so they can be restored."""
        if not await self._table_exists("prospects"):
            return []
        st = self.state()
        await st.init_db()
        return [{"id": p.id, "name": p.full_name() or "—", "email": p.email,
                 "company": p.company, "reason": p.excluded_reason}
                for p in await st.get_excluded_prospects(limit)]

    async def restore_recipient(self, prospect_id: str) -> None:
        """Undo one exclusion: the recipient rejoins the sending list."""
        st = self.state()
        await st.init_db()
        await st.restore_prospect(prospect_id)

    async def generate_general_message(self) -> dict:
        """Ask the user's own model for ONE direct first email + subject that
        works for every lead, whatever their industry, from the profile, and
        save it as the message (in an AI mode: the fallback the writer sends
        when the model cannot). Returns {subject, body}."""
        from bb.mail.brain import MailBrain
        from harvey import enquiry

        brain = MailBrain(self.mail_brain_env())
        if not brain.configured:
            raise ValueError("no AI provider is set up for your account yet (AI provider page)")
        prof = self.profile()
        name = (prof.get("name") or "").strip() or "the sender"
        first = name.split()[0]
        inquiry = (prof.get("mode") or "inquiry") == "inquiry"
        facts = []
        if prof.get("intent"):
            facts.append(f"WHAT {name.upper()} NEEDS (the reason for writing): {prof['intent']}")
        if prof.get("sender_line"):
            facts.append(f"ABOUT {name.upper()}: {prof['sender_line']}")
        if prof.get("campaign_goal"):
            facts.append(f"CAMPAIGN GOAL, in {name}'s words: {prof['campaign_goal']}")
        if not inquiry and prof.get("offer_description"):
            facts.append(f"WHAT IS OFFERED: {prof['offer_description']}")
        if prof.get("ask_detail"):
            facts.append(f"CLOSING ASK: {prof['ask_detail']}")
        who = (f"You write one first email for {name}, who is the prospective customer or partner of the recipient "
               "(nothing is sold)." if inquiry else
               f"You write one first email for {name}, {prof.get('role') or ''} at {prof.get('company') or 'their company'}, "
               "writing to a prospective customer.")
        system = (who + " It is ONE general message that must work for every recipient, whatever their industry: "
                  "direct, concrete and about the actual thing, with no sector-specific wording. Plain words a person "
                  "would type, first person, contractions, no marketing vocabulary, no capitals for emphasis, no "
                  "exclamation marks, no links, no bullet points, 50 to 90 words. Start with the greeting line "
                  "\"Hi {{first_name}},\" exactly, make one clear request, and end with a plain sign-off of the first "
                  "name. The subject names the thing the email is about in the email's own words, 3 to 10 words, "
                  "sentence case; never \"quick question\", \"opportunity\", \"proposal\", \"partnership\", "
                  "\"following up\" or anything about the act of writing."
                  "\n\nReturn ONLY JSON: {\"subject\": \"...\", \"body\": \"...\"}")
        user = "\n\n".join(facts) or f"{name} is writing to businesses they want to work with."
        data = await brain.complete_json(system, user)
        if not isinstance(data, dict) or not str(data.get("body") or "").strip():
            raise ValueError("the model did not return a message; try again in a moment")
        body = str(data["body"]).strip()
        if not body.lower().startswith(("hi ", "hello ", "dear ")):
            body = "Hi {{first_name}},\n\n" + body
        if inquiry:
            body = enquiry.plain_signoff(body, first)
        subject = str(data.get("subject") or "").strip()[:120] or "A question about working together"
        form = _profile_form(prof)
        form["template_subject"], form["template_body"] = subject, body
        if form.get("writing") != "template":
            form["template_fallback"] = "on"     # in an AI mode a general message is there to fall back on
        self.write_profile(form)
        return {"subject": subject, "body": body}

    async def rewrite_item(self, item: dict, instruction: str) -> dict:
        """Rewrite one queued email with the user's own AI provider, to their
        instruction (or simply better when none is given), keeping the facts,
        the greeting line and the sign-off, under the campaign goal and the
        same checks every email passes. Saves it and returns {subject, body, note}."""
        from bb.mail.brain import MailBrain
        from harvey import enquiry
        from harvey.spamcheck import REVIEW, SUBJECT_REVIEW, describe, spam_risk, subject_from_need, subject_risk

        brain = MailBrain(self.mail_brain_env())
        if not brain.configured:
            raise ValueError("no AI provider is set up for your account yet (AI provider page)")
        cfg = self.config() or {}
        outreach, persona = cfg.get("outreach") or {}, cfg.get("persona") or {}
        name = (persona.get("name") or "").strip() or "the sender"
        first = name.split()[0]
        goal = (outreach.get("goal") or "").strip()
        need = ""
        try:
            from harvey.config import HarveyConfig
            hc = HarveyConfig(**cfg)
            state = self.state()
            await state.init_db()
            prospect = await state.get_prospect(item.get("prospect_id") or "") if item.get("prospect_id") else None
            need = enquiry.need_for(hc.outreach, getattr(prospect, "industry", "") or "")["need"] if hc.outreach.mode == "inquiry" else ""
        except Exception:
            state = self.state()
            await state.init_db()
        from harvey.integrations.mail_provider import MD_LINK_RE, embed_links
        # Its links on their words ([words](address)): the model keeps them as they are.
        body = embed_links((item.get("body") or "").strip(), self.link_names())
        links = [m.group(0) for m in MD_LINK_RE.finditer(body)]
        greeting_line = body.split("\n", 1)[0] if body else ""
        inquiry = (outreach.get("mode") or "inquiry") == "inquiry"
        is_reply = (item.get("kind") or "sequence") == "reply"
        if inquiry:
            who = (f"You rewrite one email for {name}, who is the prospective customer or partner of the recipient "
                   "(nothing is sold). ")
        else:
            who = (f"You rewrite one email for {name}, {persona.get('role') or ''} at {persona.get('company') or 'their company'}, "
                   "who is writing to a prospective customer. ")
        subject_rule = ("This is a reply in an existing thread: keep the subject exactly as it is. " if is_reply else
                        "The subject names the thing the email is about in the email's own words, 3 to 10 words, sentence case; "
                        "never \"quick question\", \"opportunity\", \"proposal\", \"partnership\", \"following up\" or anything "
                        "about the act of writing.")
        system = (who
                  + "Keep every fact, the greeting line exactly as it is, the sign-off and the same request; plain words a "
                    "person would type, first person, contractions, no marketing vocabulary, no capitals, no exclamation "
                    "marks, no new links, no bullet points. A link is written [words](address): keep each one exactly as it "
                    "is, inside a sentence that still names what it opens. " + subject_rule
                  + (f"\n\nWHAT {name.upper()} NEEDS FROM THIS BUSINESS: {need}" if need else "")
                  + (f"\n\nCAMPAIGN GOAL, in {name}'s words (the email serves it): {goal}" if goal else "")
                  + "\n\nReturn ONLY JSON: {\"subject\": \"...\", \"body\": \"...\"}")
        user = ((f"INSTRUCTION FROM {name}: {instruction}\n\n" if instruction else "Make it better: shorter, more concrete, more natural.\n\n")
                + f"SUBJECT: {item.get('subject', '')}\n\nBODY:\n{body}")
        # (No salvage here: a rewrite replaces an email that may already be
        # approved, so an answer not in the form asked for is never used.)
        data = await brain.complete_json(system, user)
        if not isinstance(data, dict) or not str(data.get("body") or "").strip():
            raise ValueError("the model did not return an email")
        new_body = str(data["body"]).strip()
        if greeting_line and not new_body.startswith(greeting_line):
            rest = new_body.split("\n\n", 1)[-1] if new_body.lower().startswith(("hi", "hello", "dear")) else new_body
            new_body = f"{greeting_line}\n\n{rest}"
        new_body = enquiry.plain_signoff(new_body, first) if inquiry else new_body
        for link in links:
            url = MD_LINK_RE.match(link).group(2)
            if url not in new_body:
                # The model left a link out: it goes back, on its own line before the sign-off.
                head, sep, tail = new_body.rpartition("\n\n")
                new_body = f"{head}\n\n{link}{sep}{tail}" if sep else f"{new_body}\n\n{link}"
        new_subject = str(data.get("subject") or item.get("subject") or "").strip()[:120]
        notes = []
        if is_reply:
            new_subject = item.get("subject") or new_subject      # a reply stays in its thread
        elif subject_risk(new_subject, new_body, need)[0] >= SUBJECT_REVIEW:
            plain = subject_from_need(need, new_body)
            if plain and subject_risk(plain, new_body, need)[0] < SUBJECT_REVIEW:
                new_subject = plain
                notes.append("the subject was replaced with a plain one from your need")
            else:
                notes.append("the subject still reads like a campaign (" + describe(subject_risk(new_subject, new_body, need)[1])
                             + "), so it waits for your review")
        score, reasons = spam_risk(new_subject, new_body)
        fields = {"subject": new_subject, "body": new_body[:4000], "error": ""}
        if score >= REVIEW:
            notes.append("a spam filter would still flag " + describe(reasons) + ", so it waits for your review")
        if any("waits for your review" in n for n in notes):
            fields["status"] = "pending_review"
        await state.update_outbox_item(item["id"], **fields)
        return {"subject": new_subject, "body": new_body, "note": ("; ".join(notes) + ".") if notes else "It passed the checks."}

    async def mailbox_health(self) -> dict:
        """The worker's view of the mailbox: last login check, last delivery,
        and the alert it raised, if any (see harvey.health)."""
        if not await self._table_exists("settings"):
            return {}
        from harvey import health
        rows = await self._query("SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?, ?, ?, ?)",
                                 (health.CHECK_AT_KEY, health.CHECK_OK_KEY, health.CHECK_ERROR_KEY,
                                  health.LAST_SENT_KEY, health.ALERT_KEY, health.ALERT_SINCE_KEY, health.CHECK_AUTH_KEY))
        got = {r["key"]: r["value"] or "" for r in rows}
        return {"ok": got.get(health.CHECK_OK_KEY) != "0", "checked_at": got.get(health.CHECK_AT_KEY, ""),
                "error": got.get(health.CHECK_ERROR_KEY, ""), "last_sent_at": got.get(health.LAST_SENT_KEY, ""),
                "alert": got.get(health.ALERT_KEY, ""), "alert_since": got.get(health.ALERT_SINCE_KEY, ""),
                "auth": got.get(health.CHECK_AUTH_KEY, "")}

    async def outbox(self, status: str = "pending_review", limit: int = 200) -> list[dict]:
        if not await self._table_exists("outbox"):
            return []
        if status == "bounced":
            return await self._query(
                "SELECT o.*, p.first_name, p.last_name, p.company FROM outbox o "
                "LEFT JOIN prospects p ON p.id = o.prospect_id "
                "WHERE o.status IN ('sent', 'failed') AND o.error LIKE 'Bounced:%' "
                "ORDER BY o.updated_at DESC LIMIT ?", (limit,))
        return await self._query(
            "SELECT o.*, p.first_name, p.last_name, p.company FROM outbox o "
            "LEFT JOIN prospects p ON p.id = o.prospect_id "
            "WHERE o.status = ? ORDER BY o.send_at ASC, o.created_at ASC LIMIT ?", (status, limit))

    async def conversations(self, limit: int = 100) -> list[dict]:
        if not await self._table_exists("conversations"):
            return []
        rows = await self._query(
            "SELECT c.*, p.first_name, p.last_name, p.email AS prospect_email, p.company "
            "FROM conversations c LEFT JOIN prospects p ON p.id = c.prospect_id "
            "ORDER BY c.updated_at DESC LIMIT ?", (limit,))
        for row in rows:
            try:
                thread = json.loads(row.get("thread_json") or "[]")
            except (ValueError, TypeError):
                thread = []
            theirs = [m for m in thread if isinstance(m, dict) and m.get("sender") == "prospect"]
            row["last_message"] = (theirs[-1].get("content", "") if theirs else "")[:400]
            row["messages"] = len(thread)
        return rows

    async def set_sending_paused(self, paused: bool):
        state = self.state()
        await state.init_db()
        if paused:
            await state.set_setting("sending_paused", "paused from the web app")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")

    # ── worker process ──

    def worker_running(self) -> int | None:
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
        if _process_gone(pid):
            self.pid_file.unlink(missing_ok=True)
            return None
        if not _is_harvey_process(pid):
            # After a reboot the number can belong to anything: forget it.
            self.pid_file.unlink(missing_ok=True)
            return None
        return pid

    def worker_env(self) -> dict:
        env = {**os.environ, **self.settings.worker_env(), "HARVEY_HOME": str(self.root),
               "PYTHONUNBUFFERED": "1"}
        # One of possibly several campaigns on this account: the shared ledger.
        from bb import campaigns as campaigns_mod
        env.update({"HARVEY_LEDGER": str(campaigns_mod.ledger_path(self.user_root)),
                    "HARVEY_LEDGER_ACCOUNT": (self.sending_address() or "").lower(),
                    "HARVEY_LEDGER_CAMPAIGN": self.campaign_id,
                    "HARVEY_LEDGER_LABEL": self.campaign_name()})
        for key in list(env):
            if key.startswith("BB_") or key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                                 "ANTHROPIC_PROFILE", "ANTHROPIC_WORKSPACE_ID"):
                env.pop(key)
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                    "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD"):
            env.pop(key, None)
        # The user's own provider and key: usage is theirs, never the site's.
        llm = self.llm()
        env.pop("LLM_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        if llm["provider"] == "anthropic":
            env["ANTHROPIC_API_KEY"] = llm["api_key"]
            if llm["workspace_id"]:
                env["ANTHROPIC_WORKSPACE_ID"] = llm["workspace_id"]
        elif llm["api_key"]:
            env["LLM_API_KEY"] = llm["api_key"]
        if self.mail_provider() == "smtp":
            s = self.smtp_settings()
            env.update({
                "SMTP_HOST": s["smtp_host"], "SMTP_PORT": str(s["smtp_port"] or 587),
                "SMTP_USERNAME": s["smtp_username"], "SMTP_PASSWORD": s["smtp_password"],
                "IMAP_HOST": s["imap_host"] or s["smtp_host"],
                "IMAP_PORT": str(s["imap_port"] or 993),
                "IMAP_USERNAME": s["imap_username"] or s["smtp_username"],
                "IMAP_PASSWORD": s["imap_password"] or s["smtp_password"],
            })
            if s.get("reply_to"):
                env["SMTP_REPLY_TO"] = s["reply_to"]
        for key in ("M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET", "M365_MAILBOX"):
            env.pop(key, None)
        if self.mail_provider() == "microsoft_app":
            m = self.m365_settings()
            env.update({"M365_TENANT_ID": m["m365_tenant_id"], "M365_CLIENT_ID": m["m365_client_id"],
                        "M365_CLIENT_SECRET": m["m365_client_secret"], "M365_MAILBOX": m["m365_mailbox"]})
        elif self.mail_provider() == "microsoft":
            # The signed-in user's mailbox through the site's own app registration.
            env.update({"M365_TENANT_ID": getattr(self.settings, "ms_tenant", "") or "common",
                        "M365_CLIENT_ID": getattr(self.settings, "ms_client_id", "") or "",
                        "M365_CLIENT_SECRET": getattr(self.settings, "ms_client_secret", "") or ""})
        return env

    def start_worker(self) -> tuple[bool, str]:
        if not self.llm_configured():
            return False, "Set up your AI provider first."
        if not self.is_main:
            problem = self.bind_account()
            if problem:
                return False, problem[:1].upper() + problem[1:] + "."
        if not self.is_configured():
            return False, "Fill in your profile first."
        if not self.mailbox_connected():
            return False, "Connect your mailbox first."
        if self.is_main:
            from bb import campaigns as campaigns_mod
            other = next((r for r in campaigns_mod.on_account(self.user_root, self.sending_address())
                          if r["id"] != campaigns_mod.MAIN), None)
            if other:
                return False, (f"{self.sending_address()} already sends for the campaign {other.get('name') or other['id']}; "
                               "connect another mailbox here (one campaign per account).")
        try:
            # The account's daily limit, shared with any reply campaign or scan on it.
            self.ledger().set_limit(self.sending_address(), self.account_daily_limit(), hard=self.mailbox_daily_ceiling())
            if self.is_main:
                self.sync_main_registry()
            from bb import campaigns as campaigns_mod
            campaigns_mod.backfill_suppressed(self.user_root)   # every earlier opt-out holds here too
            campaigns_mod.backfill_answered(self.user_root)     # and what was answered is not answered again
        except Exception as e:
            logger.warning("Could not set the shared limit for %s: %s", self.sending_address(), e)
        if pid := self.worker_running():
            return True, f"Already running (pid {pid})."
        self.ensure()
        from harvey import wake
        for leftover in (wake.WAKE_FILE, wake.BEAT_FILE):    # the previous process's, not this one's
            (self.data_dir / leftover).unlink(missing_ok=True)
        try:
            with open(self.log_file, "a") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "harvey.main"], cwd=str(PROJECT_ROOT),
                    env=self.worker_env(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
        except Exception as e:
            logger.error("Could not start worker for %s: %s", self.user["email"], e)
            return False, f"Could not start: {e}"
        self.pid_file.write_text(str(proc.pid))
        self.autostart_file.write_text(str(int(time.time())))
        self._stamp_version(self.pid_file)
        return True, f"Started (pid {proc.pid})."

    async def stop_worker(self, reason: str = "Stop pressed on the dashboard",
                          grace: float = STOP_GRACE_SECONDS) -> tuple[bool, str]:
        self.autostart_file.unlink(missing_ok=True)
        pid = self.worker_running()
        if not pid:
            self.pid_file.unlink(missing_ok=True)
            self._leave_ledger()         # its share of the account goes back to the others
            return True, "Not running."
        self._note_in_log(f"BB is stopping the worker: {reason}.")
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(max(1, int(grace / 0.25))):   # a worker mid-send finishes the email first
                await asyncio.sleep(0.25)
                if _process_gone(pid):
                    break
            else:
                os.kill(pid, signal.SIGKILL)
                await asyncio.sleep(0.2)
                _process_gone(pid)
        except (ProcessLookupError, PermissionError):
            pass
        _unlink_if_pid(self.pid_file, pid)
        # Only now, with the last send pass over, does its share go back: a
        # send in flight would otherwise sign it up again.
        self._leave_ledger()
        return True, "Stopped."

    def wants_autostart(self) -> bool:
        return self.autostart_file.exists()

    # ── outreach campaigns: one per account, each its own Harvey workspace ──

    def campaign_workspaces(self) -> list["Workspace"]:
        """Every outreach campaign other than the main one."""
        from bb import campaigns as campaigns_mod
        return [Workspace(self.settings, self.user, r["id"]) for r in campaigns_mod.load(self.user_root)
                if r["id"] != campaigns_mod.MAIN]

    def all_campaigns(self) -> list["Workspace"]:
        return [self.main(), *self.campaign_workspaces()]

    def campaign_row(self) -> dict:
        from bb import campaigns as campaigns_mod
        return campaigns_mod.get(self.user_root, self.campaign_id) or {}

    def campaign_name(self) -> str:
        return self.campaign_row().get("name") or ("Main campaign" if self.is_main else self.campaign_id)

    def campaign(self, campaign_id: str) -> "Workspace | None":
        """Another campaign of the same user, or None when it does not exist."""
        from bb import campaigns as campaigns_mod
        if not campaign_id or campaign_id == campaigns_mod.MAIN:
            return self.main()
        if not campaigns_mod.valid_id(campaign_id) or not campaigns_mod.get(self.user_root, campaign_id):
            return None
        return Workspace(self.settings, self.user, campaign_id)

    def selected(self) -> "Workspace":
        """The campaign the pages show (chosen in the nav), else the main one."""
        try:
            cid = self.current_campaign_file.read_text().strip()
        except OSError:
            cid = ""
        return (self.campaign(cid) if cid else None) or self.main()

    def select(self, campaign_id: str) -> bool:
        target = self.campaign(campaign_id)
        if target is None:
            return False
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.current_campaign_file.write_text(target.campaign_id)
        return True

    def sync_main_registry(self):
        """The main campaign is in the registry with the account it sends
        from, so a reply campaign or scan on that account knows its leads."""
        from bb import campaigns as campaigns_mod
        main = self.main()
        row = campaigns_mod.get(self.user_root, campaigns_mod.MAIN) or {}
        account = (main.sending_address() or "").lower() if main.mailbox_connected() else ""
        if row.get("account", None) != account or not row:
            campaigns_mod.upsert(self.user_root, {**row, "id": campaigns_mod.MAIN, "name": row.get("name") or "Main campaign",
                                                  "account": account, "mailbox_id": ""})

    def ledger(self):
        from bb import campaigns as campaigns_mod
        from harvey.ledger import Ledger
        return Ledger(campaigns_mod.ledger_path(self.user_root))

    # the account an outreach campaign sends from

    def _mailbox_sync(self, mailbox_id: str = "", address: str = "") -> dict | None:
        """A command-center account (mail.db), read without the async layer."""
        import sqlite3
        if not self.mail_db_path.exists() or not (mailbox_id or address):
            return None
        try:
            con = sqlite3.connect(f"file:{self.mail_db_path}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                if mailbox_id:
                    row = con.execute("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,)).fetchone()
                else:
                    row = con.execute("SELECT * FROM mailboxes WHERE lower(address) = ?", (address.lower(),)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        if not row:
            return None
        out = dict(row)
        try:
            out["settings"] = json.loads(out.get("settings_json") or out.get("settings") or "{}")
        except (TypeError, ValueError):
            out["settings"] = {}
        return out

    def bound_mailbox(self) -> dict | None:
        mid = self.campaign_row().get("mailbox_id") or ""
        return self._mailbox_sync(mailbox_id=mid) if mid else None

    def bind_account(self) -> str:
        """Copy the bound account's connection into this campaign's own files
        (the same files a mailbox connected on the Mailbox page uses), so the
        campaign's worker sends and reads as that account. Only copied again
        when the account's credentials changed (a reconnect): a Microsoft
        token the worker rotated since is kept. Returns a problem or ""."""
        import hashlib
        from bb import gmail_oauth, microsoft_oauth
        from bb.mail import crypto
        if self.is_main:
            return ""
        box = self.bound_mailbox()
        if not box:
            return "the account this campaign sends from was removed from Accounts; choose another on its Mailbox page"
        if not box.get("enabled", 1):
            return f"{box['address']} is disabled on the Accounts page"
        try:
            creds = crypto.open_json(self.settings.secret_key(), box.get("creds") or "")
        except Exception:
            return f"the stored sign-in for {box['address']} could not be read; reconnect it on the Accounts page"
        reply_to_now = str((box.get("settings") or {}).get("reply_to") or "")
        stamp = hashlib.sha256(((box.get("creds") or "") + "|" + reply_to_now).encode()).hexdigest()
        try:
            binding = json.loads(self.binding_file.read_text())
        except (OSError, ValueError):
            binding = {}
        if (binding.get("mailbox_id") == box["id"] and binding.get("stamp") == stamp and self.mailbox_connected()
                and (self.sending_address() or "").lower() == (box.get("address") or "").lower()):
            return ""          # unchanged, and still sending as the bound address
        self.ensure()
        address = (box.get("address") or "").lower()
        reply_to = str((box.get("settings") or {}).get("reply_to") or "")
        provider = box.get("provider")
        if provider == "imap":
            self.save_secrets(smtp_host=creds.get("smtp_host", ""), smtp_port=str(creds.get("smtp_port") or 587),
                              smtp_username=creds.get("username", ""), smtp_password=creds.get("password", ""),
                              imap_host=creds.get("imap_host", ""), imap_port=str(creds.get("imap_port") or 993),
                              imap_username=creds.get("imap_username", "") or "", imap_password=creds.get("imap_password", "") or "",
                              from_email=address if address != (creds.get("username") or "").lower() else "", reply_to=reply_to)
            self.set_mail_provider("smtp")
        elif provider == "gmail" and creds.get("refresh_token"):
            gmail_oauth.save_token(self, {"refresh_token": creds["refresh_token"]}, address)
            self.set_mail_provider("gmail")
        elif provider == "graph" and creds.get("refresh_token"):
            microsoft_oauth.save_token(self, {"refresh_token": creds["refresh_token"]}, address)
            self.set_mail_provider("microsoft")
        elif provider == "graph":
            self.save_secrets(m365_tenant_id=creds.get("tenant_id", ""), m365_client_id=creds.get("client_id", ""),
                              m365_client_secret=creds.get("client_secret", ""), m365_mailbox=address)
            self.set_mail_provider("microsoft_app")
        else:
            return f"{address} has no usable sign-in; reconnect it on the Accounts page"
        self.binding_file.write_text(json.dumps({"mailbox_id": box["id"], "address": address, "stamp": stamp,
                                                 "at": int(time.time())}))
        self.clear_mailbox_problem()     # a new or renewed sign-in: the old failure is history
        return ""

    def account_daily_limit(self) -> int:
        """The daily limit of the account this campaign sends from, shared
        with whatever else runs on it: the cap set on the Accounts page,
        else what the provider allows."""
        from bb.mail import service as mail_service
        box = self.bound_mailbox() if not self.is_main else self._mailbox_sync(address=self.sending_address())
        if box and int((box.get("settings") or {}).get("account_limit") or 0):
            return int(box["settings"]["account_limit"])
        if self.is_main or not box:
            # The main campaign's own mailbox: what it allows (0 for a
            # sending service, which has no ceiling to share).
            return self.mailbox_daily_ceiling()
        try:
            from bb.mail import crypto
            creds = crypto.open_json(self.settings.secret_key(), box.get("creds") or "")
        except Exception:
            creds = {}
        return mail_service.account_daily_limit(box, creds)

    def leads_elsewhere(self, emails: list[str]) -> tuple[set[str], set[str]]:
        """(addresses already on another campaign's list, addresses that asked
        any campaign never to be contacted): neither is added here. Each
        account sends only to its own leads, and nobody gets the same
        enquiry from two of them."""
        import sqlite3
        wanted = sorted({(e or "").strip().lower() for e in emails if e and "@" in e})
        taken: set[str] = set()
        # Every other campaign's list, and the people a removed campaign had
        # already written to (its folder is kept under campaigns/.removed).
        sources = [(o.db_path, "") for o in self.all_campaigns() if o.campaign_id != self.campaign_id]
        removed_dir = self.user_root / "campaigns" / ".removed"
        if removed_dir.is_dir():
            sources += [(d / "data" / "harvey.db", " AND status != 'new'") for d in removed_dir.iterdir() if d.is_dir()]
        for db_path, only in sources:
            if not db_path.exists():
                continue
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            except sqlite3.Error:
                continue
            try:
                for i in range(0, len(wanted), 500):
                    chunk = wanted[i:i + 500]
                    marks = ",".join("?" for _ in chunk)
                    taken |= {r[0] for r in con.execute(f"SELECT lower(email) FROM prospects WHERE lower(email) IN ({marks}){only}", chunk)}
            except sqlite3.Error:
                pass
            finally:
                con.close()
        try:
            suppressed = self.ledger().suppressed_among(wanted)
        except Exception:
            suppressed = set()
        return taken, suppressed

    async def account_changed(self, mailbox_id: str, how: str = "reconnected") -> list[str]:
        """An account on the Accounts page was signed in again, disabled or
        removed: the outreach campaign bound to it follows at once. A new
        sign-in is copied in and its worker restarted with it; a disabled or
        removed account stops the campaign's worker. Returns its names."""
        touched = []
        for camp in self.main().campaign_workspaces():
            if camp.campaign_row().get("mailbox_id") != mailbox_id:
                continue
            touched.append(camp.campaign_name())
            if how in ("disabled", "removed"):
                if camp.worker_running():
                    await camp.stop_worker(f"its account was {how} on the Accounts page")
                continue
            camp.binding_file.unlink(missing_ok=True)
            if not camp.bind_account():
                await camp.restart_worker_if_running("the account's new sign-in")
        return touched

    def _leave_ledger(self):
        try:
            self.ledger().leave_everywhere(self.campaign_id)
        except Exception as e:
            logger.debug("ledger leave failed for %s: %s", self.campaign_id, e)

    # ── the mail command center (bb/mail) ──

    @property
    def mail_db_path(self):
        return self.user_data_dir / "mail.db"

    def mail_db(self):
        from bb.mail.db import MailDB
        self.ensure()
        return MailDB(self.mail_db_path)

    def gmail_token(self) -> dict:
        try:
            data = json.loads(self.gmail_token_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @property
    def mail_pid_file(self):
        return self.user_data_dir / "mailworker.pid"

    @property
    def mail_autostart_file(self):
        return self.user_data_dir / "mailworker.autostart"

    @property
    def mail_log_file(self):
        return self.user_data_dir / "mailworker.log"

    def mail_worker_running(self) -> int | None:
        try:
            pid = int(self.mail_pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
        # A zombie, or a number reused by another program after a reboot,
        # is not the mail worker (and Stop must never signal that program).
        if _process_gone(pid) or not _is_harvey_process(pid):
            self.mail_pid_file.unlink(missing_ok=True)
            return None
        return pid

    def mail_worker_stuck(self) -> str:
        """Why the running mail worker looks frozen, or "": its process is
        there but it has not touched its heartbeat for five minutes."""
        from harvey import wake
        if not self.mail_worker_running():
            return ""
        try:
            age = time.time() - int((self.user_data_dir / "mailworker.heartbeat").read_text().strip())
            started = self.mail_pid_file.stat().st_mtime
        except (OSError, ValueError):
            return ""
        if age < wake.STALE_SECONDS or time.time() - started < wake.STALE_SECONDS:
            return ""
        return f"nothing from it for {int(age // 60)} minutes although its process is still there"

    async def restart_mail_if_stuck(self, seen: dict) -> str:
        """The site's watch for the mail worker: frozen on two looks in a row → restarted."""
        key = "mail:" + str(self.user_root)
        why = "" if self.user.get("disabled") else self.mail_worker_stuck()
        if not why:
            seen.pop(key, None)
            return ""
        if key not in seen:
            seen[key] = time.time()
            return ""
        seen.pop(key, None)
        return (await self.restart_mail_worker(why))[1]

    async def restart_mail_worker(self, why: str = "Restart pressed") -> tuple[bool, str]:
        try:
            with open(self.mail_log_file, "a") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} bb INFO Restarting the mail worker: {why}.\n")
        except OSError:
            pass
        async with _lifecycle_lock("mail:" + str(self.user_root)):
            await self.stop_mail_worker()
            ok, detail = self.start_mail_worker()
        return ok, (f"The mail worker was restarted ({why}). {detail}" if ok else detail)

    def mail_worker_alive(self) -> bool:
        """Running and heartbeating within the last minute."""
        if not self.mail_worker_running():
            return False
        try:
            return time.time() - int((self.user_data_dir / "mailworker.heartbeat").read_text().strip()) < 60
        except (OSError, ValueError):
            return False

    def start_mail_worker(self) -> tuple[bool, str]:
        if pid := self.mail_worker_running():
            return True, f"Already running (pid {pid})."
        self.ensure()
        from bb.mail.brain import env_from_llm
        env = {k: v for k, v in os.environ.items() if not k.startswith("BB_")}
        env.update(self.settings.worker_env())
        env.update(env_from_llm(self.llm()))
        env["BB_SECRET_KEY"] = self.settings.secret_key()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            with open(self.mail_log_file, "a") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "bb.mail.worker", str(self.user_root)], cwd=str(PROJECT_ROOT),
                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                )
        except Exception as e:
            logger.error("Could not start the mail worker for %s: %s", self.user["email"], e)
            return False, f"Could not start: {e}"
        self.mail_pid_file.write_text(str(proc.pid))
        self.mail_autostart_file.write_text(str(int(time.time())))
        self._stamp_version(self.mail_pid_file)
        return True, f"Started (pid {proc.pid})."

    async def stop_mail_worker(self) -> tuple[bool, str]:
        self.mail_autostart_file.unlink(missing_ok=True)
        pid = self.mail_worker_running()
        if not pid:
            self.mail_pid_file.unlink(missing_ok=True)
            return True, "Not running."
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                await asyncio.sleep(0.25)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        _unlink_if_pid(self.mail_pid_file, pid)
        return True, "Stopped."

    def mail_brain_env(self) -> dict:
        """The same BB_LLM_* the worker gets, for drafting inside a request."""
        from bb.mail.brain import env_from_llm
        return {**self.settings.worker_env(), **env_from_llm(self.llm())}

    async def restart_mail_worker_if_running(self) -> bool:
        if not self.mail_worker_running():
            return False
        await self.stop_mail_worker()
        ok, _ = self.start_mail_worker()
        return ok

    def revive_mail_worker(self) -> str:
        if self.user.get("disabled") or self.mail_worker_running() or not self.mail_autostart_file.exists():
            return ""
        ok, detail = self.start_mail_worker()
        return f"The background worker had stopped and was started again ({detail})." if ok else ""

    def worker_paused(self) -> bool:
        return self.paused_file.exists() and not self.worker_running()

    async def pause_worker(self) -> tuple[bool, str]:
        """Stop the worker so settings can be changed, remembering that it
        is meant to come back. Queued emails and conversations stay put."""
        ok, detail = await self.stop_worker("paused for edits from the dashboard")
        self.paused_file.write_text(str(int(time.time())))
        return ok, ("Paused. Make your edits, then press Resume." if detail != "Not running."
                    else "The worker was not running. Make your edits, then press Resume.")

    def resume_worker(self) -> tuple[bool, str]:
        ok, detail = self.start_worker()
        if ok:
            self.paused_file.unlink(missing_ok=True)
            detail = "Resumed with your latest settings." if "Started" in detail else detail
        return ok, detail

    async def restart_worker_if_running(self, reason: str) -> bool:
        """Settings only reach the worker at start, so a running worker is
        restarted after a save. Returns True when that happened."""
        if not self.worker_running():
            return False
        async with _lifecycle_lock(str(self.root)):
            await self.stop_worker(f"restarting to apply {reason}")
            ok, _ = self.start_worker()
        return ok

    async def reset_data(self) -> tuple[bool, str]:
        """Start over: stop the worker and delete everything it collected
        (recipients, companies, written emails, the outbox, replies,
        counters). Profile, mailbox, AI provider, attachments and signature
        stay. The next upload begins a fresh list."""
        was_running = bool(self.worker_running())
        await self.stop_worker("starting over from the dashboard")
        self.paused_file.unlink(missing_ok=True)
        removed = 0
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm"),
                     self.data_dir / "analytics.json"):
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                return False, f"Could not clear {path.name}: {e}"
        self._note_in_log("Started over: recipients, emails, replies and counters were cleared "
                          "from the dashboard. Profile, mailbox and AI provider were kept.")
        return True, ("Everything was cleared: recipients, emails, replies and counters. Your profile, "
                      "mailbox, AI provider, attachments and signature are kept. Upload a new list, then "
                      + ("press Start." if was_running else "start the worker."))

    def _note_in_log(self, text: str):
        try:
            with open(self.log_file, "a") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - bb - INFO - {text}\n")
        except OSError:
            pass

    # ── code version: a worker started on older code is restarted after an update ──

    def _stamp_version(self, pid_file):
        try:
            pid_file.with_suffix(".version").write_text(code_version())
        except OSError:
            pass

    def _started_on(self, pid_file) -> str:
        try:
            return pid_file.with_suffix(".version").read_text().strip()
        except OSError:
            return ""

    async def restart_workers_on_old_code(self) -> list[str]:
        """After `git pull` and a site restart, running workers still carry
        the previous code (they are separate processes). Restart those that
        were started on a different version. Returns what was restarted."""
        current = code_version()
        out = []
        if self.worker_running() and self._started_on(self.pid_file) != current:
            if await self.restart_worker_if_running("the updated code"):
                out.append("worker")
        if self.mail_worker_running() and self._started_on(self.mail_pid_file) != current:
            if await self.restart_mail_worker_if_running():
                out.append("mail worker")
        return out

    def revive_worker(self) -> str:
        """If the worker should be running (it was started and never
        stopped from here) but its process is gone — the Mac rebooted, the
        system killed it, it crashed — start it again. Returns a note for
        the page, or ""."""
        if self.user.get("disabled") or self.worker_running() or not self.wants_autostart():
            return ""
        self._note_in_log("The worker was not running although it was never stopped from "
                          "here (a crash, a reboot, or the system ended it). Starting it again.")
        ok, detail = self.start_worker()
        return f"The worker had stopped on its own and was started again ({detail})." if ok else ""

    # ── waking a sleeping worker, and one that stopped answering ──

    def worker_beat(self) -> dict | None:
        """The running worker's last beat (what it is doing, until when),
        or None: not running, or started on code that writes none."""
        from harvey import wake
        pid = self.worker_running()
        beat = wake.read_beat(self.data_dir) if pid else None
        if not beat or int(beat.get("pid") or 0) != pid:
            return None                       # left over from an earlier process
        return beat

    def worker_stuck(self) -> str:
        """Why the running worker looks frozen, or "": its process is alive
        but it has written no beat for five minutes (something blocked it)."""
        from harvey import wake
        beat = self.worker_beat()
        if not beat:
            return ""
        if beat["age"] < wake.STALE_SECONDS:
            # Beating, but one cycle that never ends (a step waiting on
            # something that will not answer) is stuck too.
            # (Time the machine was awake: a cycle a closed lid paused is not stuck.)
            busy = float(beat["busy_for"]) if "busy_for" in beat else time.time() - float(beat.get("since") or time.time())
            if beat.get("state") == "working" and busy > STUCK_CYCLE_SECONDS:
                return f"one cycle has been running for {int(busy // 60)} minutes"
            return ""
        try:
            started = self.pid_file.stat().st_mtime
        except OSError:
            started = 0.0
        if time.time() - started < wake.STALE_SECONDS:
            return ""
        return f"nothing from it for {int(beat['age'] // 60)} minutes although its process is still there"

    def worker_doing(self) -> dict:
        """For the dashboard: {state, text} of the running worker, e.g. a
        sleep with the time left, or {} when there is nothing to say."""
        beat = self.worker_beat()
        if not beat:
            return {}
        if self.worker_stuck():
            return {"state": "stuck", "text": "Not answering: it is restarted on its own within a few minutes, or press Wake up now."}
        state, left = beat.get("state") or "", max(0, int(float(beat.get("until") or 0) - time.time()))
        when = (f"{left} seconds" if left < 90 else f"{round(left / 60)} minutes" if left < 5400
                else f"{left // 3600}h {(left % 3600) // 60}m")
        until = float(beat.get("until") or 0)
        if state == "sleeping":
            return {"state": "sleeping", "until": until, "text": f"Sleeping: the next cycle starts in {when}."}
        if state == "quiet":
            return {"state": "quiet", "until": until, "text": f"Quiet hours: it starts again in {when}."}
        if state == "paused":
            return {"state": "paused", "until": until,
                    "text": f"Waiting {when} before it checks again: {beat.get('detail') or 'see the activity below'}."}
        if state == "backoff":
            return {"state": "backoff", "until": until, "text": f"Recovering from an error: it tries again in {when}."}
        if state == "working":
            return {"state": "working", "text": "Working on a cycle now."}
        return {}

    async def wake_worker(self, by: str = "the dashboard") -> tuple[bool, str]:
        """Wake up now: the running worker ends whatever sleep it is in and
        runs a cycle (a busy one goes straight on to the next instead of
        sleeping). One that does not answer is restarted; one that is not
        running is started (unless it was paused for edits)."""
        from harvey import wake
        if self.worker_paused():
            return False, "The worker is paused for edits: press Resume to start it again."
        pid = self.worker_running()
        if not pid:
            ok, detail = self.start_worker()
            if ok:
                self.paused_file.unlink(missing_ok=True)
                return True, f"It was not running, so it was started. {detail}"
            return False, detail
        stuck = self.worker_stuck()
        if stuck:
            return await self.restart_stuck_worker(stuck, pid_seen=pid)    # what the page promised: at once
        wake.request_wake(self.data_dir, by)
        for _ in range(int(WAKE_WAIT_SECONDS / 0.25)):
            await asyncio.sleep(0.25)
            if not wake.wake_pending(self.data_dir):
                break
        else:
            beat = self.worker_beat()
            if beat and beat["age"] < WAKE_BUSY_SECONDS:
                # It beat a moment ago: a slow step (a shared file another
                # process is writing, a long call) holds it. The wake waits for it.
                return True, ("It is busy with a slow step right now. The wake is waiting for it and it carries on "
                              "the moment that step is done; press again in a minute if nothing changes.")
            return await self.restart_stuck_worker(f"it did not answer Wake up now within {WAKE_WAIT_SECONDS} seconds", pid_seen=pid)
        beat = self.worker_beat() or {}
        woke_from, until = beat.get("woken_from") or "", float(beat.get("woken_until") or 0)
        if woke_from == "quiet":
            left = max(0, int(until - time.time()))
            return True, (f"It is awake, but it is quiet hours for another {left // 3600}h {(left % 3600) // 60}m, so nothing "
                          "is sent until then. Untick \"Only send during these hours\" on the profile to send around the clock.")
        if woke_from == "paused":
            return True, f"It is checking again now. Last time it was waiting because: {beat.get('woken_detail') or 'see the activity'}."
        if woke_from == "working":
            return True, "It is in the middle of a cycle; when that is done it goes straight on to the next one instead of sleeping."
        return True, "Woken up: it is running a cycle now."

    async def restart_stuck_worker(self, why: str, pid_seen: int | None = None) -> tuple[bool, str]:
        """End a worker that stopped answering and start it again. With
        ``pid_seen``, only that process: one already replaced is left be."""
        async with _lifecycle_lock(str(self.root)):
            pid = self.worker_running()
            if pid_seen is not None and pid != pid_seen:
                return True, "It was restarted a moment ago." if pid else "It had already stopped."
            return await self._restart_stuck(why)

    async def _restart_stuck(self, why: str) -> tuple[bool, str]:
        self._note_in_log(f"The worker stopped answering ({why}). Restarting it; nothing is lost.")
        pid = self.worker_running()
        if pid and hasattr(signal, "SIGUSR2"):
            # Where it was stuck, every thread's stack, into its own log
            # (harvey.main registers this): the cause can be read later.
            try:
                os.kill(pid, signal.SIGUSR2)
                await asyncio.sleep(0.5)
            except (ProcessLookupError, PermissionError):
                pass
        await self.stop_worker(f"it stopped answering ({why})", grace=STUCK_GRACE_SECONDS)
        ok, detail = self.start_worker()
        if ok:
            self.paused_file.unlink(missing_ok=True)
            return True, f"It was not answering ({why}), so it was restarted. {detail}"
        return False, f"It was not answering ({why}) and could not be started again: {detail}"

    async def restart_if_stuck(self, seen: dict) -> str:
        """The site's watch: a worker found frozen on two checks in a row is
        restarted. ``seen`` remembers the first check between calls."""
        key = str(self.root)
        why = "" if self.user.get("disabled") else self.worker_stuck()
        if not why:
            seen.pop(key, None)
            return ""
        if key not in seen:
            seen[key] = time.time()
            return ""
        seen.pop(key, None)
        ok, detail = await self.restart_stuck_worker(why, pid_seen=self.worker_running())
        return detail

    def recent_activity(self, limit: int = 30) -> list[dict]:
        """The worker log as a readable feed: newest first, routine chatter
        left out, repeats folded together. Each entry: when, agent, kind,
        text, count."""
        return parse_activity(self.log_tail(600), limit)

    def log_tail(self, lines: int = 60) -> list[str]:
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        return text.strip().splitlines()[-lines:]


# ── the worker log as a feed ──

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}(?:,\d+)?\s+(?:\[([^\]]+)\]|-\s*(\S+)\s*-)\s*(\w+)[:\s-]+\s*(.*)$")
_NOISE = (
    "checking pipeline state", "checking for replies", "checking for campaigns", "no new replies",
    "no draft campaigns", "no new prospects", "no active campaigns", "decision:", "running ", "crafting email",
    "harvey is online", "always be closing", "database initialized", "goodbye", "=====", "independently of the heartbeat",
    "idle cycle", "analyst:", "cycle complete. sleeping for", "email channel disabled",
    # (Only the routine sleep line is left out: an error in the heartbeat, a
    # timed-out cycle or an hour's wait on the AI limit stay in the feed.)
)
_AGENT_RE = re.compile(r"^(Sender|Writer|Handler|Scout|Analyst|Harvey|BB)\s*:\s*", re.I)


def classify_activity(text: str, level: str = "INFO") -> str:
    low = text.lower()
    if level in ("WARNING", "ERROR", "CRITICAL") or "bounce" in low or "failed" in low or "could not" in low:
        return "warning"
    if "reply from" in low or "replied to" in low or "reply to" in low or "answered" in low or "opted out" in low or "not interested" in low:
        return "reply"
    if "sent" in low or "deploying" in low or "activated" in low:
        return "sent"
    if "queued" in low or "wrote" in low or "found names" in low or "sorted" in low or "personali" in low:
        return "wrote"
    if "stopping" in low or "started over" in low or "paused" in low or "resum" in low or "stopped" in low or "woken up" in low:
        return "control"
    return "info"


def parse_activity(lines: list[str], limit: int = 30) -> list[dict]:
    today = time.strftime("%Y-%m-%d")
    out: list[dict] = []
    for raw in lines:
        m = _LOG_LINE.match(raw.strip())
        if not m:
            continue
        day, hhmm, name_a, name_b, level, text = m.groups()
        text = text.strip()
        low = text.lower()
        if not text or any(n in low for n in _NOISE):
            continue
        agent = ""
        am = _AGENT_RE.match(text)
        if am:
            agent, text = am.group(1).capitalize(), text[am.end():]
        elif (name_a or name_b or "").lower() == "bb":
            agent = "BB"
        text = text[:1].upper() + text[1:]
        when = hhmm if day == today else f"{day[5:]} {hhmm}"
        kind = classify_activity(text, level.upper())
        if out and out[-1]["text"] == text and out[-1]["agent"] == agent:
            out[-1]["count"] += 1
            out[-1]["when"] = when
            continue
        out.append({"when": when, "agent": agent, "kind": kind, "text": text, "count": 1, "level": level.upper()})
    out.reverse()
    return out[:limit]


_CODE_VERSION: dict = {}


def code_version() -> str:
    """The code the workers run: the git commit when there is one, else the
    newest change time of the source tree. Computed once per site process."""
    if "v" not in _CODE_VERSION:
        _CODE_VERSION["v"] = _compute_code_version()
    return _CODE_VERSION["v"]


def _compute_code_version() -> str:
    try:
        r = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    newest = 0.0
    for folder in ("harvey", "bb"):
        for path in (PROJECT_ROOT / folder).rglob("*.py"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
    return str(int(newest))


code_version()


def _process_gone(pid: int) -> bool:
    """True when the process has exited. A child that exited is a zombie
    until reaped, and os.kill(pid, 0) still sees it: reap it first."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except ChildProcessError:
        pass   # not our child: fall through to the signal probe
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


_PROCESS_SEEN: dict[int, tuple[bool, float]] = {}
_PROCESS_SEEN_SECONDS = 60.0


def _is_harvey_process(pid: int) -> bool:
    """True when the process with this pid is one of our workers (or we
    cannot tell). A reused pid after a reboot must not count as running.
    The answer is kept a minute per pid: on a Mac it costs a ps run, and
    the pages ask often (the Mail page's live feed every two seconds)."""
    seen = _PROCESS_SEEN.get(pid)
    if seen and time.monotonic() - seen[1] < _PROCESS_SEEN_SECONDS:
        return seen[0]
    answer = _ask_is_harvey_process(pid)
    if len(_PROCESS_SEEN) > 512:
        _PROCESS_SEEN.clear()
    _PROCESS_SEEN[pid] = (answer, time.monotonic())
    return answer


def _ask_is_harvey_process(pid: int) -> bool:
    try:
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists():
            cmd = proc.read_bytes().replace(b"\0", b" ").decode(errors="replace")
            return "harvey" in cmd or "bb.mail" in cmd
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
            return ("harvey" in out or "bb.mail" in out) if out.strip() else False
    except Exception:
        return True
    return True


def all_workspaces(settings, accounts) -> list[Workspace]:
    return [Workspace(settings, u) for u in accounts.list_users()]
