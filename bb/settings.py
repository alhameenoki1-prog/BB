"""Server-wide settings for the hosted web app, read from the environment.

Everything a user needs is derived per workspace; these are the few things
the operator of the site sets once: where state lives, the public URL (for
cookies and the Google OAuth redirect), the admin bootstrap account, and the
credentials every worker is started with.
"""

import os
import secrets
from pathlib import Path

from harvey.paths import PROJECT_ROOT

# Environment variables handed to every per-user worker process. Users never
# see or set these; the site operator does, once, on the server.
WORKER_ENV_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
    "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
    "SERPER_API_KEY", "PRECLEARED_API_KEY",
)


class Settings:
    def __init__(self, env: dict | None = None):
        self.env = dict(os.environ if env is None else env)
        get = lambda k, d="": (self.env.get(k) or d)  # noqa: E731
        self.home = Path(get("BB_HOME") or PROJECT_ROOT / "data" / "bb").expanduser().resolve()
        self.base_url = get("BB_BASE_URL", "http://localhost:8000").rstrip("/")
        self.secure_cookies = self.base_url.startswith("https://")
        self.admin_email = get("BB_ADMIN_EMAIL").strip().lower()
        self.admin_password = get("BB_ADMIN_PASSWORD")
        self.gmail_client_id = get("GMAIL_CLIENT_ID").strip()
        self.gmail_client_secret = get("GMAIL_CLIENT_SECRET").strip()
        self.anthropic_api_key = get("ANTHROPIC_API_KEY").strip()
        self.default_model = get("BB_DEFAULT_MODEL").strip()
        self.default_daily_cost_usd = float(get("BB_DEFAULT_DAILY_COST_USD", "5") or 5)
        self.session_days = int(get("BB_SESSION_DAYS", "14") or 14)
        self._secret = get("BB_SECRET_KEY")

    @property
    def accounts_db(self) -> Path:
        return self.home / "bb.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.home / "workspaces"

    def secret_key(self) -> str:
        """BB_SECRET_KEY, or a key generated once and kept in BB_HOME so
        sessions survive restarts without the operator setting anything."""
        if self._secret:
            return self._secret
        path = self.home / "secret.key"
        try:
            existing = path.read_text().strip()
        except FileNotFoundError:
            existing = ""
        if not existing:
            self.home.mkdir(parents=True, exist_ok=True)
            existing = secrets.token_urlsafe(48)
            path.write_text(existing)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        self._secret = existing
        return existing

    def worker_env(self) -> dict:
        return {k: self.env[k] for k in WORKER_ENV_KEYS if self.env.get(k)}

    def problems(self) -> list[str]:
        """Server configuration gaps worth showing the admin."""
        out = []
        if not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY is not set: workers cannot write emails.")
        if not (self.gmail_client_id and self.gmail_client_secret):
            out.append("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET are not set: users cannot connect Gmail.")
        if not self.secure_cookies and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            out.append("BB_BASE_URL is not https: session cookies travel in clear text.")
        return out
