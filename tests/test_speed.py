"""How fast the pipeline moves: the list check beside the writer under a
budget and in parallel, the writer's ceiling, the busy cycle, and the
sender's last look at an address."""
import asyncio
import time

import pytest

from harvey import listcheck
from harvey.config import EnvConfig, HarveyConfig
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from tests.test_outbox_native import Cfg, Env, FakeProvider, make_sender, state  # noqa: F401
from tests.test_outreach import BASE, _Brain


@pytest.mark.asyncio
async def test_domain_lookups_run_in_parallel_and_under_a_budget(state, monkeypatch):  # noqa: F811
    for i in range(12):
        await state.add_prospect(Prospect(id=f"s{i}", first_name="X", email=f"p{i}@d{i}.example", email_status="provided"))
    seen, peak, inflight = [], [0], [0]

    async def slow(domain):
        inflight[0] += 1
        peak[0] = max(peak[0], inflight[0])
        await asyncio.sleep(0.2)
        inflight[0] -= 1
        seen.append(domain)
        return ("mx", "mx")
    monkeypatch.setattr(listcheck, "lookup_domain", slow)
    started = time.monotonic()
    counts = await listcheck.check_domains(state)
    assert counts["checked"] == 12 and len(seen) == 12 and peak[0] >= 8      # twelve lookups, not one after another
    assert time.monotonic() - started < 1.5                                 # 12 × 0.2 s would be 2.4 s in series

    # The whole check keeps to its budget and leaves the rest for the next cycle.
    for i in range(12, 20):
        await state.add_prospect(Prospect(id=f"s{i}", first_name="X", email=f"p{i}@d{i}.example", email_status="provided"))

    async def stuck(domain):
        await asyncio.sleep(5)
        return ("mx", "mx")
    monkeypatch.setattr(listcheck, "lookup_domain", stuck)
    started = time.monotonic()
    out = await listcheck.run_all(state, Env(), budget=0.3)
    assert time.monotonic() - started < 1.0 and out["domains"] == {}


@pytest.mark.asyncio
async def test_the_writer_keeps_a_real_lead_over_the_sender_and_the_cycle_knows_when_to_hurry(state):  # noqa: F811
    from harvey.agents.writer import Writer
    from harvey.main import _more_to_write

    cfg = HarveyConfig(**{**BASE, "outreach": {"mode": "inquiry", "intent": "About 40 tons of rebar a month."},
                          "channels": {"email": {**BASE["channels"]["email"], "max_daily_sends": 50, "sends_per_cycle": 25}}})
    brain = _Brain()
    writer = Writer(brain, state, cfg, EnvConfig())
    room, ahead, backlog = await writer.room()
    assert ahead == 500 and room == 500 and backlog == 0            # 20 send batches of 25 ahead, not two days of 50
    cfg.channels.email.sends_per_cycle = 1
    assert (await writer.room())[1] == 100                              # never under a hundred per-person drafts
    cfg.outreach.personalize_each = False
    assert (await writer.room())[1] == 500                              # segment mode looks further ahead
    cfg.brain.concurrency = 12
    assert writer._concurrency == 12
    assert HarveyConfig(**{**BASE, "brain": {**BASE.get("brain", {}), "concurrency": 99}}).brain.concurrency == 16

    # Nothing waiting: no hurry. A deliverable recipient without an email: hurry. Brain down: no hurry.
    assert await _more_to_write(state, writer, brain) is False
    await state.add_prospect(Prospect(id="w1", first_name="A", email="a@w.example", email_status="provided"))
    assert await _more_to_write(state, writer, brain) is True
    brain.paused_reason = "401"
    assert await _more_to_write(state, writer, brain) is False


@pytest.mark.asyncio
async def test_an_address_found_dead_after_staging_is_not_sent(state):  # noqa: F811
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await state.add_prospect(Prospect(id="d1", first_name="A", email="a@dead.example", email_status="provided"))
    await state.add_prospect(Prospect(id="d2", first_name="B", email="b@live.example", email_status="provided"))
    c = Campaign(id="", name="seg", channel="email", prospect_ids=["d1", "d2"], status="draft",
                 sequence=[EmailStep(step=1, subject="Rebar, about 40 tons a month", body="{{first_name}} plain words about rebar " * 6)])
    c.id = await state.add_campaign(c)
    await sender._stage_campaign_native(c)
    assert len(await state.get_outbox(status="approved")) == 2
    # The list check, running beside the writer, finds the first address dead before the sender gets to it.
    await state.update_prospect_email("d1", "a@dead.example", "invalid")
    await sender._drain_due()
    assert [s["to"] for s in provider.sent] == ["b@live.example"]
    cancelled = await state.get_outbox(status="cancelled")
    assert len(cancelled) == 1 and cancelled[0]["to_email"] == "a@dead.example" and "marked invalid" in cancelled[0]["error"]
