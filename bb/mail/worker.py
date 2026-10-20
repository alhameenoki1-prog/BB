"""The mail worker: one process per user, started by the site, running
the job queue. Jobs for different mailboxes run concurrently; two jobs
for the same mailbox never overlap. A heartbeat file tells the site it
is alive; the log is plain text at data/mailworker.log.

    python -m bb.mail.worker /path/to/workspace
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from bb.mail import service
from bb.mail.db import MailDB
from bb.mail.jobs import Jobs

logger = logging.getLogger("bb.mailworker")

POLL_SECONDS = 2.0
MAX_CONCURRENT = 6
SCHEDULE_EVERY = 60.0


class Handlers:
    """Job kinds → coroutines. Phases add sync, draft and send jobs here."""

    def __init__(self, db: MailDB, jobs: Jobs, secret: str, env: dict):
        self.db, self.jobs, self.secret, self.env = db, jobs, secret, env

    async def check_mailbox(self, job: dict):
        mailbox = await self.db.mailbox(job["mailbox_id"])
        if not mailbox or not mailbox.get("enabled", 1):
            return {"skipped": "mailbox gone"}
        status = await service.check_mailbox(self.db, self.secret, mailbox, self.env)
        return {"status": status}

    async def sync_backfill(self, job: dict):
        mailbox = await self.db.mailbox(job["mailbox_id"])
        if not mailbox or not mailbox.get("enabled", 1):
            return {"skipped": "mailbox gone"}
        return await service.run_sync(self.db, self.secret, mailbox, self.env, self.jobs, job["id"], backfill=True)

    async def sync_incremental(self, job: dict):
        mailbox = await self.db.mailbox(job["mailbox_id"])
        if not mailbox or not mailbox.get("enabled", 1):
            return {"skipped": "mailbox gone"}
        return await service.run_sync(self.db, self.secret, mailbox, self.env, self.jobs, job["id"], backfill=False)

    async def run(self, job: dict) -> dict:
        handler = getattr(self, job["kind"], None)
        if handler is None:
            raise RuntimeError(f"no handler for job kind {job['kind']!r}")
        return await handler(job) or {}


class Worker:
    def __init__(self, root: Path, secret: str, env: dict):
        self.root = root
        self.db = MailDB(root / "data" / "mail.db")
        self.jobs = Jobs(self.db)
        self.handlers = Handlers(self.db, self.jobs, secret, env)
        self.heartbeat = root / "data" / "mailworker.heartbeat"
        self.stop = asyncio.Event()
        self.busy: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    async def _run_job(self, job: dict):
        key = job["mailbox_id"] or f"job:{job['id']}"
        self.busy.add(key)
        try:
            result = await self.handlers.run(job)
            await self.jobs.complete(job["id"], **(result or {}))
        except asyncio.CancelledError:
            await self.jobs.fail(job["id"], "worker stopped")
            raise
        except Exception as e:
            status = await self.jobs.fail(job["id"], f"{type(e).__name__}: {str(e)[:300]}")
            logger.warning("job %s (%s) failed: %s → %s", job["id"], job["kind"], e, status)
            if status == "dead":
                await self.db.log("warning", f"A background task ({job['kind'].replace('_', ' ')}) failed "
                                             "repeatedly and is waiting on the Jobs page.", job["mailbox_id"])
        finally:
            self.busy.discard(key)

    async def loop(self):
        await self.db.init()
        await self.db.log("info", "Background worker started.")
        last_schedule = 0.0
        while not self.stop.is_set():
            try:
                self.heartbeat.write_text(str(int(time.time())))
            except OSError:
                pass
            if time.time() - last_schedule > SCHEDULE_EVERY:
                try:
                    await service.schedule_checks(self.db, self.jobs)
                    await service.schedule_syncs(self.db, self.jobs)
                    await self.jobs.prune()
                except Exception as e:
                    logger.warning("scheduling failed: %s", e)
                last_schedule = time.time()
            started = 0
            while len(self.tasks) < MAX_CONCURRENT:
                job = await self.jobs.claim(exclude_mailboxes=self.busy)
                if not job:
                    break
                task = asyncio.create_task(self._run_job(job))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                started += 1
            if not started:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        if self.tasks:
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.db.log("info", "Background worker stopped.")


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: python -m bb.mail.worker <workspace root>")
        return 2
    root = Path(argv[0])
    log_file = root / "data" / "mailworker.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)])
    secret = os.environ.get("BB_SECRET_KEY", "")
    if not secret:
        print("BB_SECRET_KEY is required")
        return 2
    env = {k: v for k, v in os.environ.items() if k.startswith(("GMAIL_", "M365_"))}
    worker = Worker(root, secret, env)

    async def run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, worker.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await worker.loop()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
