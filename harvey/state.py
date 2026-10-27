"""SQLite state manager. All of Harvey's memory lives here.

Concurrency: the DB runs in WAL mode (set persistently at init) so the
dashboard can read while the agent writes. Every connection gets a busy
timeout so concurrent writers wait instead of raising "database is locked".

Migrations: schema changes are applied via a linear, idempotent migration
list tracked with SQLite's ``PRAGMA user_version`` so existing user DBs
upgrade cleanly in place.
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import aiosqlite

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep  # noqa: F401 (EmailStep re-exported)
from harvey.models.conversation import Conversation, Message  # noqa: F401

from harvey.paths import DATA_DIR

DB_PATH = DATA_DIR / "harvey.db"

# How long (seconds) a connection waits on a locked database before failing.
BUSY_TIMEOUT_SECONDS = 30.0


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime:
    """Naive UTC now (matches how timestamps are stored in the DB)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(value: str | None) -> str:
    """Normalize an identity key (email/domain) for dedup: strip + lowercase."""
    return (value or "").strip().lower()


# ── Schema migrations ─────────────────────────────────────────────────
# Each entry is an idempotent SQL script. The index into this list + 1 is
# the schema version stored in PRAGMA user_version. Never edit or reorder
# released migrations — append new ones.

MIGRATIONS: list[str] = [
    # ── v1: base schema (idempotent, so pre-migration DBs adopt cleanly) ──
    """
    CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        website TEXT DEFAULT '',
        description TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        location TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS prospects (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '' REFERENCES companies(id),
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0,
        phone TEXT DEFAULT '',
        phone_verified INTEGER DEFAULT 0,
        linkedin_url TEXT DEFAULT '',
        title TEXT DEFAULT '',
        seniority TEXT DEFAULT '',
        department TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        company TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        instantly_campaign_id TEXT DEFAULT '',
        sequence_json TEXT DEFAULT '[]',
        prospect_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        prospect_id TEXT REFERENCES prospects(id),
        campaign_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        thread_json TEXT DEFAULT '[]',
        intent TEXT DEFAULT '',
        stage TEXT DEFAULT 'initial_outreach',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        entity_type TEXT DEFAULT '',
        entity_id TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        action_type TEXT,
        agent TEXT,
        details_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_log (
        id TEXT PRIMARY KEY,
        date TEXT UNIQUE,
        claude_calls INTEGER DEFAULT 0,
        usage_percent REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS processed_replies (
        reply_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
    CREATE INDEX IF NOT EXISTS idx_prospects_company_id ON prospects(company_id);
    CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
    CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
    CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
    CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
    CREATE INDEX IF NOT EXISTS idx_feedback_entity ON feedback(entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
    """,
    # ── v2: normalize identity keys, dedup, uniqueness, hot-path indexes ──
    """
    -- Normalize emails/domains so uniqueness is case-insensitive going forward.
    UPDATE prospects SET email = LOWER(TRIM(email)) WHERE email != LOWER(TRIM(email));
    UPDATE companies SET domain = LOWER(TRIM(domain)) WHERE domain != LOWER(TRIM(domain));

    -- Dedup existing rows (keep the earliest) so unique indexes can be built.
    DELETE FROM prospects WHERE email != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE email != '' GROUP BY email
    );
    DELETE FROM prospects WHERE linkedin_url != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM prospects WHERE linkedin_url != '' GROUP BY linkedin_url
    );
    DELETE FROM companies WHERE domain != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM companies WHERE domain != '' GROUP BY domain
    );

    -- Enforce uniqueness at the DB level (partial: blank values allowed).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_email
        ON prospects(email) WHERE email != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_prospects_linkedin
        ON prospects(linkedin_url) WHERE linkedin_url != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_domain
        ON companies(domain) WHERE domain != '';

    -- Hot-path indexes: campaign stats subqueries, reply handling, dedup checks.
    CREATE INDEX IF NOT EXISTS idx_conversations_campaign_id ON conversations(campaign_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_prospect_id ON conversations(prospect_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_intent ON conversations(intent);
    CREATE INDEX IF NOT EXISTS idx_prospects_name_company
        ON prospects(LOWER(first_name), LOWER(last_name), LOWER(company));
    CREATE INDEX IF NOT EXISTS idx_prospects_status_updated ON prospects(status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at);
    """,
    # ── v3: per-call usage accounting (tokens, cost, attribution) ──
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        agent TEXT DEFAULT '',
        task TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        request_key TEXT DEFAULT '',
        model TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER DEFAULT 0,
        num_turns INTEGER DEFAULT 0,
        is_error INTEGER DEFAULT 0,
        source TEXT DEFAULT 'result_json'
    );

    CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_usage_events_agent ON usage_events(agent);
    CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
    -- Transcript-backfilled rows carry a request_key; uniqueness makes
    -- reconciliation idempotent (INSERT OR IGNORE).
    CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_request
        ON usage_events(request_key) WHERE request_key != '';
    """,
    # ── v4: honest email statuses + per-domain pattern cache ──
    """
    -- verified: a provider confirmed the mailbox exists
    -- risky:    catch-all / accept-all domain — sendable only in low volume
    -- guess:    pattern guess, never verified — never auto-sent
    -- invalid:  provider said undeliverable
    ALTER TABLE prospects ADD COLUMN email_status TEXT DEFAULT '';

    -- Backfill: the old email_verified flag over-reported (pattern guesses
    -- at any MX-bearing domain were marked verified), so every existing
    -- address is downgraded to an honest 'guess' and must re-verify.
    UPDATE prospects SET email_status = 'guess', email_verified = 0
        WHERE email != '';

    CREATE TABLE IF NOT EXISTS email_patterns (
        domain TEXT PRIMARY KEY,
        pattern TEXT DEFAULT '',
        source TEXT DEFAULT '',
        confidence REAL DEFAULT 0.0,
        mx_type TEXT DEFAULT '',
        is_catch_all INTEGER DEFAULT -1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v5: buying signals on companies (tech stack, hiring, etc.) ──
    """
    ALTER TABLE companies ADD COLUMN tech_stack_json TEXT DEFAULT '[]';
    ALTER TABLE companies ADD COLUMN signals_json TEXT DEFAULT '[]';
    """,
    # ── v6: outbox (approval ladder + native sending) and settings KV ──
    """
    -- Every outgoing email becomes an outbox row first. Status ladder:
    --   pending_review -> approved -> sent
    --   (or rejected / cancelled / failed)
    -- The unique index on (campaign_id, prospect_id, step) is the
    -- double-send guard: retries and re-stages physically cannot
    -- duplicate a send.
    CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY,
        campaign_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        conversation_id TEXT DEFAULT '',
        step INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'sequence',
        to_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        status TEXT DEFAULT 'pending_review',
        send_at TIMESTAMP,
        sent_at TIMESTAMP,
        provider TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        thread_ref TEXT DEFAULT '',
        in_reply_to TEXT DEFAULT '',
        error TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_campaign_step
        ON outbox(campaign_id, prospect_id, step)
        WHERE campaign_id != '' AND kind = 'sequence';
    CREATE INDEX IF NOT EXISTS idx_outbox_status_send_at ON outbox(status, send_at);
    CREATE INDEX IF NOT EXISTS idx_outbox_prospect ON outbox(prospect_id);

    -- Simple key/value store for operational flags (kill switch, counters).
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v7: observation model — every fact is a row, never a column ──
    """
    -- The governed vocabulary. A collector may only emit a code that exists
    -- here (enforced by the observations FK), so a typo fails loudly instead
    -- of quietly inventing a junk signal. `status` is the user-confirmation
    -- gate: Harvey PROPOSES signals, the user confirms which ones to
    -- prospect against, and only confirmed signals get collected.
    CREATE TABLE IF NOT EXISTS signal_codes (
        code TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        value_type TEXT DEFAULT 'text',
        collector TEXT DEFAULT '',
        cost_note TEXT DEFAULT '',
        status TEXT DEFAULT 'proposed',
        confidence_floor REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- One row per fact. Confidence and provenance travel WITH the fact, and
    -- re-observing the same signal over time is a free time series.
    CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        company_id TEXT DEFAULT '',
        prospect_id TEXT DEFAULT '',
        signal_code TEXT NOT NULL,
        collector TEXT DEFAULT '',
        value_num REAL,
        value_text TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        evidence_url TEXT DEFAULT '',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_obs_company ON observations(company_id);
    CREATE INDEX IF NOT EXISTS idx_obs_signal ON observations(signal_code);
    CREATE INDEX IF NOT EXISTS idx_obs_company_signal
        ON observations(company_id, signal_code, observed_at);
    CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);

    -- The governed-vocabulary guarantee. A trigger (not a foreign key) so it
    -- holds regardless of the per-connection foreign_keys pragma, and applies
    -- only here: the legacy tables default several id columns to '' and would
    -- break under blanket FK enforcement.
    CREATE TRIGGER IF NOT EXISTS trg_observations_signal_known
    BEFORE INSERT ON observations
    FOR EACH ROW
    WHEN NEW.signal_code NOT IN (SELECT code FROM signal_codes)
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code: not in signal_codes vocabulary');
    END;

    -- A log of what ran, when, how much it produced and cost. Purely a
    -- record: spend is capped inside the collector, never here.
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        stage TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        provider TEXT DEFAULT '',
        records INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        params_json TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_runs_stage_started ON runs(stage, started_at);
    """,
    # ── v8: discovery — entity resolution for businesses without a website ──
    """
    -- The best prospect for anyone selling websites is a business that has
    -- none, so `domain` cannot be the only identity key. `external_id` holds
    -- the provider's stable id ("dataforseo:ChIJ...", "osm:node/123") and is
    -- what dedups a re-run for those records.
    ALTER TABLE companies ADD COLUMN external_id TEXT DEFAULT '';
    ALTER TABLE companies ADD COLUMN phone TEXT DEFAULT '';

    CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_external
        ON companies(external_id) WHERE external_id != '';
    """,

    # ── v9: track prospect-backfill attempts on known companies ──
    """
    -- Without this, a company that gets scraped for contacts and comes up
    -- empty (no team page, site blocked, etc.) is indistinguishable from one
    -- never attempted, so the backfill pass would retry the same handful of
    -- companies forever instead of rotating through the rest.
    ALTER TABLE companies ADD COLUMN prospects_checked_at TIMESTAMP DEFAULT NULL;
    """,

    # ── v10: same fix, for the profile collector ──
    """
    -- The profile collector only ever wrote an observation on a SUCCESSFUL
    -- fetch; an unreachable site produced zero rows despite the code's own
    -- comment claiming "a failure IS an observation." Staleness was judged
    -- purely from observations, so an unreachable company never looked
    -- checked and the same handful got retried every cycle forever.
    -- Backfill from existing observations so already-profiled companies
    -- aren't treated as unchecked just because this column is new.
    ALTER TABLE companies ADD COLUMN profile_checked_at TIMESTAMP DEFAULT NULL;

    UPDATE companies SET profile_checked_at = (
        SELECT MAX(observed_at) FROM observations
        WHERE observations.company_id = companies.id
          AND observations.collector = 'profile'
    ) WHERE id IN (
        SELECT DISTINCT company_id FROM observations WHERE collector = 'profile'
    );
    """,
    # ── v11: which subscription each call ran on (claude | codex) ──
    """
    -- Every row before this migration was a Claude call.
    ALTER TABLE usage_events ADD COLUMN provider TEXT DEFAULT 'claude';
    CREATE INDEX IF NOT EXISTS idx_usage_events_provider ON usage_events(provider);

    -- claude_calls keeps meaning Claude calls; Codex calls count separately
    -- so one subscription's budget never consumes the other's.
    ALTER TABLE usage_log ADD COLUMN codex_calls INTEGER DEFAULT 0;
    """,
    # ── v12: files sent with an outbox item (names under HARVEY_HOME/attachments) ──
    """
    ALTER TABLE outbox ADD COLUMN attachments_json TEXT DEFAULT '[]';
    """,
    # ── v13: calls on an OpenAI-compatible endpoint count separately too ──
    """
    ALTER TABLE usage_log ADD COLUMN openai_calls INTEGER DEFAULT 0;
    """,
    # ── v14: Excluded list — the free list-hygiene sweep moves provably-dead
    # addresses out of the sending pool. 'excluded' gates writing and sending
    # without touching email_status, so restoring is a clean reversal. ──
    """
    ALTER TABLE prospects ADD COLUMN excluded INTEGER DEFAULT 0;
    ALTER TABLE prospects ADD COLUMN excluded_reason TEXT DEFAULT '';
    CREATE INDEX IF NOT EXISTS idx_prospects_excluded ON prospects(excluded);
    """,
]

BRAIN_PROVIDERS = ("claude", "codex", "openai")
_USAGE_LOG_COLUMN = {"claude": "claude_calls", "codex": "codex_calls", "openai": "openai_calls"}

# When a company was last profiled: the newer of the explicit check stamp
# (set on every attempt, reachable or not) and its latest profile observation
# (written by any path, including ones that never stamp the column).
_LAST_PROFILED_JOIN = """LEFT JOIN (
                       SELECT company_id, MAX(observed_at) AS last_seen
                       FROM observations WHERE collector = 'profile'
                       GROUP BY company_id
                   ) p ON p.company_id = c.id"""
_LAST_PROFILED = (
    "(CASE WHEN p.last_seen IS NULL OR c.profile_checked_at > p.last_seen"
    " THEN c.profile_checked_at ELSE p.last_seen END)"
)

# Column whitelists for dynamic UPDATEs (prevents SQL injection via kwargs).
_CAMPAIGN_COLUMNS = frozenset({
    "name", "channel", "instantly_campaign_id",
    "sequence_json", "prospect_ids_json", "status",
})
_CONVERSATION_COLUMNS = frozenset({
    "prospect_id", "campaign_id", "channel",
    "thread_json", "intent", "stage", "status",
})


class StateManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)

    @asynccontextmanager
    async def _connect(self):
        """Open a connection with sane concurrency settings.

        `timeout` maps to SQLite's busy handler, so writers wait for locks
        (e.g. while the dashboard holds a read) instead of erroring.
        """
        db = await aiosqlite.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            yield db
        finally:
            await db.close()

    async def init_db(self):
        """Create/upgrade the schema. Safe to call on every startup."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            # WAL is persistent in the DB file: readers (dashboard) never
            # block the writer (agent) and vice versa.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")

            async with db.execute("PRAGMA user_version") as cursor:
                (version,) = await cursor.fetchone()

            for target, script in enumerate(MIGRATIONS, start=1):
                if version < target:
                    await db.executescript(script)
                    await db.execute(f"PRAGMA user_version = {target}")
                    await db.commit()

    # ── Companies ──

    @staticmethod
    def _company_from_row(row: aiosqlite.Row) -> Company:
        d = dict(row)
        for json_col, field in (("tech_stack_json", "tech_stack"), ("signals_json", "signals")):
            raw = d.pop(json_col, None)
            try:
                parsed = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                parsed = []
            d[field] = parsed if isinstance(parsed, list) else []
        return Company(**d)

    async def add_company(self, company: Company) -> str:
        """Insert a company, or return the id of the one already recorded.

        Identity is the normalised domain when there is one, and the
        provider's ``external_id`` when there isn't — a business with no
        website still has to dedup across re-runs, and for anyone selling
        websites those are the best prospects on the list.
        """
        if not company.id:
            company.id = _new_id()
        company.domain = _norm(company.domain)
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO companies
                   (id, name, domain, website, description, industry,
                    company_size, location, phone, source, source_url,
                    external_id, notes, tech_stack_json, signals_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company.id, company.name, company.domain, company.website,
                    company.description, company.industry, company.company_size,
                    company.location, company.phone, company.source,
                    company.source_url, company.external_id, company.notes,
                    json.dumps(company.tech_stack), json.dumps(company.signals),
                    company.created_at.isoformat(),
                    company.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                # Uniqueness conflict: hand back the existing record's id.
                for column, value in (("domain", company.domain),
                                      ("external_id", company.external_id)):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM companies WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            company.id = row[0]
                            break
        return company.id

    async def update_company_signals(
        self,
        company_id: str,
        tech_stack: list[str] | None = None,
        new_signals: list[dict] | None = None,
    ):
        """Merge freshly-detected tech + signals into a company record.

        Signals are appended with dedup on (type, detail) so re-scans
        don't multiply the same finding.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT tech_stack_json, signals_json FROM companies WHERE id = ?",
                (company_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return

            def _load(raw):
                try:
                    parsed = json.loads(raw) if raw else []
                except (json.JSONDecodeError, TypeError):
                    parsed = []
                return parsed if isinstance(parsed, list) else []

            tech = _load(row["tech_stack_json"])
            signals = _load(row["signals_json"])

            for t in tech_stack or []:
                if t not in tech:
                    tech.append(t)
            seen = {(s.get("type"), s.get("detail")) for s in signals if isinstance(s, dict)}
            for s in new_signals or []:
                if not isinstance(s, dict):
                    continue
                if (s.get("type"), s.get("detail")) in seen:
                    continue
                signals.append(s)
                seen.add((s.get("type"), s.get("detail")))

            await db.execute(
                "UPDATE companies SET tech_stack_json = ?, signals_json = ?, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(tech), json.dumps(signals),
                 _utcnow().isoformat(), company_id),
            )
            await db.commit()

    async def get_company(self, company_id: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_company_by_domain(self, domain: str) -> Company | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_company_by_external_id(self, external_id: str) -> Company | None:
        """Look a company up by its provider-stable id.

        The identity path for businesses with no website — which is exactly
        the cohort worth the most to anyone selling one.
        """
        if not external_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE external_id = ?", (external_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._company_from_row(row) if row else None

    async def get_contacts_for_company(self, company_id: str) -> list[Prospect]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
                (company_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def company_exists(self, domain: str) -> bool:
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM companies WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                return bool(await cursor.fetchone())

    # ── Prospects (Contacts) ──

    @staticmethod
    def _prospect_from_row(row: aiosqlite.Row) -> Prospect:
        d = dict(row)
        d["email_verified"] = bool(d.get("email_verified", 0))
        d["phone_verified"] = bool(d.get("phone_verified", 0))
        d["email_status"] = d.get("email_status") or ""
        d["excluded"] = bool(d.get("excluded", 0))
        return Prospect(**d)

    async def add_prospect(self, prospect: Prospect) -> str:
        """Insert a prospect. Duplicates (same email or LinkedIn URL) are not
        re-inserted; the existing record's id is returned instead."""
        if not prospect.id:
            prospect.id = _new_id()
        prospect.email = _norm(prospect.email)
        prospect.linkedin_url = (prospect.linkedin_url or "").strip()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO prospects
                   (id, company_id, first_name, last_name, email, email_verified,
                    email_status, phone, phone_verified, linkedin_url, title,
                    seniority, department, source, source_url, status, score,
                    personalization_notes, company, industry, company_size,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prospect.id, prospect.company_id,
                    prospect.first_name, prospect.last_name,
                    prospect.email, int(prospect.email_verified),
                    prospect.email_status,
                    prospect.phone, int(prospect.phone_verified),
                    prospect.linkedin_url, prospect.title,
                    prospect.seniority, prospect.department,
                    prospect.source, prospect.source_url,
                    prospect.status, prospect.score,
                    prospect.personalization_notes,
                    prospect.company, prospect.industry, prospect.company_size,
                    prospect.created_at.isoformat(),
                    prospect.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                # Unique-constraint conflict: resolve to the existing record.
                for column, value in (
                    ("email", prospect.email),
                    ("linkedin_url", prospect.linkedin_url),
                    ("id", prospect.id),
                ):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM prospects WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            prospect.id = row[0]
                            break
        return prospect.id

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def get_prospects_by_status(self, status: str, limit: int | None = None,
                                      email_statuses: tuple | None = None,
                                      include_excluded: bool = False) -> list[Prospect]:
        """Prospects in a status, newest first. ``email_statuses`` narrows to
        deliverable addresses in SQL, so a page limit never stalls on rows
        the writer would skip anyway. Prospects moved to the Excluded list are
        left out unless ``include_excluded`` is set."""
        params: list = [status]
        where = "status = ?"
        if not include_excluded:
            where += " AND COALESCE(excluded, 0) = 0"
        if email_statuses:
            where += f" AND COALESCE(NULLIF(email_status, ''), 'guess') IN ({','.join('?' for _ in email_statuses)})"
            params += list(email_statuses)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM prospects WHERE {where} ORDER BY created_at DESC"
                + (f" LIMIT {int(limit)}" if limit else ""),
                params,
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def update_prospect_status(self, prospect_id: str, status: str):
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def exclude_prospect(self, prospect_id: str, reason: str = "") -> None:
        """Move a prospect to the Excluded list: out of the sending and
        writing pool, reversibly. email_status is left untouched, so a later
        restore returns the prospect exactly as it was."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET excluded = 1, excluded_reason = ?, updated_at = ? WHERE id = ?",
                (reason or "", _utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def restore_prospect(self, prospect_id: str) -> None:
        """Undo an exclusion: the prospect rejoins the sending pool."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET excluded = 0, excluded_reason = '', updated_at = ? WHERE id = ?",
                (_utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def get_excluded_prospects(self, limit: int | None = None) -> list[Prospect]:
        """The Excluded list: prospects pulled from sending, newest first."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE COALESCE(excluded, 0) = 1 ORDER BY updated_at DESC"
                + (f" LIMIT {int(limit)}" if limit else "")
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def count_prospects_excluded(self) -> int:
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE COALESCE(excluded, 0) = 1"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0

    async def get_prospects_with_email(self, limit: int | None = None,
                                       include_excluded: bool = False) -> list[Prospect]:
        """Every prospect that has an email address, any status — the input to
        a list-hygiene sweep. Excluded ones are skipped unless asked for."""
        where = "email != ''"
        if not include_excluded:
            where += " AND COALESCE(excluded, 0) = 0"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM prospects WHERE {where} ORDER BY created_at DESC"
                + (f" LIMIT {int(limit)}" if limit else "")
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._prospect_from_row(r) for r in rows]

    async def update_prospect_name(self, prospect_id: str, first_name: str, last_name: str,
                                   note: str = "", title: str = ""):
        """Record a name found after import (team page, LinkedIn, address
        pattern), appending where it came from to the research notes. A
        title fills in only when the row has none."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE prospects SET first_name = ?, last_name = ?, "
                "title = CASE WHEN title = '' OR title IS NULL THEN ? ELSE title END, "
                "personalization_notes = CASE WHEN ? = '' THEN personalization_notes "
                "WHEN personalization_notes = '' OR personalization_notes IS NULL THEN ? "
                "ELSE personalization_notes || char(10) || ? END, "
                "updated_at = ? WHERE id = ?",
                (first_name, last_name, title or "", note, note, note,
                 _utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def update_prospect_segment(self, prospect_id: str, *, industry: str = "",
                                      seniority: str = "", department: str = ""):
        """Record which sector / role tier / department a contact belongs to
        (the writer's classification step), leaving blanks untouched."""
        sets, params = ["updated_at = ?"], [_utcnow().isoformat()]
        for column, value in (("industry", industry), ("seniority", seniority),
                              ("department", department)):
            if value:
                sets.append(f"{column} = ?")
                params.append(value)
        params.append(prospect_id)
        async with self._connect() as db:
            await db.execute(f"UPDATE prospects SET {', '.join(sets)} WHERE id = ?", params)
            await db.commit()

    async def update_company_profile(self, company_id: str, *, name: str = "", description: str = ""):
        """What the company's own site says it is called and does. The name
        replaces only a missing one or a bare domain; the description fills
        only an empty one. Prospects carrying the old name are renamed too."""
        from harvey.names import looks_like_domain
        if not company_id:
            return
        async with self._connect() as db:
            async with db.execute("SELECT name, description FROM companies WHERE id = ?",
                                  (company_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                return
            old_name, old_desc = row[0] or "", row[1] or ""
            now = _utcnow().isoformat()
            if name and (not old_name or looks_like_domain(old_name)) and name != old_name:
                await db.execute("UPDATE companies SET name = ?, updated_at = ? WHERE id = ?",
                                 (name, now, company_id))
                await db.execute(
                    "UPDATE prospects SET company = ?, updated_at = ? WHERE company_id = ? "
                    "AND (company = ? OR company = '' OR company IS NULL)",
                    (name, now, company_id, old_name))
            if description and not old_desc:
                await db.execute("UPDATE companies SET description = ?, updated_at = ? WHERE id = ?",
                                 (description[:600], now, company_id))
            await db.commit()

    async def update_company_industry(self, company_id: str, industry: str):
        if not (company_id and industry):
            return
        async with self._connect() as db:
            await db.execute(
                "UPDATE companies SET industry = ?, updated_at = ? WHERE id = ? "
                "AND (industry IS NULL OR industry = '')",
                (industry, _utcnow().isoformat(), company_id))
            await db.commit()

    async def get_prospect_by_email(self, email: str) -> Prospect | None:
        """Look up a prospect by email address (indexed, case-insensitive)."""
        email = _norm(email)
        if not email:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE email = ?", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._prospect_from_row(row) if row else None

    async def update_prospect_email(
        self, prospect_id: str, email: str, email_status: str
    ):
        """Set a prospect's email + honesty status (verified/risky/guess/invalid)."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE prospects
                   SET email = ?, email_status = ?, email_verified = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    _norm(email), email_status,
                    1 if email_status == "verified" else 0,
                    _utcnow().isoformat(), prospect_id,
                ),
            )
            await db.commit()

    # ── Email pattern cache (per-domain) ──

    async def get_email_pattern(self, domain: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM email_patterns WHERE domain = ?", (_norm(domain),)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def save_email_pattern(
        self,
        domain: str,
        pattern: str = "",
        source: str = "",
        confidence: float = 0.0,
        mx_type: str = "",
        is_catch_all: int | None = None,
    ):
        """Upsert what we've learned about a domain's email conventions.

        Only overwrites the stored pattern when the new one has equal or
        higher confidence; mx_type/is_catch_all always refresh.
        """
        domain = _norm(domain)
        if not domain:
            return
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO email_patterns
                       (domain, pattern, source, confidence, mx_type, is_catch_all, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(domain) DO UPDATE SET
                       pattern = CASE WHEN excluded.confidence >= email_patterns.confidence
                                       AND excluded.pattern != ''
                                      THEN excluded.pattern ELSE email_patterns.pattern END,
                       source = CASE WHEN excluded.confidence >= email_patterns.confidence
                                      AND excluded.pattern != ''
                                     THEN excluded.source ELSE email_patterns.source END,
                       confidence = MAX(email_patterns.confidence, excluded.confidence),
                       mx_type = CASE WHEN excluded.mx_type != ''
                                      THEN excluded.mx_type ELSE email_patterns.mx_type END,
                       is_catch_all = CASE WHEN excluded.is_catch_all != -1
                                           THEN excluded.is_catch_all
                                           ELSE email_patterns.is_catch_all END,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    domain, pattern, source, float(confidence), mx_type,
                    -1 if is_catch_all is None else int(is_catch_all),
                ),
            )
            await db.commit()

    # ── Outbox (approval ladder + native sending) ──

    async def add_outbox_item(
        self,
        *,
        prospect_id: str,
        to_email: str,
        subject: str,
        body: str,
        send_at: str,
        status: str = "pending_review",
        campaign_id: str = "",
        conversation_id: str = "",
        step: int = 1,
        kind: str = "sequence",
        provider: str = "",
        thread_ref: str = "",
        in_reply_to: str = "",
        attachments: list[str] | None = None,
    ) -> str | None:
        """Queue one outgoing email. Returns its id, or None when the
        (campaign, prospect, step) slot already exists — the double-send guard."""
        item_id = _new_id()
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO outbox
                   (id, campaign_id, prospect_id, conversation_id, step, kind,
                    to_email, subject, body, status, send_at, provider,
                    thread_ref, in_reply_to, attachments_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id, campaign_id, prospect_id, conversation_id,
                    int(step), kind, _norm(to_email), subject, body,
                    status, send_at, provider, thread_ref, in_reply_to,
                    json.dumps(list(attachments or [])),
                ),
            )
            await db.commit()
            return item_id if cursor.rowcount > 0 else None

    async def get_outbox(
        self,
        status: str | None = None,
        due_before: str | None = None,
        limit: int = 200,
        exclude_kind: str | None = None,
        first_only: bool = False,
    ) -> list[dict]:
        """``first_only``: no follow-ups (a sequence step after the first);
        first emails and replies only."""
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if due_before:
            where.append("(send_at IS NULL OR send_at <= ?)")
            params.append(due_before)
        if exclude_kind:
            where.append("(kind IS NULL OR kind != ?)")
            params.append(exclude_kind)
        if first_only:
            where.append("(kind IS NULL OR kind != 'sequence' OR step IS NULL OR step <= 1)")
        sql = "SELECT * FROM outbox"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY send_at ASC, created_at ASC LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def get_outbox_item(self, item_id: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE id = ?", (item_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    _OUTBOX_COLUMNS = frozenset({
        "status", "error", "message_id", "thread_ref", "sent_at",
        "subject", "body", "send_at", "provider",
    })

    async def update_outbox_item(self, item_id: str, **kwargs):
        fields = {k: v for k, v in kwargs.items() if k in self._OUTBOX_COLUMNS}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE outbox SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _utcnow().isoformat(), item_id),
            )
            await db.commit()

    async def sequence_step_status(self, campaign_id: str, prospect_id: str, step: int) -> str | None:
        """The status of one step of a prospect's sequence, or None."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT status FROM outbox WHERE campaign_id = ? AND prospect_id = ? AND step = ? AND kind = 'sequence' LIMIT 1",
                (campaign_id, prospect_id, int(step)),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def outbox_slots(self, campaign_id: str) -> set[tuple[str, int]]:
        """(prospect_id, step) pairs a campaign already has in the outbox,
        whatever their status: the slots the unique index keeps."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT prospect_id, step FROM outbox WHERE campaign_id = ? AND kind = 'sequence'",
                (campaign_id,),
            ) as cur:
                return {(r[0], int(r[1])) for r in await cur.fetchall()}

    async def claim_outbox_item(self, item_id: str) -> bool:
        """Move one approved row to 'sending'. False when someone else got
        it first (or it is no longer approved): never send a row twice."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE outbox SET status = 'sending', updated_at = ? WHERE id = ? AND status = 'approved'",
                (_utcnow().isoformat(), item_id))
            await db.commit()
            return cur.rowcount == 1

    async def release_stale_sending(self, minutes: int = 15) -> int:
        """Rows left in 'sending' by a process that died mid-send. Whether
        the email left is unknown, so they are failed with a note rather
        than sent again."""
        from datetime import timedelta
        cutoff = (_utcnow() - timedelta(minutes=minutes)).isoformat()
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE outbox SET status = 'failed', error = 'the worker stopped while this was being sent; "
                "it may or may not have left', updated_at = ? WHERE status = 'sending' AND updated_at < ?",
                (_utcnow().isoformat(), cutoff))
            await db.commit()
            return cur.rowcount

    async def approve_outbox(self, item_id: str | None = None) -> int:
        """Approve one pending item, or ALL pending when item_id is None."""
        async with self._connect() as db:
            if item_id:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', updated_at = ? "
                    "WHERE id = ? AND status = 'pending_review'",
                    (_utcnow().isoformat(), item_id),
                )
            else:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', updated_at = ? "
                    "WHERE status = 'pending_review'",
                    (_utcnow().isoformat(),),
                )
            await db.commit()
            return cursor.rowcount

    async def reject_all_outbox(self, status: str = "pending_review") -> int:
        """Reject every outbox item in a status (the counterpart of
        approve-all): 'pending_review' held emails, or 'approved' ones still
        scheduled to send. Sent emails are never touched."""
        if status not in ("pending_review", "approved"):
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'rejected', error = 'rejected in the web app', updated_at = ? "
                "WHERE status = ?",
                (_utcnow().isoformat(), status),
            )
            await db.commit()
            return cursor.rowcount

    async def cancel_pending_outbox_for_prospect(
        self, prospect_id: str, reason: str = "stop_on_reply"
    ) -> int:
        """Stop-on-reply: cancel everything queued for a prospect who replied."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
                "WHERE prospect_id = ? AND status IN ('pending_review', 'approved')",
                (reason, _utcnow().isoformat(), prospect_id),
            )
            await db.commit()
            return cursor.rowcount

    async def hold_domain_sends(self, domain: str, exclude_prospect_id: str, note: str) -> int:
        """Approved first emails to other people at ``domain`` go back to
        review with ``note``: one nonexistent address at a company usually
        means the whole guessed pattern is wrong."""
        if not domain:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'pending_review', error = ?, updated_at = ? "
                "WHERE status = 'approved' AND kind = 'sequence' AND step = 1 AND lower(to_email) LIKE ? "
                "AND prospect_id != ?",
                (note[:600], _utcnow().isoformat(), f"%@{domain.lower()}", exclude_prospect_id or ""),
            )
            await db.commit()
            return cursor.rowcount

    async def update_prospect_notes(self, prospect_id: str, notes: str) -> None:
        async with self._connect() as db:
            await db.execute("UPDATE prospects SET personalization_notes = ?, updated_at = ? WHERE id = ?",
                             (notes, _utcnow().isoformat(), prospect_id))
            await db.commit()

    async def count_outbox_queued(self, kind: str = "sequence", statuses: tuple = ("pending_review", "approved"),
                                  first_step_only: bool = True) -> int:
        """Emails written but not yet sent. The writer keeps this to a
        couple of days of sends, so it counts what the sender is actually
        waiting on: first emails (follow-ups are scheduled days ahead) in
        the given statuses (held-for-review rows are not a backlog in
        autopilot; they wait for a person)."""
        params: list = [kind, *statuses]
        marks = ",".join("?" for _ in statuses) or "''"
        async with self._connect() as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM outbox WHERE kind = ? AND status IN ({marks})"
                + (" AND step = 1" if first_step_only else ""), params
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0) if row else 0

    async def count_outbox_for_conversation(self, conversation_id: str, kind: str = "reply") -> int:
        """How many replies have been queued or sent in this conversation."""
        if not conversation_id:
            return 0
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE conversation_id = ? AND kind = ? "
                "AND status != 'rejected'", (conversation_id, kind)
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0] or 0) if row else 0

    async def sent_first_email(self, prospect_id: str) -> dict | None:
        """The enquiry we sent this person (step 1), so a reply can be
        answered in the same voice and with the same facts."""
        if not prospect_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT subject, body FROM outbox WHERE prospect_id = ? AND kind = 'sequence' AND step = 1 "
                "AND status = 'sent' ORDER BY sent_at DESC LIMIT 1", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def outbox_bodies_for_conversation(self, conversation_id: str) -> list[str]:
        """Every body we queued, sent or hold in this conversation (not the
        rejected or cancelled ones), so a link is sent once."""
        if not conversation_id:
            return []
        async with self._connect() as db:
            async with db.execute(
                "SELECT body FROM outbox WHERE conversation_id = ? AND status NOT IN ('rejected', 'cancelled')",
                (conversation_id,)
            ) as cursor:
                return [r[0] or "" for r in await cursor.fetchall()]

    async def outbox_answers(self, message_id: str) -> bool:
        """Whether an outbox item (queued, sent, or held) already replies to
        this incoming Message-ID."""
        if not message_id:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM outbox WHERE in_reply_to = ? AND status != 'cancelled' LIMIT 1",
                (message_id,),
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def count_outbox_to_since(self, to_email: str, kind: str, since_iso: str) -> int:
        """Emails of one kind queued or sent to an address since a time —
        the loop guard for inbox auto-replies."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE lower(to_email) = ? AND kind = ? "
                "AND created_at >= ? AND status != 'cancelled'",
                (_norm(to_email), kind, since_iso),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outbox_sent(self) -> int:
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent'"
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def first_outbox_sent_at(self) -> str | None:
        """When this mailbox sent its first email through Harvey (ISO), or
        None. The warm-up ramp counts days from here."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT MIN(sent_at) FROM outbox WHERE status = 'sent' AND sent_at IS NOT NULL"
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row and row[0] else None

    async def count_outbox_sent_since(self, minutes: int) -> int:
        """Emails sent in the last ``minutes`` (first emails, follow-ups and replies)."""
        since = (_utcnow() - timedelta(minutes=max(1, int(minutes)))).isoformat()
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent' AND sent_at >= ?", (since,)
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def count_outbox_sent_between(self, start: datetime, end: datetime) -> int:
        """Emails sent with ``start`` <= sent_at <= ``end`` (naive UTC)."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent' AND sent_at >= ? AND sent_at <= ?",
                (start.isoformat(), end.isoformat()),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def requeue_last_first_emails(self, count: int, note: str) -> int:
        """The ``count`` most recently sent first emails go again (each with
        its follow-ups moved along): for a run that went out while the
        mailbox was blocked or capped. Returns how many were queued again."""
        count = max(0, int(count))
        if not count:
            return 0
        async with self._connect() as db:
            async with db.execute(
                "SELECT id FROM outbox WHERE status = 'sent' AND COALESCE(kind, 'sequence') = 'sequence' "
                "AND COALESCE(step, 1) = 1 ORDER BY sent_at DESC, created_at DESC LIMIT ?", (count,)
            ) as cur:
                ids = [row[0] for row in await cur.fetchall()]
        done = 0
        for item_id in ids:
            if await self.requeue_first_email(item_id, note):
                done += 1
        return done

    async def requeue_first_email(self, item_id: str, note: str) -> bool:
        """A first email that bounced because the mailbox was blocked from
        sending goes again once sending resumes: the row is approved again
        for now, and the person's later steps move by the same delay, so the
        spacing between the emails is kept."""
        item = await self.get_outbox_item(item_id)
        if not item or item.get("status") != "sent":
            return False
        now = _utcnow()
        was = None
        for key in ("sent_at", "send_at"):
            try:
                was = datetime.fromisoformat(str(item.get(key) or "").replace("Z", "")) if item.get(key) else None
            except ValueError:
                was = None
            if was:
                break
        shift = (now - was) if was else timedelta(0)
        await self.update_outbox_item(item_id, status="approved", send_at=now.isoformat(), sent_at=None, error=note[:600])
        if item.get("campaign_id") and item.get("prospect_id") and shift > timedelta(0):
            async with self._connect() as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT id, send_at FROM outbox WHERE campaign_id = ? AND prospect_id = ? AND kind = 'sequence' "
                    "AND step > ? AND status IN ('approved', 'pending_review')",
                    (item["campaign_id"], item["prospect_id"], int(item.get("step") or 1)),
                ) as cur:
                    later = [dict(r) for r in await cur.fetchall()]
            for row in later:
                try:
                    at = datetime.fromisoformat(str(row["send_at"] or "").replace("Z", ""))
                except ValueError:
                    continue
                await self.update_outbox_item(row["id"], send_at=(at + shift).isoformat())
        return True

    async def count_outbox_sent_today(self, kind: str = "") -> int:
        """Emails sent today (UTC); ``kind`` counts only that kind ("sequence":
        the first emails and follow-ups the daily limit is for)."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'sent' "
                "AND date(sent_at) = date('now')" + (" AND COALESCE(kind, 'sequence') = ?" if kind else ""),
                (kind,) if kind else (),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def find_outbox_sent_to(self, email: str) -> dict | None:
        """The most recent email sent to this address (a bounce that names
        the recipient but carries no Message-ID is matched through it)."""
        email = (email or "").strip().lower()
        if not email:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE status = 'sent' AND lower(to_email) = ? "
                "ORDER BY sent_at DESC, created_at DESC LIMIT 1", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def find_outbox_by_message_id(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    # ── Signal vocabulary (governed; user-confirmed before collection) ──

    async def upsert_signal_code(
        self,
        code: str,
        *,
        label: str = "",
        description: str = "",
        category: str = "",
        value_type: str = "text",
        collector: str = "",
        cost_note: str = "",
        confidence_floor: float = 0.0,
        status: str | None = None,
    ):
        """Register a signal in the vocabulary. Never downgrades a user's
        decision: an existing row's ``status`` is preserved unless explicitly
        passed, so re-seeding can't silently re-enable a rejected signal."""
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO signal_codes
                       (code, label, description, category, value_type,
                        collector, cost_note, confidence_floor, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'proposed'))
                   ON CONFLICT(code) DO UPDATE SET
                       label = excluded.label,
                       description = excluded.description,
                       category = excluded.category,
                       value_type = excluded.value_type,
                       collector = excluded.collector,
                       cost_note = excluded.cost_note,
                       confidence_floor = excluded.confidence_floor,
                       status = COALESCE(?, signal_codes.status),
                       updated_at = CURRENT_TIMESTAMP""",
                (code, label, description, category, value_type, collector,
                 cost_note, float(confidence_floor), status, status),
            )
            await db.commit()

    async def get_signal_codes(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signal_codes"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY category, code"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def set_signal_status(self, code: str, status: str) -> bool:
        """Confirm / reject a proposed signal. Only confirmed signals are
        collected — Harvey proposes, the user decides."""
        if status not in ("proposed", "confirmed", "rejected"):
            raise ValueError(f"invalid signal status: {status}")
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE signal_codes SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE code = ?",
                (status, code),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def confirmed_signal_codes(self) -> set[str]:
        return {r["code"] for r in await self.get_signal_codes(status="confirmed")}

    # ── Observations (every fact is a row, never a column) ──

    async def add_observation(
        self,
        signal_code: str,
        *,
        company_id: str = "",
        prospect_id: str = "",
        collector: str = "",
        value_num: float | None = None,
        value_text: str = "",
        confidence: float = 1.0,
        evidence_url: str = "",
        run_id: str = "",
        observed_at: str | None = None,
    ) -> str:
        """Record one fact. Raises if signal_code isn't in the vocabulary —
        a typo must fail loudly rather than create a junk signal."""
        obs_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO observations
                       (id, company_id, prospect_id, signal_code, collector,
                        value_num, value_text, confidence, evidence_url,
                        observed_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, CURRENT_TIMESTAMP), ?)""",
                (obs_id, company_id, prospect_id, signal_code, collector,
                 value_num, value_text, float(confidence), evidence_url,
                 observed_at, run_id),
            )
            await db.commit()
        return obs_id

    async def add_observations(self, rows: list[dict], run_id: str = "") -> int:
        """Batch insert. Flushed per batch by callers — a long run that dies
        must not lose everything it observed."""
        if not rows:
            return 0
        payload = [
            (
                _new_id(), r.get("company_id", ""), r.get("prospect_id", ""),
                r["signal_code"], r.get("collector", ""), r.get("value_num"),
                r.get("value_text", ""), float(r.get("confidence", 1.0)),
                r.get("evidence_url", ""), r.get("observed_at"),
                r.get("run_id", run_id),
            )
            for r in rows
        ]
        async with self._connect() as db:
            await db.executemany(
                """INSERT INTO observations
                       (id, company_id, prospect_id, signal_code, collector,
                        value_num, value_text, confidence, evidence_url,
                        observed_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, CURRENT_TIMESTAMP), ?)""",
                payload,
            )
            await db.commit()
        return len(payload)

    async def get_observations(
        self,
        company_id: str = "",
        signal_code: str = "",
        latest_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        where, params = [], []
        if company_id:
            where.append("company_id = ?")
            params.append(company_id)
        if signal_code:
            where.append("signal_code = ?")
            params.append(signal_code)
        sql = "SELECT * FROM observations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if latest_only:
            # Newest observation per (company, signal) — the current view of
            # the world, with history still on disk underneath.
            sql = (
                "SELECT * FROM (" + sql + " ORDER BY observed_at DESC) "
                "GROUP BY company_id, signal_code"
            )
        else:
            sql += " ORDER BY observed_at DESC"
        sql += " LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # A boolean signal is recorded either way: "checked, not running ads" is a
    # real finding, and a later flip from 0 to 1 is a real event. But a COHORT
    # asks who *has* the signal, so it must read the value, not merely the
    # presence of a row. Without this, "companies running Google Ads" silently
    # means "companies we checked for Google Ads" — which is everyone.
    #
    # `latest` is the current view of the world (newest observation per company
    # and signal, with history still on disk underneath). `positive` is the
    # subset where the finding is actually true — value_num IS NULL covers text
    # signals like INCUMBENT_AGENCY, where the row's existence IS the finding.
    _CURRENT_CTE = """
    WITH latest AS (
        SELECT o.* FROM observations o
        JOIN (SELECT company_id, signal_code, MAX(observed_at) AS t
              FROM observations GROUP BY company_id, signal_code) newest
          ON newest.company_id = o.company_id
         AND newest.signal_code = o.signal_code
         AND newest.t = o.observed_at
    ),
    positive AS (
        SELECT * FROM latest WHERE value_num IS NULL OR value_num != 0
    )
    """

    async def signal_counts(self) -> list[dict]:
        """How many entities actually carry each signal — the cohort sizes."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                self._CURRENT_CTE + """
                SELECT p.signal_code,
                       COALESCE(sc.label, p.signal_code) AS label,
                       sc.status AS status,
                       COUNT(DISTINCT p.company_id) AS companies,
                       COUNT(*) AS observations
                FROM positive p
                LEFT JOIN signal_codes sc ON sc.code = p.signal_code
                GROUP BY p.signal_code
                ORDER BY companies DESC"""
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def cohort(
        self,
        require: list[str],
        exclude: list[str] | None = None,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> list[str]:
        """Company IDs carrying ALL required signals and none excluded.

        Set intersection happens in SQL — doing it in application code over a
        capped SELECT silently returns the wrong cohort.
        """
        if not require:
            return []
        req_ph = ",".join("?" for _ in require)
        sql = self._CURRENT_CTE + (
            f"SELECT company_id FROM positive "
            f"WHERE signal_code IN ({req_ph}) AND company_id != '' "
            "AND confidence >= ? "
            "GROUP BY company_id HAVING COUNT(DISTINCT signal_code) = ?"
        )
        params: list = [*require, float(min_confidence), len(set(require))]
        if exclude:
            exc_ph = ",".join("?" for _ in exclude)
            sql += (
                " AND company_id NOT IN ("
                f"SELECT company_id FROM positive WHERE signal_code IN ({exc_ph})"
                ")"
            )
            params.extend(exclude)
        sql += " LIMIT ?"
        params.append(int(limit))
        async with self._connect() as db:
            async with db.execute(sql, params) as cursor:
                return [row[0] for row in await cursor.fetchall()]

    async def companies_needing_profile(
        self, limit: int = 100, stale_days: int = 90
    ) -> list[dict]:
        """Companies a profile run should visit next.

        Never checked, or last checked longer ago than ``stale_days`` —
        ordered nulls-first so it rotates through the whole backlog instead
        of retrying the same top-N-by-created_at companies every cycle.
        ``profile_checked_at`` is set on every attempt regardless of outcome
        (see ProfileCollector.run), so an unreachable site still counts as
        checked and rotates out instead of being retried forever. A profile
        observation written any other way still counts too, whichever is newer.

        Businesses with no domain are excluded — there is no site to read.
        They are not a failure, they are the NO_WEBSITE cohort.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT c.id, c.name, c.domain
                   FROM companies c
                   {_LAST_PROFILED_JOIN}
                   WHERE c.domain != ''
                     AND ({_LAST_PROFILED} IS NULL
                          OR {_LAST_PROFILED} < datetime('now', ?))
                   ORDER BY {_LAST_PROFILED} IS NOT NULL, {_LAST_PROFILED}
                   LIMIT ?""",
                (f"-{int(stale_days)} days", int(limit)),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def count_companies_needing_profile(self, stale_days: int = 90) -> int:
        async with self._connect() as db:
            async with db.execute(
                f"""SELECT COUNT(*) FROM companies c
                   {_LAST_PROFILED_JOIN}
                   WHERE c.domain != ''
                     AND ({_LAST_PROFILED} IS NULL
                          OR {_LAST_PROFILED} < datetime('now', ?))""",
                (f"-{int(stale_days)} days",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def mark_company_profiled(self, company_id: str):
        """Record that a profile pass looked at this company, whether or
        not it was reachable — advances the rotation regardless."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE companies SET profile_checked_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (company_id,),
            )
            await db.commit()

    async def companies_needing_prospects(
        self, limit: int = 10, stale_days: int = 14
    ) -> list[dict]:
        """Companies with a domain, zero prospects, and no recent check.

        `harvey discover` (and other company-only sources) can add companies
        without ever finding a named contact at them. This is what lets a
        prospecting pass go back and fill that in, instead of only ever
        looking at companies it stumbles into fresh in the same cycle.

        Ordered by ``prospects_checked_at`` (nulls first) rather than
        recency, so a backfill pass rotates through the whole backlog
        instead of retrying the same top-N-by-created_at companies every
        cycle — a company a team page didn't exist for last time is worth
        another look after ``stale_days``, not a permanent skip.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.domain, c.industry
                   FROM companies c
                   LEFT JOIN prospects p ON p.company_id = c.id
                   WHERE c.domain != ''
                     AND (c.prospects_checked_at IS NULL
                          OR c.prospects_checked_at < datetime('now', ?))
                   GROUP BY c.id
                   HAVING COUNT(p.id) = 0
                   ORDER BY c.prospects_checked_at IS NOT NULL, c.prospects_checked_at
                   LIMIT ?""",
                (f"-{int(stale_days)} days", int(limit)),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def mark_prospects_checked(self, company_id: str):
        """Record that a prospect-backfill pass looked at this company,
        whether or not it found anyone — advances the rotation regardless."""
        async with self._connect() as db:
            await db.execute(
                """UPDATE companies SET prospects_checked_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (company_id,),
            )
            await db.commit()

    # ── Run log (what ran, when, what it produced and cost) ──

    async def start_run(
        self, stage: str, provider: str = "", params: dict | None = None
    ) -> str:
        run_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO runs (id, stage, provider, params_json, status) "
                "VALUES (?, ?, ?, ?, 'running')",
                (run_id, stage, provider, json.dumps(params or {})),
            )
            await db.commit()
        return run_id

    async def finish_run(
        self,
        run_id: str,
        status: str = "completed",
        records: int = 0,
        cost_usd: float = 0.0,
        error: str = "",
    ):
        async with self._connect() as db:
            await db.execute(
                "UPDATE runs SET status = ?, records = ?, cost_usd = ?, "
                "error = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, int(records), float(cost_usd), error[:500], run_id),
            )
            await db.commit()

    async def sweep_stale_runs(self, older_than_hours: int = 6) -> int:
        """A killed collector can't close its own run — never trust it to."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE runs SET status = 'stale', ended_at = CURRENT_TIMESTAMP "
                "WHERE status = 'running' "
                "AND started_at < datetime('now', ?)",
                (f"-{int(older_than_hours)} hours",),
            )
            await db.commit()
            return cursor.rowcount

    async def get_runs(self, limit: int = 25) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    # ── Settings (operational flags: kill switch, counters) ──

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def set_setting(self, key: str, value: str):
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                (key, str(value)),
            )
            await db.commit()

    async def increment_setting(self, key: str, by: int = 1) -> int:
        current = await self.get_setting(key, "0")
        try:
            value = int(current) + by
        except ValueError:
            value = by
        await self.set_setting(key, str(value))
        return value

    async def prospect_exists(
        self, email: str = "", linkedin_url: str = "",
        first_name: str = "", last_name: str = "", company: str = "",
    ) -> bool:
        async with self._connect() as db:
            if email:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE email = ?", (_norm(email),)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            if linkedin_url:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE linkedin_url = ?",
                    (linkedin_url.strip(),),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            # Name + company dedup (case-insensitive, expression-indexed)
            if first_name and last_name and company:
                async with db.execute(
                    """SELECT 1 FROM prospects
                       WHERE LOWER(first_name) = ? AND LOWER(last_name) = ?
                         AND LOWER(company) = ?""",
                    (first_name.lower(), last_name.lower(), company.lower()),
                ) as cursor:
                    if await cursor.fetchone():
                        return True
        return False

    async def count_prospects_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Feedback ──

    async def add_feedback(
        self, entity_type: str, entity_id: str, comment: str
    ) -> str:
        feedback_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO feedback (id, entity_type, entity_id, comment)
                   VALUES (?, ?, ?, ?)""",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
        return feedback_id

    async def get_feedback(
        self, entity_type: str, entity_id: str
    ) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM feedback
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY created_at DESC""",
                (entity_type, entity_id),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_feedback(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ── Reply Deduplication ──

    async def is_reply_processed(self, reply_id: str) -> bool:
        """Check if a reply has already been processed."""
        if not reply_id:
            return False
        async with self._connect() as db:
            async with db.execute(
                "SELECT 1 FROM processed_replies WHERE reply_id = ?", (reply_id,)
            ) as cursor:
                return bool(await cursor.fetchone())

    async def mark_reply_processed(self, reply_id: str):
        """Mark a reply as processed to avoid double-handling."""
        if not reply_id:
            return
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
                (reply_id,),
            )
            await db.commit()

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        if not campaign.id:
            campaign.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, instantly_campaign_id, sequence_json,
                    prospect_ids_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel,
                    campaign.instantly_campaign_id, campaign.sequence_json(),
                    json.dumps(campaign.prospect_ids), campaign.status,
                    campaign.created_at.isoformat(),
                ),
            )
            await db.commit()
        return campaign.id

    async def commit_campaign(self, campaign: Campaign, prospect_ids: list[str], prospect_status: str = "queued") -> str:
        """Insert the campaign and mark its people in ONE transaction: a stop
        between the two would write the same people again next cycle."""
        if not campaign.id:
            campaign.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, instantly_campaign_id, sequence_json,
                    prospect_ids_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (campaign.id, campaign.name, campaign.channel, campaign.instantly_campaign_id,
                 campaign.sequence_json(), json.dumps(campaign.prospect_ids), campaign.status,
                 campaign.created_at.isoformat()))
            await db.executemany("UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                                 [(prospect_status, _utcnow().isoformat(), pid) for pid in prospect_ids])
            await db.commit()
        return campaign.id

    async def release_writing_campaigns(self) -> int:
        """Campaigns a writer left in 'writing' (it died mid-personalisation)
        become drafts: the sender stages the segment template for anyone
        without a personal first email."""
        async with self._connect() as db:
            cur = await db.execute("UPDATE campaigns SET status = 'draft' WHERE status = 'writing'")
            await db.commit()
            return cur.rowcount

    async def latest_campaign_named(self, name: str, hours: int = 24) -> Campaign | None:
        """The newest campaign with this name created within ``hours``, so a
        segment's sequence can be reused instead of written again."""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)).isoformat()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE name = ? AND created_at >= ? AND status != 'failed' "
                "ORDER BY created_at DESC LIMIT 1", (name, since)
            ) as cursor:
                r = await cursor.fetchone()
                if not r:
                    return None
                d = dict(r)
                d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json"))
                d["prospect_ids"] = json.loads(d.pop("prospect_ids_json") or "[]")
                return Campaign(**d)

    async def get_campaigns_by_status(self, status: str) -> list[Campaign]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                campaigns = []
                for r in rows:
                    d = dict(r)
                    d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json"))
                    d["prospect_ids"] = json.loads(d.pop("prospect_ids_json") or "[]")
                    campaigns.append(Campaign(**d))
                return campaigns

    async def update_campaign(self, campaign_id: str, **kwargs):
        """Update whitelisted campaign columns in a single atomic statement."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CAMPAIGN_COLUMNS
        if invalid:
            raise ValueError(f"Invalid campaign column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                (*kwargs.values(), campaign_id),
            )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, convo: Conversation) -> str:
        if not convo.id:
            convo.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, prospect_id, campaign_id, channel, thread_json,
                    intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.stage, convo.status, convo.created_at.isoformat(),
                    convo.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return convo.id

    async def get_conversation(self, convo_id: str) -> Conversation | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE id = ?", (convo_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                return Conversation(**d)

    async def get_conversations_by_status(self, status: str) -> list[Conversation]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                convos = []
                for r in rows:
                    d = dict(r)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convos.append(Conversation(**d))
                return convos

    async def update_conversation(self, convo_id: str, **kwargs):
        """Update whitelisted conversation columns atomically (bumps updated_at)."""
        if not kwargs:
            return
        invalid = set(kwargs) - _CONVERSATION_COLUMNS
        if invalid:
            raise ValueError(f"Invalid conversation column(s): {sorted(invalid)}")
        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE conversations SET {set_clause}, updated_at = ? WHERE id = ?",
                (*kwargs.values(), _utcnow().isoformat(), convo_id),
            )
            await db.commit()

    # ── Actions Log ──

    async def log_action(self, action_type: str, agent: str, details: dict | None = None):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                (_new_id(), action_type, agent, json.dumps(details or {})),
            )
            await db.commit()

    # ── Analytics ──

    async def get_campaign_stats(self) -> list[dict]:
        """Get performance stats for each campaign (one indexed pass over conversations)."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.status, c.prospect_ids_json,
                          COUNT(v.id) as reply_count,
                          SUM(CASE WHEN v.intent = 'interested' THEN 1 ELSE 0 END) as interested_count,
                          SUM(CASE WHEN v.intent = 'objection' THEN 1 ELSE 0 END) as objection_count,
                          SUM(CASE WHEN v.intent = 'not_interested' THEN 1 ELSE 0 END) as not_interested_count
                   FROM campaigns c
                   LEFT JOIN conversations v ON v.campaign_id = c.id
                   WHERE c.status IN ('active', 'completed')
                   GROUP BY c.id
                   ORDER BY c.created_at DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                stats = []
                for r in rows:
                    d = dict(r)
                    for key in ("interested_count", "objection_count", "not_interested_count"):
                        d[key] = d[key] or 0
                    prospect_ids = json.loads(d.get("prospect_ids_json") or "[]")
                    d["leads_count"] = len(prospect_ids)
                    d["reply_rate"] = (
                        round(d["reply_count"] / len(prospect_ids) * 100, 1)
                        if prospect_ids else 0
                    )
                    stats.append(d)
                return stats

    async def get_intent_distribution(self) -> dict[str, int]:
        """Count conversations by intent."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY intent"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_stage_distribution(self) -> dict[str, int]:
        """Count conversations by sales stage."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT stage, COUNT(*) FROM conversations WHERE stage != '' GROUP BY stage"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Usage Tracking ──

    async def get_usage_today(self, provider: str = "claude") -> int:
        """Brain calls made today on one provider's subscription."""
        column = _USAGE_LOG_COLUMN[provider]
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                f"SELECT {column} FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return (row[0] or 0) if row else 0

    async def increment_usage(self, provider: str = "claude"):
        column = _USAGE_LOG_COLUMN[provider]
        today = date.today().isoformat()
        async with self._connect() as db:
            await db.execute(
                f"""INSERT INTO usage_log (id, date, {column})
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET {column} = {column} + 1""",
                (_new_id(), today),
            )
            await db.commit()

    # ── Per-call usage accounting (usage_events) ──

    async def record_usage_event(
        self,
        *,
        agent: str = "",
        task: str = "",
        session_id: str = "",
        request_key: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        num_turns: int = 0,
        is_error: bool = False,
        source: str = "result_json",
        created_at: str | None = None,
        provider: str = "claude",
    ) -> bool:
        """Insert one usage row (one model within one brain call).

        Returns False when the row was skipped as a duplicate (request_key
        uniqueness makes transcript reconciliation idempotent).
        """
        if provider not in BRAIN_PROVIDERS:
            raise ValueError(f"unknown brain provider: {provider!r}")
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO usage_events
                   (id, created_at, agent, task, session_id, request_key, model,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cost_usd, duration_ms, num_turns,
                    is_error, source, provider)
                   VALUES (?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _new_id(), created_at, agent, task, session_id, request_key,
                    model, int(input_tokens or 0), int(output_tokens or 0),
                    int(cache_read_tokens or 0), int(cache_creation_tokens or 0),
                    float(cost_usd or 0.0), int(duration_ms or 0),
                    int(num_turns or 0), 1 if is_error else 0, source, provider,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    _USAGE_SUM = (
        "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
        "SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, "
        "SUM(cache_read_tokens) AS cache_read_tokens, "
        "SUM(cache_creation_tokens) AS cache_creation_tokens, "
        "SUM(cost_usd) AS cost_usd"
    )

    @staticmethod
    def _usage_row_to_dict(row: aiosqlite.Row) -> dict:
        d = dict(row)
        for key, value in d.items():
            if value is None and key != "period":
                d[key] = 0
        if "cost_usd" in d:
            d["cost_usd"] = round(float(d["cost_usd"] or 0.0), 6)
        return d

    async def _usage_grouped(self, group_expr: str, alias: str, days: int) -> list[dict]:
        sql = (
            f"SELECT {group_expr} AS {alias}, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY {alias} ORDER BY cost_usd DESC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_by_agent(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent", days
        )

    async def usage_by_task(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN task = '' THEN 'other' ELSE task END", "task", days
        )

    async def usage_by_model(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped(
            "CASE WHEN model = '' THEN 'unknown' ELSE model END", "model", days
        )

    async def usage_by_provider(self, days: int = 30) -> list[dict]:
        return await self._usage_grouped("provider", "provider", days)

    async def usage_by_day(self, days: int = 30) -> list[dict]:
        sql = (
            f"SELECT date(created_at) AS day, {self._USAGE_SUM} "
            f"FROM usage_events "
            f"WHERE created_at >= datetime('now', ?) "
            f"GROUP BY day ORDER BY day ASC"
        )
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (f"-{int(days)} days",)) as cursor:
                return [self._usage_row_to_dict(r) for r in await cursor.fetchall()]

    async def usage_totals(self) -> dict:
        """Rollups for today / last 7 days / last 30 days (UTC)."""
        totals = {}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            for label, where in (
                ("today", "date(created_at) = date('now')"),
                ("week", "created_at >= datetime('now', '-7 days')"),
                ("month", "created_at >= datetime('now', '-30 days')"),
            ):
                async with db.execute(
                    f"SELECT {self._USAGE_SUM} FROM usage_events WHERE {where}"
                ) as cursor:
                    row = await cursor.fetchone()
                    totals[label] = self._usage_row_to_dict(row) if row else {}
        return totals

    # ── Summary for Decision Making ──

    async def get_state_summary(self) -> dict:
        prospect_counts = await self.count_prospects_by_status()
        today = date.today().isoformat()
        async with self._connect() as db:
            async with db.execute(
                """SELECT
                     SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END)
                   FROM campaigns"""
            ) as cursor:
                row = await cursor.fetchone()
                draft_campaigns = row[0] or 0
                active_campaigns = row[1] or 0
            async with db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
            ) as cursor:
                open_conversations = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT claude_calls, codex_calls, openai_calls FROM usage_log WHERE date = ?",
                (today,)
            ) as cursor:
                usage_row = await cursor.fetchone()
                usage_today = (usage_row[0] or 0) if usage_row else 0
                codex_today = (usage_row[1] or 0) if usage_row else 0
                openai_today = (usage_row[2] or 0) if usage_row else 0

        return {
            "prospects": prospect_counts,
            "draft_campaigns": draft_campaigns,
            "active_campaigns": active_campaigns,
            "open_conversations": open_conversations,
            "usage_today": usage_today,  # Claude calls (kept for compatibility)
            "usage_today_by_provider": {"claude": usage_today, "codex": codex_today,
                                        "openai": openai_today},
            # Companies discovered but not yet read. The heartbeat profiles
            # these every cycle — it is free, so it never competes with the
            # Claude budget the rest of the loop is rationing.
            "unprofiled": await self.count_companies_needing_profile(),
        }
