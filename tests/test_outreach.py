"""Inquiry mode, standing reply instructions, attachments on replies, and the
writer's drafting throttle."""

import asyncio
import json
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.agents.sender as sender_mod
from harvey.agents.handler import Handler
from harvey.agents.writer import Writer
from harvey.config import EnvConfig, HarveyConfig, OutreachConfig
from harvey.integrations.mail_provider import attach_files
from harvey.models.conversation import Conversation
from harvey.models.prospect import Prospect
from harvey.state import StateManager

BASE = {
    "persona": {"name": "Jane", "company": "Acme", "role": "Founder", "email": "jane@acme.test",
                "linkedin": "", "tone": "direct"},
    "product": {"name": "Acme", "description": "D", "pricing": "", "key_benefits": ["fast"],
                "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": [], "auto_prospect": False},
    "channels": {"email": {"provider": "smtp", "require_approval": False, "max_daily_sends": 1}},
}


def _config(**outreach) -> HarveyConfig:
    return HarveyConfig(**{**BASE, "outreach": outreach})


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "t.db"))
    await sm.init_db()
    yield sm


class _Brain:
    """Records prompts; answers with a fixed draft."""

    def __init__(self):
        self.prompts = []
        self.paused_reason = ""

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return f"[{name} prompt]"

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return "Thanks, attached is the brief."

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        if "3-email" in prompt:  # the per-batch sequence (follow-ups)
            return [{"step": i, "subject": f"step {i}", "body": "{{first_name}} hi", "delay_days": d}
                    for i, d in ((1, 0), (2, 3), (3, 4))]
        return {"subject": "hello", "body": "hi"}


def test_outreach_config_validation():
    assert HarveyConfig(**BASE).outreach == OutreachConfig()
    cfg = _config(mode="Inquiry", intent="hire an advisor", reply_attachments=["a.pdf", "../x", ".hidden", "b c.docx"])
    assert cfg.outreach.mode == "inquiry"
    assert cfg.outreach.reply_attachments == ["a.pdf", "b c.docx"]  # names only, never paths
    with pytest.raises(ValidationError):
        _config(mode="spam")
    with pytest.raises(ValidationError):
        _config(attach_on="sometimes")


def test_attach_files_builds_mime_parts(tmp_path):
    (tmp_path / "brief.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "notes.txt").write_text("hello")
    msg = EmailMessage()
    msg["To"] = "x@y.z"
    msg.set_content("body")
    names = attach_files(msg, [tmp_path / "brief.pdf", tmp_path / "notes.txt", tmp_path / "missing.doc"])
    assert names == ["brief.pdf", "notes.txt"]  # the missing one is skipped, not fatal
    parsed = message_from_bytes(msg.as_bytes())
    parts = [(p.get_filename(), p.get_content_type()) for p in parsed.walk() if p.get_filename()]
    assert ("brief.pdf", "application/pdf") in parts and ("notes.txt", "text/plain") in parts


def test_sender_resolves_attachment_names_inside_the_home_folder(tmp_path, monkeypatch):
    import harvey.paths as paths
    folder = tmp_path / "attachments"
    folder.mkdir()
    (folder / "deck.pdf").write_bytes(b"x")
    monkeypatch.setattr(paths, "ATTACHMENTS_DIR", folder)
    paths_out = sender_mod._attachment_paths(json.dumps(["deck.pdf", "../secrets.json", "gone.pdf"]))
    assert paths_out == [folder / "deck.pdf"]
    assert sender_mod._attachment_paths(None) == [] and sender_mod._attachment_paths("not json") == []


@pytest.mark.asyncio
async def test_outbox_rows_carry_attachments(state):
    item_id = await state.add_outbox_item(prospect_id="p", to_email="a@b.co", subject="s", body="b",
                                          send_at="2026-01-01T00:00:00", kind="reply",
                                          conversation_id="c1", attachments=["deck.pdf"])
    item = await state.get_outbox_item(item_id)
    assert json.loads(item["attachments_json"]) == ["deck.pdf"]
    plain = await state.add_outbox_item(prospect_id="p", to_email="a@b.co", subject="s", body="b",
                                        send_at="2026-01-01T00:00:00", conversation_id="c1")
    assert json.loads((await state.get_outbox_item(plain))["attachments_json"]) == []
    assert await state.count_outbox_for_conversation("c1", kind="reply") == 1
    assert await state.count_outbox_queued() == 1  # the sequence item


@pytest.mark.asyncio
async def test_handler_reply_gets_instructions_and_attachments(state):
    cfg = _config(mode="inquiry", intent="find a financial advisor",
                  reply_instructions="Offer a call on Tuesday afternoons.",
                  reply_attachments=["brief.pdf"], attach_on="first_reply")
    brain = _Brain()
    handler = Handler(brain, state, cfg, EnvConfig())
    prospect = Prospect(id="p1", first_name="Sam", last_name="Lee", email="sam@adv.co",
                        email_status="provided", title="Advisor", company="Adv Co")
    await state.add_prospect(prospect)
    convo = Conversation(id="c9", prospect_id="p1")

    text = await handler._generate_response("interested", "Sure, tell me more", prospect, convo)
    assert text.startswith("Hi Sam,\n\nThanks")
    prompt = brain.prompts[-1]
    assert "prospective client or partner" in prompt and "find a financial advisor" in prompt
    assert "STANDING INSTRUCTIONS" in prompt and "Tuesday afternoons" in prompt
    assert "ATTACHED TO THIS REPLY: brief.pdf" in prompt

    await handler._queue_native_reply(text, prospect, convo, {"subject": "hello"}, "interested")
    items = await state.get_outbox(status="approved")
    assert len(items) == 1 and json.loads(items[0]["attachments_json"]) == ["brief.pdf"]
    assert items[0]["subject"] == "Re: hello" and items[0]["kind"] == "reply"

    # Second reply in the same conversation: first_reply policy → no attachment.
    await handler._generate_response("question", "What does it cost?", prospect, convo)
    assert "ATTACHED TO THIS REPLY" not in brain.prompts[-1]
    await handler._queue_native_reply("Answer.", prospect, convo, {"subject": "hello"}, "question")
    items = await state.get_outbox(status="approved")
    assert [json.loads(i["attachments_json"]) for i in items] == [["brief.pdf"], []]

    # every_reply attaches each time; never attaches nothing; offer mode adds no inquiry context.
    handler.config = _config(reply_attachments=["brief.pdf"], attach_on="every_reply")
    assert await handler._reply_attachments(convo) == ["brief.pdf"]
    handler.config = _config(reply_attachments=["brief.pdf"], attach_on="never")
    assert await handler._reply_attachments(convo) == []
    handler.config = _config()
    assert handler._outreach_context([]) == ""


@pytest.mark.asyncio
async def test_writer_inquiry_mode_and_throttle(state):
    cfg = _config(mode="inquiry", intent="get a quote for a two-storey extension")
    brain = _Brain()
    writer = Writer(brain, state, cfg, EnvConfig())
    assert writer.is_native
    assert not hasattr(writer, "_outreach_block")   # folded into prompts/inquiry_writer.md
    assert "two-storey extension" in writer._need("construction")["need"]

    for i in range(3):
        await state.add_prospect(Prospect(id=f"p{i}", first_name=f"P{i}", last_name="X",
                                          email=f"p{i}@build{i}.co", email_status="provided",
                                          title="Owner", company=f"Build {i}"))
    # The writer stays well ahead of the sender (never under a hundred first
    # emails in per-person mode), so all three are drafted in one cycle.
    room, ahead, backlog = await writer.room()
    assert ahead >= 100 and backlog == 0 and room == ahead
    await writer.run()
    queued = await state.get_outbox()
    assert len(queued) == 3
    assert (await writer.room()) == (ahead - 3, ahead, 3)
    # The stub's "hello" is boilerplate, so the subject check swaps in a plain one from the need.
    from harvey.spamcheck import subject_from_need
    assert all(q["subject"] == subject_from_need("get a quote for a two-storey extension") != "hello" for q in queued)
    personal = [p for p in brain.prompts if "Write ONE enquiry email" in p]
    assert len(personal) == 3
    for p in personal:
        # Inquiry mode gets its own brief (the fake brain stands prompt files in
        # as "[name prompt]"): no cold-email prompt, no sales skills, no old block.
        assert p.startswith("[inquiry_writer prompt]") and "[writer prompt]" not in p
        assert "OUTREACH MODE" not in p and "Jane needs from this business" in p
        assert "sentence case, 3 to 10 words" in p
    sequence = [p for p in brain.prompts if "3-email" in p]
    assert sequence and all(p.startswith("[inquiry_writer prompt]") and "the enquiry itself" in p
                            for p in sequence)

    # Everyone is written: the next cycle makes no model calls at all. And when
    # the outbox holds the whole allowance, the writer stops before any call.
    calls = len(brain.prompts)
    await writer.run()
    assert len(brain.prompts) == calls
    assert (await state.count_prospects_by_status()).get("new", 0) == 0
    await state.add_prospect(Prospect(id="p9", first_name="P9", last_name="X", email="p9@build9.co", email_status="provided", title="Owner"))
    for i in range(ahead):
        await state.add_outbox_item(prospect_id="p0", to_email="p0@build0.co", subject="s", body="b", send_at="2030-01-01T00:00:00", status="approved")
    assert (await writer.room())[0] == 0
    await writer.run()
    assert len(brain.prompts) == calls and (await state.count_prospects_by_status()).get("new") == 1


# ── sectors, role tiers, and one email per segment ──


class _ClassifyingBrain(_Brain):
    """Answers classification with sectors by domain, and sequences/drafts
    like _Brain. Records how many personal drafts were requested."""

    async def think_json(self, prompt, **kw):
        if "Sort these contacts" in prompt:
            self.prompts.append(prompt)
            import json as _json
            contacts = _json.loads(prompt.split("CONTACTS (JSON): ", 1)[1].split("\n\nReturn ONLY", 1)[0])
            out = []
            for c in contacts:
                if c["title"] == "Receptionist":
                    continue  # left out on purpose: exercises the deterministic fallback
                if "build" in c["domain"]:
                    out.append({"id": c["id"], "sector": "Construction", "tier": "executive", "department": "leadership"})
                elif "wealth" in c["domain"]:
                    out.append({"id": c["id"], "sector": "financial_advisory", "tier": "manager", "department": "finance"})
                # the receptionist is deliberately left out → deterministic fallback
            return out
        return await super().think_json(prompt, **kw)


def _people():
    return [
        Prospect(id="b1", first_name="Ann", last_name="Lee", email="ann@bigbuild.com", email_status="provided",
                 title="Managing Director", company="Big Build"),
        Prospect(id="b2", first_name="Bo", last_name="Ng", email="bo@fastbuild.com", email_status="provided",
                 title="Owner", company="Fast Build"),
        Prospect(id="w1", first_name="Cy", last_name="Oh", email="cy@truewealth.com", email_status="provided",
                 title="Portfolio Manager", company="True Wealth"),
        Prospect(id="r1", first_name="", last_name="", email="info@bigbuild.com", email_status="provided",
                 title="Receptionist", company="Big Build"),
    ]


def test_tier_of_reads_titles_and_shared_inboxes():
    t = Writer.tier_of
    assert t(Prospect(title="Chief Financial Officer")) == "executive"
    assert t(Prospect(title="VP Sales")) == "executive"
    assert t(Prospect(title="Operations Manager")) == "manager"
    assert t(Prospect(title="Executive Assistant")) == "admin"
    assert t(Prospect(title="", email="info@acme.com")) == "admin"
    assert t(Prospect(title="Senior Engineer")) == "individual"
    assert t(Prospect(seniority="c_suite", title="")) == "executive"  # scout-sourced values


@pytest.mark.asyncio
async def test_segment_mode_sorts_then_writes_one_sequence_per_segment(state):
    cfg = _config(mode="inquiry", intent="get quotes for an extension", personalize_each=False)
    brain = _ClassifyingBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    for p in _people():
        await state.add_prospect(p)
    await writer.run()

    # Everyone is sorted: two builders (executive), one advisor (manager), and
    # the receptionist fell back to the deterministic rule.
    rows = {}
    async with state._connect() as db:
        async with db.execute("SELECT id, industry, seniority, department FROM prospects") as cur:
            for r in await cur.fetchall():
                rows[r[0]] = r[1:]
    assert rows["b1"] == ("construction", "executive", "leadership")
    assert rows["w1"] == ("financial_advisory", "manager", "finance")
    assert rows["r1"][:2] == ("other", "admin")
    # The company picked up the sector too.
    async with state._connect() as db:
        async with db.execute("SELECT industry FROM companies") as cur:
            assert set(r[0] for r in await cur.fetchall()) <= {"", "construction", "financial_advisory"}

    campaigns = await state.get_campaigns_by_status("draft")
    names = sorted(c.name for c in campaigns)
    assert names == ["construction · executive", "financial_advisory · manager", "other · admin"]
    # One sequence call per segment, no per-person drafts, one classify call.
    assert len([p for p in brain.prompts if "Sort these contacts" in p]) == 1
    sequences = [p for p in brain.prompts if "3-email" in p and "would land in the junk folder" not in p
                 and "different wording" not in p]
    assert len(sequences) == 3
    assert not [p for p in brain.prompts if "Write ONE cold email" in p]
    exec_prompt = next(p for p in sequences if "works in construction" in p)
    assert "executives" in exec_prompt and "whether they will take it on" in exec_prompt
    admin_prompt = next(p for p in sequences if "readers are admins" in p)
    assert "pass it to the person who does" in admin_prompt and "Never remark on the inbox" in admin_prompt
    assert all(p.startswith("[inquiry_writer prompt]") and "Shape the request for what a" in p
               for p in sequences)
    # Nothing staged yet: the sender stages segment templates on its cycle.
    assert await state.count_outbox_queued() == 0
    assert (await state.count_prospects_by_status()).get("queued") == 4

    # Already-sorted contacts are not sent for classification again.
    await writer.run()
    assert len([p for p in brain.prompts if "Sort these contacts" in p]) == 1


@pytest.mark.asyncio
async def test_per_person_mode_still_classifies_and_drafts_each(state):
    cfg = _config(personalize_each=True)
    cfg.channels.email.max_daily_sends = 10
    brain = _ClassifyingBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    for p in _people():
        await state.add_prospect(p)
    await writer.run()
    personal = [p for p in brain.prompts if "Write ONE cold email" in p]
    assert len(personal) == 4
    assert await state.count_outbox_queued() == 4


@pytest.mark.asyncio
async def test_smtp_reply_to_header(monkeypatch):
    from types import SimpleNamespace

    import harvey.integrations.smtp_mail as smtp_mod

    sent = {}

    async def fake_send(msg, **kw):
        sent["msg"] = msg

    monkeypatch.setattr(smtp_mod.aiosmtplib, "send", fake_send)
    cfg = _config()
    env = SimpleNamespace(smtp_host="smtp.x", smtp_port=587, smtp_username="me@x.com", smtp_password="p",
                          smtp_reply_to="replies@x.com", imap_host="", imap_port=993,
                          imap_username="", imap_password="")
    result = await smtp_mod.SmtpImapProvider(cfg, env).send_email("to@y.com", "s", "b")
    assert result.ok and sent["msg"]["Reply-To"] == "replies@x.com"
    env.smtp_reply_to = ""
    await smtp_mod.SmtpImapProvider(cfg, env).send_email("to@y.com", "s", "b")
    assert sent["msg"]["Reply-To"] is None


# ── the closing ask: a call, a coffee, a visit ──


def test_ask_theme_shapes_the_writer_and_reply_prompts(state):
    from harvey.agents.handler import Handler
    from harvey.config import ASK_THEMES
    assert set(ASK_THEMES) == {"zoom", "phone", "coffee", "in_person", "visit", "email"}
    cfg = _config(mode="inquiry", intent="get a quote for a two-storey extension", ask="coffee",
                  ask_detail="I'm in Lagos the week of the 20th")
    writer = Writer(_Brain(), state, cfg, EnvConfig())
    block = writer._ask_block()
    assert "THE ASK" in block and "coffee" in block and "week of the 20th" in block and "never invent a place" in block
    assert writer._base_prompt().endswith(block)            # inquiry prompt carries it
    assert "Booking link" not in block                       # only calls get the link
    offer = Writer(_Brain(), state, _config(mode="offer", ask="zoom"), EnvConfig())
    offer.skills = ""
    offer.config.product.offer.booking_url = "https://cal.test/jane"
    block = offer._ask_block()
    assert "video call" in block and "https://cal.test/jane" in block and offer._base_prompt().endswith(block)
    assert Writer(_Brain(), state, _config(mode="inquiry"), EnvConfig())._ask_block() == ""
    assert Writer(_Brain(), state, _config(mode="inquiry", ask="telepathy"), EnvConfig())._ask_block() == ""
    handler = Handler(_Brain(), state, cfg, EnvConfig())
    ctx = handler._outreach_context([])
    assert "WHAT JANE WANTS NEXT" in ctx.upper() and "coffee" in ctx and "week of the 20th" in ctx
    assert "WANTS NEXT" not in Handler(_Brain(), state, _config(mode="inquiry"), EnvConfig())._outreach_context([])


class _CountingBrain(_ClassifyingBrain):
    """Tracks how many model calls are in flight at once."""
    def __init__(self):
        super().__init__()
        self.in_flight = 0; self.peak = 0

    async def think_json(self, prompt, **kw):
        import asyncio as _a
        self.in_flight += 1; self.peak = max(self.peak, self.in_flight)
        try:
            await _a.sleep(0.01)
            return await super().think_json(prompt, **kw)
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_writer_runs_model_calls_in_parallel(state):
    """A big upload is sorted and written several calls at a time, not one
    after another, and the result is the same campaigns."""
    import harvey.agents.writer as writer_mod
    cfg = _config(mode="inquiry", intent="hire a builder", personalize_each=False)
    for i in range(60):
        await state.add_prospect(Prospect(id=f"q{i}", first_name=f"P{i}", last_name="X", email=f"p{i}@build{i % 4}.co",
                                          email_status="provided", title="Owner", company=f"Build {i % 4}"))
    brain = _CountingBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    await writer.run()
    assert brain.peak >= 2 and brain.peak <= writer_mod.MODEL_CONCURRENCY
    assert len([p for p in brain.prompts if "Sort these contacts" in p]) == 3   # 60 contacts, 25 a call
    assert (await state.count_prospects_by_status()).get("queued", 0) == 60


@pytest.mark.asyncio
async def test_writer_backlog_ignores_follow_ups_scheduled_days_ahead(state):
    """Three-step sequences stage two follow-ups per person days ahead; those
    must not make the writer believe the outbox is full."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(3):
        await state.add_outbox_item(prospect_id=f"p{i}", campaign_id="c", step=1, to_email=f"p{i}@x.co",
                                    subject="s", body="b", send_at=now.isoformat(), status="approved", provider="smtp")
        await state.add_outbox_item(prospect_id=f"p{i}", campaign_id="c", step=2, to_email=f"p{i}@x.co",
                                    subject="s", body="b", send_at=(now + timedelta(days=3)).isoformat(), status="approved", provider="smtp")
    assert await state.count_outbox_queued() == 3
    assert await state.count_outbox_queued(first_step_only=False) == 6
    await state.update_outbox_item((await state.get_outbox(status="approved"))[0]["id"], status="pending_review")
    assert await state.count_outbox_queued(statuses=("approved",)) == 2      # held rows are not a backlog in autopilot
    assert await state.count_outbox_queued() == 3


@pytest.mark.asyncio
async def test_segment_sequences_are_reused_within_a_day(state):
    """The second batch of the same sector and tier costs no sequence call."""
    cfg = _config(mode="inquiry", intent="hire a builder", personalize_each=False)
    for i in range(4):
        await state.add_prospect(Prospect(id=f"s{i}", first_name=f"P{i}", last_name="X", email=f"p{i}@build.co",
                                          email_status="provided", title="Owner", company="Build"))
    brain = _ClassifyingBrain()
    writer = Writer(brain, state, cfg, EnvConfig())
    await writer.run()
    seq_calls = len([p for p in brain.prompts if "3-email" in p])
    assert seq_calls >= 1
    for i in range(4, 8):
        await state.add_prospect(Prospect(id=f"s{i}", first_name=f"P{i}", last_name="X", email=f"p{i}@build.co",
                                          email_status="provided", title="Owner", company="Build"))
    await writer.run()
    assert len([p for p in brain.prompts if "3-email" in p]) == seq_calls          # reused
    drafts = await state.get_campaigns_by_status("draft")
    assert len(drafts) == 2 and drafts[0].sequence[0].subject == drafts[1].sequence[0].subject


class _DownBrain(_Brain):
    """A model that cannot answer right now: rate-limited, or out of credit."""

    async def think_json(self, prompt, **kw):
        raise RuntimeError("429 rate limit reached")


@pytest.mark.asyncio
async def test_writer_falls_back_to_the_general_message_when_the_model_cannot_write(state):
    cfg = _config(mode="inquiry", intent="get a quote for a two-storey extension", personalize_each=False,
                  template_fallback=True, template_subject="Two-storey extension, looking for a builder",
                  template_body="Hi {{first_name}},\n\nWe are planning a two-storey extension and want to start in "
                                "March. Do you take on work like this?\n\nJane")
    writer = Writer(_DownBrain(), state, cfg, EnvConfig())
    assert writer.template_fallback and not writer.use_template
    for i in range(2):
        await state.add_prospect(Prospect(id=f"p{i}", first_name=f"P{i}", last_name="X", email=f"p{i}@build{i}.co",
                                          email_status="provided", title="Owner", company=f"Build {i}"))
    await writer.run()
    # The batch the model could not write got the general message as its
    # sequence; everyone in it is queued. (As in segment mode, the sender
    # stages the outbox rows on its own cycle, so nothing is staged yet.)
    campaigns = await state.get_campaigns_by_status("draft")
    assert len(campaigns) == 1
    step = campaigns[0].sequence[0]
    assert step.subject == "Two-storey extension, looking for a builder" and "two-storey extension" in step.body
    assert (await state.count_prospects_by_status()).get("queued") == 2
    assert await state.count_outbox_queued() == 0


@pytest.mark.asyncio
async def test_without_the_fallback_nothing_goes_out_while_the_model_is_down(state):
    cfg = _config(mode="inquiry", intent="get a quote for a two-storey extension", personalize_each=False,
                  template_body="Hi {{first_name}},\n\nA message long enough to count.\n\nJane")   # fallback off
    writer = Writer(_DownBrain(), state, cfg, EnvConfig())
    assert not writer.template_fallback
    await state.add_prospect(Prospect(id="p0", first_name="P0", last_name="X", email="p0@build0.co",
                                      email_status="provided", title="Owner", company="Build 0"))
    await writer.run()
    # No campaign and nobody queued: they wait for the model, as before.
    assert await state.get_campaigns_by_status("draft") == []
    assert not (await state.count_prospects_by_status()).get("queued")
