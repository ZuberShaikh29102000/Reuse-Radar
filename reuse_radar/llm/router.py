"""LLM provider router: Groq primary, Gemini fallback, cache first. Batch pipeline only.

SPEC section 2, item 2: nothing here may be imported by request-path code (Django views,
serializers). The web service reads precomputed rows.

Flow for one request:
1. Look up the cache under every provider/model. A hit from any provider is returned, so reruns
   are free no matter which provider originally answered.
2. Otherwise try providers in order. A provider is paced client-side (requests and tokens per
   minute) so free-tier limits are rarely hit. On 429 with a short Retry-After (a per-minute
   limit) we wait and retry the same provider; on a long one (a daily quota), on 413 (request too
   large for the provider's per-minute budget), or on persistent 5xx, we fall back to the next.
3. If every provider is exhausted, raise AllProvidersExhaustedError. The extract stage stops,
   keeping what is done; the next run resumes from the cache.

Request and response shapes were checked against the live APIs on 2026-10-01 (docs/adr/0003).
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from opentelemetry import metrics

from reuse_radar.llm.cache import CachedResponse, LLMCache, canonical_prompt

logger = logging.getLogger(__name__)
_meter = metrics.get_meter(__name__)
_cache_counter = _meter.create_counter(
    "llm_cache_lookups_total", description="LLM cache lookups, by result"
)
_fallback_counter = _meter.create_counter(
    "llm_provider_fallbacks_total", description="Requests moved to the next provider, by reason"
)
_tokens_counter = _meter.create_counter(
    "llm_tokens_total", description="Tokens consumed by live LLM calls, by provider and direction"
)
_requests_counter = _meter.create_counter(
    "llm_requests_total", description="Live LLM HTTP requests, by provider and status"
)

Clock = Callable[[], float]
Sleep = Callable[[float], None]
Role = Literal["user", "assistant"]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
# gemini-3.8-flash answered 503 "high demand" repeatedly on 2026-10-01; 3.5-flash was reliable.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


# --- request / result types -------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    system: str
    messages: tuple[tuple[Role, str], ...]
    schema_name: str
    json_schema: dict[str, Any]
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class LLMResult:
    provider: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    cached: bool
    finish_reason: str = "stop"


class LLMError(RuntimeError):
    """Base class for provider failures."""


class RateLimitedError(LLMError):
    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RequestTooLargeError(LLMError):
    """The request exceeds what this provider accepts (e.g. Groq's per-minute token budget)."""


class UnavailableError(LLMError):
    """5xx or transport failure; transient."""


class ProviderFatalError(LLMError):
    """Bad request, auth failure or an unexpected response shape. Never retried: fix the code
    or the configuration."""


class AllProvidersExhaustedError(LLMError):
    """Every provider is rate-limited or unavailable. The stage stops; rerun later."""


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str


class Provider(Protocol):
    name: str
    model: str

    def payload(self, request: LLMRequest) -> dict[str, Any]:
        """Provider-specific request body; its canonical JSON is the cache prompt."""
        ...

    def estimate_tokens(self, payload: dict[str, Any]) -> int: ...

    def send(self, payload: dict[str, Any]) -> Completion: ...


# --- pacing -----------------------------------------------------------------------------------


class MinuteBudget:
    """Client-side pacing to a requests-per-minute and tokens-per-minute budget."""

    def __init__(
        self,
        rpm: int,
        tpm: int | None,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._rpm = rpm
        self._tpm = tpm
        self._clock = clock
        self._sleep = sleep
        self._events: deque[list[float]] = deque()  # [timestamp, tokens]

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= 60.0:
            self._events.popleft()

    def acquire(self, tokens: int) -> list[float]:
        """Block until a request of `tokens` fits; return its handle for `settle`."""
        if self._tpm is not None and tokens > self._tpm:
            raise RequestTooLargeError(f"request of ~{tokens} tokens exceeds {self._tpm} TPM")
        while True:
            now = self._clock()
            self._prune(now)
            used = sum(e[1] for e in self._events)
            over_rpm = len(self._events) >= self._rpm
            over_tpm = self._tpm is not None and used + tokens > self._tpm
            if not over_rpm and not over_tpm:
                event = [now, float(tokens)]
                self._events.append(event)
                return event
            self._sleep(max(0.05, self._events[0][0] + 60.0 - now))

    @staticmethod
    def settle(event: list[float], actual_tokens: int) -> None:
        """Replace the estimate with the provider-reported count."""
        event[1] = float(actual_tokens)


def _text_chars(value: Any) -> int:
    """Characters of every string (keys included) in a JSON-like value, *unescaped*.

    Measuring the serialised JSON instead would double-count every LaTeX backslash and newline
    and overestimate a paper's request by ~25% (seen live: 8.4k estimated for a request Groq
    accepts).
    """
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(len(str(k)) + _text_chars(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(_text_chars(v) for v in value)
    return len(str(value))


def _estimate(payload: dict[str, Any], max_output: int) -> int:
    # ~3.5 characters per token for LaTeX-heavy English, plus the full output budget (providers
    # reserve max output tokens against per-minute limits).
    return math.ceil(_text_chars(payload) / 3.5) + max_output


def _retry_after_header(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


# --- providers --------------------------------------------------------------------------------


class GroqProvider:
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GROQ_MODEL,
        *,
        http: httpx.Client | None = None,
        budget: MinuteBudget | None = None,
        # "low" found 5 of ~12 products on a test paper (missed a likelihood scan and yield
        # tables); "medium" found 12 for ~5x the output tokens. See docs/adr/0003.
        reasoning_effort: str = "medium",
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is empty")
        self.model = model
        self._key = api_key
        self._http = http or httpx.Client(timeout=120.0)
        # Free tier for gpt-oss-120b (2026-10-01): 30 RPM, 8K TPM, 1K RPD, 200K TPD.
        # Budgeted at 7.6K: estimates run ~15% above Groq's own count (3.9K vs 3.3K seen live),
        # so this leaves real headroom under 8K.
        self.budget = budget or MinuteBudget(rpm=25, tpm=7_600)
        self._reasoning_effort = reasoning_effort

    def payload(self, request: LLMRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": r, "content": t} for r, t in request.messages),
            ],
            "temperature": 0,
            "reasoning_effort": self._reasoning_effort,
            "include_reasoning": False,
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            },
        }

    def estimate_tokens(self, payload: dict[str, Any]) -> int:
        return _estimate(payload, int(payload["max_completion_tokens"]))

    def send(self, payload: dict[str, Any]) -> Completion:
        event = self.budget.acquire(self.estimate_tokens(payload))
        try:
            response = self._http.post(
                GROQ_URL, json=payload, headers={"Authorization": f"Bearer {self._key}"}
            )
        except httpx.TransportError as exc:
            raise UnavailableError(f"groq transport error: {exc}") from exc
        _requests_counter.add(1, {"provider": self.name, "status": response.status_code})

        if response.status_code == 429:
            raise RateLimitedError(
                f"groq 429: {response.text[:300]}", _retry_after_header(response)
            )
        if response.status_code == 413:
            raise RequestTooLargeError(f"groq 413: {response.text[:300]}")
        if response.status_code >= 500:
            raise UnavailableError(f"groq {response.status_code}: {response.text[:300]}")
        if response.status_code == 400:
            failed = self._failed_generation(response)
            if failed is not None:
                # Strict mode intermittently rejects the model's own output (seen live). Surface
                # it as an output problem so the caller's schema-retry handles it; it is not
                # cached because finish_reason is not "stop".
                return Completion(failed, 0, 0, "json_validate_failed")
        if response.status_code != 200:
            raise ProviderFatalError(f"groq {response.status_code}: {response.text[:500]}")

        body = response.json()
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"]
            usage = body["usage"]
            input_tokens = int(usage["prompt_tokens"])
            output_tokens = int(usage["completion_tokens"])  # includes reasoning tokens
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderFatalError(f"unexpected groq response shape: {str(body)[:500]}") from exc
        MinuteBudget.settle(event, input_tokens + output_tokens)
        return Completion(text or "", input_tokens, output_tokens, choice.get("finish_reason", ""))

    @staticmethod
    def _failed_generation(response: httpx.Response) -> str | None:
        try:
            error = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            return None
        if error.get("code") != "json_validate_failed":
            return None
        return str(error.get("failed_generation", ""))


_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        *,
        http: httpx.Client | None = None,
        budget: MinuteBudget | None = None,
        thinking_level: str = "medium",
        min_output_tokens: int = 12_000,
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is empty")
        # Gemini counts thinking tokens against maxOutputTokens; at "medium" a 2.5K cap truncated
        # the answer mid-string (seen live). Gemini has no per-minute token cap to respect here.
        self._min_output_tokens = min_output_tokens
        self.model = model
        self._key = api_key
        self._http = http or httpx.Client(timeout=180.0)
        # Free-tier limits are per project and shown only in AI Studio; pace conservatively and
        # let 429 Retry-After handling cover the rest.
        self.budget = budget or MinuteBudget(rpm=8, tpm=None)
        self._thinking_level = thinking_level

    def payload(self, request: LLMRequest) -> dict[str, Any]:
        role_map = {"user": "user", "assistant": "model"}
        return {
            "model": self.model,  # not sent in the body; part of the cache prompt only
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [
                {"role": role_map[r], "parts": [{"text": t}]} for r, t in request.messages
            ],
            "generationConfig": {
                "maxOutputTokens": max(request.max_output_tokens, self._min_output_tokens),
                "responseMimeType": "application/json",
                "responseJsonSchema": request.json_schema,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
            },
        }

    def estimate_tokens(self, payload: dict[str, Any]) -> int:
        return _estimate(payload, int(payload["generationConfig"]["maxOutputTokens"]))

    def send(self, payload: dict[str, Any]) -> Completion:
        body_out = {k: v for k, v in payload.items() if k != "model"}
        event = self.budget.acquire(self.estimate_tokens(payload))
        try:
            response = self._http.post(
                GEMINI_URL.format(model=self.model),
                json=body_out,
                headers={"x-goog-api-key": self._key},
            )
        except httpx.TransportError as exc:
            raise UnavailableError(f"gemini transport error: {exc}") from exc
        _requests_counter.add(1, {"provider": self.name, "status": response.status_code})

        if response.status_code == 429:
            raise RateLimitedError(
                f"gemini 429: {response.text[:300]}", self._retry_delay(response)
            )
        if response.status_code >= 500:
            raise UnavailableError(f"gemini {response.status_code}: {response.text[:300]}")
        if response.status_code != 200:
            raise ProviderFatalError(f"gemini {response.status_code}: {response.text[:500]}")

        body = response.json()
        try:
            candidate = body["candidates"][0]
            parts = candidate["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            usage = body["usageMetadata"]
            input_tokens = int(usage["promptTokenCount"])
            output_tokens = int(usage.get("candidatesTokenCount", 0)) + int(
                usage.get("thoughtsTokenCount", 0)
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderFatalError(
                f"unexpected gemini response shape: {str(body)[:500]}"
            ) from exc
        MinuteBudget.settle(event, input_tokens + output_tokens)
        finish = str(candidate.get("finishReason", "")).lower()
        return Completion(text, input_tokens, output_tokens, finish)

    @staticmethod
    def _retry_delay(response: httpx.Response) -> float | None:
        header = _retry_after_header(response)
        if header is not None:
            return header
        try:
            details = response.json()["error"]["details"]
        except (ValueError, KeyError, TypeError):
            return None
        for detail in details:
            match = _RETRY_DELAY.match(str(detail.get("retryDelay", "")))
            if match:
                return float(match.group(1))
        return None


# --- router -----------------------------------------------------------------------------------


@dataclass
class Router:
    providers: Sequence[Provider]
    cache: LLMCache
    sleep: Sleep = time.sleep
    clock: Clock = time.monotonic
    max_wait_s: float = 65.0  # longest Retry-After worth waiting for (per-minute limits)
    max_attempts: int = 3
    # When a provider reports a long rate limit (a daily quota), skip it until it resets
    # instead of spending a request per paper to rediscover the 429.
    default_pause_s: float = 3600.0
    used_live: dict[str, int] = field(default_factory=dict)
    paused_until: dict[str, float] = field(default_factory=dict)

    def complete(self, request: LLMRequest) -> LLMResult:
        prompts = [(p, canonical_prompt(p.payload(request))) for p in self.providers]

        for provider, prompt in prompts:
            hit = self.cache.get(provider.name, provider.model, prompt)
            if hit is not None:
                _cache_counter.add(1, {"result": "hit"})
                return LLMResult(
                    hit.provider, hit.model, hit.text, hit.input_tokens, hit.output_tokens, True
                )
        _cache_counter.add(1, {"result": "miss"})

        for index, (provider, prompt) in enumerate(prompts):
            if self.paused_until.get(provider.name, 0.0) > self.clock():
                continue
            completion = self._try_provider(provider, request)
            has_next = index + 1 < len(prompts)
            # Groq strict mode intermittently rejects its own output; another provider usually
            # answers the same request fine, so prefer that over failing the caller.
            invalid = completion is not None and completion.finish_reason == "json_validate_failed"
            if completion is None or (invalid and has_next):
                if has_next:
                    reason = "invalid_output" if invalid else "exhausted"
                    _fallback_counter.add(
                        1,
                        {"from": provider.name, "to": prompts[index + 1][0].name, "reason": reason},
                    )
                    logger.warning(
                        "llm provider failed, falling back",
                        extra={
                            "from": provider.name,
                            "to": prompts[index + 1][0].name,
                            "reason": reason,
                        },
                    )
                continue
            assert completion is not None
            # Only well-formed completions are cached; truncated ones would replay forever.
            if completion.finish_reason in ("stop", ""):
                self.cache.set(
                    prompt,
                    CachedResponse(
                        provider=provider.name,
                        model=provider.model,
                        text=completion.text,
                        input_tokens=completion.input_tokens,
                        output_tokens=completion.output_tokens,
                        created_at=time.time(),
                    ),
                )
            _tokens_counter.add(completion.input_tokens, {"provider": provider.name, "dir": "in"})
            _tokens_counter.add(completion.output_tokens, {"provider": provider.name, "dir": "out"})
            self.used_live[provider.name] = self.used_live.get(provider.name, 0) + 1
            return LLMResult(
                provider.name,
                provider.model,
                completion.text,
                completion.input_tokens,
                completion.output_tokens,
                False,
                completion.finish_reason or "stop",
            )
        raise AllProvidersExhaustedError("every LLM provider is rate-limited or unavailable")

    def _try_provider(self, provider: Provider, request: LLMRequest) -> Completion | None:
        """Return a completion, or None if this provider should be skipped for now."""
        payload = provider.payload(request)
        for attempt in range(self.max_attempts):
            try:
                return provider.send(payload)
            except RequestTooLargeError as exc:
                logger.warning(
                    "llm request too large", extra={"provider": provider.name, "error": str(exc)}
                )
                return None
            except RateLimitedError as exc:
                wait = exc.retry_after
                if wait is None or wait > self.max_wait_s or attempt + 1 == self.max_attempts:
                    pause = wait if wait is not None else self.default_pause_s
                    self.paused_until[provider.name] = self.clock() + pause
                    logger.warning(
                        "llm provider rate-limited; pausing it",
                        extra={"provider": provider.name, "retry_after": wait},
                    )
                    return None
                self.sleep(wait)
            except UnavailableError as exc:
                if attempt + 1 == self.max_attempts:
                    logger.warning(
                        "llm provider unavailable",
                        extra={"provider": provider.name, "error": str(exc)},
                    )
                    return None
                self.sleep(5.0 * 2**attempt)
        return None


def router_from_env(cache_dir: Path) -> Router:
    """Build the default router (Groq, then Gemini) from environment variables."""
    providers: list[Provider] = [
        GroqProvider(
            os.environ.get("GROQ_API_KEY", "").strip(),
            os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        ),
        GeminiProvider(
            os.environ.get("GEMINI_API_KEY", "").strip(),
            os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        ),
    ]
    return Router(providers=providers, cache=LLMCache(cache_dir / "llm"))
