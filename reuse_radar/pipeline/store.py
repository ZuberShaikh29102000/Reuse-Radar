"""Store stage: load the pipeline's JSON outputs into Postgres.

Reads harvest, filter, extract and reconcile outputs for a corpus and upserts:
- every harvested paper, with how far the pipeline got (`processing_status`);
- for reconciled papers, their HEPData tables, declared products and gaps.

Idempotent: rerunning with the same files changes nothing. Products are matched across reruns
by a fingerprint of (type, evidence span, description), so rows are updated in place. A product
that a newer extraction no longer produces is deleted, unless a curator has reviewed it: then it
is kept with `is_current = False`, because reviews are the evaluation gold set (SPEC section 5).

Run: python -m reuse_radar.pipeline.store --corpus config/corpus.toml
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from opentelemetry import trace

from reuse_radar.log import configure_logging
from reuse_radar.pipeline.harvest import CorpusConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

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


def store_corpus(corpus: CorpusConfig, data_dir: Path) -> dict[str, int]:
    """Load one corpus's outputs into the database. Requires Django to be set up."""
    from django.db import transaction  # imported here so the module loads without Django

    from reuse_radar.api.models import Paper

    counts = {"papers": 0, "reconciled": 0, "products": 0, "tables": 0, "superseded": 0}
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

                with transaction.atomic():
                    paper, _ = Paper.objects.update_or_create(
                        inspire_id=inspire_id,
                        defaults={
                            "arxiv_id": harvested.get("arxiv_id"),
                            "title": harvested["title"],
                            "collaboration": "; ".join(harvested.get("collaborations") or []),
                            "earliest_date": parse_earliest_date(harvested["earliest_date"]),
                            "processing_status": status,
                            "extraction_version": (extracted or {}).get("extraction_version") or "",
                            "last_extracted_at": _mtime(extract_path) if extracted else None,
                        },
                    )
                    counts["papers"] += 1
                    if reconciled and reconciled["status"] == "ok":
                        _store_reconciliation(paper, reconciled, counts)
                        counts["reconciled"] += 1
        logger.info("store stage finished", extra={"corpus": corpus.name, **counts})
    return counts


def _store_reconciliation(paper: Any, rec: dict[str, Any], counts: dict[str, int]) -> None:
    from reuse_radar.api.models import DeclaredProduct, Gap, PublishedTable

    paper.hepdata_record_id = rec["hepdata_record_id"]
    paper.hepdata_record_doi = rec["hepdata_record_doi"] or ""
    paper.hepdata_version = rec["hepdata_version"]
    paper.readiness_score = rec["readiness_score"]
    paper.save(
        update_fields=[
            "hepdata_record_id",
            "hepdata_record_doi",
            "hepdata_version",
            "readiness_score",
        ]
    )

    tables: dict[str, Any] = {}
    for table in rec["tables"]:
        table_row, _ = PublishedTable.objects.update_or_create(
            table_doi=table["table_doi"],
            defaults={
                "hepdata_record_id": rec["hepdata_record_id"],
                "name": table["name"],
                "description": table["description"],
                "kind": table["kind"],
                "resource_type": table["resource_type"],
                "embedding": table["embedding"],
            },
        )
        tables[table_row.table_doi] = table_row
        counts["tables"] += 1

    seen: set[str] = set()
    for product in rec["products"]:
        fp = fingerprint(product)
        seen.add(fp)
        row, _ = DeclaredProduct.objects.update_or_create(
            paper=paper,
            fingerprint=fp,
            defaults={
                "product_type": product["product_type"],
                "description": product["description"],
                "evidence_span": product["evidence_span"],
                "evidence_section": product["evidence_section"],
                "evidence_kind": product["evidence_kind"],
                "confidence": product["confidence"],
                "embedding": product["embedding"],
                "is_current": True,
                "extraction_version": rec.get("extraction_version") or "",
                "provider": product["provider"],
                "model": product["model"],
                "merged_duplicates": product["merged_duplicates"],
            },
        )
        match = product.get("best_match") or {}
        matched = (
            tables.get(match.get("table_doi", "")) if product["status"] != "no_record" else None
        )
        Gap.objects.update_or_create(
            declared_product=row,
            defaults={
                "status": product["status"],
                "severity": product["severity"],
                "matched_table": matched,
                "match_score": match.get("score"),
                "embedding_similarity": match.get("embedding_similarity"),
                "caption_overlap": match.get("caption_overlap"),
                "reconcile_version": rec["reconcile_version"],
            },
        )
        counts["products"] += 1

    stale = DeclaredProduct.objects.filter(paper=paper).exclude(fingerprint__in=seen)
    reviewed = stale.filter(reviews__isnull=False).distinct()
    counts["superseded"] += reviewed.update(is_current=False)
    stale.filter(reviews__isnull=True).delete()


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
