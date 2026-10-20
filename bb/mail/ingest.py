"""Storing synced messages: once each, threaded, with the reply state and
the per-mailbox counts kept current. Provider ids make a re-sync a no-op."""

import json
import re

from bb.mail.db import MailDB, new_id, now_iso
from bb.mail.providers.base import RawMessage
from bb.mail.state import COUNTED, compute_state
from harvey.integrations.mail_provider import looks_automated

_RE_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv|vs)\s*:\s*)+", re.I)


def normalise_subject(subject: str) -> str:
    return _RE_PREFIX.sub("", subject or "").strip().lower()


def _snippet(text: str, n: int = 160) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


class Ingest:
    def __init__(self, db: MailDB, mailbox: dict):
        self.db = db
        self.mailbox = mailbox
        self.mailbox_id = mailbox["id"]
        self.me = {(mailbox.get("address") or "").lower()}
        for alias in (mailbox.get("settings") or {}).get("aliases", []) or []:
            self.me.add(str(alias).lower())

    async def store_batch(self, batch: list[RawMessage]) -> int:
        """Insert what is new; returns how many were new. Threads touched
        get their aggregates and state recomputed."""
        touched: set[str] = set()
        new = 0
        async with self.db._connect() as db:
            await db.execute("PRAGMA busy_timeout=15000")
            for raw in batch:
                if not raw.provider_id:
                    continue
                async with db.execute(
                    "SELECT thread_id FROM messages WHERE mailbox_id = ? AND provider_id = ?",
                    (self.mailbox_id, raw.provider_id)) as cur:
                    row = await cur.fetchone()
                if row:
                    continue
                if raw.message_id:
                    async with db.execute(
                        "SELECT thread_id FROM messages WHERE mailbox_id = ? AND message_id = ?",
                        (self.mailbox_id, raw.message_id)) as cur:
                        dup = await cur.fetchone()
                    if dup:
                        continue   # the same email seen through another folder or the send path
                thread_id = await self._thread_for(db, raw)
                outbound = raw.outbound or (raw.from_email or "").lower() in self.me
                automated = (not outbound) and looks_automated(raw.from_email, raw.subject, raw.headers)
                await db.execute(
                    "INSERT OR IGNORE INTO messages(id, mailbox_id, thread_id, provider_id, message_id, in_reply_to, "
                    "references_text, direction, sent_by, from_email, from_name, to_json, cc_json, subject, snippet, "
                    "body, headers_json, folder, sent_at, automated, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_id(), self.mailbox_id, thread_id, raw.provider_id, raw.message_id or "", raw.in_reply_to or "",
                     raw.references or "", "outbound" if outbound else "inbound", "me" if outbound else "them",
                     (raw.from_email or "").lower(), raw.from_name or "", json.dumps(raw.to or []),
                     json.dumps(raw.cc or []), raw.subject or "", raw.snippet or _snippet(raw.body),
                     (raw.body or "")[:20000], json.dumps({k.lower(): v for k, v in (raw.headers or {}).items()})[:8000],
                     raw.folder or "", raw.sent_at or "", 1 if automated else 0, now_iso()))
                touched.add(thread_id)
                new += 1
            await db.commit()
        for thread_id in touched:
            await self.refresh_thread(thread_id)
        if touched:
            await self.refresh_counts()
        return new

    async def _thread_for(self, db, raw: RawMessage) -> str:
        """The thread this message belongs to: the provider's thread id when
        it has one, else the thread of the message it answers, else a new
        thread."""
        key = raw.thread_key or ""
        if key:
            async with db.execute("SELECT id FROM threads WHERE mailbox_id = ? AND provider_thread_id = ?",
                                  (self.mailbox_id, key)) as cur:
                row = await cur.fetchone()
            if row:
                return row[0]
        refs = [t for t in re.findall(r"<[^>]+>", f"{raw.in_reply_to} {raw.references}")]
        for ref in reversed(refs):
            async with db.execute("SELECT thread_id FROM messages WHERE mailbox_id = ? AND message_id = ? LIMIT 1",
                                  (self.mailbox_id, ref)) as cur:
                row = await cur.fetchone()
            if row:
                return row[0]
        thread_id = new_id()
        now = now_iso()
        await db.execute(
            "INSERT INTO threads(id, mailbox_id, provider_thread_id, subject, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, self.mailbox_id, key or raw.message_id or thread_id, raw.subject or "(no subject)", now, now))
        return thread_id

    async def refresh_thread(self, thread_id: str):
        rows = await self.db.query(
            "SELECT id, direction, from_email, from_name, to_json, subject, snippet, sent_at, automated, headers_json "
            "FROM messages WHERE thread_id = ? ORDER BY sent_at, created_at", (thread_id,))
        if not rows:
            await self.db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            return
        for r in rows:
            try:
                r["headers"] = json.loads(r.get("headers_json") or "{}")
            except ValueError:
                r["headers"] = {}
        current = await self.db.one("SELECT state FROM threads WHERE id = ?", (thread_id,))
        state, reason = compute_state(rows, (current or {}).get("state", ""))
        last = rows[-1]
        participants: dict[str, dict] = {}
        for r in rows:
            if r["from_email"]:
                participants.setdefault(r["from_email"], {"email": r["from_email"], "name": r["from_name"],
                                                          "role": "me" if r["from_email"] in self.me else "them"})
            for addr in json.loads(r.get("to_json") or "[]"):
                email = (addr.get("email") if isinstance(addr, dict) else str(addr)).lower()
                if email:
                    participants.setdefault(email, {"email": email, "name": (addr.get("name") if isinstance(addr, dict) else ""),
                                                    "role": "me" if email in self.me else "them"})
        await self.db.execute(
            "UPDATE threads SET state = ?, state_reason = ?, last_message_at = ?, last_direction = ?, last_from = ?, "
            "message_count = ?, snippet = ?, participants_json = ?, subject = CASE WHEN subject = '' OR subject = '(no subject)' "
            "THEN ? ELSE subject END, updated_at = ? WHERE id = ?",
            (state, reason, last["sent_at"], last["direction"], last["from_name"] or last["from_email"], len(rows),
             last["snippet"], json.dumps(list(participants.values())), rows[0]["subject"] or "(no subject)",
             now_iso(), thread_id))
        async with self.db._connect() as db:
            await db.execute("DELETE FROM participants WHERE thread_id = ?", (thread_id,))
            for p in participants.values():
                await db.execute("INSERT OR IGNORE INTO participants(thread_id, email, name, role) VALUES (?, ?, ?, ?)",
                                 (thread_id, p["email"], p["name"] or "", p["role"]))
            await db.commit()

    async def refresh_counts(self) -> dict:
        rows = await self.db.query("SELECT state, COUNT(*) AS n FROM threads WHERE mailbox_id = ? GROUP BY state",
                                   (self.mailbox_id,))
        counts = {s: 0 for s in COUNTED}
        for r in rows:
            if r["state"] in counts:
                counts[r["state"]] = r["n"]
        total = await self.db.one("SELECT COUNT(*) AS n FROM messages WHERE mailbox_id = ?", (self.mailbox_id,))
        counts["total"] = total["n"] if total else 0
        counts["threads"] = sum(r["n"] for r in rows)
        await self.db.update_mailbox(self.mailbox_id, counts=counts)
        return counts
