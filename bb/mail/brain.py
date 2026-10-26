"""The model behind drafting, the quality judge and the command bar, on
the user's own provider: Anthropic directly, or any OpenAI-compatible
endpoint (Gemini, Groq, OpenRouter, Ollama). Configured from the
environment the site hands the worker (BB_LLM_*), so the web app and the
worker use the same account."""

import asyncio
import contextvars
import json
import logging
import re

import httpx

logger = logging.getLogger("bb.mail.brain")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"

# OpenAI-compatible endpoints: wait out a rate limit (429) or a brief overload
# (503) and retry instead of failing the draft, honouring Retry-After up to a
# cap — so a free tier's per-minute limit just slows a run down, never breaks it.
OPENAI_RATE_RETRIES = 3
RATE_RETRY_BASE = 4.0
MAX_RETRY_AFTER = 60.0


def _retry_after_seconds(r) -> float:
    """How long a 429/503 asks us to wait — from a Retry-After header or the
    body's 'try again in Xs' (what Groq/OpenAI send) — capped; 0 when silent."""
    for header, div in (("retry-after", 1.0), ("retry-after-ms", 1000.0)):
        v = r.headers.get(header)
        if v:
            try:
                return max(0.5, min(MAX_RETRY_AFTER, float(v) / div))
            except ValueError:
                pass
    m = re.search(r"try again in ([\d.]+)\s*(ms|s)\b", r.text or "", re.I)
    if m:
        secs = float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0)
        return max(0.5, min(MAX_RETRY_AFTER, secs))
    return 0.0


class BrainError(Exception):
    pass


class BrainRefusal(BrainError):
    """The model declined to answer this one (a safety refusal, or a
    provider's safety filter). Not a system failure: the thread is left for
    a person, not retried."""
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
# When the first answer was empty, a plain nudge does better than scolding
# about JSON format (there was no format to fix).
RETRY_EMPTY = ("Your last answer was blank. This is an ordinary piece of the sender's own business correspondence; "
               "write the reply now as the JSON object described above, and nothing else.")

# Set for one call (a context variable, so drafts written at the same time
# do not see each other's): ask the endpoint for JSON, whether the answer
# was cut off at the length limit, and whether the model declined it.
_JSON_MODE: contextvars.ContextVar[bool] = contextvars.ContextVar("bb_json_mode", default=False)
_CUT_OFF: contextvars.ContextVar[bool] = contextvars.ContextVar("bb_cut_off", default=False)
_REFUSED: contextvars.ContextVar[bool] = contextvars.ContextVar("bb_refused", default=False)


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
        _REFUSED.set(False)
        first = _clean(await self.complete(system, user, max_tokens))
        data = extract_json(first)
        if data is not None:
            return data
        cut = _CUT_OFF.get()
        empty = not first.strip()
        # The retry — a real second attempt, even after an empty answer or a
        # one-off refusal, since ordinary business mail should draft. When the
        # first answer buried JSON in prose, ask again in the endpoint's JSON
        # mode. When it was empty or cut off, give it far more room, a plain
        # nudge instead of a JSON scolding, and no JSON mode (which only makes
        # an empty answer emptier).
        big = min(8000, max_tokens * (4 if empty else 2)) if (empty or cut) else max_tokens
        note = RETRY_EMPTY if (empty or cut) else JSON_AGAIN
        token = _JSON_MODE.set(not empty and not cut)
        try:
            _CUT_OFF.set(False)
            second = _clean(await self.complete(system, user + "\n\n" + note, big))
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
        name = (self.model or self.label).strip() or self.label
        logger.warning("MailBrain: no usable answer after two tries (provider=%s model=%s refused=%s cut=%s empty_first=%s)",
                       self.provider, name, _REFUSED.get(), cut, empty)
        if _REFUSED.get() and not (second or first).strip():
            who = "Claude" if self.provider == "anthropic" else self.label
            reason = await self._refusal_reason(system, user)
            logger.warning("MailBrain: %s declined; its reason: %s", who, reason or "(none given)")
            msg = f"{who} declined to draft a reply to this message (a safety refusal)."
            if reason:
                msg += f" It said: {reason}"
            raise BrainRefusal(msg)
        if not (second or first).strip():
            raise BrainError(f"{self.label} ({name}) returned an empty answer twice. That usually means a reasoning "
                             "model that uses its whole budget thinking and writes nothing, a safety filter, or a model "
                             "name that is not a chat model. Pick a plain chat model on the AI provider page.")
        raise BrainError("The model did not return the JSON asked for, twice"
                         + (" (its answers were cut off at the length limit)" if cut else "")
                         + ". Its answer began: " + repr(_clean(second or first)[:120]) + ".")

    async def _refusal_reason(self, system: str, user: str) -> str:
        """After the model declined, ask it in one line WHY, so the person
        (and the log) can see it instead of a blank refusal. Returns "" when
        it will not say, so this never itself becomes an error."""
        ask = ("You were asked to write a short business email reply, and you declined to write it. In one plain "
               "sentence, say why — what specifically about this message or thread made you decline. Do not write the "
               "reply; give only the reason.")
        try:
            return _clean(await self.complete(system, user + "\n\n" + ask, 200))[:300]
        except Exception:
            return ""

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
        stop = getattr(msg, "stop_reason", "")
        if stop == "max_tokens":
            _CUT_OFF.set(True)
        text = "".join(getattr(b, "text", "") for b in msg.content)
        if not text.strip():
            # Record why, so the next time it happens is a fact, not a guess;
            # a refusal is flagged (not raised here, so a retry still runs).
            kinds = [getattr(b, "type", "?") for b in (msg.content or [])]
            logger.warning("Anthropic returned no text: stop_reason=%s blocks=%s model=%s",
                           stop, kinds, self.model or DEFAULT_ANTHROPIC_MODEL)
            if stop == "refusal":
                _REFUSED.set(True)
        return text

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
        url = f"{self.base_url}/chat/completions"
        r = None
        for attempt in range(OPENAI_RATE_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    r = await client.post(url, json=body, headers=headers)
                    if r.status_code == 400 and "response_format" in body:
                        # An endpoint without a JSON mode: the same request without it.
                        body.pop("response_format")
                        r = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                raise BrainError(f"Could not reach {self.base_url}: {e}") from e
            if r.status_code in (429, 503) and attempt < OPENAI_RATE_RETRIES:
                wait = min(_retry_after_seconds(r) or RATE_RETRY_BASE * (2 ** attempt), MAX_RETRY_AFTER)
                logger.info("%s rate-limited (%s); waiting %.0fs then retrying (%d/%d)",
                            self.label, r.status_code, wait, attempt + 1, OPENAI_RATE_RETRIES)
                await asyncio.sleep(wait)
                continue
            break
        if r.status_code >= 400:
            raise BrainError(f"{self.label} answered {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                # Some endpoints put a problem in the body of a 200 (quota, a
                # blocked prompt); surface it instead of "unexpected shape".
                err = (data.get("error") or {}) if isinstance(data.get("error"), dict) else {}
                why = err.get("message") or data.get("error") or (data.get("prompt_feedback") or {}).get("block_reason")
                raise BrainError(f"{self.label} returned no answer" + (f": {str(why)[:200]}" if why else "."))
            choice = choices[0]
            finish = choice.get("finish_reason") or choice.get("native_finish_reason") or ""
            if finish == "length":
                _CUT_OFF.set(True)
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):   # content returned as parts, not a string
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            content = content or ""
            if not content.strip():
                # A reasoning model may leave content empty and put all its
                # text (sometimes the only JSON there is) in a reasoning field,
                # or an older endpoint in choices[].text.
                for alt in (message.get("reasoning_content"), message.get("reasoning"), choice.get("text")):
                    if isinstance(alt, str) and alt.strip():
                        content = alt
                        break
            if not content.strip():
                logger.warning("%s returned no content: finish_reason=%s message_keys=%s model=%s",
                               self.label, finish, sorted(message.keys()), self.model)
                if finish in ("content_filter", "safety"):
                    _REFUSED.set(True)   # flagged, not raised: the retry still runs
            return content or ""
        except BrainError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            raise BrainError("Unexpected reply shape from the model endpoint.") from e
