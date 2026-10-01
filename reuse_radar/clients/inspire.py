"""INSPIRE-HEP REST client.

Implements SPEC section 2, item 5 and item 7:

- Sliding-window rate limiter: at most 15 requests per 5 s, paced to ~1 req/s. Every attempt,
  including ones answered with 429, is recorded against the window.
- Disk cache keyed by sha256(url). Reruns cost zero requests.
- Exponential backoff on 429 with a 5 s floor (one full window), honouring Retry-After.
- Pagination via `links.next`.
- Raises (never truncates) when a query matches more than 10,000 records, and verifies that the
  number of records yielded equals `hits.total`.

Response shapes used here (`hits.total`, `hits.hits`, `links.next`, page size <= 1000, 400 past
10,000 results) were checked against the live API on 2026-10-01; see docs/adr/0001.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from opentelemetry import metrics

from reuse_radar import __version__

BASE_URL = "https://inspirehep.net/api"
MAX_RESULTS = 10_000
MAX_PAGE_SIZE = 1_000
MIN_429_BACKOFF_S = 5.0

logger = logging.getLogger(__name__)
_meter = metrics.get_meter(__name__)
_requests_counter = _meter.create_counter(
    "inspire_requests_total", description="HTTP requests sent to INSPIRE, by status code"
)
_cache_counter = _meter.create_counter(
    "inspire_cache_lookups_total", description="INSPIRE disk-cache lookups, by result"
)


class InspireError(RuntimeError):
    """Base class for INSPIRE client failures."""


class QueryTooLargeError(InspireError):
    """The query matches more records than INSPIRE will paginate through."""


class IncompleteHarvestError(InspireError):
    """Pagination finished with a different record count than `hits.total` promised."""


class UnexpectedResponseError(InspireError):
    """A field this client relies on is absent or has the wrong type."""


class InspireHTTPError(InspireError):
    """Non-retryable HTTP error, or retries exhausted."""


Clock = Callable[[], float]
Sleep = Callable[[float], None]


class SlidingWindowRateLimiter:
    """Allows at most `max_requests` in any `window_s` interval, spaced >= `min_interval_s` apart.

    Not thread-safe; the pipeline issues INSPIRE requests from a single thread.
    """

    def __init__(
        self,
        max_requests: int = 15,
        window_s: float = 5.0,
        min_interval_s: float = 1.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        if max_requests < 1 or window_s <= 0 or min_interval_s < 0:
            raise ValueError("invalid rate-limiter parameters")
        self._max = max_requests
        self._window = window_s
        self._min_interval = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()

    def acquire(self) -> None:
        """Block until a request may be sent, then record it."""
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self._window:
                self._sent.popleft()
            wait = 0.0
            if len(self._sent) >= self._max:
                wait = self._sent[0] + self._window - now
            if self._sent:
                wait = max(wait, self._sent[-1] + self._min_interval - now)
            if wait <= 0:
                self._sent.append(now)
                return
            self._sleep(wait)


class DiskCache:
    """JSON response cache keyed by sha256(url). Writes are atomic.

    Entries record when they were fetched so callers can bound staleness per lookup.
    """

    def __init__(self, root: Path, now: Clock = time.time) -> None:
        self._root = root
        self._now = now

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._root / digest[:2] / f"{digest}.json"

    def get(self, url: str, max_age_s: float | None = None) -> dict[str, Any] | None:
        """Return the cached body, or None if absent or older than `max_age_s` (None: no limit)."""
        path = self._path(url)
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("url") != url:
            raise UnexpectedResponseError(f"cache entry {path} does not belong to {url}")
        if max_age_s is not None:
            fetched_at = entry.get("fetched_at")
            if not isinstance(fetched_at, int | float) or self._now() - fetched_at > max_age_s:
                return None
        body: dict[str, Any] = entry["body"]
        return body

    def set(self, url: str, body: dict[str, Any]) -> None:
        path = self._path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(
            json.dumps({"url": url, "fetched_at": self._now(), "body": body}), encoding="utf-8"
        )
        os.replace(tmp, path)


def scrub_emails(value: Any) -> Any:
    """Recursively drop any key containing 'email'. Defence in depth: we never request
    author fields, but nothing email-shaped may reach the cache or outputs."""
    if isinstance(value, dict):
        return {k: scrub_emails(v) for k, v in value.items() if "email" not in k.lower()}
    if isinstance(value, list):
        return [scrub_emails(v) for v in value]
    return value


def _require(obj: Any, *path: str) -> Any:
    current = obj
    for i, key in enumerate(path):
        if not isinstance(current, dict) or key not in current:
            raise UnexpectedResponseError(
                f"INSPIRE response is missing field {'.'.join(path[: i + 1])!r}"
            )
        current = current[key]
    return current


class InspireClient:
    def __init__(
        self,
        *,
        contact_email: str,
        cache_dir: Path,
        http: httpx.Client | None = None,
        limiter: SlidingWindowRateLimiter | None = None,
        sleep: Sleep = time.sleep,
        use_cache: bool = True,
        max_retries: int = 4,
        base_url: str = BASE_URL,
        search_cache_ttl_s: float = 86_400.0,
        now: Clock = time.time,
    ) -> None:
        if "@" not in contact_email:
            raise ValueError("contact_email must be a real address")
        self._http = http or httpx.Client(timeout=30.0)
        self._http.headers["User-Agent"] = f"ReuseRadar/{__version__} (mailto:{contact_email})"
        self._http.headers["Accept"] = "application/json"
        self._limiter = limiter or SlidingWindowRateLimiter(sleep=sleep)
        self._sleep = sleep
        self._cache = DiskCache(cache_dir / "inspire", now=now)
        # Search results change as papers are added; single records and other resources
        # fetched via get_json are cached without expiry unless the caller passes max_age_s.
        self._search_ttl = search_cache_ttl_s
        self._use_cache = use_cache
        self._max_retries = max_retries
        self._base_url = base_url.rstrip("/")

    def get_json(self, url: str, *, max_age_s: float | None = None) -> dict[str, Any]:
        """GET a URL, serving from cache when an entry younger than `max_age_s` exists.
        Only 200 responses are cached."""
        return self._get_json(url, max_age_s)[0]

    def _get_json(self, url: str, max_age_s: float | None) -> tuple[dict[str, Any], bool]:
        """Like get_json, but also reports whether the body came from the cache."""
        if self._use_cache:
            cached = self._cache.get(url, max_age_s)
            if cached is not None:
                _cache_counter.add(1, {"result": "hit"})
                return cached, True
            _cache_counter.add(1, {"result": "miss"})

        for attempt in range(self._max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._http.get(url)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise InspireHTTPError(f"transport error for {url}: {exc}") from exc
                delay = 2.0**attempt
                logger.warning(
                    "inspire transport error, retrying",
                    extra={"url": url, "attempt": attempt, "delay_s": delay, "error": str(exc)},
                )
                self._sleep(delay)
                continue

            _requests_counter.add(1, {"status": response.status_code})
            if response.status_code == 200:
                body = scrub_emails(response.json())
                if not isinstance(body, dict):
                    raise UnexpectedResponseError(f"expected a JSON object from {url}")
                if self._use_cache:
                    self._cache.set(url, body)
                return body, False

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == self._max_retries:
                raise InspireHTTPError(
                    f"INSPIRE returned {response.status_code} for {url}: {response.text[:500]}"
                )
            delay = self._backoff(response, attempt)
            logger.warning(
                "inspire request throttled or failed, backing off",
                extra={
                    "url": url,
                    "status": response.status_code,
                    "attempt": attempt,
                    "delay_s": delay,
                },
            )
            self._sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _backoff(response: httpx.Response, attempt: int) -> float:
        if response.status_code != 429:
            return 2.0**attempt
        delay = MIN_429_BACKOFF_S * 2.0**attempt
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            # An HTTP-date Retry-After is ignored; the exponential delay already exceeds a window.
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        return max(delay, MIN_429_BACKOFF_S)

    def search_url(
        self, query: str, *, fields: Sequence[str], sort: str = "mostrecent", page_size: int = 250
    ) -> str:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be in 1..{MAX_PAGE_SIZE}")
        params = {
            "q": query,
            "size": page_size,
            "page": 1,
            "sort": sort,
            "fields": ",".join(fields),
        }
        return f"{self._base_url}/literature?{urlencode(params)}"

    def search_literature(
        self,
        query: str,
        *,
        fields: Sequence[str],
        sort: str = "mostrecent",
        page_size: int = 250,
    ) -> Iterator[dict[str, Any]]:
        """Yield every hit for `query`, following `links.next`.

        Raises QueryTooLargeError before yielding anything if the query exceeds 10,000 records,
        and IncompleteHarvestError if the yielded count does not match `hits.total`.
        """
        url: str | None = self.search_url(query, fields=fields, sort=sort, page_size=page_size)
        total: int | None = None
        seen = 0
        max_age_s: float | None = self._search_ttl

        while url is not None:
            page, from_cache = self._get_json(url, max_age_s)
            if not from_cache:
                # Once any page is live, fetch the rest live too, so one result set is never
                # stitched together from snapshots taken at different times.
                max_age_s = 0.0
            page_total = _require(page, "hits", "total")
            if not isinstance(page_total, int):
                raise UnexpectedResponseError(f"hits.total is not an integer: {page_total!r}")
            if total is None:
                total = page_total
                if total > MAX_RESULTS:
                    raise QueryTooLargeError(
                        f"query {query!r} matches {total} records, above INSPIRE's "
                        f"{MAX_RESULTS}-record pagination ceiling; narrow the slice"
                    )
                logger.info("inspire search started", extra={"query": query, "total": total})
            elif page_total != total:
                raise IncompleteHarvestError(
                    f"hits.total changed mid-pagination for {query!r}: {total} -> {page_total}"
                )

            hits = _require(page, "hits", "hits")
            if not isinstance(hits, list):
                raise UnexpectedResponseError("hits.hits is not a list")
            links = _require(page, "links")
            next_url = links.get("next") if isinstance(links, dict) else None

            if not hits and next_url is not None:
                raise IncompleteHarvestError(f"empty page with a next link at {url}")
            seen += len(hits)
            if seen > total:
                raise IncompleteHarvestError(f"received {seen} records but hits.total is {total}")
            yield from hits

            if next_url is not None and not next_url.startswith(self._base_url + "/"):
                raise UnexpectedResponseError(f"links.next points off-API: {next_url}")
            url = next_url

        if seen != total:
            raise IncompleteHarvestError(
                f"query {query!r}: received {seen} records, hits.total is {total}"
            )
