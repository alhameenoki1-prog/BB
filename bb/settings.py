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
# see or set these; the site operator does, once, on the server. The
# Anthropic key is NOT among them: each user supplies their own (stored in
# their workspace), so model usage is billed to them, never to the site.
WORKER_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
    "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT",
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
        # The site's Microsoft app registration (Entra): one-click Outlook,
        # Hotmail and Microsoft 365 sign-in. MS_TENANT "common" takes both
        # personal and work accounts; a tenant id restricts it to one company.
        self.ms_client_id = get("MS_CLIENT_ID").strip()
        self.ms_client_secret = get("MS_CLIENT_SECRET").strip()
        self.ms_tenant = get("MS_TENANT", "common").strip() or "common"
        self.default_model = get("BB_DEFAULT_MODEL").strip()
        self.default_daily_cost_usd = float(get("BB_DEFAULT_DAILY_COST_USD", "5") or 5)
        self.session_days = int(get("BB_SESSION_DAYS", "14") or 14)
        self._secret = get("BB_SECRET_KEY")
        # A public address from this computer: BB_TUNNEL=cloudflare starts a
        # quick cloudflared tunnel; BB_TUNNEL_TOKEN runs a named one whose
        # address is BB_BASE_URL.
        self.tunnel_token = get("BB_TUNNEL_TOKEN").strip()
        raw = get("BB_TUNNEL").strip().lower()
        self.tunnel_mode = "named" if self.tunnel_token else ("quick" if raw in ("cloudflare", "cloudflared", "quick", "on", "1", "true", "yes") else "")
        self.cloudflared = get("BB_CLOUDFLARED").strip() or "cloudflared"
        self.port = int(get("BB_PORT", "8000") or 8000)
        # With a tunnel the only client is cloudflared on this computer, so
        # the plain-http port stays off the network unless BB_HOST says so.
        self.host = get("BB_HOST").strip() or ("127.0.0.1" if self.tunnel_mode else "0.0.0.0")
        # Who may set X-Forwarded-For (the proxy in front): the login brake
        # keys on the client address, so a forged header must not move it.
        self.trusted_proxies = get("BB_TRUSTED_PROXIES", "127.0.0.1").strip()

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
        if not (self.gmail_client_id and self.gmail_client_secret):
            out.append("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET are not set: users cannot connect "
                       "Gmail with one click (they can still use their mailbox's SMTP settings).")
        if not self.secure_cookies and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            out.append("BB_BASE_URL is not https: session cookies travel in clear text.")
        if not self.tunnel_mode and ("localhost" in self.base_url or "127.0.0.1" in self.base_url):
            out.append("Only this computer can reach the site. For other people, add BB_TUNNEL=cloudflare to bb.env "
                       "(and install cloudflared); the link to share then appears on this page.")
        return out

    def set_public_url(self, url: str):
        """The tunnel told us the public address: cookies and links follow it."""
        self.base_url = url.rstrip("/")
        self.secure_cookies = self.base_url.startswith("https://")
