"""A guessed list is the list's fault, not the mailbox's: the Microsoft 365
rejection is named, one bounce holds the rest of that company, uploaded
addresses are checked when a verifier exists, and emails written with an
old brief are rewritten rather than sent."""
from datetime import datetime

import pytest

from harvey import listcheck
from harvey.integrations import email_finder
from harvey.integrations.mail_provider import BLOCK_KINDS, InboundMessage, bounce_kind, bounce_reason
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect

from tests.test_outbox_native import Cfg, Env, FakeProvider, StubBrain, make_sender, state  # noqa: F401

DBEB = ("Your message to pattypowers@fryreglet.com has been blocked. See technical details below for more information.\n"
        "The response from the remote server was:\n550 5.4.1 Recipient address rejected: Access denied. For more information "
        "see https://aka.ms/EXOSmtpErrors [CH2PEPF0000013D.namprd02.prod.outlook.com 2026-10-21T17:22:10.515Z]")


def test_the_microsoft_directory_rejection_is_a_bad_address_not_a_block():
    kind = bounce_kind(DBEB)
    assert kind.startswith("no such address (Microsoft 365") and kind not in BLOCK_KINDS
    reason, hint = bounce_reason(DBEB)
    assert reason.startswith("550 5.4.1 Recipient address rejected") and "not a spam verdict" in hint


@pytest.mark.asyncio
async def test_one_bounce_at_a_company_holds_the_rest_of_that_company(state):  # noqa: F811
    from harvey.agents.handler import Handler

    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    handler = Handler(StubBrain(), state, Cfg(), Env())
    handler.provider = provider
    people = [Prospect(id=f"f{i}", first_name=f"P{i}", email=e, email_status="provided")
              for i, e in enumerate(["pattypowers@fryreglet.com", "johnsmith@fryreglet.com", "anne.lee@fryreglet.com",
                                     "someone@elsewhere.example"])]
    for p in people:
        await state.add_prospect(p)
    c = Campaign(id="", name="seg", channel="email", prospect_ids=[p.id for p in people], status="draft",
                 sequence=[EmailStep(step=1, subject="Rebar, around 40 tons a month", body="{{first_name}} plain words about rebar " * 6)])
    c.id = await state.add_campaign(c)
    sender.send_pacing = False
    # Only the first goes out this pass; the rest are approved and waiting.
    Cfg.channels.email.sends_per_cycle = 1
    try:
        await sender._run_native()
    finally:
        del Cfg.channels.email.sends_per_cycle
    assert len(provider.sent) == 1 and provider.sent[0]["to"] == "pattypowers@fryreglet.com"
    sent = (await state.get_outbox(status="sent"))[0]
    await handler._handle_bounce(InboundMessage(provider_id="b", from_email="mailer-daemon@googlemail.com",
                                                subject="Delivery Status Notification (Failure)", body=DBEB,
                                                message_id="<b@g>", in_reply_to=sent["message_id"], is_bounce=True))
    assert (await state.get_prospect("f0")).email_status == "invalid"
    held = await state.get_outbox(status="pending_review")
    assert sorted(i["to_email"] for i in held) == ["anne.lee@fryreglet.com", "johnsmith@fryreglet.com"]
    assert all("address pattern for this company may be wrong" in i["error"] for i in held)
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == ["someone@elsewhere.example"]
    assert "Microsoft 365" in (await state.get_outbox_item(sent["id"]))["error"]


@pytest.mark.asyncio
async def test_uploaded_addresses_are_checked_when_a_verifier_exists(state, monkeypatch):  # noqa: F811
    for i, e in enumerate(["ok@google-shop.example", "gone@m365.example", "any@catchall.example",
                           "who@unknown.example", "dead@no-mx.example"]):
        await state.add_prospect(Prospect(id=f"u{i}", first_name="X", email=e, email_status="provided"))

    async def mx(domain):
        return None if domain == "no-mx.example" else ("aspmx.l.google.com" if "google" in domain else "mail.protection.outlook.com")

    async def verdict(email, mx_type, env):
        assert mx_type in ("google", "microsoft", "gateway")
        return {"ok@google-shop.example": ("verified", False), "gone@m365.example": ("invalid", False),
                "any@catchall.example": ("risky", True)}.get(email, (None, False))

    monkeypatch.setattr(email_finder, "get_mx_host", mx)
    monkeypatch.setattr(email_finder, "_verify_candidate", verdict)

    class Env2:
        reoon_api_key = "k"; zerobounce_api_key = ""
    assert listcheck.verifier_configured(Env2()) and not listcheck.verifier_configured(Env())
    counts = await listcheck.verify_uploaded(state, Env2())
    assert counts == {"verified": 1, "invalid": 2, "risky": 1, "unknown": 1, "checked": 5}
    statuses = {p.email: p.email_status for p in await state.get_prospects_by_status("new")}
    assert statuses == {"ok@google-shop.example": "verified", "gone@m365.example": "invalid", "any@catchall.example": "risky",
                        "who@unknown.example": "provided", "dead@no-mx.example": "invalid"}
    # The inconclusive one is noted and not checked again next cycle.
    assert listcheck.INCONCLUSIVE_NOTE in (await state.get_prospect("u3")).personalization_notes
    assert (await listcheck.verify_uploaded(state, Env2()))["checked"] == 0


@pytest.mark.asyncio
async def test_a_sequence_written_before_the_brief_changed_is_not_reused(state, tmp_path, monkeypatch):  # noqa: F811
    from harvey import paths
    from harvey.agents.writer import Writer
    from harvey.config import EnvConfig
    from tests.test_outreach import _Brain, _config

    cfg_file = tmp_path / "harvey.yaml"
    cfg_file.write_text("persona: {}\n")
    monkeypatch.setattr("harvey.config._find_config_file", lambda: str(cfg_file))
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    brief = prompts / "inquiry_writer.md"
    brief.write_text("old brief")
    monkeypatch.setattr(paths, "PROMPTS_DIR", prompts)
    writer = Writer(_Brain(), state, _config(mode="inquiry", intent="a builder for a 24-unit block in Towson"), EnvConfig())
    import os, time
    old = time.time() - 3600
    os.utime(cfg_file, (old, old)); os.utime(brief, (old, old))
    campaign = Campaign(id="x", name="construction · executive", sequence=[], created_at=datetime.utcnow())
    assert writer._written_after_config_change(campaign) is True
    brief.write_text("new brief after an update")      # touched now: newer than the campaign
    assert writer._written_after_config_change(campaign) is False
