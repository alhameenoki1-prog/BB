"""The mail worker: one process per user, started by the site, running
the job queue. Jobs for different mailboxes run concurrently, and so do
the campaigns on one mailbox (each run has a lane of its own); two jobs
in the same lane never overlap. A heartbeat file tells the site it
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
MAX_CONCURRENT = 16
# Runs and reply campaigns can take long (paced sends); they never take the
# last few slots, so syncs, checks and a person's Send always get one.
LONG_KINDS = ("reply_run", "reply_campaign")
MAX_LONG = MAX_CONCURRENT - 4
SCHEDULE_EVERY = 60.0


class Handlers:
    """Job kinds → coroutines. Phases add sync, draft and send jobs here."""

    def __init__(self, db: MailDB, jobs: Jobs, secret: str, env: dict, root: Path | None = None):
        self.db, self.jobs, self.secret, self.env, self.root = db, jobs, secret, env, root

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

    async def send_draft(self, job: dict):
        from bb.mail.sending import SendRefused, send_draft
        draft = await self.db.one("SELECT * FROM drafts WHERE id = ?", (job["payload"].get("draft_id", ""),))
        if not draft:
            return {"skipped": "draft gone"}
        if draft["status"] not in ("approved",):
            return {"skipped": f"draft is {draft['status']}"}
        try:
            return await send_draft(self.db, self.secret, self.env, draft, dry_run=bool(job["payload"].get("dry_run")), pressed=True)
        except SendRefused as e:
            await self.db.execute("UPDATE drafts SET status = 'pending_review', error = ?, updated_at = ? WHERE id = ?",
                                  (f"Not sent: {e}", service.now_iso(), draft["id"]))
            await self.db.log("review", f"Held a reply to {draft['to_json'][:60]}: {e}", draft["mailbox_id"], draft_id=draft["id"])
            return {"status": "held", "reason": str(e)}

    async def _run_env(self, run: dict | None, mailbox: dict | None = None) -> tuple[dict, dict | None]:
        """The env and the profile (goal, brief, files) a run writes with on
        this account: the campaign the run was given, else the outreach
        campaign that sends from the account, else the user's main profile.
        Read fresh each time: the profile may have changed since the worker
        started, and a campaign may have been removed."""
        from bb import campaigns as campaigns_mod
        from bb.mail import scope as scope_mod
        env, root = self.env, self.root
        if self.root:
            pid = (run or {}).get("profile_id") or ""
            if not pid or (pid != campaigns_mod.MAIN and not campaigns_mod.get(self.root, pid)):
                pid = campaigns_mod.profile_for_account(self.root, (mailbox or {}).get("address") or "")
            root = campaigns_mod.profile_root(self.root, pid)
            env = {**self.env, "HARVEY_PROFILE_HOME": str(root)}
        return env, (scope_mod.load(root) if root else None)

    async def reply_run(self, job: dict):
        from bb.mail import runs
        from bb.mail.brain import MailBrain
        brain = MailBrain(os.environ)
        if not brain.configured:
            raise RuntimeError("no AI provider is configured for this account (AI provider page)")
        run = await self.db.one("SELECT * FROM reply_runs WHERE id = ?", (job["payload"].get("run_id", ""),))
        env, scope = await self._run_env(run, await self.db.mailbox(job["mailbox_id"]))
        return await runs.process_mailbox(self.db, self.secret, env, brain, self.jobs, job["id"],
                                          job["payload"].get("run_id", ""), job["mailbox_id"], scope=scope)

    async def reply_campaign(self, job: dict):
        """One tick of a reply campaign: take in what arrived, answer it,
        and come back in a couple of minutes. A failure here is noted on
        the campaign and tried again on the next tick; it never stops
        another campaign, and never stops this one."""
        from bb.mail import runs
        from bb.mail.brain import MailBrain
        run_id = job["payload"].get("run_id", "")
        run = await self.db.one("SELECT * FROM reply_runs WHERE id = ?", (run_id,))
        mailbox = await self.db.mailbox(job["mailbox_id"])
        if not run or not mailbox or run["status"] not in ("queued", "running"):
            return {"stopped": (run or {}).get("status", "gone")}
        out: dict = {}
        try:
            brain = MailBrain(os.environ)
            if not brain.configured:
                raise RuntimeError("no AI provider is configured for this account (AI provider page)")
            env, scope = await self._run_env(run, mailbox)
            led = runs.ledger_for(env)
            if led is not None and run["mode"] != "dry_run":
                led.join(mailbox["address"], run_id, "reply", runs.run_label(run, mailbox["address"]))
            added = await runs.collect_campaign_threads(self.db, run, mailbox)
            out = await runs.process_mailbox(self.db, self.secret, env, brain, self.jobs, job["id"], run_id, mailbox["id"], scope=scope)
            out["added"] = added
            if not out.get("waiting") and run.get("detail"):
                await self.db.execute("UPDATE reply_runs SET detail = '' WHERE id = ? AND detail NOT LIKE 'waiting%'", (run_id,))
        except Exception as e:
            logger.warning("reply campaign %s: %s", run_id, e)
            await self.db.execute("UPDATE reply_runs SET detail = ?, updated_at = ? WHERE id = ?",
                                  (f"last try failed, trying again shortly: {type(e).__name__}: {str(e)[:200]}", service.now_iso(), run_id))
            out = {"error": str(e)[:200]}
        fresh = await self.db.one("SELECT status FROM reply_runs WHERE id = ?", (run_id,))
        if fresh and fresh["status"] in ("queued", "running"):
            # More waiting than one batch: back in a moment, otherwise at the next round.
            delay = 5 if out.get("more") else runs.CAMPAIGN_TICK_SECONDS
            await self.jobs.enqueue("reply_campaign", mailbox["id"], {"run_id": run_id}, delay_seconds=delay,
                                    max_attempts=3, lane=runs.lane(mailbox["id"], run_id))
        return out

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
        self.handlers = Handlers(self.db, self.jobs, secret, env, root)
        self.heartbeat = root / "data" / "mailworker.heartbeat"
        self.stop = asyncio.Event()
        self.busy: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.long_running = 0

    async def _run_job(self, job: dict):
        key = job.get("lane") or job["mailbox_id"] or f"job:{job['id']}"
        self.busy.add(key)
        long = job["kind"] in LONG_KINDS
        self.long_running += 1 if long else 0
        try:
            result = await self.handlers.run(job)
            await self.jobs.complete(job["id"], **(result or {}))
        except asyncio.CancelledError:
            await asyncio.shield(self.jobs.release(job["id"], "worker stopped"))
            raise
        except Exception as e:
            status = await self.jobs.fail(job["id"], f"{type(e).__name__}: {str(e)[:300]}")
            logger.warning("job %s (%s) failed: %s → %s", job["id"], job["kind"], e, status)
            if status == "dead":
                await self.db.log("warning", f"A background task ({job['kind'].replace('_', ' ')}) failed "
                                             "repeatedly and is waiting on the Jobs page.", job["mailbox_id"])
                if job["kind"] in ("reply_run", "reply_campaign"):
                    from bb.mail import runs
                    await runs.job_died(self.db, job["payload"].get("run_id", ""), job["mailbox_id"], f"{type(e).__name__}: {e}")
        finally:
            self.busy.discard(key)
            self.long_running -= 1 if long else 0

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
                    from bb.mail import runs
                    await runs.reconcile(self.db, self.jobs)
                    await self.jobs.prune()
                except Exception as e:
                    logger.warning("scheduling failed: %s", e)
                last_schedule = time.time()
            started = 0
            while len(self.tasks) < MAX_CONCURRENT:
                job = await self.jobs.claim(exclude_mailboxes=self.busy,
                                            exclude_kinds=LONG_KINDS if self.long_running >= MAX_LONG else ())
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
    # The site's OAuth clients: Google, Microsoft sign-in (MS_*) and the admin app (M365_*).
    env = {k: v for k, v in os.environ.items() if k.startswith(("GMAIL_", "M365_", "MS_"))}
    env["HARVEY_HOME"] = str(root)       # the workspace: the profile's reply files live under it
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
