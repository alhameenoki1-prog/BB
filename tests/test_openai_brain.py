"""brain.provider: openai — any OpenAI-compatible chat-completions endpoint."""

import json

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.brain as brain_mod
from harvey.brain import Brain
from harvey.config import BrainConfig
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "t.db"))
    await sm.init_db()
    yield sm


def _brain(state, responses, monkeypatch, key="llm-key", base_url="https://api.groq.com/openai/v1",
           model="llama-3.3-70b-versatile"):
    """A Brain whose HTTP goes to a scripted transport."""
    if key is None:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)

    brain = Brain(state, BrainConfig(provider="openai", base_url=base_url, model=model))
    brain.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return brain, seen


def _ok(text="hello", model="llama-3.3-70b-versatile", finish="stop"):
    return (200, {"id": "chatcmpl-1", "model": model,
                  "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
                  "usage": {"prompt_tokens": 12, "completion_tokens": 5}})


def test_openai_config_validation():
    cfg = BrainConfig(provider="openai", base_url="https://api.openai.com/v1/", model="gpt-4o-mini",
                      reasoning_effort="LOW")
    assert cfg.base_url == "https://api.openai.com/v1" and cfg.reasoning_effort == "low"
    with pytest.raises(ValidationError):
        BrainConfig(provider="openai", model="x")  # base_url required
    with pytest.raises(ValidationError):
        BrainConfig(provider="openai", base_url="api.openai.com/v1", model="x")
    with pytest.raises(ValidationError):
        BrainConfig(provider="openai", base_url="https://x/v1", model="x", reasoning_effort="max")
    assert BrainConfig().base_url == ""  # claude ignores it


@pytest.mark.asyncio
async def test_openai_call_records_usage(state, monkeypatch):
    brain, seen = _brain(state, [_ok()], monkeypatch)
    assert brain.provider_label == "LLM API" and brain.client is None
    assert await brain.preflight() == ""
    assert await brain.think("hi", agent="writer", task="draft") == "hello"

    req = seen[0]
    assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer llm-key"
    body = json.loads(req.content)
    assert body["model"] == "llama-3.3-70b-versatile" and body["max_tokens"] == brain_mod.OPENAI_MAX_TOKENS
    assert body["messages"][0]["role"] == "system" and body["messages"][1] == {"role": "user", "content": "hi"}
    assert "tools" not in body

    assert await state.get_usage_today("openai") == 1
    assert await state.get_usage_today("claude") == 0
    rows = await state.usage_by_provider(days=1)
    assert [(r["provider"], r["calls"], r["output_tokens"], r["cost_usd"]) for r in rows] == [("openai", 1, 5, 0.0)]
    summary = await state.get_state_summary()
    assert summary["usage_today_by_provider"]["openai"] == 1


@pytest.mark.asyncio
async def test_openai_dot_com_uses_max_completion_tokens_and_effort(state, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    brain = Brain(state, BrainConfig(provider="openai", base_url="https://api.openai.com/v1",
                                     model="gpt-4o-mini", reasoning_effort="low"))
    seen = []
    brain.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (seen.append(json.loads(r.content)) or httpx.Response(200, json=_ok()[1]))))
    assert await brain.think("hi") == "hello"
    assert seen[0]["max_completion_tokens"] == brain_mod.OPENAI_MAX_TOKENS and "max_tokens" not in seen[0]
    assert seen[0]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_openai_retries_rate_limits_and_server_errors(state, monkeypatch):
    brain, seen = _brain(state, [(429, {"error": "slow down"}), (503, "down"),
                                 httpx.ConnectError("boom"), _ok("late")], monkeypatch)
    assert await brain.think("hi", max_retries=3) == "late"
    assert len(seen) == 4
    brain, seen = _brain(state, [(500, "x"), (500, "x"), (500, "x"), _ok()], monkeypatch)
    assert await brain.think("hi", max_retries=2) == "" and len(seen) == 3


@pytest.mark.asyncio
async def test_openai_bad_key_and_bad_model_pause(state, monkeypatch):
    brain, seen = _brain(state, [(401, {"error": {"message": "Invalid API key"}})], monkeypatch)
    assert await brain.think("one") == "" and await brain.think("two") == ""
    assert len(seen) == 1 and "LLM_API_KEY" in brain.paused_reason
    brain, _ = _brain(state, [(404, {"error": "model not found"})], monkeypatch)
    assert await brain.think("x") == "" and "brain.model" in brain.paused_reason


@pytest.mark.asyncio
async def test_openai_out_of_allowance_pauses_like_a_usage_limit(state, monkeypatch):
    brain, _ = _brain(state, [(400, {"error": {"message": "You exceeded your current quota"}})], monkeypatch)
    assert await brain.think("x") == ""
    assert "usage limit" in brain.paused_reason
    assert await brain.is_within_budget(1000) is False


@pytest.mark.asyncio
async def test_openai_local_server_needs_no_key(state, monkeypatch):
    brain, seen = _brain(state, [_ok("local")], monkeypatch, key=None,
                         base_url="http://localhost:11434/v1", model="llama3.1")
    assert await brain.preflight() == ""
    assert await brain.think("hi") == "local"
    assert "authorization" not in seen[0].headers
    remote, _ = _brain(state, [], monkeypatch, key=None)
    assert "LLM_API_KEY is not set" in await remote.preflight()


@pytest.mark.asyncio
async def test_openai_content_parts_and_truncation(state, monkeypatch):
    parts = (200, {"id": "c", "model": "m", "choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "finish_reason": "length"}], "usage": {}})
    brain, _ = _brain(state, [parts], monkeypatch)
    assert await brain.think("hi") == "ab"
    brain, _ = _brain(state, [(200, "not json")], monkeypatch)
    assert await brain.think("hi") == ""
