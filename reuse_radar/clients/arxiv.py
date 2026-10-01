"""arXiv source client: downloads a paper's LaTeX and keeps only its .tex files.

Access policy (see docs/adr/0002): arxiv.org's robots.txt disallows /e-print and /src; arXiv's
bulk-data page directs programmatic harvesting to export.arxiv.org at a reasonable rate. We use
export.arxiv.org only, one request every 3 s (the arXiv API terms' figure), one connection, an
identifying User-Agent, and a permanent disk cache so each paper is downloaded at most once.

We request /src/<id> directly: /e-print/<id> only 301-redirects there, and the redirect would be
a second, unpaced request. The endpoint returns one of: a gzipped tar (multi-file source),
a gzipped single .tex file, or a PDF when the authors submitted no source. The last case is
reported, not an error.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import httpx
from opentelemetry import metrics

from reuse_radar import __version__
from reuse_radar.clients.inspire import Sleep, SlidingWindowRateLimiter

BASE_URL = "https://export.arxiv.org"
MIN_INTERVAL_S = 3.0
MAX_SOURCE_BYTES = 200 * 1024 * 1024  # refuse pathological archives rather than fill the disk

# New-style (2401.05299) and old-style (hep-ex/0601001) identifiers, optional version suffix.
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?/\d{7})(v\d+)?$")

logger = logging.getLogger(__name__)
_meter = metrics.get_meter(__name__)
_requests_counter = _meter.create_counter(
    "arxiv_requests_total", description="HTTP requests sent to arXiv, by status code"
)
_cache_counter = _meter.create_counter(
    "arxiv_cache_lookups_total", description="arXiv source-cache lookups, by result"
)


class ArxivError(RuntimeError):
    """Fatal arXiv failure (retries exhausted, unexpected redirect). Stops the stage."""


class ArxivAccessDeniedError(ArxivError):
    """arXiv answered 403. Never caught per paper: continuing would look like an attack."""


class ArxivSourceError(ArxivError):
    """This one paper's source is missing or unusable. The stage records it and moves on."""


SourceKind = Literal["latex", "pdf_only"]


@dataclass(frozen=True)
class PaperSource:
    arxiv_id: str
    kind: SourceKind
    # Relative path -> decoded text, .tex files only. Empty when kind == "pdf_only".
    tex_files: dict[str, str]


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Older submissions are often Latin-1; this decode never fails and keeps every byte.
        return raw.decode("latin-1")


def unpack_source(arxiv_id: str, payload: bytes) -> PaperSource:
    """Turn an e-print payload into a PaperSource. Pure function; no I/O."""
    if payload.startswith(b"%PDF"):
        return PaperSource(arxiv_id, "pdf_only", {})
    if not payload.startswith(b"\x1f\x8b"):
        raise ArxivSourceError(
            f"{arxiv_id}: unrecognised e-print payload (first bytes {payload[:8]!r})"
        )

    raw = gzip.decompress(payload)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ArxivSourceError(f"{arxiv_id}: source is {len(raw)} bytes, above {MAX_SOURCE_BYTES}")

    if tarfile.is_tarfile(io.BytesIO(raw)):
        files: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                path = PurePosixPath(member.name)  # normalises "./a/b.tex" to "a/b.tex"
                name = str(path)
                if not member.isfile() or not name.lower().endswith(".tex"):
                    continue
                if ".." in path.parts or path.is_absolute():
                    raise ArxivSourceError(f"{arxiv_id}: unsafe path in archive: {member.name!r}")
                extracted = tar.extractfile(member)
                if extracted is not None:
                    files[name] = _decode(extracted.read())
        if not files:
            raise ArxivSourceError(f"{arxiv_id}: archive contains no .tex files")
        return PaperSource(arxiv_id, "latex", files)

    if raw.startswith(b"%PDF"):
        return PaperSource(arxiv_id, "pdf_only", {})
    # A single gzipped file. Accept it only if it looks like LaTeX.
    text = _decode(raw)
    if "\\begin{document}" not in text and "\\documentclass" not in text:
        raise ArxivSourceError(f"{arxiv_id}: single-file source is not recognisable LaTeX")
    return PaperSource(arxiv_id, "latex", {"main.tex": text})


class ArxivClient:
    def __init__(
        self,
        *,
        contact_email: str,
        cache_dir: Path,
        http: httpx.Client | None = None,
        limiter: SlidingWindowRateLimiter | None = None,
        sleep: Sleep = time.sleep,
        max_retries: int = 3,
        base_url: str = BASE_URL,
    ) -> None:
        if "@" not in contact_email:
            raise ValueError("contact_email must be a real address")
        self._http = http or httpx.Client(timeout=120.0, follow_redirects=True)
        self._http.headers["User-Agent"] = f"ReuseRadar/{__version__} (mailto:{contact_email})"
        # One request per 3 s; the window bound is redundant at this pace but kept explicit.
        self._limiter = limiter or SlidingWindowRateLimiter(
            max_requests=1, window_s=MIN_INTERVAL_S, min_interval_s=MIN_INTERVAL_S, sleep=sleep
        )
        self._sleep = sleep
        self._cache_root = cache_dir / "arxiv"
        self._max_retries = max_retries
        self._base_url = base_url.rstrip("/")

    def _cache_path(self, arxiv_id: str) -> Path:
        digest = hashlib.sha256(arxiv_id.encode("utf-8")).hexdigest()
        return self._cache_root / digest[:2] / f"{digest}.json"

    def get_source(self, arxiv_id: str) -> PaperSource:
        """Return the paper's LaTeX (.tex files only), downloading at most once per id."""
        if not _ARXIV_ID.match(arxiv_id):
            raise ValueError(f"not an arXiv identifier: {arxiv_id!r}")

        path = self._cache_path(arxiv_id)
        if path.exists():
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("arxiv_id") != arxiv_id:
                raise ArxivError(f"cache entry {path} does not belong to {arxiv_id}")
            _cache_counter.add(1, {"result": "hit"})
            return PaperSource(arxiv_id, entry["kind"], entry["tex_files"])
        _cache_counter.add(1, {"result": "miss"})

        source = unpack_source(arxiv_id, self._download(arxiv_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(
            json.dumps({"arxiv_id": arxiv_id, "kind": source.kind, "tex_files": source.tex_files}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return source

    def _download(self, arxiv_id: str) -> bytes:
        url = f"{self._base_url}/src/{arxiv_id}"  # /e-print/ just 301s here
        for attempt in range(self._max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._http.get(url)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise ArxivError(f"transport error for {url}: {exc}") from exc
                self._sleep(MIN_INTERVAL_S * 2.0**attempt)
                continue

            _requests_counter.add(1, {"status": response.status_code})
            final_host = response.url.host
            if final_host != httpx.URL(self._base_url).host:
                raise ArxivError(f"{url} redirected off the export host to {response.url}")
            if response.status_code == 200:
                return response.content
            if response.status_code == 403:
                # arXiv treats continued requests after a 403 as an attack. Stop immediately.
                raise ArxivAccessDeniedError(
                    f"arXiv denied access (403) for {url}; stopping all requests"
                )
            if response.status_code == 404:
                raise ArxivSourceError(f"no e-print for {arxiv_id} (404)")
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == self._max_retries:
                raise ArxivError(f"arXiv returned {response.status_code} for {url}")
            delay = max(MIN_INTERVAL_S * 2.0**attempt, _retry_after(response))
            logger.warning(
                "arxiv request throttled or failed, backing off",
                extra={"url": url, "status": response.status_code, "delay_s": delay},
            )
            self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else 0.0
    except ValueError:
        return 0.0
