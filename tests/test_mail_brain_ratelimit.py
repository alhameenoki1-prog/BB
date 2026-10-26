"""MailBrain (reply drafting / command bar) waits out a rate limit (429) or a
brief overload (503) and retries, instead of failing the draft."""

import pytest

import bb.mail.brain as brain_mod
from bb.mail.brain import MailBrain, _retry_after_seconds


class _Resp:
    def __init__(self, status, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


class _Client:
    """A stand-in for httpx.AsyncClient that pops scripted responses."""
    def __init__(self, queue):
        self.queue = queue

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        return self.queue.pop(0)


GOOD = _Resp(200, {"choices": [{"message": {"content": "Here is the reply."}, "finish_reason": "stop"}]})


def _brain():
    return MailBrain({"BB_LLM_PROVIDER": "groq", "BB_LLM_API_KEY": "k",
                      "BB_LLM_BASE_URL": "https://groq.test/v1", "BB_LLM_MODEL": "m"})


@pytest.mark.asyncio
async def test_openai_retries_on_429_then_succeeds(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)
    monkeypatch.setattr(brain_mod.asyncio, "sleep", fake_sleep)

    queue = [_Resp(429, text="Rate limit reached, try again in 2s", headers={"retry-after": "2"}), GOOD]
    monkeypatch.setattr(brain_mod.httpx, "AsyncClient", lambda timeout=90: _Client(queue))

    out = await _brain().complete("You write emails.", "Draft a short reply.")
    assert out == "Here is the reply."
    assert waits == [2.0]        # honoured Retry-After, retried once, got the answer


@pytest.mark.asyncio
async def test_openai_gives_up_after_the_retries(monkeypatch):
    async def fake_sleep(s):
        return None
    monkeypatch.setattr(brain_mod.asyncio, "sleep", fake_sleep)

    queue = [_Resp(429, text="slow down") for _ in range(brain_mod.OPENAI_RATE_RETRIES + 1)]
    monkeypatch.setattr(brain_mod.httpx, "AsyncClient", lambda timeout=90: _Client(queue))

    with pytest.raises(brain_mod.BrainError, match="429"):
        await _brain().complete("s", "u")


def test_retry_after_seconds_parsing():
    class R:
        def __init__(self, headers, text=""):
            self.headers = headers
            self.text = text
    assert _retry_after_seconds(R({"retry-after": "5"})) == 5.0
    assert _retry_after_seconds(R({"retry-after-ms": "1500"})) == 1.5
    assert _retry_after_seconds(R({}, "Please try again in 3.5s.")) == 3.5
    assert _retry_after_seconds(R({}, "no hint here")) == 0.0
    assert _retry_after_seconds(R({"retry-after": "9999"})) == brain_mod.MAX_RETRY_AFTER   # capped
