"""The command bar: plain language in, actions out. On Anthropic the model
drives real tools (list accounts, counts, threads, summaries, drafts,
reply runs, sends) in a short loop. On any other provider the same
actions are reached through a JSON protocol the model must follow; when
it cannot, the bar says so instead of guessing. Anything that sends mail
is never done on the model's say-so: it becomes a pending action with the
exact accounts and counts, and a person confirms it."""

import json

from bb.mail import service
from bb.mail.brain import BrainError, MailBrain, extract_json
from bb.mail.db import MailDB, new_id, now_iso
from bb.mail.jobs import Jobs

MAX_ROUNDS = 8
HISTORY_KEY = "command_history"
PENDING_KEY = "command_pending"
HISTORY_KEEP = 12

TOOLS = [
    {"name": "list_accounts", "description": "The connected mailboxes: address, status, whether it can send, unreplied count. "
                                             "The accounts in scope are listed in the system prompt; use this for details.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_counts", "description": "Thread counts per state (needs_reply, awaiting_them, replied, no_reply_needed) for the "
                                          "accounts in scope, or for the accounts named.",
     "input_schema": {"type": "object", "properties": {"accounts": {"type": "array", "items": {"type": "string"},
                                                                    "description": "Mailbox addresses; empty = the scope"}}}},
    {"name": "list_threads", "description": "Threads in a state, newest first. Returns id, subject, who wrote last, when, a snippet.",
     "input_schema": {"type": "object", "properties": {
         "state": {"type": "string", "enum": ["needs_reply", "awaiting_them", "replied", "no_reply_needed", "snoozed", "all"]},
         "accounts": {"type": "array", "items": {"type": "string"}}, "query": {"type": "string", "description": "text to match"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}},
    {"name": "summarize_thread", "description": "The messages of one thread, oldest first, trimmed, so you can summarise or answer questions about it.",
     "input_schema": {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]}},
    {"name": "generate_draft", "description": "Draft a reply to a thread with the writing check. The draft waits on the Review page; "
                                              "nothing is sent. Returns the draft id, body and confidence.",
     "input_schema": {"type": "object", "properties": {"thread_id": {"type": "string"}, "instructions": {"type": "string"}},
                      "required": ["thread_id"]}},
    {"name": "start_reply_run", "description": "Reply to many threads at once. thread_ids empty = every thread that needs a reply "
                                               "in the accounts. Mode review drafts and holds; dry_run drafts and sends nothing; "
                                               "auto sends the confident ones and NEEDS the person's confirmation (returned as pending).",
     "input_schema": {"type": "object", "properties": {
         "thread_ids": {"type": "array", "items": {"type": "string"}}, "accounts": {"type": "array", "items": {"type": "string"}},
         "mode": {"type": "string", "enum": ["review", "auto", "dry_run"]}, "instructions": {"type": "string"},
         "reply_to": {"type": "string"}}}},
    {"name": "send_reply", "description": "Send a draft that is waiting for review. NEEDS the person's confirmation (returned as pending).",
     "input_schema": {"type": "object", "properties": {"draft_id": {"type": "string"}}, "required": ["draft_id"]}},
]

SYSTEM = """You are the command bar of a mail command center. You act on the person's connected mailboxes with the tools; you never invent data.
Be brief and concrete: say what you found or did in one to four plain sentences, name accounts by address and give counts. No markdown headings, no bullet lists longer than five items.
SCOPE: the accounts the person has selected are:
{scope}
Work on those unless the person names other accounts in the request (an address, or a word in it like "the sales mailbox"); then pass those as `accounts`.
Anything that sends email (an auto reply run, send_reply) is only prepared: the tool returns a pending action with the exact accounts and counts, and the person confirms it on screen. Say that it is waiting for their confirmation. Review and dry-run reply runs start at once.
If a request is not about the mailboxes, say so in one sentence."""

FALLBACK = """You are the command bar of a mail command center and you can only act through the ACTIONS below.
Reply with ONE JSON object and nothing else. Either call an action: {{"action": "<name>", "args": {{...}}}} or answer the person: {{"answer": "<one to four plain sentences>"}}.
Call one action per reply; you will get its result and can then call another or answer.
ACTIONS:
{actions}
SCOPE (accounts the person selected; use them unless the request names others):
{scope}
Sending email (auto reply runs, send_reply) only prepares a pending action the person confirms on screen; review and dry-run reply runs start at once."""


class CommandCenter:
    def __init__(self, db: MailDB, brain: MailBrain, mailboxes: list[dict], selected: list[dict], secret: str = "",
                 env: dict | None = None, user_email: str = "", on_worker_needed=None, scope: dict | None = None,
                 scope_for=None):
        self.db, self.brain, self.secret, self.env = db, brain, secret, env or {}
        self.scope = scope
        # address -> the goal and brief that account writes with (its outreach campaign's, else the main one)
        self.scope_for = scope_for
        self.mailboxes, self.selected, self.user_email = mailboxes, selected, user_email
        self.on_worker_needed = on_worker_needed
        self.steps: list[dict] = []
        self.pending: dict | None = None

    # ── resolving what the person means ──

    def _accounts(self, names) -> list[dict]:
        if not names:
            return list(self.selected)
        out = []
        for n in names:
            n = str(n or "").strip().lower()
            for m in self.mailboxes:
                addr = m["address"].lower()
                if n and (n == m["id"] or n == addr or n == addr.split("@")[0] or n in addr) and m not in out:
                    out.append(m)
        return out

    @staticmethod
    def _scope_text(boxes: list[dict]) -> str:
        if not boxes:
            return "(none selected: ask the person to connect or select a mailbox)"
        return "\n".join(f"- {m['address']} (id {m['id']}; {'can send' if m.get('ready') else 'cannot send yet: ' + str(m.get('ready_why') or '')}; "
                         f"{(m.get('counts') or {}).get('needs_reply', 0)} unreplied)" for m in boxes)

    # ── the tools ──

    async def list_accounts(self, **_):
        return [{"id": m["id"], "address": m["address"], "status": m["status"], "can_send": bool(m.get("ready")),
                 "why_not": "" if m.get("ready") else m.get("ready_why", ""), "counts": m.get("counts") or {}}
                for m in self.mailboxes]

    async def get_counts(self, accounts=None, **_):
        boxes = self._accounts(accounts)
        out = {"accounts": [], "total": {}}
        for m in boxes:
            c = m.get("counts") or {}
            out["accounts"].append({"address": m["address"], **c})
            for k, v in c.items():
                out["total"][k] = out["total"].get(k, 0) + (v or 0)
        return out

    async def list_threads(self, state="needs_reply", accounts=None, query="", limit=20, **_):
        boxes = self._accounts(accounts)
        ids = [m["id"] for m in boxes]
        where, params = [f"mailbox_id IN ({','.join('?' for _ in ids) or chr(39) * 2})"], list(ids)
        if state and state != "all":
            where.append("state = ?"); params.append(state)
        if query:
            like = f"%{query}%"
            where.append("(subject LIKE ? OR last_from LIKE ? OR snippet LIKE ?)"); params += [like, like, like]
        rows = await self.db.query(f"SELECT id, mailbox_id, subject, last_from, last_message_at, snippet, state FROM threads "
                                   f"WHERE {' AND '.join(where)} ORDER BY last_message_at DESC LIMIT ?", (*params, max(1, min(int(limit or 20), 50))))
        names = {m["id"]: m["address"] for m in self.mailboxes}
        return [{"id": r["id"], "account": names.get(r["mailbox_id"], ""), "subject": r["subject"], "from": r["last_from"],
                 "when": r["last_message_at"], "state": r["state"], "snippet": (r["snippet"] or "")[:160]} for r in rows]

    async def summarize_thread(self, thread_id="", **_):
        t = await self.db.one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not t:
            return {"error": "no such thread"}
        box = await self.db.mailbox(t["mailbox_id"])
        msgs = await self.db.query("SELECT direction, from_name, from_email, sent_at, body FROM messages WHERE thread_id = ? "
                                   "ORDER BY sent_at, created_at LIMIT 12", (thread_id,))
        return {"id": t["id"], "account": box["address"] if box else "", "subject": t["subject"], "state": t["state"],
                "messages": [{"who": ("me" if m["direction"] == "outbound" else (m["from_name"] or m["from_email"])),
                              "when": m["sent_at"], "text": (m["body"] or "")[:700]} for m in msgs]}

    async def generate_draft(self, thread_id="", instructions="", **_):
        from bb.mail.drafting import draft_reply
        t = await self.db.one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not t:
            return {"error": "no such thread"}
        box = await self.db.mailbox(t["mailbox_id"])
        messages = await self.db.query("SELECT * FROM messages WHERE thread_id = ? ORDER BY sent_at, created_at", (thread_id,))
        them = next((m for m in reversed(messages) if m["direction"] == "inbound"), None)
        if not them:
            return {"error": "there is no message from them to answer"}
        from bb.mail import runs as runs_mod
        hard_no = next((m for m in messages if m["direction"] == "inbound" and runs_mod.is_opt_out(m)), None)
        if hard_no:
            await runs_mod.suppress(self.db, box, hard_no["from_email"], thread_id)
            return {"error": f"{hard_no['from_email']} asked not to be contacted; nothing drafted, and the address is on the excluded list"}
        result = await draft_reply(self.brain, service.settings_of(box), box, t, messages, instructions=instructions or "",
                                   scope=self.scope_for(box["address"]) if self.scope_for else self.scope)
        if result.get("stance") == "opt_out":
            await runs_mod.suppress(self.db, box, them["from_email"], thread_id)
            return {"error": f"{them['from_email']} asked not to be contacted (the model's reading); nothing drafted, "
                             "and the address is on the excluded list"}
        subject = t["subject"] if t["subject"].lower().startswith("re:") else f"Re: {t['subject']}"
        draft_id, now = new_id(), now_iso()
        await self.db.execute(
            "INSERT INTO drafts(id, mailbox_id, thread_id, kind, to_json, reply_to, subject, body, in_reply_to, references_text, "
            "status, qa_json, confidence, created_at, updated_at) VALUES (?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)",
            (draft_id, box["id"], thread_id, json.dumps([{"name": them["from_name"], "email": them["from_email"]}]),
             service.settings_of(box).reply_to, subject[:200], result["body"], them.get("message_id") or "",
             them.get("references_text") or "", json.dumps({**result["qa"], "attempts": result["attempts"],
                                                            "confidence_reason": result["confidence_reason"]}),
             result["confidence"], now, now))
        return {"draft_id": draft_id, "account": box["address"], "to": them["from_email"], "subject": subject, "body": result["body"],
                "confidence": result["confidence"], "confidence_reason": result["confidence_reason"],
                "writing_check": "passed" if result["qa"].get("ok") else "; ".join(result["qa"].get("reasons", [])),
                "note": "waiting on the Review page; not sent"}

    async def start_reply_run(self, thread_ids=None, accounts=None, mode="review", instructions="", reply_to="", **_):
        from bb.mail import runs
        boxes = self._accounts(accounts)
        if not boxes:
            return {"error": "no accounts match"}
        if mode not in runs.MODES:
            mode = "review"
        if thread_ids:
            marks = ",".join("?" for _ in thread_ids)
            rows = await self.db.query(f"SELECT id, mailbox_id FROM threads WHERE id IN ({marks}) AND state = 'needs_reply'", list(thread_ids))
        else:
            rows = [t for t in await runs.candidates(self.db, boxes) if not t["excluded"]]
        rows = [r for r in rows if r["mailbox_id"] in {m["id"] for m in boxes}]
        taken = await runs.threads_in_live_runs(self.db)
        in_runs = sum(1 for r in rows if r["id"] in taken)
        rows = [r for r in rows if r["id"] not in taken]
        if not rows:
            return {"error": "nothing needs a reply in those accounts"
                    + (f" that is not already in a run ({in_runs} are waiting in one)" if in_runs else "")}
        if in_runs:
            instructions_note = f" ({in_runs} more are already waiting in another run and are left to it)"
        else:
            instructions_note = ""
        per = {}
        for r in rows:
            per[r["mailbox_id"]] = per.get(r["mailbox_id"], 0) + 1
        names = {m["id"]: m for m in self.mailboxes}
        breakdown = [{"account": names[k]["address"], "threads": v, "can_send": bool(names[k].get("ready"))} for k, v in per.items()]
        args = {"thread_ids": [r["id"] for r in rows], "mode": mode, "instructions": instructions or "", "reply_to": reply_to or "",
                "mailbox_ids": list(per)}
        if mode == "auto":
            cannot = [b["account"] for b in breakdown if not b["can_send"]]
            if cannot:
                return {"error": "these accounts cannot send yet (no signature or project link): " + ", ".join(cannot)}
            return self._propose("start_reply_run", args,
                                 f"Reply to {len(rows)} thread{'s' if len(rows) != 1 else ''} and send the confident ones: "
                                 + ", ".join(f"{b['threads']} from {b['account']}" for b in breakdown) + instructions_note
                                 + (f". Instructions: {instructions}" if instructions else "") + (f". Reply-To {reply_to}" if reply_to else ""),
                                 breakdown)
        run = await self._do_start_reply_run(args)
        return {"run_id": run["id"], "total": run["total"], "mode": mode, "accounts": breakdown,
                "note": "started; drafts wait on the Review page" if mode == "review" else "started as a dry run; nothing will be sent"}

    async def _do_start_reply_run(self, args: dict) -> dict:
        from bb.mail import runs
        boxes = [m for m in self.mailboxes if m["id"] in set(args.get("mailbox_ids") or [])] or self.mailboxes
        run = await runs.create_run(self.db, boxes, args["thread_ids"], mode=args["mode"], reply_to=args.get("reply_to", ""),
                                    instructions=args.get("instructions", ""), created_by=self.user_email)
        if not run:
            raise BrainError("Nothing in that list still needs a reply (or it is already in a run that has not finished).")
        if self.on_worker_needed:
            self.on_worker_needed()
        return run

    async def send_reply(self, draft_id="", **_):
        d = await self.db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        if not d:
            return {"error": "no such draft"}
        if d["status"] in ("sent", "dry_run"):
            return {"error": "that draft was already sent"}
        box = await self.db.mailbox(d["mailbox_id"])
        ok, why = service.ready_to_send(box) if box else (False, "mailbox missing")
        if not ok:
            return {"error": why}
        to = json.loads(d.get("to_json") or "[]")
        who = (to[0].get("name") or to[0].get("email")) if to else "?"
        return self._propose("send_reply", {"draft_id": draft_id},
                             f"Send the reply to {who} — \"{d['subject']}\" from {box['address']}",
                             [{"account": box["address"], "threads": 1, "can_send": True}])

    async def _do_send_reply(self, args: dict) -> dict:
        d = await self.db.one("SELECT * FROM drafts WHERE id = ?", (args["draft_id"],))
        if not d or d["status"] in ("sent", "dry_run"):
            raise BrainError("That draft is gone or already sent.")
        await self.db.execute("UPDATE drafts SET status = 'approved', error = '', updated_at = ? WHERE id = ?", (now_iso(), d["id"]))
        await Jobs(self.db).enqueue("send_draft", d["mailbox_id"], {"draft_id": d["id"]}, max_attempts=4)
        if self.on_worker_needed:
            self.on_worker_needed()
        return {"queued": True}

    def _propose(self, kind: str, args: dict, description: str, accounts: list[dict]) -> dict:
        self.pending = {"id": new_id(), "kind": kind, "args": args, "description": description, "accounts": accounts,
                        "created_at": now_iso()}
        return {"pending": True, "action_id": self.pending["id"], "description": description, "accounts": accounts,
                "note": "waiting for the person to confirm on screen; do not call again"}

    # ── running a request ──

    async def call(self, name: str, args: dict):
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            return {"error": f"unknown action {name}"}
        try:
            result = await fn(**(args or {}))
        except BrainError as e:
            result = {"error": str(e)}
        except TypeError as e:
            result = {"error": f"bad arguments: {e}"}
        self.steps.append({"tool": name, "args": args, "result": result})
        return result

    async def run(self, text: str) -> dict:
        """{reply, pending, steps}. Never raises for model trouble: the reply says what went wrong."""
        text = (text or "").strip()
        if not text:
            return {"reply": "Say what you want done, for example: how many need a reply? or reply to everything in sales@ for review.",
                    "pending": None, "steps": []}
        if not self.brain.configured:
            return {"reply": "No AI provider is set up yet. Add one on the AI provider page and the command bar will work.",
                    "pending": None, "steps": []}
        try:
            reply = await (self._run_tools(text) if self.brain.supports_tools else self._run_fallback(text))
        except BrainError as e:
            reply = f"The model could not finish that: {e}"
        return {"reply": reply, "pending": self.pending, "steps": self.steps}

    async def _run_tools(self, text: str) -> str:
        system = SYSTEM.format(scope=self._scope_text(self.selected))
        messages = [{"role": "user", "content": text}]
        for _ in range(MAX_ROUNDS):
            turn = await self.brain.tool_turn(system, messages, TOOLS)
            if not turn["tool_calls"]:
                return turn["text"].strip() or "Done."
            messages.append({"role": "assistant", "content": turn["content"]})
            results = []
            for call in turn["tool_calls"]:
                result = await self.call(call["name"], call["input"])
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(result, default=str)[:6000]})
            messages.append({"role": "user", "content": results})
            if self.pending:
                messages.append({"role": "user", "content": "The action is pending the person's confirmation on screen. Tell them so in one or two sentences and stop."})
                turn = await self.brain.tool_turn(system, messages, TOOLS)
                return turn["text"].strip() or f"Waiting for your confirmation: {self.pending['description']}."
        return "I stopped after several steps without a final answer. " + (self.steps[-1]["tool"].replace("_", " ") + " was the last thing done." if self.steps else "")

    async def _run_fallback(self, text: str) -> str:
        actions = "\n".join(f"- {t['name']}({', '.join(t['input_schema'].get('properties', {}))}): {t['description']}" for t in TOOLS)
        system = FALLBACK.format(actions=actions, scope=self._scope_text(self.selected))
        transcript = f"PERSON: {text}"
        cant = ("I could not turn that into an action with this AI provider. Try something plainer, like "
                "\"how many need a reply?\", \"list what needs a reply in sales@\", or \"reply to everything for review\". "
                "Anthropic as the provider gives the command bar full tool use.")
        for _ in range(MAX_ROUNDS):
            raw = await self.brain.complete(system, transcript, max_tokens=800)
            data = extract_json(raw)
            if not isinstance(data, dict):
                return cant
            if "answer" in data and "action" not in data:
                return str(data["answer"]).strip() or "Done."
            name = str(data.get("action") or "")
            if name not in {t["name"] for t in TOOLS}:
                return cant
            result = await self.call(name, data.get("args") or {})
            transcript += f"\nACTION {name} -> {json.dumps(result, default=str)[:4000]}"
            if self.pending:
                return f"Waiting for your confirmation: {self.pending['description']}."
            transcript += "\nNow call another action or answer the person."
        return "I stopped after several steps without an answer."


# ── persistence for the page: history and the one pending action ──

async def history(db: MailDB) -> list[dict]:
    try:
        return json.loads(await db.get_setting(HISTORY_KEY, "[]"))
    except ValueError:
        return []


async def remember(db: MailDB, question: str, reply: str, steps: list[dict]):
    items = await history(db)
    items.append({"q": question, "a": reply, "at": now_iso(), "tools": [s["tool"] if isinstance(s, dict) else str(s) for s in steps]})
    await db.set_setting(HISTORY_KEY, json.dumps(items[-HISTORY_KEEP:]))


async def pending(db: MailDB) -> dict | None:
    try:
        data = json.loads(await db.get_setting(PENDING_KEY, "null"))
    except ValueError:
        data = None
    return data or None


async def set_pending(db: MailDB, action: dict | None):
    await db.set_setting(PENDING_KEY, json.dumps(action) if action else "null")


async def confirm(center: CommandCenter, action: dict) -> str:
    """Execute a confirmed pending action; returns the line for the page."""
    if action["kind"] == "start_reply_run":
        run = await center._do_start_reply_run(action["args"])
        return f"Run started: {run['total']} threads. /mail/runs/{run['id']}"
    if action["kind"] == "send_reply":
        await center._do_send_reply(action["args"])
        return "Sending."
    raise BrainError("Unknown action.")
