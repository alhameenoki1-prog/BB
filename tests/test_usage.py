"""Tests for usage tracking: schema, aggregations, and the Claude API ledger rows."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.brain as brain_mod
from harvey.brain import Brain
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        sm = StateManager(db_path)
        await sm.init_db()
        yield sm


# ── usage_events table + aggregations ──


@pytest.mark.asyncio
async def test_record_usage_event_and_totals(state):
    ok = await state.record_usage_event(
        agent="writer", task="write_sequence", session_id="sess-1",
        model="claude-opus-4-8", input_tokens=100, output_tokens=500,
        cache_read_tokens=2000, cache_creation_tokens=300,
        cost_usd=0.05, duration_ms=1200, num_turns=1,
    )
    assert ok

    totals = await state.usage_totals()
    assert totals["today"]["calls"] == 1
    assert totals["today"]["output_tokens"] == 500
    assert totals["today"]["cost_usd"] == pytest.approx(0.05)
    assert totals["week"]["input_tokens"] == 100


@pytest.mark.asyncio
async def test_usage_by_agent_groups_and_counts_sessions(state):
    # Two model-rows from the SAME call (same session) → one "call"
    await state.record_usage_event(
        agent="scout", session_id="sess-a", model="claude-opus-4-8",
        output_tokens=10, cost_usd=0.01,
    )
    await state.record_usage_event(
        agent="scout", session_id="sess-a", model="claude-haiku-4-5",
        output_tokens=5, cost_usd=0.001,
    )
    await state.record_usage_event(
        agent="handler", session_id="sess-b", model="claude-opus-4-8",
        output_tokens=20, cost_usd=0.02,
    )

    rows = await state.usage_by_agent(days=7)
    by_name = {r["agent"]: r for r in rows}
    assert by_name["scout"]["calls"] == 1
    assert by_name["scout"]["output_tokens"] == 15
    assert by_name["handler"]["calls"] == 1


@pytest.mark.asyncio
async def test_request_key_dedup(state):
    first = await state.record_usage_event(
        request_key="msg_1:req_1", model="claude-opus-4-8",
        output_tokens=50, source="transcript",
    )
    second = await state.record_usage_event(
        request_key="msg_1:req_1", model="claude-opus-4-8",
        output_tokens=50, source="transcript",
    )
    assert first is True
    assert second is False  # unique index makes reconciliation idempotent

    totals = await state.usage_totals()
    assert totals["today"]["output_tokens"] == 50


# ── Brain usage rows from Messages API responses ──


def _response(**overrides):
    base = dict(
        id="msg_01", model="claude-opus-5-5", stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Hello from Claude")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=51,
                              cache_read_input_tokens=17198, cache_creation_input_tokens=8155),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_brain_records_usage_from_response(state):
    brain = Brain(state, client=SimpleNamespace(api_key="sk-ant-test", auth_token=None))
    await brain._record_claude_usage(
        _response(), model="claude-opus-5-5", duration_ms=1428,
        agent="", task="", label="harvey-scout-score",
    )
    rows = await state.usage_by_agent(days=1)
    assert len(rows) == 1
    assert rows[0]["agent"] == "scout"  # derived from the harvey-* label
    assert rows[0]["output_tokens"] == 51
    assert rows[0]["cache_read_tokens"] == 17198
    expected = brain_mod.estimate_cost_usd("claude-opus-5-5", 10, 51, 17198, 8155)
    assert expected > 0
    assert rows[0]["cost_usd"] == pytest.approx(expected, abs=1e-6)

    by_model = await state.usage_by_model(days=1)
    assert by_model[0]["model"] == "claude-opus-5-5"
    by_provider = await state.usage_by_provider(days=1)
    assert [r["provider"] for r in by_provider] == ["claude"]


@pytest.mark.asyncio
async def test_served_model_wins_over_requested_model(state):
    """With a refusal fallback the response names the model that answered."""
    brain = Brain(state, client=SimpleNamespace(api_key="sk-ant-test", auth_token=None))
    await brain._record_claude_usage(
        _response(model="claude-opus-4-8"), model="claude-opus-5-5", duration_ms=1,
        agent="writer", task="draft", label="",
    )
    by_model = await state.usage_by_model(days=1)
    assert by_model[0]["model"] == "claude-opus-4-8"
    assert by_model[0]["cost_usd"] == pytest.approx(
        brain_mod.estimate_cost_usd("claude-opus-4-8", 10, 51, 17198, 8155))


@pytest.mark.asyncio
async def test_usage_is_harvey_only(state):
    """Sanity: the ledger only contains rows Harvey's Brain wrote. There is
    no transcript scan to pull in other projects' sessions, and no reading
    of the operator's own Claude Code login."""
    assert not hasattr(brain_mod, "reconcile_transcripts")
    assert not hasattr(brain_mod, "parse_transcript_events")
    import importlib.util
    assert importlib.util.find_spec("harvey.integrations.quota") is None
    assert importlib.util.find_spec("harvey.usage") is None
