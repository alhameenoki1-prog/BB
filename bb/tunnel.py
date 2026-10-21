"""A public address for a site running on someone's own computer.

With BB_TUNNEL=cloudflare the site runs `cloudflared` itself, reads the
https://....trycloudflare.com address it is given, and uses it as the
site's public URL: the Users page shows the link to share, and the
terminal prints it. With BB_TUNNEL_TOKEN (a named Cloudflare tunnel on
your own domain) the address is the one in BB_BASE_URL and never changes.

A quick tunnel's address lives exactly as long as its cloudflared process,
so that process is kept out of the site's own life: it is started detached
(its own session, output to a log file) and recorded in BB_HOME/tunnel.json,
and a site that starts later adopts it instead of starting a new one. An
update or a restart of the site therefore keeps the link; only a reboot,
a crash of cloudflared, or `python -m bb tunnel restart` changes it. If
cloudflared dies it is started again; if it is not installed the page says
so instead of the site failing to start."""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
RESTART_BACKOFF = (5, 10, 20, 40, 60)
INSTALL_HINT = ("Install cloudflared: on a Mac `brew install cloudflared`, elsewhere "
                "https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/")
STATE_FILE = "tunnel.json"
LOG_FILE = "tunnel.log"
POLL_SECONDS = 0.5


def _alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_of(pid: int) -> str:
    """The process's command line, from /proc where it exists, else ps;
    "" when it cannot be told (then the pid alone is trusted)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


class Tunnel:
    def __init__(self, mode: str, port: int, token: str = "", public_url: str = "", binary: str = "",
                 on_url=None, state_dir=None):
        self.mode = mode            # "quick" | "named" | ""
        self.port = port
        self.token = token
        self.binary = binary or "cloudflared"
        self.on_url = on_url
        self.url = public_url if mode == "named" else ""
        self.status = "off" if not mode else "starting"
        self.detail = ""
        self.started_at = 0.0
        self.pid = 0
        self.adopted = False
        self.state_dir = Path(state_dir) if state_dir else None
        self._proc = None
        self._task = None
        self._stopping = False
        self._restarts = 0
        self._log_pos = 0
        self._tmp_log = None

    @property
    def enabled(self) -> bool:
        return bool(self.mode)

    @property
    def state_file(self) -> Path | None:
        return self.state_dir / STATE_FILE if self.state_dir else None

    @property
    def log_file(self) -> Path:
        if self.state_dir:
            return self.state_dir / LOG_FILE
        if self._tmp_log is None:
            self._tmp_log = Path(tempfile.mkstemp(prefix="bb-tunnel-", suffix=".log")[1])
        return self._tmp_log

    def command(self) -> list[str]:
        if self.mode == "named":
            return [self.binary, "tunnel", "--no-autoupdate", "run", "--token", self.token]
        return [self.binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}"]

    # ── the recorded process ──

    def read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text()) if self.state_file and self.state_file.exists() else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"mode": self.mode, "port": self.port, "pid": self.pid,
                                                   "url": self.url, "started_at": self.started_at}))
        except OSError as e:
            logger.debug("tunnel: state not saved: %s", e)

    def _clear_state(self) -> None:
        try:
            if self.state_file and self.state_file.exists():
                self.state_file.unlink()
        except OSError:
            pass

    def recorded_process(self) -> dict:
        """{pid, url, alive, started_at} for the cloudflared this site last
        started, from the state file; alive only if it is still cloudflared."""
        st = self.read_state()
        pid = int(st.get("pid") or 0)
        cmd = _command_of(pid) if _alive(pid) else ""
        alive = _alive(pid) and (not cmd or "cloudflared" in cmd.lower() or Path(self.binary).name in cmd)
        return {"pid": pid, "url": st.get("url", ""), "alive": alive, "started_at": st.get("started_at", 0),
                "mode": st.get("mode", ""), "port": st.get("port")}

    def kill_recorded(self) -> bool:
        """Stop the cloudflared this site started (by hand: `python -m bb
        tunnel stop|restart`). A running site starts a fresh one."""
        rec = self.recorded_process()
        if not rec["alive"]:
            self._clear_state()
            return False
        pid = rec["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _alive(pid):
                    break
                time.sleep(0.1)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._clear_state()
        return True

    # ── running ──

    async def start(self):
        if not self.enabled or self._task:
            return
        if not shutil.which(self.binary):
            self.status, self.detail = "missing", f"{self.binary} is not installed. {INSTALL_HINT}"
            logger.warning("tunnel: %s", self.detail)
            return
        self._task = asyncio.create_task(self._run())

    def _adopt(self) -> bool:
        """Take over the cloudflared a previous site process left running:
        same mode and port, still alive, and (quick mode) with a known
        address. The link stays the one people already have."""
        rec = self.recorded_process()
        if not rec["alive"] or rec["mode"] != self.mode or rec["port"] != self.port:
            return False
        if self.mode == "quick" and not rec["url"]:
            # Alive but its address was never recorded: nobody can use it.
            self.pid = rec["pid"]
            self.kill_recorded()
            return False
        self.pid, self.adopted, self._proc = rec["pid"], True, None
        self.started_at = float(rec.get("started_at") or time.time())
        if self.mode == "quick":
            self.url = rec["url"]
        self.status, self.detail = "up", "kept running across the restart"
        self._restarts = 0
        try:
            self._log_pos = self.log_file.stat().st_size
        except OSError:
            self._log_pos = 0
        logger.info("tunnel: adopted cloudflared pid %s; public address %s", self.pid, self.url)
        print(f"Share this link with your users: {self.url}", flush=True)
        self._notify()
        return True

    def _spawn(self) -> bool:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._log_pos = self.log_file.stat().st_size if self.log_file.exists() else 0
            with open(self.log_file, "ab") as log:
                # Its own session: a stop or restart of the site (Ctrl+C, launchd,
                # systemd with KillMode=process) does not take the tunnel with it.
                self._proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                              start_new_session=True)
        except (OSError, ValueError) as e:
            self.status, self.detail = "error", f"could not start {self.binary}: {e}"
            logger.warning("tunnel: %s", self.detail)
            return False
        self.pid, self.adopted = self._proc.pid, False
        self.started_at = time.time()
        if self.mode == "quick":
            self.url = ""
        self.status, self.detail = "starting", ""
        self._write_state()
        return True

    def _notify(self) -> None:
        if self.on_url and self.url:
            try:
                self.on_url(self.url)
            except Exception as e:   # the callback must never kill the watcher
                logger.warning("tunnel: on_url failed: %s", e)

    def _read_new_lines(self) -> list[str]:
        try:
            with open(self.log_file, "rb") as f:
                f.seek(self._log_pos)
                data = f.read()
                self._log_pos = f.tell()
        except OSError:
            return []
        return [ln.strip() for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]

    def _process_alive(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        return _alive(self.pid)

    async def _follow(self):
        """Tail the log and watch the process until it exits (or we stop)."""
        while not self._stopping:
            for text in self._read_new_lines():
                low = text.lower()
                if self.mode == "named" and self.status != "up" and "registered" in low and "connection" in low:
                    self.status, self.detail = "up", ""
                    self._restarts = 0
                    logger.info("tunnel: connected; public address %s", self.url)
                    print(f"Share this link with your users: {self.url}", flush=True)
                m = URL_RE.search(text)
                if m and self.mode == "quick" and m.group(0) != self.url:
                    self.url, self.status, self.detail = m.group(0), "up", ""
                    self._restarts = 0
                    self._write_state()
                    logger.info("tunnel: public address %s", self.url)
                    print(f"Share this link with your users: {self.url}", flush=True)
                    self._notify()
                elif "error" in low and "failed" in low:
                    self.detail = text[-200:]
            if not self._process_alive():
                return self._proc.returncode if self._proc is not None else None
            await asyncio.sleep(POLL_SECONDS)
        return None

    async def _run(self):
        while not self._stopping:
            if not self._adopt() and not self._spawn():
                return
            code = await self._follow()
            if self._stopping:
                break
            self.status = "down"
            self._clear_state()
            wait = RESTART_BACKOFF[min(self._restarts, len(RESTART_BACKOFF) - 1)]
            self._restarts += 1
            self.detail = (f"cloudflared exited (code {code}); starting it again in {wait}s."
                           + (f" Last message: {self.detail}" if self.detail else ""))
            logger.warning("tunnel: %s", self.detail)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise

    async def stop(self, kill: bool = False):
        """Stop watching. The tunnel process is left running on purpose, so
        the link survives a restart or an update; ``kill`` ends it too."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if kill and self.pid and _alive(self.pid):
            try:
                os.kill(self.pid, signal.SIGTERM)
                for _ in range(50):
                    if not _alive(self.pid):
                        break
                    await asyncio.sleep(0.1)
                if _alive(self.pid):
                    os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if self._proc is not None:
                try:
                    self._proc.wait(timeout=1)
                except Exception:
                    pass
            self._clear_state()
        self.status = "off"

    def describe(self) -> dict:
        return {"mode": self.mode, "status": self.status, "url": self.url, "detail": self.detail,
                "changes": self.mode == "quick", "pid": self.pid, "adopted": self.adopted,
                "started_at": self.started_at}
