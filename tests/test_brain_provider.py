"""Brain provider selection: Claude stays the default, Codex is opt-in, and the
choice flows through config, main, setup, training, budgets and the dashboard
without falling back or mislabelling one subscription as the other."""

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
import pytest_asyncio
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import harvey.brain as brain_mod
import harvey.dashboard as dash
from harvey.brain import DEFAULT_CLAUDE_MODEL, Brain
from harvey.codex import CodexError, CodexResult
from harvey.config import BrainConfig, ConfigError, HarveyConfig, load_brain_config
from harvey.state import StateManager

BASE = {
    "persona": {"name": "H", "company": "A", "role": "B", "email": "e", "linkedin": "", "tone": "t"},
    "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [],
                "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
}


@pytest.fixture(autouse=True)
def _codex_platform(monkeypatch):
    """These tests cover provider plumbing with stubs, on any OS; the
    Windows refusal itself is tested in test_codex_platform.py."""
    import harvey.codex as hc
    monkeypatch.setattr(hc, "_PLATFORM", "linux")


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── config ──


def test_claude_is_the_default():
    assert HarveyConfig(**BASE).brain.provider == "claude"
    assert BrainConfig().model == ""


def test_brain_config_validation():
    cfg = BrainConfig(provider=" Codex ", model="gpt-5.5", reasoning_effort="HIGH")
    assert (cfg.provider, cfg.model, cfg.reasoning_effort) == ("codex", "gpt-5.5", "high")
    assert BrainConfig(reasoning_effort="MAX").reasoning_effort == "max"  # Claude has max
    assert BrainConfig().fallbacks is True
    for bad in ({"provider": "openai"}, {"model": "--dangerously-skip-permissions"},
                {"model": "gpt 5; rm -rf /"},
                {"provider": "codex", "reasoning_effort": "max"},   # Codex has no max
                {"reasoning_effort": "minimal"}):                   # Claude has no minimal
        with pytest.raises(ValidationError):
            BrainConfig(**bad)


def test_load_brain_config(tmp_path):
    path = tmp_path / "harvey.yaml"
    assert load_brain_config(str(path)) == BrainConfig()  # missing file
    path.write_text(yaml.dump({"persona": "placeholder"}))
    assert load_brain_config(str(path)).provider == "claude"  # no section
    path.write_text(yaml.dump({"brain": {"provider": "codex", "model": "gpt-5.5"}}))
    assert load_brain_config(str(path)) == BrainConfig(provider="codex", model="gpt-5.5")
    path.write_text(yaml.dump({"brain": {"provider": "gemini"}}))
    with pytest.raises(ConfigError):  # never silently becomes Claude
        load_brain_config(str(path))


# ── Claude via the Anthropic API ──


def _response(text="ok", model=DEFAULT_CLAUDE_MODEL, stop_reason="end_turn", **usage):
    counts = {"input_tokens": 10, "output_tokens": 4,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, **usage}
    return SimpleNamespace(
        id="msg_" + text[:8], model=model, stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(**counts),
    )


def _api_error(cls, status, message="boom", headers=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls(message, response=response, body={"error": {"message": message}})


class _FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic. ``outcomes`` are returned (or
    raised) in order; every request is recorded with the path it took."""

    def __init__(self, outcomes, api_key="sk-ant-test"):
        self.outcomes = list(outcomes)
        self.api_key = api_key
        self.auth_token = None
        self.requests = []
        self.messages = SimpleNamespace(create=self._plain)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))
        self.options = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    async def _plain(self, **kwargs):
        return await self._take("messages", kwargs)

    async def _beta(self, **kwargs):
        return await self._take("beta", kwargs)

    async def _take(self, path, kwargs):
        self.requests.append((path, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _claude_brain(state, outcomes, config=None):
    fake = _FakeAnthropic(outcomes)
    return Brain(state, config or BrainConfig(), client=fake), fake


@pytest.mark.asyncio
async def test_default_brain_calls_the_api_with_opus_and_fallbacks(state):
    brain, fake = _claude_brain(state, [_response("hello")])
    assert type(brain.quota).__name__ == "_NoQuota" and brain.codex is None
    assert await brain.think("hi", agent="writer", task="draft", timeout=42) == "hello"

    path, request = fake.requests[0]
    assert path == "beta"  # the refusal fallback rides the beta endpoint
    assert request["model"] == DEFAULT_CLAUDE_MODEL
    assert request["betas"] == [brain_mod._FALLBACK_BETA] and request["fallbacks"] == "default"
    assert request["max_tokens"] == brain_mod.CLAUDE_MAX_TOKENS
    assert "output_config" not in request and "thinking" not in request
    assert fake.options == [{"timeout": 42}]

    assert await state.get_usage_today("claude") == 1
    assert await state.get_usage_today("codex") == 0
    rows = await state.usage_by_provider(days=1)
    assert [r["provider"] for r in rows] == ["claude"]
    assert rows[0]["output_tokens"] == 4
    assert rows[0]["cost_usd"] == pytest.approx(
        brain_mod.estimate_cost_usd(DEFAULT_CLAUDE_MODEL, 10, 4))
    by_agent = await state.usage_by_agent(days=1)
    assert by_agent[0]["agent"] == "writer"


@pytest.mark.asyncio
async def test_claude_model_effort_and_fallback_toggle_are_passed_per_call(state):
    config = BrainConfig(model="claude-haiku-5-5", reasoning_effort="low")
    brain, fake = _claude_brain(state, [_response(model="claude-haiku-5-5")], config)
    await brain.think("hi")
    path, request = fake.requests[0]
    assert path == "messages"  # no fallback for models outside the supported set
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_config"] == {"effort": "low"}
    assert "fallbacks" not in request and "betas" not in request

    brain, fake = _claude_brain(state, [_response()], BrainConfig(fallbacks=False))
    await brain.think("hi")
    assert fake.requests[0][0] == "messages"


@pytest.mark.asyncio
async def test_claude_think_json(state):
    brain, _ = _claude_brain(state, [_response('```json\n{"intent": "interested"}\n```')])
    assert await brain.think_json("classify") == {"intent": "interested"}


@pytest.mark.asyncio
async def test_claude_transient_failures_are_retried_and_bounded(state, monkeypatch):
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    limited = _api_error(anthropic.RateLimitError, 429, "slow down", {"retry-after": "0"})
    server = _api_error(anthropic.InternalServerError, 500, "oops")
    network = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    brain, fake = _claude_brain(state, [limited, server, _response("late")])
    assert await brain.think("hi", max_retries=2) == "late"
    assert len(fake.requests) == 3

    brain, fake = _claude_brain(state, [network, network, network, _response("never")])
    assert await brain.think("hi", max_retries=2) == ""
    assert len(fake.requests) == 3
    assert await state.get_usage_today("claude") == 1  # only the successful call counted


@pytest.mark.asyncio
async def test_claude_rejected_key_pauses_until_the_key_changes(state):
    import json
    brain, fake = _claude_brain(
        state, [_api_error(anthropic.AuthenticationError, 401, "invalid x-api-key")])
    assert await brain.think("one") == ""
    assert await brain.think("two") == ""
    assert len(fake.requests) == 1  # the second call never reached the API
    assert "ANTHROPIC_API_KEY" in brain.paused_reason
    stored = json.loads(await state.get_setting(brain_mod.BRAIN_STATUS_KEY))
    assert stored == {**stored, "provider": "claude", "status": "paused", "kind": "auth"}
    assert await brain.preflight() == brain.paused_reason

    fake.api_key = "sk-ant-rotated"  # the operator fixed .env and restarted
    assert await brain.preflight() == ""
    stored = json.loads(await state.get_setting(brain_mod.BRAIN_STATUS_KEY))
    assert stored["status"] == "ok"


@pytest.mark.asyncio
async def test_claude_bad_requests_are_not_retried(state):
    brain, fake = _claude_brain(
        state, [_api_error(anthropic.BadRequestError, 400, "messages: too long")])
    assert await brain.think("hi") == ""
    assert len(fake.requests) == 1 and brain.paused_reason == ""

    unknown = _api_error(anthropic.NotFoundError, 404, "model: claude-nope")
    brain, fake = _claude_brain(state, [unknown], BrainConfig(model="claude-nope"))
    assert await brain.think("hi") == ""
    assert "claude-nope" in brain.paused_reason and "brain.model" in brain.paused_reason


@pytest.mark.asyncio
async def test_claude_out_of_credit_pauses_like_a_usage_limit(state):
    brain, fake = _claude_brain(state, [_api_error(
        anthropic.BadRequestError, 400, "Your credit balance is too low to access the API.")])
    assert await brain.think("hi") == ""
    assert "usage limit" in brain.paused_reason
    assert await brain.is_within_budget(1000) is False
    assert await brain.think("again") == "" and len(fake.requests) == 1


@pytest.mark.asyncio
async def test_claude_refusal_returns_nothing_but_is_recorded(state):
    brain, _ = _claude_brain(state, [_response("", stop_reason="refusal")])
    assert await brain.think("hi") == ""
    rows = await state.usage_by_provider(days=1)
    assert rows[0]["calls"] == 1
    assert brain.paused_reason == ""  # one declined prompt doesn't pause the brain


@pytest.mark.asyncio
async def test_claude_preflight_needs_a_key_and_makes_no_call(state):
    brain, fake = _claude_brain(state, [AssertionError("preflight must not call the API")])
    assert await brain.preflight() == ""
    assert fake.requests == []

    brain, fake = _claude_brain(state, [])
    fake.api_key = None
    reason = await brain.preflight()
    assert "ANTHROPIC_API_KEY is not set" in reason
    assert await brain.think("hi") == "" and fake.requests == []

    # No client at all (the SDK couldn't be constructed) reads the same way.
    monkey = Brain(state, BrainConfig(), client=_FakeAnthropic([]))
    monkey.client = None
    assert "ANTHROPIC_API_KEY" in await monkey.preflight()


@pytest.mark.asyncio
async def test_claude_daily_spend_cap(state):
    brain, _ = _claude_brain(state, [_response(input_tokens=1_000_000, output_tokens=0)])
    await brain.think("expensive")  # $4.00 of Opus input
    assert await brain.is_within_budget(1000) is True              # no cap
    assert await brain.is_within_budget(1000, max_daily_cost_usd=10.0) is True
    assert await brain.is_within_budget(1000, max_daily_cost_usd=3.0) is False
    assert await brain.is_within_budget(1, max_daily_cost_usd=10.0) is False  # call count still applies


def test_estimate_cost_usd():
    assert brain_mod.estimate_cost_usd("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert brain_mod.estimate_cost_usd("claude-opus-5-5", 0, 1_000_000) == 20.0
    assert brain_mod.estimate_cost_usd("claude-opus-5", 1_000_000) == 5.0  # longest prefix wins
    assert brain_mod.estimate_cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == 0.6
    assert brain_mod.estimate_cost_usd("claude-sonnet-4-6", cache_read_tokens=1_000_000) == 0.3
    assert brain_mod.estimate_cost_usd("some-future-model", 1_000_000, 1_000_000) == 0.0


# ── Codex path ──


class _StubCodex:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.preflight_outcomes = []
        self.preflights = 0
        self.print = ("auth-v1",)

    def fingerprint(self):
        return self.print

    async def preflight(self):
        self.preflights += 1
        if self.preflight_outcomes:
            outcome = self.preflight_outcomes.pop(0)
            if outcome is not None:
                raise outcome

    async def run(self, prompt, timeout, skip_if=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _codex_brain(state, outcomes):
    brain = Brain(state, BrainConfig(provider="codex"))
    brain.codex = _StubCodex(outcomes)
    return brain


@pytest.mark.asyncio
async def test_codex_brain_never_touches_claude(state, monkeypatch):
    def no_claude(*a, **k):
        raise AssertionError("Codex brain must not build an Anthropic client")

    monkeypatch.setattr(brain_mod.anthropic, "AsyncAnthropic", no_claude)
    result = CodexResult(text="hello", model="gpt-5.5", thread_id="thr-1", duration_ms=900,
                         usage={"input_tokens": 10, "output_tokens": 4, "cache_read_tokens": 2,
                                "cache_creation_tokens": 0})
    brain = _codex_brain(state, [result])
    assert type(brain.quota).__name__ == "CodexQuotaClient" and brain.client is None
    assert await brain.think("hi", agent="writer", task="draft") == "hello"

    assert await state.get_usage_today("codex") == 1
    assert await state.get_usage_today("claude") == 0
    rows = await state.usage_by_provider(days=1)
    assert rows == [{**rows[0], "provider": "codex", "calls": 1, "output_tokens": 4,
                     "cost_usd": 0.0}]
    by_model = await state.usage_by_model(days=1)
    assert by_model[0]["model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_codex_think_json(state):
    brain = _codex_brain(state, [CodexResult(text='```json\n{"intent": "interested"}\n```')])
    assert await brain.think_json("classify") == {"intent": "interested"}


@pytest.mark.asyncio
async def test_codex_non_retryable_failure_stops_immediately(state):
    brain = _codex_brain(state, [CodexError("isolation", kind="isolation")])
    assert await brain.think("hi") == ""
    assert brain.codex.calls == 1
    assert await state.get_usage_today("codex") == 0


@pytest.mark.asyncio
async def test_codex_retries_are_bounded(state, monkeypatch):
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    err = CodexError("timeout", kind="timeout", retryable=True)
    brain = _codex_brain(state, [err, err, err, CodexResult(text="late")])
    assert await brain.think("hi", max_retries=2) == ""
    assert brain.codex.calls == 3

    brain = _codex_brain(state, [err, CodexResult(text="recovered")])
    assert await brain.think("hi", max_retries=2) == "recovered"


@pytest.mark.asyncio
async def test_codex_pause_is_sticky_until_the_login_or_cli_changes(state):
    import json
    brain = _codex_brain(state, [])
    brain.codex.preflight_outcomes = [CodexError("login expired", kind="auth"), None]

    assert await brain.preflight() == "login expired"
    assert await brain.preflight() == "login expired"  # nothing changed: no re-check
    assert brain.codex.preflights == 1
    stored = json.loads(await state.get_setting(brain_mod.BRAIN_STATUS_KEY))
    assert stored["status"] == "paused" and stored["reason"] == "login expired"

    brain.codex.print = ("auth-v2",)  # the operator renewed the login
    assert await brain.preflight() == ""
    assert brain.codex.preflights == 2
    stored = json.loads(await state.get_setting(brain_mod.BRAIN_STATUS_KEY))
    assert stored["status"] == "ok"


@pytest.mark.asyncio
async def test_codex_transient_preflight_failure_is_not_sticky(state):
    brain = _codex_brain(state, [])
    brain.codex.preflight_outcomes = [
        CodexError("Codex call timed out.", kind="timeout", retryable=True), None]
    assert await brain.preflight() == "Codex call timed out."
    assert await brain.preflight() == ""
    assert brain.codex.preflights == 2
    assert await state.get_setting(brain_mod.BRAIN_STATUS_KEY) == ""


@pytest.mark.asyncio
async def test_codex_persistent_call_failure_stops_further_runtimes(state):
    brain = _codex_brain(state, [CodexError("hooks are configured", kind="isolation")])
    assert await brain.think("one") == ""
    assert await brain.think("two") == ""
    assert brain.codex.calls == 1  # the second call never reached the adapter
    assert brain.paused_reason == "hooks are configured"


@pytest.mark.asyncio
async def test_codex_unexpected_errors_return_empty_without_retry(state):
    brain = _codex_brain(state, [RuntimeError("detail that must not be logged")])
    assert await brain.think("hi") == ""
    assert brain.codex.calls == 1


@pytest.mark.asyncio
async def test_codex_cancellation_propagates(state):
    brain = _codex_brain(state, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await brain.think("hi")


@pytest.mark.asyncio
async def test_codex_known_denial_closes_the_budget(state):
    """An exhausted plan with no windows must not fall back to call counts."""
    brain = _codex_brain(state, [])

    class Denied:
        async def get_utilization(self):
            return {"limit_reached": {"utilization": 100.0, "resets_at": "",
                                      "window_minutes": None}}

    brain.quota = Denied()
    assert await brain.is_within_budget(1000, max_percent=80) is False


@pytest.mark.asyncio
async def test_codex_budget_uses_codex_quota_and_counter(state):
    brain = _codex_brain(state, [])

    class Quota:
        async def get_utilization(self):
            return {"five_hour": {"utilization": 85.0, "resets_at": ""}}

    brain.quota = Quota()
    assert await brain.is_within_budget(10, max_percent=80) is False

    class NoQuota:
        async def get_utilization(self):
            return None

    brain.quota = NoQuota()
    for _ in range(3):
        await state.increment_usage(provider="claude")  # Claude calls don't count
    assert await brain.is_within_budget(2, max_percent=80) is True
    for _ in range(2):
        await state.increment_usage(provider="codex")
    assert await brain.is_within_budget(2, max_percent=80) is False


@pytest.mark.asyncio
async def test_main_heartbeat_builds_brain_from_config(monkeypatch):
    import harvey.main as main_mod

    seen = {}

    class StopBrain(Exception):
        pass

    def fake_brain(state, brain_config=None):
        seen["config"] = brain_config
        raise StopBrain

    cfg = HarveyConfig(**BASE, brain={"provider": "codex", "model": "gpt-5.5"})
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "Brain", fake_brain)
    with pytest.raises(StopBrain):
        await main_mod.heartbeat(asyncio.Event())
    assert seen["config"] == BrainConfig(provider="codex", model="gpt-5.5")


# ── state migration ──


@pytest.mark.asyncio
async def test_existing_usage_rows_become_claude(tmp_path):
    import aiosqlite
    from harvey.state import MIGRATIONS

    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:10]:  # a database from before v11
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 10")
        await db.execute("INSERT INTO usage_events (id, model, output_tokens) VALUES ('a', 'm', 5)")
        await db.execute("INSERT INTO usage_log (id, date, claude_calls) VALUES ('u', date('now'), 7)")
        await db.commit()

    sm = StateManager(db_path)
    await sm.init_db()
    rows = await sm.usage_by_provider(days=30)
    assert [(r["provider"], r["output_tokens"]) for r in rows] == [("claude", 5)]
    assert await sm.get_usage_today("claude") == 7
    assert await sm.get_usage_today("codex") == 0
    summary = await sm.get_state_summary()
    assert summary["usage_today"] == 7
    assert summary["usage_today_by_provider"] == {"claude": 7, "codex": 0, "openai": 0}
    with pytest.raises(ValueError):
        await sm.record_usage_event(provider="gemini")


# ── training and setup preserve the selection ──


def test_trainer_uses_and_writes_back_the_selection(tmp_path, monkeypatch):
    import harvey.trainer as trainer_mod

    monkeypatch.setattr(trainer_mod, "load_brain_config",
                        lambda: BrainConfig(provider="codex", model="gpt-5.5"))
    trainer = trainer_mod.Trainer()
    assert trainer.brain.provider == "codex" and trainer.brain.model == "gpt-5.5"
    built = trainer._build_config({"product_name": "W"}, {}, {}, "acme.com")
    assert built["brain"] == {"provider": "codex", "model": "gpt-5.5"}
    # The written config loads back with the same selection.
    assert HarveyConfig(**built).brain == BrainConfig(provider="codex", model="gpt-5.5")

    explicit = trainer_mod.Trainer(brain_config=BrainConfig())
    assert explicit.brain.provider == "claude"
    assert explicit._build_config({}, {}, {}, "acme.com")["brain"] == {"provider": "claude"}


def test_cli_train_flags_select_the_provider(monkeypatch):
    import harvey.cli as cli
    import harvey.trainer as trainer_mod
    import harvey.config as config_mod

    seen = {}

    class FakeTrainer:
        def __init__(self, brain_config=None):
            seen["config"] = brain_config

        async def train(self, url, max_pages=100):
            seen["url"] = url

    monkeypatch.setattr(trainer_mod, "Trainer", FakeTrainer)
    monkeypatch.setattr(config_mod, "load_brain_config",
                        lambda: BrainConfig(provider="claude", model="claude-opus-4-8"))

    args = type("A", (), {"url": "acme.com", "max_pages": 5, "provider": "codex",
                          "model": None, "reasoning_effort": "low"})()
    cli.cmd_train(args)
    # Switching provider never carries the other provider's model along.
    assert seen["config"] == BrainConfig(provider="codex", reasoning_effort="low")

    args.provider, args.reasoning_effort = None, None
    cli.cmd_train(args)
    assert seen["config"] == BrainConfig(provider="claude", model="claude-opus-4-8")


def _answering(answers, prompts):
    """A stand-in for setup's _ask that, like the real one, returns the
    default when the answer is blank."""
    answers = iter(answers)

    def ask(prompt, default="", required=True, secret=False):
        prompts.append((prompt, default))
        answer = next(answers)
        return answer or default

    return ask


@pytest.mark.parametrize("answers, expected", [
    (["codex", ""], BrainConfig(provider="codex", model="gpt-5.5")),         # accept suggestion
    (["codex", "gpt-6-sol"], BrainConfig(provider="codex", model="gpt-6-sol")),  # explicit kept
    (["codex", "--model", "my-model"], BrainConfig(provider="codex", model="my-model")),
    (["claude", ""], BrainConfig()),
    (["claude", "claude-sonnet-5-5"], BrainConfig(model="claude-sonnet-5-5")),
])
def test_setup_model_prompt(monkeypatch, answers, expected):
    import harvey.setup as setup_mod
    from harvey.codex import SUGGESTED_MODEL

    prompts = []
    monkeypatch.setattr(setup_mod, "_ask", _answering(answers, prompts))
    assert setup_mod._ask_brain_config() == expected
    model_prompts = prompts[1:]
    if expected.provider == "codex":
        # Suggested, never forced: an explicit name is kept as typed (and
        # validated; a flag-like answer is asked again).
        assert all(p == ("Model", SUGGESTED_MODEL) for p in model_prompts)
    else:
        assert model_prompts == [(f"Model (leave blank for {DEFAULT_CLAUDE_MODEL})", "")]


def test_setup_writes_the_selection(tmp_path, monkeypatch):
    import harvey.setup as setup_mod

    answers = iter(["codex", "gpt-5.5"])
    monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: next(answers))
    wizard = setup_mod.SetupWizard()
    wizard.brain_config = setup_mod._ask_brain_config()
    assert wizard.brain_config == BrainConfig(provider="codex", model="gpt-5.5")

    config_file = tmp_path / "harvey.yaml"
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", config_file)
    wizard.config = dict(BASE)
    wizard._write_config()
    assert yaml.safe_load(config_file.read_text())["brain"] == {"provider": "codex", "model": "gpt-5.5"}

    # Re-running setup over a trained config updates the selection too.
    wizard.brain_config = BrainConfig()
    wizard._write_config()
    assert yaml.safe_load(config_file.read_text())["brain"] == {"provider": "claude"}


@pytest.mark.asyncio
async def test_codex_setup_does_not_require_claude(monkeypatch, tmp_path):
    import harvey.setup as setup_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # no Claude key either
    monkeypatch.setattr(setup_mod, "_check_codex_cli", lambda: True)
    monkeypatch.setattr("harvey.codex.check_native_auth", lambda *a: Path("auth.json"))
    monkeypatch.setattr("harvey.codex.check_cli_version", lambda *a: "0.159.3")
    monkeypatch.setattr(setup_mod, "StateManager",
                        lambda: StateManager(str(tmp_path / "s.db")))

    async def ready(brain):
        assert brain.provider == "codex"
        return True

    monkeypatch.setattr(setup_mod, "_test_claude", ready)
    answers = iter(["codex", ""])
    monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: next(answers))
    wizard = setup_mod.SetupWizard()
    await wizard._check_prerequisites()  # would SystemExit if Claude were required
    assert wizard.brain_config.provider == "codex"


# ── dashboard labels the active provider's quota only ──


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(str(db)).init_db())
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_quota_clients", {})
    with TestClient(dash.app) as c:
        c.db_path = db
        yield c


class _Quota:
    def __init__(self, windows):
        self.windows = windows

    async def get_utilization(self):
        return self.windows


class _NeverRead:
    async def get_utilization(self):
        raise AssertionError("Claude quota must not be read when Codex is active")


def test_dashboard_codex_quota_is_labelled_codex(client, monkeypatch):
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig(provider="codex"))
    monkeypatch.setitem(dash._quota_clients, ("codex", ""),
                        _Quota({"five_hour": {"utilization": 33.0, "resets_at": ""}}))
    monkeypatch.setitem(dash._quota_clients, ("claude", ""), _NeverRead())
    asyncio.run(StateManager(str(client.db_path)).record_usage_event(
        provider="codex", model="gpt-5.5", output_tokens=9))
    data = client.get("/api/usage").json()
    assert data["provider"] == "codex" and data["quota_provider"] == "codex"
    assert data["quota_status"] == "available"
    assert data["quota"]["five_hour"]["utilization"] == 33.0
    assert [r["provider"] for r in data["by_provider"]] == ["codex"]

    stats = client.get("/api/stats").json()
    assert stats["brain_provider"] == "codex"
    assert stats["codex_calls_today"] == 0 and stats["claude_calls_today"] == 0


def test_dashboard_claude_reports_spend_not_quota(client, monkeypatch):
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig())
    monkeypatch.setitem(dash._quota_clients, ("claude", ""), _NeverRead())
    asyncio.run(StateManager(str(client.db_path)).record_usage_event(
        provider="claude", model=DEFAULT_CLAUDE_MODEL, output_tokens=9, cost_usd=0.25))
    data = client.get("/api/usage").json()
    assert data["quota_provider"] == "claude" and data["quota"] is None
    assert data["quota_status"] == "unavailable"  # no subscription window exists
    assert data["totals"]["today"]["cost_usd"] == pytest.approx(0.25)
    assert ("claude", "") not in dash._quota_clients or isinstance(
        dash._quota_clients[("claude", "")], _NeverRead)


def test_dashboard_reports_unavailable_codex_quota(client, monkeypatch):
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig(provider="codex"))
    monkeypatch.setitem(dash._quota_clients, ("codex", ""), _Quota(None))
    monkeypatch.setitem(dash._quota_clients, ("claude", ""), _NeverRead())
    data = client.get("/api/usage").json()
    assert data["quota"] is None
    assert data["quota_provider"] == "codex" and data["quota_status"] == "unavailable"


def test_dashboard_codex_quota_client_follows_the_configured_model(client, monkeypatch):
    import harvey.codex as codex_mod

    built = []

    class Quota(_Quota):
        def __init__(self, codex_client):
            built.append(codex_client.model)
            super().__init__(None)

    monkeypatch.setattr(codex_mod, "CodexQuotaClient", Quota)
    monkeypatch.setattr(dash, "_active_brain",
                        lambda: BrainConfig(provider="codex", model="gpt-5.5"))
    client.get("/api/usage")
    assert built == ["gpt-5.5"]


def _render_js(func: str, data, helpers=("escHtml", "formatDate")) -> str:
    """Run one app.js render function (and the helpers it uses) under node."""
    import json
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (Path(dash.__file__).parent / "web" / "app.js").read_text()
    funcs = []
    for name in (*helpers, func):
        match = re.search(rf"^function {name}\(.*?^}}\n", source, re.S | re.M)
        assert match, name
        funcs.append(match.group(0))
    script = "\n".join(funcs) + f"\nprocess.stdout.write({func}({json.dumps(data)}));"
    return subprocess.run([node, "-e", script], capture_output=True, text=True,
                          check=True, timeout=30).stdout


def _render_quota_card(data) -> str:
    return _render_js("quotaCardHtml", data)


def test_paused_brain_is_visible_on_the_dashboard(client, monkeypatch):
    import json
    asyncio.run(StateManager(str(client.db_path)).set_setting(
        brain_mod.BRAIN_STATUS_KEY, json.dumps({"provider": "codex", "status": "paused",
                                                "reason": "Your Codex login has expired.",
                                                "since": "2026-10-13T12:00:00Z"})))
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig(provider="codex"))
    stats = client.get("/api/stats").json()
    assert stats["brain_status"]["status"] == "paused"
    html = _render_js("brainCallChips", stats)
    assert "Codex <b>paused</b>" in html and "login has expired" in html
    healthy = _render_js("brainCallChips", {**stats, "brain_status": {}})
    assert "paused" not in healthy


def _status_cli(monkeypatch, tmp_path, capsys, *, check, config, setting=""):
    import harvey.cli as cli
    import harvey.config as config_mod
    import harvey.state as state_mod

    db = str(tmp_path / "status.db")
    if setting:
        async def seed():
            sm = StateManager(db)
            await sm.init_db()
            await sm.set_setting(brain_mod.BRAIN_STATUS_KEY, setting)
        asyncio.run(seed())
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(db))
    monkeypatch.setattr(config_mod, "load_brain_config", lambda: config)
    cli.cmd_status(type("A", (), {"check": check})())
    return capsys.readouterr().out


def test_cli_status_shows_a_recorded_pause(monkeypatch, tmp_path, capsys):
    import json
    out = _status_cli(monkeypatch, tmp_path, capsys, check=False,
                      config=BrainConfig(provider="codex", model="gpt-5.5"),
                      setting=json.dumps({"provider": "codex", "status": "paused",
                                          "reason": "Codex CLI 0.160.0 hasn't been verified",
                                          "since": "2026-10-13T12:00:00Z"}))
    assert "Codex PAUSED since 2026-10-13T12:00:00Z" in out
    assert "0.160.0 hasn't been verified" in out


def test_cli_status_check_reports_readiness(monkeypatch, tmp_path, capsys):
    real_preflight = Brain.preflight

    async def not_ready(self):
        return "Your Codex login has expired or is due for renewal."

    monkeypatch.setattr(brain_mod.Brain, "preflight", not_ready)
    with pytest.raises(SystemExit) as exit_info:  # scripts can act on it
        _status_cli(monkeypatch, tmp_path, capsys, check=True,
                    config=BrainConfig(provider="codex", model="gpt-5.5"))
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "Codex (gpt-5.5) NOT READY" in out and "due for renewal" in out

    async def ready(self):
        return ""

    monkeypatch.setattr(brain_mod.Brain, "preflight", ready)
    out = _status_cli(monkeypatch, tmp_path, capsys, check=True,
                      config=BrainConfig(provider="codex", model="gpt-5.5"))
    assert "Codex (gpt-5.5) ready" in out
    monkeypatch.setattr(brain_mod.Brain, "preflight", real_preflight)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    out = _status_cli(monkeypatch, tmp_path, capsys, check=True, config=BrainConfig())
    assert "Claude ready (API key present" in out
    # No credentials at all (the SDK found nothing in env or profiles).
    monkeypatch.setattr(brain_mod.anthropic, "AsyncAnthropic",
                        lambda **k: SimpleNamespace(api_key=None, auth_token=None))
    with pytest.raises(SystemExit):
        _status_cli(monkeypatch, tmp_path, capsys, check=True, config=BrainConfig())
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


def test_quota_card_renders_unavailable_codex_quota():
    html = _render_quota_card({"quota_provider": "codex", "quota_status": "unavailable",
                               "quota": None})
    assert "Codex (ChatGPT plan) Quota" in html
    assert "Quota unavailable" in html and "ChatGPT plan" in html
    assert "progress-bar" not in html


def test_quota_card_renders_gauges_and_limit_reached():
    html = _render_quota_card({"quota_provider": "codex", "quota_status": "available",
                               "quota": {"limit_reached": {"utilization": 100.0,
                                                           "resets_at": ""}}})
    assert "Usage limit reached" in html and "100%" in html
    assert _render_quota_card({"quota_provider": "", "quota": None}) == ""
    claude = _render_quota_card({"quota_provider": "claude", "quota": None,
                                 "totals": {"today": {"cost_usd": 1.5}, "week": {"cost_usd": 7}}})
    assert "Claude API Spend" in claude and "$1.50" in claude and "$7.00" in claude
    assert "max_daily_cost_usd" in claude and "Quota unavailable" not in claude
    assert "$0.00" in _render_quota_card({"quota_provider": "claude", "quota": None})


def test_cli_usage_reads_only_the_active_providers_quota(monkeypatch, tmp_path, capsys):
    import harvey.cli as cli
    import harvey.codex as codex_mod
    import harvey.config as config_mod
    import harvey.state as state_mod

    monkeypatch.setattr(config_mod, "load_brain_config", lambda: BrainConfig(provider="codex"))
    monkeypatch.setattr(state_mod, "StateManager",
                        lambda: StateManager(str(tmp_path / "u.db")))

    class CodexQuota:
        def __init__(self, client):
            assert client.model == ""

        async def get_utilization(self):
            return {"five_hour": {"utilization": 21.0, "resets_at": ""}}

    monkeypatch.setattr(codex_mod, "CodexQuotaClient", CodexQuota)
    cli.cmd_usage(type("A", (), {"days": 30})())
    out = capsys.readouterr().out
    assert "Codex Usage" in out and "Claude Usage" not in out
    assert "21.0% used" in out


_STALE_CODEX_PAUSE = (
    '{"provider": "codex", "status": "paused", "kind": "auth", '
    '"reason": "Your Codex login has expired.", "since": "2026-10-13T12:00:00Z"}')


def test_dashboard_hides_another_providers_pause(client, monkeypatch):
    asyncio.run(StateManager(str(client.db_path)).set_setting(
        brain_mod.BRAIN_STATUS_KEY, _STALE_CODEX_PAUSE))
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig())  # now Claude
    stats = client.get("/api/stats").json()
    assert stats["brain_status"] == {}
    assert "paused" not in _render_js("brainCallChips", stats)
    monkeypatch.setattr(dash, "_active_brain", lambda: BrainConfig(provider="codex"))
    assert client.get("/api/stats").json()["brain_status"]["status"] == "paused"


def test_cli_status_hides_another_providers_pause(monkeypatch, tmp_path, capsys):
    out = _status_cli(monkeypatch, tmp_path, capsys, check=False, config=BrainConfig(),
                      setting=_STALE_CODEX_PAUSE)
    assert "PAUSED" not in out
    out = _status_cli(monkeypatch, tmp_path / "x", capsys, check=False,
                      config=BrainConfig(provider="codex", model="gpt-5.5"),
                      setting=_STALE_CODEX_PAUSE)
    assert "Codex PAUSED" in out


def test_anthropic_headers_carry_the_workspace(monkeypatch):
    from harvey.brain import anthropic_headers

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert anthropic_headers() == {}
    assert anthropic_headers("wrkspc_1") == {"anthropic-workspace-id": "wrkspc_1"}
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", " wrkspc_2 ")
    assert anthropic_headers() == {"anthropic-workspace-id": "wrkspc_2"}
    assert anthropic_headers("") == {}      # an explicit blank wins over the environment
