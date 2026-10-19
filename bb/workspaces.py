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
        self.gmail_token_file = self.data_dir / "gmail_token.json"
        self.gmail_account_file = self.data_dir / "gmail_account.json"

    def ensure(self):
        for d in (self.root, self.data_dir, self.skills_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

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
        return {
            "name": persona.get("name", ""),
            "company": persona.get("company", ""),
            "role": persona.get("role", ""),
            "email": persona.get("email", "") or self.gmail_account().get("email", ""),
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
            "max_daily_sends": int(email.get("max_daily_sends", 30)),
            "require_approval": bool(email.get("require_approval", False)),
            "quiet_start": quiet.get("start", "20:00"),
            "quiet_end": quiet.get("end", "08:00"),
            "timezone": quiet.get("timezone", "UTC"),
            "max_daily_cost_usd": float(usage.get("max_daily_cost_usd",
                                                  self.settings.default_daily_cost_usd)),
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
        gmail_email = self.gmail_account().get("email", "")
        persona_email = text("email") or gmail_email
        benefits = _split_lines(form.get("benefits", ""))
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
                    "provider": "gmail",
                    "max_daily_sends": max(0, int(text("max_daily_sends", "30") or 30)),
                    "require_approval": flag("require_approval"),
                    "send_to_risky": False,
                    "max_bounce_rate": 0.05,
                },
                "linkedin": {"enabled": False},
            },
            "usage": {
                "max_daily_cost_usd": max(0.0, float(text("max_daily_cost_usd", "5") or 0)),
                "max_daily_claude_percent": 80,
                "heartbeat_interval_minutes": 15,
                "quiet_hours": {
                    "start": text("quiet_start") or "20:00",
                    "end": text("quiet_end") or "08:00",
                    "timezone": text("timezone") or "UTC",
                },
            },
            "brain": {"provider": "claude", **({"model": self.settings.default_model}
                                               if self.settings.default_model else {})},
        }
        HarveyConfig(**cfg)  # raises pydantic.ValidationError with the field
        self.ensure()
        self.config_file.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
        self._write_product_knowledge(cfg)
        return cfg

    def _write_product_knowledge(self, cfg: dict):
        """What the writer and handler know about the sender. The trainer
        produces a richer version from a website; this one is built from
        the form so a user is never writing with an empty skill."""
        persona, product = cfg["persona"], cfg["product"]
        offer = product["offer"]
        lines = [
            f"# {product['name']} — what {persona['name']} is reaching out about",
            "",
            f"**Sender:** {persona['name']}, {persona['role'] or 'contact'} at {persona['company']}.",
            f"**LinkedIn:** {persona['linkedin']}" if persona.get("linkedin") else "",
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
            "configured": self.is_configured(), "gmail": self.gmail_connected(),
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
            if key.startswith("BB_"):
                env.pop(key)
        return env

    def start_worker(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "Fill in your profile first."
        if not self.gmail_connected():
            return False, "Connect your Gmail first."
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

    async def stop_worker(self) -> tuple[bool, str]:
        self.autostart_file.unlink(missing_ok=True)
        pid = self.worker_running()
        if not pid:
            self.pid_file.unlink(missing_ok=True)
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
        self.pid_file.unlink(missing_ok=True)
        return True, "Stopped."

    def wants_autostart(self) -> bool:
        return self.autostart_file.exists()

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
