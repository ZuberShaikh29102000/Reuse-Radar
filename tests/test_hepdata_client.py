from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from reuse_radar.clients.hepdata import (
    HepDataClient,
    HepDataError,
    HepDataRecord,
    UnexpectedMetadataError,
)
from reuse_radar.clients.inspire import SlidingWindowRateLimiter

from .conftest import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "datacite"
MANIFEST: dict[str, str] = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
Override = Callable[[httpx.Request, dict[str, Any] | None], httpx.Response | None]


def _body(url: str) -> dict[str, Any] | None:
    name = MANIFEST.get(url)
    if name is None:
        return None
    body: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return body


@pytest.fixture
def make_client(
    tmp_path: Path, clock: FakeClock
) -> Callable[..., tuple[HepDataClient, list[httpx.Request]]]:
    def _make(override: Override | None = None) -> tuple[HepDataClient, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = _body(str(request.url))
            if override is not None:
                response = override(request, body)
                if response is not None:
                    return response
            if body is None:
                raise AssertionError(f"no recorded fixture for {request.url}")
            return httpx.Response(200, json=body)

        client = HepDataClient(
            contact_email="test@example.org",
            cache_dir=tmp_path,
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            limiter=SlidingWindowRateLimiter(clock=clock, sleep=clock.sleep),
            sleep=clock.sleep,
            now=clock,
        )
        return client, seen

    return _make


MakeClient = Callable[..., tuple[HepDataClient, list[httpx.Request]]]


def test_finds_latest_record_version(make_client: MakeClient) -> None:
    client, seen = make_client()
    record = client.find_record(1995886)
    assert record is not None
    assert (record.record_doi, record.hepdata_id, record.version) == (
        "10.17182/hepdata.105864.v4",
        105864,
        4,
    )
    assert len(record.table_dois) == 31
    assert "mailto:test@example.org" in seen[0].headers["User-Agent"]  # DataCite "identified"


def test_lists_tables_and_resources(make_client: MakeClient) -> None:
    client, _ = make_client()
    record = client.find_record(2745375)
    assert record is not None
    parts = client.list_tables(record)
    assert [p.kind for p in parts] == [
        "table",
        "table",
        "resource",
        "resource",
        "resource",
        "resource",
    ]
    likelihood = parts[2]
    assert likelihood.name == "inclusive_likelihoods.tar.gz"
    assert likelihood.resource_type == "HS3 file"
    assert "HistFactory JSON" in likelihood.description
    assert parts[1].name == "Table 1"


def test_no_record_returns_none(make_client: MakeClient) -> None:
    client, _ = make_client()
    assert client.find_record(1) is None


def test_lookups_are_cached(make_client: MakeClient) -> None:
    client, seen = make_client()
    client.find_record(2745375)
    client.find_record(2745375)
    assert len(seen) == 1


def test_missing_parts_fail_loudly(make_client: MakeClient) -> None:
    """Regression: resources (/r1...) were once skipped; the count check caught it."""
    client, _ = make_client()
    record = client.find_record(2745375)
    assert record is not None
    padded = HepDataRecord(
        record.record_doi,
        record.hepdata_id,
        record.version,
        record.url,
        (*record.table_dois, f"{record.record_doi}/t99"),
    )
    with pytest.raises(HepDataError, match="missing"):
        client.list_tables(padded)


def test_unknown_table_title_format_fails_loudly(make_client: MakeClient) -> None:
    def rename(request: httpx.Request, body: dict[str, Any] | None) -> httpx.Response | None:
        if body and any("/t" in item["id"] for item in body["data"]):
            for item in body["data"]:
                if "/t" in item["id"]:
                    item["attributes"]["titles"] = [{"title": "Table without quotes"}]
            return httpx.Response(200, json=body)
        return None

    client, _ = make_client(rename)
    record = client.find_record(2745375)
    assert record is not None
    with pytest.raises(UnexpectedMetadataError, match="unknown format"):
        client.list_tables(record)


def test_short_read_fails_loudly(make_client: MakeClient) -> None:
    def inflate_total(request: httpx.Request, body: dict[str, Any] | None) -> httpx.Response | None:
        if body is not None:
            body["meta"]["total"] += 1
            return httpx.Response(200, json=body)
        return None

    client, _ = make_client(inflate_total)
    with pytest.raises(HepDataError, match="returned"):
        client.find_record(2745375)


def test_http_errors_are_fatal(make_client: MakeClient) -> None:
    client, _ = make_client(lambda r, b: httpx.Response(403))
    with pytest.raises(HepDataError, match="403"):
        client.find_record(2745375)


def test_rate_limit_backs_off_then_succeeds(make_client: MakeClient, clock: FakeClock) -> None:
    calls = {"n": 0}

    def throttle_once(request: httpx.Request, body: dict[str, Any] | None) -> httpx.Response | None:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else None

    client, _ = make_client(throttle_once)
    assert client.find_record(2745375) is not None
    assert 5.0 in clock.sleeps
