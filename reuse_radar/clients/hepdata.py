"""HEPData record and table lookup, via HEPData's DOI metadata on DataCite.

Why not hepdata.net directly: on 2026-10-01 every scripted request to
`hepdata.net/record/ins<id>?format=json` (even with an honest User-Agent and an
`Accept: application/json` header) got HTTP 403 with `Cf-Mitigated: challenge`, a Cloudflare
browser check, and hepdata.net/robots.txt disallows /search. We do not try to defeat bot
protection. HEPData registers a DataCite DOI for every record version and for every table, with
the metadata reconciliation needs (see docs/adr/0004):

- record version DOI  10.17182/hepdata.<n>.v<k>   resourceTypeGeneral = Collection,
  relatedIdentifiers: IsSupplementTo https://inspirehep.net/literature/<inspire_id>,
                      HasPart 10.17182/hepdata.<n>.v<k>/t<j> for each table
- table DOI           10.17182/hepdata.<n>.v<k>/t<j> resourceTypeGeneral = Dataset,
  title '"<table name>" of "<paper title>"', descriptions[0] = the table's description
- resource DOI        10.17182/hepdata.<n>.v<k>/r<j> resourceTypeGeneral = Other, an attached
  file such as a full likelihood ("HS3 file": HistFactory JSON) or a code archive. Resources are
  published products too, so they are listed alongside tables.

DataCite's documented limit for clients identifying themselves with an email in the User-Agent
is 1000 requests per 5 minutes per IP; we pace to 1 request per second.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from opentelemetry import metrics

from reuse_radar import __version__
from reuse_radar.clients.inspire import DiskCache, Sleep, SlidingWindowRateLimiter

BASE_URL = "https://api.datacite.org/dois"
HEPDATA_PREFIX = "10.17182"
PAGE_SIZE = 500
# Records gain new versions and tables over time; a week is fresh enough for a nightly pipeline.
CACHE_TTL_S = 7 * 24 * 3600.0

_VERSIONED = re.compile(r"^10\.17182/hepdata\.(\d+)\.v(\d+)$")
_TABLE_TITLE = re.compile(r'^"(?P<name>.*)" of "(?P<paper>.*)"$', re.DOTALL)

logger = logging.getLogger(__name__)
_meter = metrics.get_meter(__name__)
_requests_counter = _meter.create_counter(
    "datacite_requests_total", description="HTTP requests sent to DataCite, by status code"
)


class HepDataError(RuntimeError):
    """Fatal lookup failure (HTTP error, retries exhausted)."""


class UnexpectedMetadataError(HepDataError):
    """DataCite metadata for a HEPData DOI lacks a field we rely on, or has an unknown shape."""


@dataclass(frozen=True)
class HepDataRecord:
    record_doi: str  # versioned, e.g. 10.17182/hepdata.105864.v4
    hepdata_id: int  # the <n> in hepdata.<n>
    version: int
    url: str
    table_dois: tuple[str, ...]


PartKind = Literal["table", "resource"]


@dataclass(frozen=True)
class PublishedTable:
    """A table or attached resource of a HEPData record version."""

    table_doi: str
    name: str
    description: str
    record_doi: str
    kind: PartKind = "table"
    resource_type: str = ""  # DataCite types.resourceType, e.g. "HS3 file" for likelihoods


def _attributes(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        raise UnexpectedMetadataError(f"DataCite item {item.get('id')!r} has no attributes")
    return attributes


def _related(attributes: dict[str, Any], relation: str) -> list[str]:
    related = attributes.get("relatedIdentifiers")
    if not isinstance(related, list):
        raise UnexpectedMetadataError("relatedIdentifiers missing")
    return [
        str(r["relatedIdentifier"])
        for r in related
        if isinstance(r, dict) and r.get("relationType") == relation and "relatedIdentifier" in r
    ]


class HepDataClient:
    def __init__(
        self,
        *,
        contact_email: str,
        cache_dir: Path,
        http: httpx.Client | None = None,
        limiter: SlidingWindowRateLimiter | None = None,
        sleep: Sleep = time.sleep,
        max_retries: int = 3,
        cache_ttl_s: float = CACHE_TTL_S,
        now: Any = time.time,
    ) -> None:
        if "@" not in contact_email:
            raise ValueError("contact_email must be a real address")
        self._http = http or httpx.Client(timeout=60.0)
        # An email in the User-Agent puts us in DataCite's "identified" rate-limit class.
        self._http.headers["User-Agent"] = f"ReuseRadar/{__version__} (mailto:{contact_email})"
        self._http.headers["Accept"] = "application/vnd.api+json"
        self._limiter = limiter or SlidingWindowRateLimiter(
            max_requests=5, window_s=5.0, min_interval_s=1.0, sleep=sleep
        )
        self._sleep = sleep
        self._cache = DiskCache(cache_dir / "datacite", now=now)
        self._ttl = cache_ttl_s
        self._max_retries = max_retries

    # -- HTTP -----------------------------------------------------------------------------------

    def _get(self, url: str) -> dict[str, Any]:
        cached = self._cache.get(url, max_age_s=self._ttl)
        if cached is not None:
            return cached
        for attempt in range(self._max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._http.get(url)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise HepDataError(f"transport error for {url}: {exc}") from exc
                self._sleep(2.0**attempt)
                continue
            _requests_counter.add(1, {"status": response.status_code})
            if response.status_code == 200:
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise UnexpectedMetadataError(f"unexpected DataCite response for {url}")
                self._cache.set(url, body)
                return body
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self._max_retries:
                    break
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.0
                self._sleep(max(delay, 5.0 * 2.0**attempt))
                continue
            raise HepDataError(f"DataCite returned {response.status_code} for {url}")
        raise HepDataError(f"DataCite retries exhausted for {url}")

    def _search(self, query: str) -> list[dict[str, Any]]:
        """All DataCite items for a query, following pagination; fails loudly on a short read."""
        params = {
            "query": query,
            "prefix": HEPDATA_PREFIX,
            "page[size]": str(PAGE_SIZE),
            "page[number]": "1",
        }
        url: str | None = f"{BASE_URL}?{urlencode(params)}"
        items: list[dict[str, Any]] = []
        total: int | None = None
        while url is not None:
            page = self._get(url)
            meta = page.get("meta")
            if not isinstance(meta, dict) or not isinstance(meta.get("total"), int):
                raise UnexpectedMetadataError("DataCite response lacks meta.total")
            total = meta["total"] if total is None else total
            items.extend(page["data"])
            links = page.get("links")
            next_url = links.get("next") if isinstance(links, dict) else None
            url = str(next_url) if next_url and page["data"] else None
        if total is not None and len(items) != total:
            raise HepDataError(f"DataCite query returned {len(items)} of {total} items: {query}")
        return items

    # -- public API -----------------------------------------------------------------------------

    def find_record(self, inspire_id: int) -> HepDataRecord | None:
        """The latest version of the HEPData record for an INSPIRE id, or None if none exists."""
        inspire_url = f"https://inspirehep.net/literature/{inspire_id}"
        items = self._search(
            f'relatedIdentifiers.relatedIdentifier:"{inspire_url}" '
            "AND types.resourceTypeGeneral:Collection"
        )
        versions: list[HepDataRecord] = []
        for item in items:
            doi = str(item.get("id", ""))
            match = _VERSIONED.match(doi)
            if match is None:
                continue  # the concept DOI (no .v<k>) carries no tables
            attributes = _attributes(item)
            if inspire_url not in _related(attributes, "IsSupplementTo"):
                continue  # a different paper that merely mentions this one
            versions.append(
                HepDataRecord(
                    record_doi=doi,
                    hepdata_id=int(match.group(1)),
                    version=int(match.group(2)),
                    url=str(attributes.get("url") or ""),
                    table_dois=tuple(_related(attributes, "HasPart")),
                )
            )
        if not versions:
            return None
        ids = {v.hepdata_id for v in versions}
        if len(ids) > 1:
            raise UnexpectedMetadataError(
                f"INSPIRE {inspire_id} maps to several HEPData records: {sorted(ids)}"
            )
        return max(versions, key=lambda v: v.version)

    def list_tables(self, record: HepDataRecord) -> list[PublishedTable]:
        """Every table and resource of a record version, with name and description."""
        items = self._search(f'relatedIdentifiers.relatedIdentifier:"{record.record_doi}"')
        tables: list[PublishedTable] = []
        for item in items:
            doi = str(item.get("id", ""))
            prefix = record.record_doi + "/"
            suffix = doi[len(prefix) :] if doi.startswith(prefix) else ""
            if suffix[:1] not in ("t", "r") or not suffix[1:].isdigit():
                continue
            attributes = _attributes(item)
            titles = attributes.get("titles")
            if not isinstance(titles, list) or not titles or "title" not in titles[0]:
                raise UnexpectedMetadataError(f"{doi} has no title")
            match = _TABLE_TITLE.match(str(titles[0]["title"]))
            if match is None:
                raise UnexpectedMetadataError(f"{doi} title has an unknown format: {titles[0]!r}")
            descriptions = attributes.get("descriptions") or []
            description = (
                str(descriptions[0].get("description", ""))
                if descriptions and isinstance(descriptions[0], dict)
                else ""
            )
            raw_types = attributes.get("types")
            types: dict[str, Any] = raw_types if isinstance(raw_types, dict) else {}
            tables.append(
                PublishedTable(
                    table_doi=doi,
                    name=match.group("name"),
                    description=description,
                    record_doi=record.record_doi,
                    kind="table" if suffix[0] == "t" else "resource",
                    resource_type=str(types.get("resourceType") or ""),
                )
            )

        expected = set(record.table_dois)
        found = {t.table_doi for t in tables}
        if expected and found != expected:
            raise HepDataError(
                f"{record.record_doi}: {len(found)} tables listed but the record has "
                f"{len(expected)} (missing {sorted(expected - found)[:5]})"
            )
        return sorted(tables, key=_part_order)


def _part_order(table: PublishedTable) -> tuple[int, int]:
    tail = table.table_doi.rsplit("/", 1)[-1]
    return (0 if tail[0] == "t" else 1, int(tail[1:]))
