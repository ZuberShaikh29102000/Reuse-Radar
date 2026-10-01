from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from reuse_radar.clients.inspire import (
    DiskCache,
    IncompleteHarvestError,
    InspireClient,
    InspireHTTPError,
    QueryTooLargeError,
    SlidingWindowRateLimiter,
    UnexpectedResponseError,
    scrub_emails,
)
from reuse_radar.pipeline.harvest import FIELDS, CorpusConfig

from .conftest import FakeClock, ReplayTransport

MakeClient = Callable[..., tuple[InspireClient, ReplayTransport]]
SLICE_QUERY = CorpusConfig(
    name="t", query="collaboration ATLAS and tc published", start_year=2021, end_year=2021
).slice_query(2021)


def _search(client: InspireClient) -> list[dict[str, Any]]:
    return list(client.search_literature(SLICE_QUERY, fields=FIELDS, page_size=40))


# --- rate limiter -------------------------------------------------------------------------


def test_limiter_never_exceeds_window(clock: FakeClock) -> None:
    limiter = SlidingWindowRateLimiter(
        max_requests=15, window_s=5.0, min_interval_s=0.0, clock=clock, sleep=clock.sleep
    )
    sent = []
    for _ in range(50):
        limiter.acquire()
        sent.append(clock.now)
    for i in range(len(sent) - 15):
        assert sent[i + 15] - sent[i] >= 5.0


def test_limiter_paces_to_min_interval(clock: FakeClock) -> None:
    limiter = SlidingWindowRateLimiter(clock=clock, sleep=clock.sleep)
    sent = []
    for _ in range(5):
        limiter.acquire()
        sent.append(clock.now)
    assert [b - a for a, b in pairwise(sent)] == [1.0] * 4


# --- pagination and totals ----------------------------------------------------------------


def test_follows_links_next_and_yields_every_record(make_client: MakeClient) -> None:
    client, transport = make_client()
    hits = _search(client)
    assert len(hits) == 67
    assert len(transport.requests) == 2
    assert len({h["metadata"]["control_number"] for h in hits}) == 67


def test_user_agent_carries_contact_email(make_client: MakeClient) -> None:
    client, transport = make_client()
    _search(client)
    assert "mailto:test@example.org" in transport.requests[0].headers["User-Agent"]


def test_query_over_10000_raises_before_yielding(make_client: MakeClient) -> None:
    client, _ = make_client()
    gen = client.search_literature("t higgs", fields=("control_number",), page_size=1)
    with pytest.raises(QueryTooLargeError, match="24887"):
        next(gen)


def test_missing_page_is_detected(make_client: MakeClient) -> None:
    """If page 2 vanishes (no links.next on page 1) the count check must fail loudly."""

    def drop_next(request: httpx.Request) -> httpx.Response | None:
        if "page=1" in str(request.url):
            body = _fixture_body(request)
            del body["links"]["next"]
            return httpx.Response(200, json=body)
        return None

    client, _ = make_client(drop_next)
    with pytest.raises(IncompleteHarvestError, match=r"received 40 records, hits\.total is 67"):
        _search(client)


def test_missing_total_fails_loudly(make_client: MakeClient) -> None:
    def no_total(request: httpx.Request) -> httpx.Response:
        body = _fixture_body(request)
        del body["hits"]["total"]
        return httpx.Response(200, json=body)

    client, _ = make_client(no_total)
    with pytest.raises(UnexpectedResponseError, match=r"hits\.total"):
        _search(client)


def test_off_api_next_link_rejected(make_client: MakeClient) -> None:
    def evil_next(request: httpx.Request) -> httpx.Response:
        body = _fixture_body(request)
        body["links"]["next"] = "https://example.com/steal"
        return httpx.Response(200, json=body)

    client, _ = make_client(evil_next)
    with pytest.raises(UnexpectedResponseError, match="off-API"):
        _search(client)


# --- caching ------------------------------------------------------------------------------


def test_rerun_is_served_entirely_from_cache(make_client: MakeClient) -> None:
    client, transport = make_client()
    first = _search(client)
    second = _search(client)
    assert first == second
    assert len(transport.requests) == 2  # no new requests on the second run


def test_search_cache_expires_after_ttl(make_client: MakeClient, clock: FakeClock) -> None:
    client, transport = make_client()
    _search(client)
    clock.now += 86_400 - 1
    _search(client)
    assert len(transport.requests) == 2  # still fresh
    clock.now += 2
    _search(client)
    assert len(transport.requests) == 4  # expired: both pages refetched


def test_one_live_page_forces_the_rest_live(
    make_client: MakeClient, clock: FakeClock, tmp_path: Path
) -> None:
    """Never stitch a result set from snapshots taken at different times."""
    client, transport = make_client()
    _search(client)
    first_page_url = str(transport.requests[0].url)
    digest = hashlib.sha256(first_page_url.encode()).hexdigest()
    (tmp_path / "cache" / "inspire" / digest[:2] / f"{digest}.json").unlink()

    _search(client)
    # page 1 refetched (evicted), page 2 refetched although its cache entry was still fresh
    assert len(transport.requests) == 4


def test_get_json_without_max_age_never_expires(tmp_path: Path) -> None:
    now = {"t": 0.0}
    cache = DiskCache(tmp_path, now=lambda: now["t"])
    cache.set("https://a/1", {"x": 1})
    now["t"] = 10**9
    assert cache.get("https://a/1") == {"x": 1}
    assert cache.get("https://a/1", max_age_s=60) is None


def test_cache_disabled_refetches(make_client: MakeClient) -> None:
    client, transport = make_client(use_cache=False)
    _search(client)
    _search(client)
    assert len(transport.requests) == 4


def test_cache_is_keyed_by_url_hash(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path)
    cache.set("https://a/1", {"x": 1})
    assert cache.get("https://a/1") == {"x": 1}
    assert cache.get("https://a/2") is None
    [entry] = list(tmp_path.rglob("*.json"))
    assert len(entry.stem) == 64  # sha256 hex digest


def test_errors_are_not_cached(make_client: MakeClient, clock: FakeClock) -> None:
    calls = {"n": 0}

    def fail_once(request: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else None

    client, transport = make_client(fail_once)
    _search(client)
    _search(client)
    assert len(transport.requests) == 3  # one 503, then two pages, then all cached


# --- 429 and backoff ----------------------------------------------------------------------


def test_429_backs_off_at_least_one_full_window(make_client: MakeClient, clock: FakeClock) -> None:
    calls = {"n": 0}

    def throttle_first(request: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else None

    client, transport = make_client(throttle_first)
    assert len(_search(client)) == 67
    assert len(transport.requests) == 3
    assert max(clock.sleeps) >= 5.0


def test_429_backoff_is_exponential_and_honours_retry_after(
    make_client: MakeClient, clock: FakeClock
) -> None:
    calls = {"n": 0}

    def throttle(request: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        if calls["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "30"})
        if calls["n"] == 3:
            return httpx.Response(429)
        return None

    client, _ = make_client(throttle)
    _search(client)
    backoffs = [s for s in clock.sleeps if s >= 5.0]
    assert backoffs == [5.0, 30.0, 20.0]


def test_429_retries_exhausted_raises(make_client: MakeClient) -> None:
    client, transport = make_client(lambda r: httpx.Response(429), max_retries=2)
    with pytest.raises(InspireHTTPError, match="429"):
        _search(client)
    assert len(transport.requests) == 3


def test_429_attempts_count_against_the_window(make_client: MakeClient, clock: FakeClock) -> None:
    """A 429 still consumes a slot: with a 2-per-100s window, the third attempt must wait."""
    limiter = SlidingWindowRateLimiter(
        max_requests=2, window_s=100.0, min_interval_s=0.0, clock=clock, sleep=clock.sleep
    )
    client, transport = make_client(lambda r: httpx.Response(429), max_retries=2, limiter=limiter)
    with pytest.raises(InspireHTTPError):
        _search(client)
    assert len(transport.requests) == 3
    # attempts at t=0 and t=5 (after a 5 s backoff), then a 10 s backoff to t=15;
    # the limiter holds the third attempt until the first leaves the window at t=100.
    assert clock.sleeps == [5.0, 10.0, 85.0]


def test_client_error_is_not_retried(make_client: MakeClient) -> None:
    client, transport = make_client(lambda r: httpx.Response(400, json={"message": "bad"}))
    with pytest.raises(InspireHTTPError, match="400"):
        _search(client)
    assert len(transport.requests) == 1


# --- privacy ------------------------------------------------------------------------------


def test_email_fields_are_scrubbed_before_caching(make_client: MakeClient, tmp_path: Path) -> None:
    def with_email(request: httpx.Request) -> httpx.Response:
        body = _fixture_body(request)
        body["hits"]["hits"][0]["metadata"]["authors"] = [{"full_name": "A", "emails": ["a@b"]}]
        return httpx.Response(200, json=body)

    client, _ = make_client(with_email)
    hits = _search(client)
    assert hits[0]["metadata"]["authors"] == [{"full_name": "A"}]
    for path in (tmp_path / "cache").rglob("*.json"):
        assert "a@b" not in path.read_text(encoding="utf-8")


def test_scrub_emails_is_recursive() -> None:
    assert scrub_emails({"a": [{"email": 1, "b": {"Emails": 2, "c": 3}}]}) == {
        "a": [{"b": {"c": 3}}]
    }


def _fixture_body(request: httpx.Request) -> dict[str, Any]:
    fixture_dir = Path(__file__).parent / "fixtures" / "inspire"
    manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    body: dict[str, Any] = json.loads(
        (fixture_dir / manifest[str(request.url)]).read_text(encoding="utf-8")
    )
    return body
