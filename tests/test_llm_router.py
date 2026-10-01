from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from reuse_radar.llm.cache import CachedResponse, LLMCache, cache_key, canonical_prompt
from reuse_radar.llm.router import (
    AllProvidersExhaustedError,
    GeminiProvider,
    GroqProvider,
    LLMRequest,
    MinuteBudget,
    ProviderFatalError,
    RequestTooLargeError,
    Router,
)
from reuse_radar.llm.schemas import EXTRACTION_JSON_SCHEMA

from .conftest import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
GROQ_OK = json.loads((FIXTURES / "groq_ok.json").read_text(encoding="utf-8"))
GEMINI_OK = json.loads((FIXTURES / "gemini_ok.json").read_text(encoding="utf-8"))
REQUEST = LLMRequest(
    system="sys",
    messages=(("user", "passages"),),
    schema_name="declared_products",
    json_schema=EXTRACTION_JSON_SCHEMA,
    max_output_tokens=1000,
)
Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, groq: list[httpx.Response | Handler], gemini: list[httpx.Response]):
        self.groq = list(groq)
        self.gemini = list(gemini)
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.host == "api.groq.com":
            self.seen.append(("groq", body))
            item = self.groq.pop(0)
        else:
            self.seen.append(("gemini", body))
            item = self.gemini.pop(0)
        return item(request) if callable(item) else item


def _router(
    tmp_path: Path, clock: FakeClock, rec: Recorder, **kwargs: Any
) -> tuple[Router, GroqProvider, GeminiProvider]:
    http = httpx.Client(transport=httpx.MockTransport(rec))
    groq = GroqProvider(
        "gsk_test",
        http=http,
        budget=MinuteBudget(rpm=1000, tpm=None, clock=clock, sleep=clock.sleep),
    )
    gemini = GeminiProvider(
        "gem_test",
        http=http,
        budget=MinuteBudget(rpm=1000, tpm=None, clock=clock, sleep=clock.sleep),
    )
    router = Router(
        providers=[groq, gemini],
        cache=LLMCache(tmp_path / "llm"),
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )
    return router, groq, gemini


def _ok_groq() -> httpx.Response:
    return httpx.Response(200, json=GROQ_OK)


def _ok_gemini() -> httpx.Response:
    return httpx.Response(200, json=GEMINI_OK)


# --- request shapes ---------------------------------------------------------------------------


def test_groq_payload_uses_strict_schema_and_medium_reasoning() -> None:
    payload = GroqProvider("k").payload(REQUEST)
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == EXTRACTION_JSON_SCHEMA
    assert payload["temperature"] == 0
    assert payload["reasoning_effort"] == "medium"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}


def test_gemini_payload_maps_roles_and_floors_output_budget() -> None:
    request = LLMRequest("sys", (("user", "a"), ("assistant", "b")), "n", {}, 1000)
    payload = GeminiProvider("k").payload(request)
    assert [c["role"] for c in payload["contents"]] == ["user", "model"]
    assert payload["generationConfig"]["maxOutputTokens"] == 12_000  # thinking needs room
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium"}
    assert payload["generationConfig"]["responseMimeType"] == "application/json"


# --- recorded responses parse -----------------------------------------------------------------


def test_parses_recorded_groq_response(tmp_path: Path, clock: FakeClock) -> None:
    router, *_ = _router(tmp_path, clock, Recorder([_ok_groq()], []))
    result = router.complete(REQUEST)
    assert result.provider == "groq" and not result.cached
    assert result.text == GROQ_OK["choices"][0]["message"]["content"]
    assert result.input_tokens == GROQ_OK["usage"]["prompt_tokens"]
    assert result.output_tokens == GROQ_OK["usage"]["completion_tokens"]


def test_parses_recorded_gemini_response_including_thinking_tokens(
    tmp_path: Path, clock: FakeClock
) -> None:
    rec = Recorder([httpx.Response(429, headers={"retry-after": "7200"})], [_ok_gemini()])
    router, *_ = _router(tmp_path, clock, rec)
    result = router.complete(REQUEST)
    usage = GEMINI_OK["usageMetadata"]
    assert result.provider == "gemini"
    assert result.output_tokens == usage["candidatesTokenCount"] + usage["thoughtsTokenCount"]
    assert "products" in json.loads(result.text)
    assert "model" not in rec.seen[1][1]  # model goes in the URL, not the body


# --- cache ------------------------------------------------------------------------------------


def test_second_call_is_served_from_cache(tmp_path: Path, clock: FakeClock) -> None:
    rec = Recorder([_ok_groq()], [])
    router, *_ = _router(tmp_path, clock, rec)
    first = router.complete(REQUEST)
    second = router.complete(REQUEST)
    assert second.cached and second.text == first.text
    assert len(rec.seen) == 1


def test_cache_hit_from_fallback_provider_is_used(tmp_path: Path, clock: FakeClock) -> None:
    """A rerun is free even if the first run was answered by the fallback provider."""
    router, _, gemini = _router(tmp_path, clock, Recorder([], []))
    prompt = canonical_prompt(gemini.payload(REQUEST))
    router.cache.set(prompt, CachedResponse("gemini", gemini.model, '{"products": []}', 1, 1, 0.0))
    assert router.complete(REQUEST).provider == "gemini"


def test_cache_key_covers_provider_model_and_prompt() -> None:
    keys = {
        cache_key("groq", "m", "p"),
        cache_key("gemini", "m", "p"),
        cache_key("groq", "m2", "p"),
        cache_key("groq", "m", "p2"),
    }
    assert len(keys) == 4


def test_truncated_output_is_not_cached(tmp_path: Path, clock: FakeClock) -> None:
    truncated = json.loads(json.dumps(GROQ_OK))
    truncated["choices"][0]["finish_reason"] = "length"
    rec = Recorder([httpx.Response(200, json=truncated), _ok_groq()], [])
    router, *_ = _router(tmp_path, clock, rec)
    assert router.complete(REQUEST).finish_reason == "length"
    assert not router.complete(REQUEST).cached
    assert len(rec.seen) == 2


# --- rate limits and fallback -----------------------------------------------------------------


def test_short_429_waits_and_retries_same_provider(tmp_path: Path, clock: FakeClock) -> None:
    rec = Recorder([httpx.Response(429, headers={"retry-after": "9"}), _ok_groq()], [])
    router, *_ = _router(tmp_path, clock, rec)
    assert router.complete(REQUEST).provider == "groq"
    assert 9.0 in clock.sleeps


def test_long_429_falls_back_and_pauses_provider(tmp_path: Path, clock: FakeClock) -> None:
    rec = Recorder(
        [httpx.Response(429, headers={"retry-after": "7200"})], [_ok_gemini(), _ok_gemini()]
    )
    router, *_ = _router(tmp_path, clock, rec)
    assert router.complete(REQUEST).provider == "gemini"
    other = LLMRequest("sys", (("user", "different passages"),), "n", EXTRACTION_JSON_SCHEMA)
    assert router.complete(other).provider == "gemini"
    assert [p for p, _ in rec.seen] == ["groq", "gemini", "gemini"]  # groq skipped while paused


def test_413_too_large_falls_back(tmp_path: Path, clock: FakeClock) -> None:
    rec = Recorder([httpx.Response(413, json={"error": {"message": "too large"}})], [_ok_gemini()])
    router, *_ = _router(tmp_path, clock, rec)
    assert router.complete(REQUEST).provider == "gemini"


def test_groq_invalid_json_generation_falls_back_to_gemini(
    tmp_path: Path, clock: FakeClock
) -> None:
    failed = {
        "error": {
            "message": "Failed to generate JSON.",
            "code": "json_validate_failed",
            "failed_generation": '{"products": [',
        }
    }
    rec = Recorder([httpx.Response(400, json=failed)], [_ok_gemini()])
    router, *_ = _router(tmp_path, clock, rec)
    assert router.complete(REQUEST).provider == "gemini"


def test_invalid_json_from_last_provider_is_returned_to_caller(
    tmp_path: Path, clock: FakeClock
) -> None:
    failed = {"error": {"code": "json_validate_failed", "failed_generation": "{bad"}}
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, json=failed)))
    groq = GroqProvider("k", http=http, budget=MinuteBudget(1000, None, clock, clock.sleep))
    router = Router([groq], LLMCache(tmp_path), sleep=clock.sleep, clock=clock)
    result = router.complete(REQUEST)
    assert result.finish_reason == "json_validate_failed" and result.text == "{bad"


def test_all_providers_exhausted_raises(tmp_path: Path, clock: FakeClock) -> None:
    rec = Recorder(
        [httpx.Response(429, headers={"retry-after": "7200"})],
        [httpx.Response(503)] * 3,
    )
    router, *_ = _router(tmp_path, clock, rec)
    with pytest.raises(AllProvidersExhaustedError):
        router.complete(REQUEST)


def test_gemini_retry_delay_from_error_details(tmp_path: Path, clock: FakeClock) -> None:
    body = {
        "error": {
            "code": 429,
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}],
        }
    }
    rec = Recorder(
        [httpx.Response(429, headers={"retry-after": "7200"})],
        [httpx.Response(429, json=body), _ok_gemini()],
    )
    router, *_ = _router(tmp_path, clock, rec)
    assert router.complete(REQUEST).provider == "gemini"
    assert 12.0 in clock.sleeps


def test_auth_failure_is_fatal_not_a_fallback(tmp_path: Path, clock: FakeClock) -> None:
    rec = Recorder([httpx.Response(401, json={"error": {"message": "Invalid API Key"}})], [])
    router, *_ = _router(tmp_path, clock, rec)
    with pytest.raises(ProviderFatalError, match="401"):
        router.complete(REQUEST)


# --- pacing -----------------------------------------------------------------------------------


def test_minute_budget_waits_for_token_headroom(clock: FakeClock) -> None:
    budget = MinuteBudget(rpm=100, tpm=1000, clock=clock, sleep=clock.sleep)
    budget.acquire(600)
    budget.acquire(600)  # must wait for the first to leave the 60 s window
    assert clock.sleeps and sum(clock.sleeps) >= 60.0


def test_minute_budget_settles_to_actual_usage(clock: FakeClock) -> None:
    budget = MinuteBudget(rpm=100, tpm=1000, clock=clock, sleep=clock.sleep)
    event = budget.acquire(900)
    MinuteBudget.settle(event, 100)
    budget.acquire(800)  # fits after settling: 100 + 800 <= 1000
    assert clock.sleeps == []


def test_request_larger_than_budget_is_rejected(clock: FakeClock) -> None:
    with pytest.raises(RequestTooLargeError):
        MinuteBudget(rpm=10, tpm=100, clock=clock, sleep=clock.sleep).acquire(101)


def test_estimate_counts_unescaped_text() -> None:
    """Backslashes and newlines must not be double-counted (seen live: 25% overestimate)."""
    latex = LLMRequest("s", (("user", "\\GeV\n" * 1000),), "n", {}, 0)
    plain = LLMRequest("s", (("user", "xGeVx" * 1000),), "n", {}, 0)
    groq = GroqProvider("k")
    assert groq.estimate_tokens(groq.payload(latex)) == groq.estimate_tokens(groq.payload(plain))
