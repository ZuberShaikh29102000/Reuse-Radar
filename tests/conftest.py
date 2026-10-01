from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from reuse_radar.clients.inspire import InspireClient, SlidingWindowRateLimiter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test that reaches a real socket fails."""

    def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"live network access in a test: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


class FakeClock:
    """Deterministic time source; sleeping advances it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class ReplayTransport(httpx.BaseTransport):
    """Serves recorded INSPIRE responses by exact URL; unknown URLs fail the test."""

    def __init__(self, override: Callable[[httpx.Request], httpx.Response | None] | None = None):
        fixture_dir = FIXTURES / "inspire"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        self._bodies = {
            url: json.loads((fixture_dir / name).read_text(encoding="utf-8"))
            for url, name in manifest.items()
        }
        self._override = override
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._override is not None:
            response = self._override(request)
            if response is not None:
                return response
        url = str(request.url)
        if url not in self._bodies:
            raise AssertionError(f"no recorded fixture for {url}")
        return httpx.Response(200, json=self._bodies[url])


@pytest.fixture
def make_client(
    tmp_path: Path, clock: FakeClock
) -> Callable[..., tuple[InspireClient, ReplayTransport]]:
    def _make(
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
        **kwargs: Any,
    ) -> tuple[InspireClient, ReplayTransport]:
        transport = ReplayTransport(override)
        limiter = kwargs.pop("limiter", None) or SlidingWindowRateLimiter(
            clock=clock, sleep=clock.sleep
        )
        client = InspireClient(
            contact_email="test@example.org",
            cache_dir=tmp_path / "cache",
            http=httpx.Client(transport=transport),
            limiter=limiter,
            sleep=clock.sleep,
            now=kwargs.pop("now", clock),
            **kwargs,
        )
        return client, transport

    return _make
