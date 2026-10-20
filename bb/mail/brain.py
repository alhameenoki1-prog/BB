"""The model behind drafting, the quality judge and the command bar, on
the user's own provider: Anthropic directly, or any OpenAI-compatible
endpoint (Gemini, Groq, OpenRouter, Ollama). Configured from the
environment the site hands the worker (BB_LLM_*), so the web app and the
worker use the same account."""

import json
import re

import httpx

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


class BrainError(Exception):
    pass


def env_from_llm(llm: dict) -> dict:
    """The BB_LLM_* variables for a workspace's AI provider settings."""
    provider = llm.get("provider") or ""
    out = {"BB_LLM_PROVIDER": provider, "BB_LLM_API_KEY": llm.get("api_key") or "",
           "BB_LLM_BASE_URL": llm.get("base_url") or "", "BB_LLM_MODEL": llm.get("model") or ""}
    if llm.get("workspace_id"):
        out["ANTHROPIC_WORKSPACE_ID"] = llm["workspace_id"]
    return {k: v for k, v in out.items() if v}


def extract_json(text: str):
    """The first JSON object or array in a model reply, fences and prose tolerated."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
    return None


class MailBrain:
    def __init__(self, env: dict):
        self.provider = (env.get("BB_LLM_PROVIDER") or "").lower()
        self.api_key = env.get("BB_LLM_API_KEY") or ""
        self.base_url = (env.get("BB_LLM_BASE_URL") or "").rstrip("/")
        self.model = env.get("BB_LLM_MODEL") or ""
        self.workspace_id = env.get("ANTHROPIC_WORKSPACE_ID") or ""

    @property
    def configured(self) -> bool:
        if self.provider == "anthropic":
            return bool(self.api_key)
        return bool(self.provider and self.base_url and self.model)

    @property
    def supports_tools(self) -> bool:
        return self.provider == "anthropic"

    @property
    def label(self) -> str:
        return {"anthropic": "Anthropic Claude"}.get(self.provider, self.provider or "no AI provider")

    async def complete(self, system: str, user: str, max_tokens: int = 1200) -> str:
        if not self.configured:
            raise BrainError("No AI provider is set up. Add one on the AI provider page.")
        if self.provider == "anthropic":
            return await self._anthropic(system, user, max_tokens)
        return await self._openai(system, user, max_tokens)

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200):
        text = await self.complete(system, user, max_tokens)
        data = extract_json(text)
        if data is None:
            raise BrainError("The model did not return the JSON asked for.")
        return data

    async def _anthropic(self, system: str, user: str, max_tokens: int) -> str:
        import anthropic

        from harvey.brain import anthropic_headers
        client = anthropic.AsyncAnthropic(api_key=self.api_key, max_retries=1, timeout=90,
                                          default_headers=anthropic_headers(self.workspace_id))
        try:
            msg = await client.messages.create(model=self.model or DEFAULT_ANTHROPIC_MODEL, max_tokens=max_tokens,
                                               system=system, messages=[{"role": "user", "content": user}])
        except anthropic.APIStatusError as e:
            raise BrainError(f"Anthropic answered {e.status_code}: {str(e)[:200]}") from e
        except anthropic.APIError as e:
            raise BrainError(f"Anthropic error: {str(e)[:200]}") from e
        return "".join(getattr(b, "text", "") for b in msg.content)

    async def _openai(self, system: str, user: str, max_tokens: int) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens, "temperature": 0.7}
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise BrainError(f"Could not reach {self.base_url}: {e}") from e
        if r.status_code >= 400:
            raise BrainError(f"{self.label} answered {r.status_code}: {r.text[:200]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise BrainError("Unexpected reply shape from the model endpoint.") from e
