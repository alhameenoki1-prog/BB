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

TONES = ("professional, consultative, confident", "friendly and direct", "warm and curious",
         "formal", "casual")
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
             "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
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
SPEEDS = {
    "gentle": {"label": "Gentle", "note": "8 emails every 5 minutes (about 95 an hour)", "interval": 5, "per_cycle": 8},
    "normal": {"label": "Normal", "note": "10 emails every 2 minutes (about 300 an hour)", "interval": 2, "per_cycle": 10},
    "fast": {"label": "Fast", "note": "25 emails every minute (about 1,500 an hour; needs a mailbox that allows it)", "interval": 1, "per_cycle": 25},
}
GREETINGS = ("Hi", "Hello", "Hey", "Dear", "Good day")
STYLES = {
    "engaging": "Engaging: 90–130 words with a hook, one useful detail, an easy question and a PS",
    "concise": "Concise: under 75 words, one observation and one question",
}
MAIL_PROVIDERS = ("gmail", "smtp", "microsoft_app")
MAX_SIGNATURE_IMAGE = 1024 * 1024

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
    def __init__(self, settings, user: dict):
        self.settings = settings
        self.user = user
        self.root = settings.workspaces_dir / user["id"]
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
        self.secrets_file = self.data_dir / "secrets.json"
        self.mailbox_file = self.data_dir / "mailbox.json"
        self.attachments_dir = self.root / "attachments"
        self.signature_dir = self.root / "signature"

    def ensure(self):
        for d in (self.root, self.data_dir, self.skills_dir, self.attachments_dir, self.signature_dir):
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

    def llm(self) -> dict:
        s = self.secrets()
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
        self.save_secrets(
            llm_provider=provider, llm_api_key=api_key.strip(),
            llm_model=(model or preset["model"]).strip(),
            llm_base_url=(base_url or preset["base_url"]).strip().rstrip("/"),
            anthropic_api_key=api_key.strip() if provider == "anthropic" else "",
            anthropic_workspace_id=workspace_id.strip() if provider == "anthropic" else "",
        )
        self.rewrite_config()

    def clear_llm(self):
        self.save_secrets(llm_provider="", llm_api_key="", llm_model="", llm_base_url="",
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
        if provider in MAIL_PROVIDERS:
            return provider
        if self.m365_configured() and not self.gmail_connected():
            return "microsoft_app"
        return "smtp" if self.smtp_configured() and not self.gmail_connected() else "gmail"

    def set_mail_provider(self, provider: str):
        if provider not in MAIL_PROVIDERS:
            raise ValueError("Choose gmail, smtp or microsoft_app.")
        self.ensure()
        self.mailbox_file.write_text(json.dumps({"provider": provider}))

    def smtp_settings(self) -> dict:
        secrets = self.secrets()
        return {k: secrets.get(k, "") for k in SMTP_FIELDS}

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
        if provider == "microsoft_app":
            return self.m365_configured()
        return self.smtp_configured()

    def sending_address(self) -> str:
        provider = self.mail_provider()
        if provider == "gmail":
            return self.gmail_account().get("email", "")
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
            "quiet_start": quiet.get("start", "20:00"),
            "quiet_end": quiet.get("end", "08:00"),
            "timezone": quiet.get("timezone", "UTC"),
            "max_daily_cost_usd": float(usage.get("max_daily_cost_usd",
                                                  self.settings.default_daily_cost_usd)),
            "mode": outreach.get("mode", "inquiry"),
            "intent": outreach.get("intent", ""),
            "reply_instructions": outreach.get("reply_instructions", ""),
            "attach_on": outreach.get("attach_on", "first_reply"),
            "writing": "person" if outreach.get("personalize_each", False) else "segment",
            "style": outreach.get("style", "engaging"),
            "greeting": outreach.get("greeting", "Hi"),
            "signature_html": outreach.get("signature_html", ""),
            "speed": next((k for k, v in SPEEDS.items()
                           if v["interval"] == usage.get("heartbeat_interval_minutes")
                           and v["per_cycle"] == email.get("sends_per_cycle")), "normal"),
            "warmup_ramp": bool(email.get("warmup_ramp", True)),
        }

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
        speed = SPEEDS.get(text("speed") or "normal", SPEEDS["normal"])
        signature_html = str(form.get("signature_html", "") or "").strip()
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
                    "require_approval": flag("require_approval"),
                    "send_to_risky": False,
                    "max_bounce_rate": 0.05,
                    "sends_per_cycle": speed["per_cycle"],
                    # Checkbox: absent in the form means off; a brand-new form
                    # (no key at all) defaults to on.
                    "warmup_ramp": flag("warmup_ramp") if "warmup_ramp" in form or "speed" in form else True,
                    "list_unsubscribe": True,
                },
                "linkedin": {"enabled": False},
            },
            "usage": {
                # No spend cap by default: the user's provider bills (or doesn't) them directly.
                "max_daily_cost_usd": max(0.0, float(text("max_daily_cost_usd", "0") or 0)),
                "max_daily_claude_percent": 80,
                "heartbeat_interval_minutes": speed["interval"],
                "quiet_hours": {
                    "start": text("quiet_start") or "20:00",
                    "end": text("quiet_end") or "08:00",
                    "timezone": text("timezone") or "UTC",
                },
            },
            "brain": self.brain_section(),
            "outreach": {
                "mode": mode,
                "intent": text("intent"),
                "reply_instructions": str(form.get("reply_instructions", "") or "").strip(),
                "reply_attachments": [a["name"] for a in self.attachments()],
                "attach_on": text("attach_on") or "first_reply",
                # Web default: one email per sector × role segment, sent in bulk.
                "personalize_each": text("writing") == "person",
                "style": text("style") if text("style") in STYLES else "engaging",
                "greeting": text("greeting") if text("greeting") in GREETINGS else "Hi",
                "signature_html": signature_html,
                "signature_image": self.signature_image(),
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
        prof = self.profile()
        form = {**prof, "auto_prospect": "on" if prof["auto_prospect"] else "",
                "require_approval": "on" if prof["require_approval"] else ""}
        try:
            return self.write_profile(form)
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
            ("## What the sender wants from each recipient\n"
             + (outreach.get("intent") or "To work with them as a client or partner.")
             + "\nEach email asks for what THAT recipient's business does; nothing is being sold.")
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
            "worker_paused": self.worker_paused(),
            "configured": self.is_configured(), "gmail": self.mailbox_connected(),
            "mailbox": self.mail_provider(), "api_key": self.llm_configured(),
            "llm": self.llm()["label"],
            "attachments": len(self.attachments()),
        }
        if not await self._table_exists("prospects"):
            return out
        for row in await self._query("SELECT status, COUNT(*) AS n FROM prospects GROUP BY status"):
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
                key = {"pending_review": "outbox_pending", "approved": "outbox_scheduled",
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

    async def outbox(self, status: str = "pending_review", limit: int = 200) -> list[dict]:
        if not await self._table_exists("outbox"):
            return []
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
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def worker_env(self) -> dict:
        env = {**os.environ, **self.settings.worker_env(), "HARVEY_HOME": str(self.root),
               "PYTHONUNBUFFERED": "1"}
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
        return env

    def start_worker(self) -> tuple[bool, str]:
        if not self.llm_configured():
            return False, "Set up your AI provider first."
        if not self.is_configured():
            return False, "Fill in your profile first."
        if not self.mailbox_connected():
            return False, "Connect your mailbox first."
        if pid := self.worker_running():
            return True, f"Already running (pid {pid})."
        self.ensure()
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
        return True, f"Started (pid {proc.pid})."

    async def stop_worker(self, reason: str = "Stop pressed on the dashboard") -> tuple[bool, str]:
        self.autostart_file.unlink(missing_ok=True)
        pid = self.worker_running()
        if not pid:
            self.pid_file.unlink(missing_ok=True)
            return True, "Not running."
        self._note_in_log(f"BB is stopping the worker: {reason}.")
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
        self.pid_file.unlink(missing_ok=True)
        return True, "Stopped."

    def wants_autostart(self) -> bool:
        return self.autostart_file.exists()

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

    def log_tail(self, lines: int = 60) -> list[str]:
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        return text.strip().splitlines()[-lines:]


def all_workspaces(settings, accounts) -> list[Workspace]:
    return [Workspace(settings, u) for u in accounts.list_users()]
