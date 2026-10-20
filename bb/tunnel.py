"""A public address for a site running on someone's own computer.

With BB_TUNNEL=cloudflare the site starts `cloudflared` itself, reads the
https://....trycloudflare.com address it is given, and uses it as the
site's public URL: the Users page shows the link to share, and the
terminal prints it. A quick tunnel gets a new address every time it
starts; with BB_TUNNEL_TOKEN (a named Cloudflare tunnel on your own
domain) the address is the one in BB_BASE_URL and never changes. If
cloudflared dies it is started again; if it is not installed the page
says so instead of the site failing to start."""

import asyncio
import logging
import re
import shutil
import time

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
RESTART_BACKOFF = (5, 10, 20, 40, 60)
INSTALL_HINT = ("Install cloudflared: on a Mac `brew install cloudflared`, elsewhere "
                "https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/")


class Tunnel:
    def __init__(self, mode: str, port: int, token: str = "", public_url: str = "", binary: str = "",
                 on_url=None):
        self.mode = mode            # "quick" | "named" | ""
        self.port = port
        self.token = token
        self.binary = binary or "cloudflared"
        self.on_url = on_url
        self.url = public_url if mode == "named" else ""
        self.status = "off" if not mode else "starting"
        self.detail = ""
        self.started_at = 0.0
        self._proc = None
        self._task = None
        self._stopping = False
        self._restarts = 0

    @property
    def enabled(self) -> bool:
        return bool(self.mode)

    def command(self) -> list[str]:
        if self.mode == "named":
            return [self.binary, "tunnel", "--no-autoupdate", "run", "--token", self.token]
        return [self.binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}"]

    async def start(self):
        if not self.enabled or self._task:
            return
        if not shutil.which(self.binary):
            self.status, self.detail = "missing", f"{self.binary} is not installed. {INSTALL_HINT}"
            logger.warning("tunnel: %s", self.detail)
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while not self._stopping:
            self.status, self.detail = "starting", ""
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.command(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            except (OSError, ValueError) as e:
                self.status, self.detail = "error", f"could not start {self.binary}: {e}"
                logger.warning("tunnel: %s", self.detail)
                return
            self.started_at = time.time()
            assert self._proc.stderr is not None
            try:
                while True:
                    line = await self._proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if self.mode == "named" and self.status != "up" and ("registered" in text.lower() and "connection" in text.lower()):
                        self.status, self.detail = "up", ""
                        self._restarts = 0
                        logger.info("tunnel: connected; public address %s", self.url)
                        print(f"Share this link with your users: {self.url}", flush=True)
                    m = URL_RE.search(text)
                    if m and self.mode == "quick" and m.group(0) != self.url:
                        self.url, self.status, self.detail = m.group(0), "up", ""
                        self._restarts = 0
                        logger.info("tunnel: public address %s", self.url)
                        print(f"Share this link with your users: {self.url}", flush=True)
                        if self.on_url:
                            try:
                                self.on_url(self.url)
                            except Exception as e:   # the callback must never kill the reader
                                logger.warning("tunnel: on_url failed: %s", e)
                    elif "error" in text.lower() and "failed" in text.lower():
                        self.detail = text[-200:]
            except asyncio.CancelledError:
                raise
            code = await self._proc.wait()
            if self._stopping:
                break
            self.status = "down"
            wait = RESTART_BACKOFF[min(self._restarts, len(RESTART_BACKOFF) - 1)]
            self._restarts += 1
            self.detail = f"cloudflared exited (code {code}); starting it again in {wait}s." + (f" Last message: {self.detail}" if self.detail else "")
            logger.warning("tunnel: %s", self.detail)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError, Exception):
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self.status = "off"

    def describe(self) -> dict:
        return {"mode": self.mode, "status": self.status, "url": self.url, "detail": self.detail,
                "changes": self.mode == "quick"}
