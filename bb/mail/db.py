"""One SQLite file per user for the mail command center (``data/mail.db``),
in WAL mode so the web app reads while the worker writes.

Every message is stored once (provider ids are unique per mailbox), every
thread carries a persisted reply state, and jobs, drafts, runs and the
activity feed are tables, not memory: a closed tab or a restarted
process loses nothing.
"""

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 1

MIGRATIONS = [
    # v1 — the foundation
    """
    CREATE TABLE IF NOT EXISTS mailboxes (
        id TEXT PRIMARY KEY,
        address TEXT NOT NULL,
        provider TEXT NOT NULL,            -- gmail | imap | graph
        mode TEXT NOT NULL DEFAULT 'full', -- full | limited (app-password IMAP)
        creds TEXT NOT NULL DEFAULT '',    -- sealed JSON
        status TEXT NOT NULL DEFAULT 'new',-- new | ok | reconnect | error | disabled
        status_detail TEXT NOT NULL DEFAULT '',
        settings_json TEXT NOT NULL DEFAULT '{}',
        sync_json TEXT NOT NULL DEFAULT '{}',
        counts_json TEXT NOT NULL DEFAULT '{}',
        last_sync_at TEXT,
        last_checked_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS mailboxes_address ON mailboxes(lower(address));

    CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY,
        mailbox_id TEXT NOT NULL,
        provider_thread_id TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'unknown', -- needs_reply | replied | awaiting_them | no_reply_needed | snoozed | dismissed | unknown
        state_reason TEXT NOT NULL DEFAULT '',
        last_message_at TEXT,
        last_direction TEXT NOT NULL DEFAULT '',
        last_from TEXT NOT NULL DEFAULT '',
        message_count INTEGER NOT NULL DEFAULT 0,
        snippet TEXT NOT NULL DEFAULT '',
        participants_json TEXT NOT NULL DEFAULT '[]',
        snoozed_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS threads_provider ON threads(mailbox_id, provider_thread_id);
    CREATE INDEX IF NOT EXISTS threads_state ON threads(mailbox_id, state, last_message_at);

    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        mailbox_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        message_id TEXT NOT NULL DEFAULT '',
        in_reply_to TEXT NOT NULL DEFAULT '',
        references_text TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL,           -- inbound | outbound
        sent_by TEXT NOT NULL DEFAULT 'them', -- them | me | ai
        from_email TEXT NOT NULL DEFAULT '',
        from_name TEXT NOT NULL DEFAULT '',
        to_json TEXT NOT NULL DEFAULT '[]',
        cc_json TEXT NOT NULL DEFAULT '[]',
        subject TEXT NOT NULL DEFAULT '',
        snippet TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        headers_json TEXT NOT NULL DEFAULT '{}',
        folder TEXT NOT NULL DEFAULT '',
        sent_at TEXT,
        automated INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS messages_provider ON messages(mailbox_id, provider_id);
    CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, sent_at);
    CREATE INDEX IF NOT EXISTS messages_message_id ON messages(message_id);

    CREATE TABLE IF NOT EXISTS participants (
        thread_id TEXT NOT NULL,
        email TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT '',     -- me | them
        PRIMARY KEY (thread_id, email)
    );

    CREATE TABLE IF NOT EXISTS reply_runs (
        id TEXT PRIMARY KEY,
        mailbox_ids_json TEXT NOT NULL DEFAULT '[]',
        mode TEXT NOT NULL DEFAULT 'review',   -- review | auto | dry_run
        reply_to TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | cancelled | failed
        total INTEGER NOT NULL DEFAULT 0,
        done INTEGER NOT NULL DEFAULT 0,
        summary_json TEXT NOT NULL DEFAULT '{}',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS run_items (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', -- queued | drafting | needs_review | sent | skipped | failed
        draft_id TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS run_items_run ON run_items(run_id, status);

    CREATE TABLE IF NOT EXISTS drafts (
        id TEXT PRIMARY KEY,
        mailbox_id TEXT NOT NULL,
        thread_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'reply',  -- reply | new
        to_json TEXT NOT NULL DEFAULT '[]',
        reply_to TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        in_reply_to TEXT NOT NULL DEFAULT '',
        references_text TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | approved | sent | rejected | failed | dry_run
        qa_json TEXT NOT NULL DEFAULT '{}',
        confidence TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        sent_message_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sent_at TEXT
    );
    CREATE INDEX IF NOT EXISTS drafts_status ON drafts(mailbox_id, status);

    CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mailbox_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,                 -- replied | synced | review | warning | connected | info
        text TEXT NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}',
        at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS activity_at ON activity(at);

    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        mailbox_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued', -- queued | running | done | dead | cancelled
        run_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        last_error TEXT NOT NULL DEFAULT '',
        locked_at TEXT,
        progress_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, run_at);
    CREATE INDEX IF NOT EXISTS jobs_mailbox ON jobs(mailbox_id, status);

    CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
        mailbox_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,                 -- exclude_sender | exclude_domain | no_reply_needed
        value TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    );
    """,
    # v2 — reply runs carry their instructions
    """
    ALTER TABLE reply_runs ADD COLUMN instructions TEXT NOT NULL DEFAULT '';
    """,
    # v3 — reconnect runs (follow up on past conversations): the run's kind
    # and window, and the contact each item is for (one per contact per scan)
    """
    ALTER TABLE reply_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'reply';
    ALTER TABLE reply_runs ADD COLUMN months INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE run_items ADD COLUMN contact TEXT NOT NULL DEFAULT '';
    """
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return secrets.token_hex(8)


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


class MailDB:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return aiosqlite.connect(str(self.path), timeout=15)

    async def init(self):
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=15000")
            async with db.execute("PRAGMA user_version") as cur:
                version = (await cur.fetchone())[0]
            for i, script in enumerate(MIGRATIONS, start=1):
                if version < i:
                    await db.executescript(script)
                    await db.execute(f"PRAGMA user_version={i}")
            await db.commit()

    async def journal_mode(self) -> str:
        async with self._connect() as db:
            async with db.execute("PRAGMA journal_mode") as cur:
                return (await cur.fetchone())[0]

    async def query(self, sql: str, params=()) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def one(self, sql: str, params=()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params=()) -> int:
        async with self._connect() as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount

    # ── settings (scope, migration marks) ──

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str):
        await self.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ── mailboxes ──

    @staticmethod
    def _mailbox(row: dict | None) -> dict | None:
        if not row:
            return None
        row = dict(row)
        row["settings"] = _loads(row.pop("settings_json", "{}"), {})
        row["sync"] = _loads(row.pop("sync_json", "{}"), {})
        row["counts"] = _loads(row.pop("counts_json", "{}"), {})
        return row

    async def add_mailbox(self, *, address: str, provider: str, creds: str, mode: str = "full",
                          settings: dict | None = None, status: str = "new") -> str:
        mid = new_id()
        now = now_iso()
        await self.execute(
            "INSERT INTO mailboxes(id, address, provider, mode, creds, status, settings_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, address.strip().lower(), provider, mode, creds, status,
             json.dumps(settings or {}), now, now))
        return mid

    async def mailboxes(self, include_disabled: bool = True) -> list[dict]:
        rows = await self.query("SELECT * FROM mailboxes ORDER BY created_at")
        out = [self._mailbox(r) for r in rows]
        return out if include_disabled else [m for m in out if m["enabled"]]

    async def mailbox(self, mailbox_id: str) -> dict | None:
        return self._mailbox(await self.one("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,)))

    async def mailbox_by_address(self, address: str) -> dict | None:
        return self._mailbox(await self.one("SELECT * FROM mailboxes WHERE lower(address) = ?",
                                            ((address or "").strip().lower(),)))

    async def update_mailbox(self, mailbox_id: str, **fields):
        allowed = {"address", "provider", "mode", "creds", "status", "status_detail", "last_sync_at",
                   "last_checked_at", "enabled"}
        sets, params = [], []
        for key, value in fields.items():
            if key == "settings":
                sets.append("settings_json = ?"); params.append(json.dumps(value))
            elif key == "sync":
                sets.append("sync_json = ?"); params.append(json.dumps(value))
            elif key == "counts":
                sets.append("counts_json = ?"); params.append(json.dumps(value))
            elif key in allowed:
                sets.append(f"{key} = ?"); params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?"); params.append(now_iso())
        params.append(mailbox_id)
        await self.execute(f"UPDATE mailboxes SET {', '.join(sets)} WHERE id = ?", params)

    async def remove_mailbox(self, mailbox_id: str):
        async with self._connect() as db:
            for table in ("messages", "threads", "drafts", "run_items", "jobs", "rules"):
                await db.execute(f"DELETE FROM {table} WHERE mailbox_id = ?", (mailbox_id,))
            await db.execute("DELETE FROM mailboxes WHERE id = ?", (mailbox_id,))
            await db.commit()

    # ── activity feed: plain sentences only ──

    async def log(self, kind: str, text: str, mailbox_id: str = "", **meta) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT INTO activity(mailbox_id, kind, text, meta_json, at) VALUES (?, ?, ?, ?, ?)",
                (mailbox_id, kind, text, json.dumps(meta), now_iso()))
            await db.commit()
            return cur.lastrowid

    async def activity(self, limit: int = 50, after_id: int = 0, mailbox_ids: list[str] | None = None) -> list[dict]:
        where, params = ["id > ?"], [after_id]
        if mailbox_ids is not None:
            marks = ",".join("?" for _ in mailbox_ids) or "''"
            where.append(f"(mailbox_id = '' OR mailbox_id IN ({marks}))")
            params += list(mailbox_ids)
        rows = await self.query(
            f"SELECT * FROM activity WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*params, limit))
        for r in rows:
            r["meta"] = _loads(r.pop("meta_json", "{}"), {})
        return rows
