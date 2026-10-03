"""Store stage: load the pipeline's JSON outputs into Postgres.

Reads harvest, filter, extract and reconcile outputs for a corpus and upserts:
- every harvested paper, with how far the pipeline got (`processing_status`);
- for reconciled papers, their HEPData tables, declared products and gaps.

Idempotent: rerunning with the same files changes nothing. Products are matched across reruns
by a fingerprint of (type, evidence span, description), so rows are updated in place. A product
that a newer extraction no longer produces is deleted, unless a curator has reviewed it: then it
is kept with `is_current = False`, because reviews are the evaluation gold set (SPEC section 5).

All files are read first, then written in one transaction as batched upserts
(INSERT ... ON CONFLICT DO UPDATE). Row-by-row writes needed several round trips per row, which
took 1.5 hours from India to a us-east-1 database on 2026-10-03; batches take a few dozen.

Run: python -m reuse_radar.pipeline.store --corpus config/corpus.toml
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from opentelemetry import trace

from reuse_radar.log import configure_logging
from reuse_radar.pipeline.harvest import CorpusConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

BATCH_SIZE = 500  # rows per INSERT; well under Postgres's 65535 bind parameters per statement

_PAPER_FIELDS = [
    "arxiv_id",
    "title",
    "collaboration",
    "earliest_date",
    "processing_status",
    "extraction_version",
    "last_extracted_at",
]
_HEPDATA_FIELDS = ["hepdata_record_id", "hepdata_record_doi", "hepdata_version", "readiness_score"]
_TABLE_FIELDS = ["hepdata_record_id", "name", "description", "kind", "resource_type", "embedding"]
_PRODUCT_FIELDS = [
    "product_type",
    "description",
    "evidence_span",
    "evidence_section",
    "evidence_kind",
    "confidence",
    "embedding",
    "is_current",
    "extraction_version",
    "provider",
    "model",
    "merged_duplicates",
]
_GAP_FIELDS = [
    "status",
    "severity",
    "matched_table",
    "match_score",
    "embedding_similarity",
    "caption_overlap",
    "reconcile_version",
]


_FILTER_STATUS = {
    "no_arxiv_id": "no_arxiv_id",
    "no_latex": "no_latex",
    "source_error": "source_error",
    "filter_error": "filter_error",
}


def fingerprint(product: dict[str, Any]) -> str:
    key = "\x00".join((product["product_type"], product["evidence_span"], product["description"]))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_earliest_date(value: str) -> dt.date:
    """INSPIRE earliest_date is YYYY-MM-DD, YYYY-MM or YYYY; missing parts become 1."""
    parts = [int(p) for p in value.split("-")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"unrecognised earliest_date {value!r}")
    year, month, day = (*parts, 1, 1)[:3]
    return dt.date(year, month, day)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _mtime(path: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(os.path.getmtime(path), tz=dt.UTC)


def _chunks[T](items: Sequence[T], size: int = BATCH_SIZE) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _upsert(model: Any, rows: Sequence[Any], unique: list[str], fields: list[str]) -> None:
    if rows:
        model.objects.bulk_create(
            rows,
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            unique_fields=unique,
            update_fields=fields,
        )


def store_corpus(corpus: CorpusConfig, data_dir: Path) -> dict[str, int]:
    """Load one corpus's outputs into the database. Requires Django to be set up."""
    from django.db import transaction  # imported here so the module loads without Django

    from reuse_radar.api.models import Paper

    counts = {"papers": 0, "reconciled": 0, "products": 0, "tables": 0, "superseded": 0}
    papers: dict[int, Any] = {}
    reconciliations: dict[int, dict[str, Any]] = {}
    with tracer.start_as_current_span("store") as span:
        span.set_attribute("corpus", corpus.name)
        for year in corpus.years:
            harvest_file = data_dir / "harvest" / corpus.name / f"{year}.jsonl"
            if not harvest_file.exists():
                raise FileNotFoundError(f"harvest output missing: {harvest_file}")
            for line in harvest_file.read_text(encoding="utf-8").splitlines():
                harvested = json.loads(line)
                inspire_id = int(harvested["inspire_id"])
                filtered = _read_json(data_dir / "filter" / corpus.name / f"{inspire_id}.json")
                extract_path = data_dir / "extract" / corpus.name / f"{inspire_id}.json"
                extracted = _read_json(extract_path)
                reconciled = _read_json(data_dir / "reconcile" / corpus.name / f"{inspire_id}.json")

                status = "harvested"
                if filtered and filtered["status"] in _FILTER_STATUS:
                    status = _FILTER_STATUS[filtered["status"]]
                if extracted:
                    status = "extracted" if extracted["status"] == "ok" else status
                    if extracted["status"] == "schema_error":
                        status = "extraction_error"
                if reconciled and reconciled["status"] == "ok":
                    status = "reconciled"
                elif reconciled and reconciled["status"] == "lookup_error":
                    status = "reconcile_error"

                paper = Paper(
                    inspire_id=inspire_id,
                    arxiv_id=harvested.get("arxiv_id"),
                    title=harvested["title"],
                    collaboration="; ".join(harvested.get("collaborations") or []),
                    earliest_date=parse_earliest_date(harvested["earliest_date"]),
                    processing_status=status,
                    extraction_version=(extracted or {}).get("extraction_version") or "",
                    last_extracted_at=_mtime(extract_path) if extracted else None,
                )
                counts["papers"] += 1
                if reconciled and reconciled["status"] == "ok":
                    paper.hepdata_record_id = reconciled["hepdata_record_id"]
                    paper.hepdata_record_doi = reconciled["hepdata_record_doi"] or ""
                    paper.hepdata_version = reconciled["hepdata_version"]
                    paper.readiness_score = reconciled["readiness_score"]
                    reconciliations[inspire_id] = reconciled
                    counts["reconciled"] += 1
                papers[inspire_id] = paper

        with transaction.atomic():
            # A paper that is not (or no longer) reconciled keeps its stored HEPData fields.
            _upsert(
                Paper,
                [p for i, p in papers.items() if i not in reconciliations],
                ["inspire_id"],
                _PAPER_FIELDS,
            )
            _upsert(
                Paper,
                [papers[i] for i in reconciliations],
                ["inspire_id"],
                _PAPER_FIELDS + _HEPDATA_FIELDS,
            )
            _store_reconciliations(reconciliations, counts)
        logger.info("store stage finished", extra={"corpus": corpus.name, **counts})
    return counts


def _store_reconciliations(recs: dict[int, dict[str, Any]], counts: dict[str, int]) -> None:
    from reuse_radar.api.models import DeclaredProduct, Gap, PublishedTable, Review

    # A table DOI can appear in more than one paper's output; the last one wins, as one row.
    tables: dict[str, Any] = {}
    for rec in recs.values():
        for table in rec["tables"]:
            tables[table["table_doi"]] = PublishedTable(
                table_doi=table["table_doi"],
                hepdata_record_id=rec["hepdata_record_id"],
                name=table["name"],
                description=table["description"],
                kind=table["kind"],
                resource_type=table["resource_type"],
                embedding=table["embedding"],
            )
            counts["tables"] += 1
    _upsert(PublishedTable, list(tables.values()), ["table_doi"], _TABLE_FIELDS)
    table_ids: dict[str, int] = {}
    for dois in _chunks(list(tables)):
        table_ids.update(
            PublishedTable.objects.filter(table_doi__in=dois).values_list("table_doi", "id")
        )

    products: dict[tuple[int, str], Any] = {}
    gaps: dict[tuple[int, str], dict[str, Any]] = {}
    for inspire_id, rec in recs.items():
        own_tables = {t["table_doi"] for t in rec["tables"]}
        for product in rec["products"]:
            key = (inspire_id, fingerprint(product))
            products[key] = DeclaredProduct(
                paper_id=inspire_id,
                fingerprint=key[1],
                product_type=product["product_type"],
                description=product["description"],
                evidence_span=product["evidence_span"],
                evidence_section=product["evidence_section"],
                evidence_kind=product["evidence_kind"],
                confidence=product["confidence"],
                embedding=product["embedding"],
                is_current=True,
                extraction_version=rec.get("extraction_version") or "",
                provider=product["provider"],
                model=product["model"],
                merged_duplicates=product["merged_duplicates"],
            )
            match = product.get("best_match") or {}
            doi = match.get("table_doi", "")
            gaps[key] = {
                "status": product["status"],
                "severity": product["severity"],
                "matched_table_id": (
                    table_ids[doi]
                    if product["status"] != "no_record" and doi in own_tables
                    else None
                ),
                "match_score": match.get("score"),
                "embedding_similarity": match.get("embedding_similarity"),
                "caption_overlap": match.get("caption_overlap"),
                "reconcile_version": rec["reconcile_version"],
            }
            counts["products"] += 1
    _upsert(DeclaredProduct, list(products.values()), ["paper", "fingerprint"], _PRODUCT_FIELDS)

    stored: dict[tuple[int, str], int] = {}
    for paper_ids in _chunks(list(recs)):
        rows = DeclaredProduct.objects.filter(paper_id__in=paper_ids)
        for pk, paper_id, fp in rows.values_list("id", "paper_id", "fingerprint"):
            stored[(paper_id, fp)] = pk
    _upsert(
        Gap,
        [Gap(declared_product_id=stored[key], **data) for key, data in gaps.items()],
        ["declared_product"],
        _GAP_FIELDS,
    )

    stale = [pk for key, pk in stored.items() if key not in products]
    for ids in _chunks(stale):
        reviewed = set(
            Review.objects.filter(declared_product_id__in=ids).values_list(
                "declared_product_id", flat=True
            )
        )
        counts["superseded"] += DeclaredProduct.objects.filter(id__in=reviewed).update(
            is_current=False
        )
        DeclaredProduct.objects.filter(id__in=[pk for pk in ids if pk not in reviewed]).delete()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load pipeline outputs into Postgres.")
    parser.add_argument("--corpus", type=Path, default=Path("config/corpus.toml"))
    args = parser.parse_args(argv)

    configure_logging()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reuse_radar.api.settings")
    import django

    django.setup()
    from reuse_radar.config import Settings

    store_corpus(CorpusConfig.from_toml(args.corpus), Settings.from_env().data_dir)


if __name__ == "__main__":
    main()
