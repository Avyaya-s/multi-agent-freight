"""LLM backend abstraction. NegotiationStrategy (negotiation.py) never talks
to a provider SDK directly -- only to this interface -- so the provider can
be swapped without touching negotiation logic.

Provider: Groq (`GROQ_API_KEY`), via its OpenAI-compatible endpoint. Not
Anthropic -- see CLAUDE.md for why, and for the real account rate limits
that drive most of the design choices here.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class LLMCallError(Exception):
    """Base for every backend-call failure NegotiationStrategy should catch
    and fall back from."""


class LLMTimeoutError(LLMCallError):
    pass


class LLMConnectionError(LLMCallError):
    pass


class LLMRateLimitedError(LLMCallError):
    pass


class LLMSchemaError(LLMCallError):
    """The call succeeded (HTTP 200) but the content wasn't valid JSON, or
    didn't match the negotiation schema. Logged as its own metric, distinct
    from the fallback-to-rule-based rate -- a schema failure that a retry
    then fixes never causes a fallback at all."""


@dataclass
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class LLMResponse:
    raw_text: str
    parsed: dict
    usage: LLMUsage
    latency_ms: float
    headers: dict


class LLMBackend(Protocol):
    def complete(self, system_prompt: str, user_prompt: str, model: str, timeout_seconds: float) -> LLMResponse: ...


class GroqBackend:
    """Groq's OpenAI-compatible chat completions endpoint, JSON-object mode.

    Deliberately not the stricter `json_schema` response_format: Groq's
    per-model support for it wasn't independently confirmed, so schema
    conformance is validated client-side (negotiation.py) instead of assumed
    from the API -- which is also what makes the schema-failure-rate metric
    meaningful rather than vacuous.

    The system prompt (protocol instructions + schema) must stay
    byte-identical across every call -- no timestamps, no per-call IDs -- so
    that if Groq does server-side prefix caching, there's a stable prefix for
    it to hit. No explicit cache-control parameter is sent: none was
    confirmed to exist on Groq's API (unlike Anthropic's), so this is the
    client-side half of that story, not the whole of it.
    """

    def __init__(self, api_key: str | None = None):
        self._client = OpenAI(api_key=api_key or os.environ.get("GROQ_API_KEY"), base_url=GROQ_BASE_URL)

    def complete(self, system_prompt: str, user_prompt: str, model: str, timeout_seconds: float) -> LLMResponse:
        t0 = time.perf_counter()
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                timeout=timeout_seconds,
            )
        except RateLimitError as e:
            raise LLMRateLimitedError(str(e)) from e
        except APITimeoutError as e:
            raise LLMTimeoutError(str(e)) from e
        except APIConnectionError as e:
            raise LLMConnectionError(str(e)) from e
        except (APIStatusError, OpenAIError) as e:
            raise LLMCallError(str(e)) from e

        latency_ms = (time.perf_counter() - t0) * 1000
        completion = raw.parse()
        headers = dict(raw.headers)

        raw_text = completion.choices[0].message.content or ""
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise LLMSchemaError(f"response was not valid JSON: {e}") from e

        usage = completion.usage
        return LLMResponse(
            raw_text=raw_text,
            parsed=parsed,
            usage=LLMUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ),
            latency_ms=latency_ms,
            headers=headers,
        )
