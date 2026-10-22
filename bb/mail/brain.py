"""The model behind drafting, the quality judge and the command bar, on
the user's own provider: Anthropic directly, or any OpenAI-compatible
endpoint (Gemini, Groq, OpenRouter, Ollama). Configured from the
environment the site hands the worker (BB_LLM_*), so the web app and the
worker use the same account."""

import contextvars
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


_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_FENCE_RE = re.compile(r"^[ \t]*```[A-Za-z]*[ \t]*$", re.M)
_OPEN_THINK_RE = re.compile(r"(?is)<(think|thinking|reasoning)>.*\Z")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_CLOSERS = {"{": "}", "[": "]"}


def _span(text: str, start: int) -> int:
    """Where the bracket at ``start`` closes (the index after it), quotes
    and escapes respected, or -1 when it never does (a cut-off answer)."""
    stack, in_str, esc = [], False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in _CLOSERS:
            stack.append(_CLOSERS[ch])
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return -1
            if not stack:
                return i + 1
    return -1


def _loads(candidate: str):
    # strict=False: a model often writes the line breaks of an email as real
    # line breaks inside the string, which strict JSON refuses.
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            return json.loads(attempt, strict=False)
        except ValueError:
            pass
    try:   # a Python dict ('single quotes', True/None) from a small model
        import ast
        data = ast.literal_eval(candidate)
        return data if isinstance(data, (dict, list)) else None
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return None


def _clean(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if re.search(r"(?i)</think>", text):        # the opening tag fell off: drop up to the close
        text = re.split(r"(?i)</think>", text)[-1]
    text = _OPEN_THINK_RE.sub("", text)          # reasoning cut off before it closed: none of it is an answer
    return _FENCE_RE.sub("", text).strip()


def extract_json(text: str):
    """The JSON object (or array) in a model reply. Tolerates code fences,
    prose around it, braces in that prose, a reasoning model's <think>
    block, real line breaks inside strings, trailing commas and Python
    style quoting."""
    text = _clean(text)
    if not text:
        return None
    for opener in ("{", "["):
        tries, start = 0, text.find(opener)
        while start >= 0 and tries < 40:
            end = _span(text, start)
            if end > 0:
                data = _loads(text[start:end])
                if data is not None:
                    return data
            tries += 1
            start = text.find(opener, start + 1)
    return None


def salvage_field(text: str, field: str) -> str:
    """A string field's value from an answer that is not valid JSON, cut
    off at the length limit included: everything after ``"field": "`` up
    to its closing quote, or to the end."""
    text = _clean(text)
    m = re.search(r'["\']' + re.escape(field) + r'["\']\s*:\s*"', text)
    if not m:
        return ""
    out, esc = [], False
    for ch in text[m.end():]:
        if esc:
            out.append({"n": "\n", "t": "\t", "r": "", '"': '"', "\\": "\\", "/": "/"}.get(ch, "\\" + ch))
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            break
        else:
            out.append(ch)
    return "".join(out).strip()


_PREAMBLE_RE = re.compile(r"(?i)^\s*(?:sure[,!.]?\s*)?(?:here(?:'s| is) (?:a |the |my |your )?(?:draft|reply|response|email|message)[^\n]*|"
                          r"(?:draft|reply|subject)\s*:)\s*\n+")


def plain_answer(text: str) -> str:
    """An answer written as the email itself instead of JSON: the email,
    without a "Here's a reply:" line. Empty when it looks like broken JSON."""
    text = _clean(text)
    if not text or text.lstrip()[:1] in "{[" or re.search(r'"\w+"\s*:', text):
        return ""
    return _PREAMBLE_RE.sub("", text, count=1).strip()


JSON_AGAIN = ("Your last answer could not be read as JSON. Answer again with ONLY the JSON object asked for: it starts with "
              "{ and ends with }, every string in double quotes, line breaks inside a string written as \\n, and nothing "
              "before or after it.")

# Set for one call (a context variable, so drafts written at the same time
# do not see each other's): ask the endpoint for JSON, and whether the
# answer was cut off at the length limit.
_JSON_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar("bb_json_mode", default=False)
_CUT_OFF: contextvars.ContextVar[bool] = contextvars.ContextVar("bb_cut_off", default=False)


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

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200, salvage: str = ""):
        """The JSON the prompt asks for. An answer that cannot be read is
        asked for once more, plainly (with more room when it was cut off,
        and in the endpoint's JSON mode where it has one). With ``salvage``
        (a string field, "body"), an answer that still is not JSON is used
        as that field, marked ``_salvaged`` so the caller routes it to a
        person, rather than failing the item."""
        _CUT_OFF.set(False)
        first = _clean(await self.complete(system, user, max_tokens))
        data = extract_json(first)
        if data is not None:
            return data
        cut = _CUT_OFF.get()
        token = _JSON_MODE.set(True)
        try:
            _CUT_OFF.set(False)
            second = _clean(await self.complete(system, user + "\n\n" + JSON_AGAIN, max_tokens * 2 if cut else max_tokens))
        except BrainError:
            if not salvage:
                raise
            second = ""            # (a refused second ask: the first answer may still be usable)
        finally:
            _JSON_MODE.reset(token)
        data = extract_json(second)
        if data is not None:
            return data
        cut = cut or _CUT_OFF.get()
        if salvage:
            for text in (second, first):
                value = salvage_field(text, salvage) or plain_answer(text)
                if value:
                    out = {salvage: value, "_salvaged": True, "_raw": text[:4000]}
                    for extra in ("subject", "confidence", "confidence_reason", "stance", "why"):
                        got = salvage_field(text, extra)
                        if got and extra != salvage:
                            out[extra] = got
                    return out
        raise BrainError("The model did not return the JSON asked for, twice"
                         + (" (its answers were cut off at the length limit)" if cut else "")
                         + (". Its answer began: " + repr(_clean(second or first)[:120]) if (second or first) else ": it answered nothing")
                         + ".")

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
        if getattr(msg, "stop_reason", "") == "max_tokens":
            _CUT_OFF.set(True)
        return "".join(getattr(b, "text", "") for b in msg.content)

    async def tool_turn(self, system: str, messages: list, tools: list, max_tokens: int = 1500) -> dict:
        """One model turn with tools (Anthropic only). Returns
        {text, tool_calls: [{id, name, input}], content: [blocks for the
        assistant turn], stop_reason}. ``messages`` is the running
        conversation in Anthropic's shape."""
        if not self.supports_tools:
            raise BrainError(f"{self.label} has no tool use here.")
        import anthropic

        from harvey.brain import anthropic_headers
        client = anthropic.AsyncAnthropic(api_key=self.api_key, max_retries=1, timeout=120,
                                          default_headers=anthropic_headers(self.workspace_id))
        try:
            msg = await client.messages.create(model=self.model or DEFAULT_ANTHROPIC_MODEL, max_tokens=max_tokens,
                                               system=system, messages=messages, tools=tools)
        except anthropic.APIStatusError as e:
            raise BrainError(f"Anthropic answered {e.status_code}: {str(e)[:200]}") from e
        except anthropic.APIError as e:
            raise BrainError(f"Anthropic error: {str(e)[:200]}") from e
        content, calls, text = [], [], []
        for b in msg.content:
            if getattr(b, "type", "") == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                calls.append({"id": b.id, "name": b.name, "input": dict(b.input or {})})
            elif getattr(b, "type", "") == "text":
                content.append({"type": "text", "text": b.text})
                text.append(b.text)
        return {"text": "".join(text), "tool_calls": calls, "content": content, "stop_reason": msg.stop_reason}

    async def _openai(self, system: str, user: str, max_tokens: int) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens, "temperature": 0.7}
        if _JSON_MODE.get():
            body["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                if r.status_code == 400 and "response_format" in body:
                    # An endpoint without a JSON mode: the same request without it.
                    body.pop("response_format")
                    r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise BrainError(f"Could not reach {self.base_url}: {e}") from e
        if r.status_code >= 400:
            raise BrainError(f"{self.label} answered {r.status_code}: {r.text[:200]}")
        try:
            choice = r.json()["choices"][0]
            if choice.get("finish_reason") == "length":
                _CUT_OFF.set(True)
            return choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            raise BrainError("Unexpected reply shape from the model endpoint.") from e
