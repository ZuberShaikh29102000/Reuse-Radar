"""Harvest stage: pull the corpus from INSPIRE, one earliest-date year per slice, into JSONL.

Output: `<data_dir>/harvest/<corpus name>/<year>.jsonl`, one paper per line, sorted by
inspire_id. Each year file is rewritten atomically, so reruns are idempotent and, with a warm
HTTP cache, cost zero requests.

Run: python -m reuse_radar.pipeline.harvest --corpus config/corpus.toml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from opentelemetry import trace

from reuse_radar.clients.inspire import InspireClient
from reuse_radar.config import Settings
from reuse_radar.log import configure_logging

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Every field below was confirmed present on live INSPIRE literature records (2026-10-01).
# No author fields are requested, so no author email can be returned.
FIELDS: tuple[str, ...] = (
    "control_number",
    "titles.title",
    "arxiv_eprints.value",
    "collaborations.value",
    "earliest_date",
)


class HarvestError(RuntimeError):
    """A harvested record violates an invariant the rest of the pipeline relies on."""


@dataclass(frozen=True)
class CorpusConfig:
    name: str
    query: str
    start_year: int
    end_year: int

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError(f"corpus name must be a plain directory name: {self.name!r}")
        if self.start_year > self.end_year:
            raise ValueError("start_year must be <= end_year")

    @classmethod
    def from_toml(cls, path: Path) -> CorpusConfig:
        with path.open("rb") as fh:
            section = tomllib.load(fh)["corpus"]
        return cls(
            name=str(section["name"]),
            query=str(section["query"]),
            start_year=int(section["start_year"]),
            end_year=int(section["end_year"]),
        )

    @property
    def years(self) -> range:
        return range(self.start_year, self.end_year + 1)

    def slice_query(self, year: int) -> str:
        # `de` matches earliest_date; plain `date` also matches other dates and leaks
        # neighbouring years into the slice (verified live, see docs/adr/0001).
        return f"{self.query} and de {year}"


@dataclass(frozen=True)
class HarvestedPaper:
    inspire_id: int
    arxiv_id: str | None
    title: str
    collaborations: list[str]
    earliest_date: str


def parse_hit(hit: dict[str, Any]) -> HarvestedPaper:
    """Map one INSPIRE hit to a HarvestedPaper, raising HarvestError on missing required fields."""
    metadata = hit.get("metadata")
    if not isinstance(metadata, dict):
        raise HarvestError(f"hit without metadata: id={hit.get('id')!r}")

    inspire_id = metadata.get("control_number")
    if not isinstance(inspire_id, int):
        raise HarvestError(f"hit without integer control_number: id={hit.get('id')!r}")

    titles = metadata.get("titles") or []
    title = next((t["title"] for t in titles if isinstance(t, dict) and t.get("title")), None)
    if title is None:
        raise HarvestError(f"record {inspire_id} has no title")

    earliest_date = metadata.get("earliest_date")
    if not isinstance(earliest_date, str) or len(earliest_date) < 4:
        raise HarvestError(f"record {inspire_id} has no earliest_date")

    # Published papers without an arXiv eprint exist; downstream stages skip them explicitly.
    eprints = metadata.get("arxiv_eprints") or []
    arxiv_id = next((e["value"] for e in eprints if isinstance(e, dict) and e.get("value")), None)

    collaborations = [
        c["value"]
        for c in metadata.get("collaborations") or []
        if isinstance(c, dict) and c.get("value")
    ]

    return HarvestedPaper(
        inspire_id=inspire_id,
        arxiv_id=arxiv_id,
        title=title,
        collaborations=collaborations,
        earliest_date=earliest_date,
    )


def _write_jsonl_atomic(path: Path, papers: Iterable[HarvestedPaper]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for paper in papers:
            fh.write(json.dumps(asdict(paper), ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def harvest_year(
    client: InspireClient, corpus: CorpusConfig, year: int, out_dir: Path, *, page_size: int = 250
) -> Path:
    """Harvest one year slice and write it to `<out_dir>/<year>.jsonl`. Returns the path."""
    with tracer.start_as_current_span("harvest.year") as span:
        span.set_attribute("corpus", corpus.name)
        span.set_attribute("year", year)
        query = corpus.slice_query(year)

        papers: dict[int, HarvestedPaper] = {}
        for hit in client.search_literature(query, fields=FIELDS, page_size=page_size):
            paper = parse_hit(hit)
            if not paper.earliest_date.startswith(str(year)):
                raise HarvestError(
                    f"record {paper.inspire_id} has earliest_date {paper.earliest_date} "
                    f"but was returned for slice {year}"
                )
            if paper.inspire_id in papers:
                raise HarvestError(f"record {paper.inspire_id} returned twice in slice {year}")
            papers[paper.inspire_id] = paper
            if paper.arxiv_id is None:
                logger.warning("paper has no arXiv eprint", extra={"inspire_id": paper.inspire_id})

        path = out_dir / f"{year}.jsonl"
        _write_jsonl_atomic(path, (papers[k] for k in sorted(papers)))
        span.set_attribute("papers", len(papers))
        logger.info(
            "harvested year slice",
            extra={"corpus": corpus.name, "year": year, "papers": len(papers), "path": str(path)},
        )
        return path


def harvest(
    client: InspireClient, corpus: CorpusConfig, data_dir: Path, *, page_size: int = 250
) -> dict[int, Path]:
    """Harvest every year slice of the corpus. Entry point for Airflow and GitHub Actions."""
    with tracer.start_as_current_span("harvest") as span:
        span.set_attribute("corpus", corpus.name)
        out_dir = data_dir / "harvest" / corpus.name
        return {
            year: harvest_year(client, corpus, year, out_dir, page_size=page_size)
            for year in corpus.years
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("config/corpus.toml"))
    parser.add_argument("--no-cache", action="store_true", help="bypass the HTTP disk cache")
    args = parser.parse_args(argv)

    configure_logging()
    settings = Settings.from_env()
    client = InspireClient(
        contact_email=settings.inspire_contact_email,
        cache_dir=settings.cache_dir,
        use_cache=not args.no_cache,
    )
    harvest(client, CorpusConfig.from_toml(args.corpus), settings.data_dir)


if __name__ == "__main__":
    main()
