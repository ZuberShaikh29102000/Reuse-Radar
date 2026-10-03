from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from django.db.models import ProtectedError

from reuse_radar.api.models import DeclaredProduct, Gap, Paper, PublishedTable, Review
from reuse_radar.pipeline.harvest import CorpusConfig
from reuse_radar.pipeline.store import fingerprint, parse_earliest_date, store_corpus

CORPUS = CorpusConfig("c", "q", 2021, 2021)
VECTOR = [0.0] * 383 + [1.0]


def _product(span: str, status: str = "missing", table: str | None = None) -> dict[str, Any]:
    return {
        "product_type": "upper_limit",
        "description": f"Limit described by {span}",
        "evidence_span": span,
        "evidence_section": "Results",
        "evidence_kind": "caption",
        "confidence": 0.9,
        "provider": "groq",
        "model": "m",
        "merged_duplicates": 0,
        "embedding": VECTOR,
        "status": status,
        "severity": 0 if status == "published" else 2,
        "best_match": (
            {"table_doi": table, "score": 0.8, "embedding_similarity": 0.7, "caption_overlap": 0.9}
            if table
            else None
        ),
    }


def _write(data: Path, *, products: list[dict[str, Any]]) -> None:
    def put(stage: str, name: str, payload: object) -> None:
        path = data / stage / "c" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    harvest = [
        {
            "inspire_id": 9,
            "arxiv_id": "2112.11876",
            "title": "HH search",
            "collaborations": ["ATLAS"],
            "earliest_date": "2021-12-22",
        },
        {
            "inspire_id": 10,
            "arxiv_id": None,
            "title": "Proceedings",
            "collaborations": ["ATLAS", "CMS"],
            "earliest_date": "2021-03",
        },
    ]
    path = data / "harvest" / "c" / "2021.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(h) + "\n" for h in harvest), encoding="utf-8")
    put("filter", "9.json", {"status": "ok"})
    put("filter", "10.json", {"status": "no_arxiv_id"})
    put("extract", "9.json", {"status": "ok", "extraction_version": "extract_v1+filter_1"})
    table_doi = "10.17182/hepdata.1.v2/t1"
    put(
        "reconcile",
        "9.json",
        {
            "status": "ok",
            "reconcile_version": "1",
            "extraction_version": "extract_v1+filter_1",
            "hepdata_record_id": 1,
            "hepdata_record_doi": "10.17182/hepdata.1.v2",
            "hepdata_version": 2,
            "readiness_score": 50.0,
            "tables": [
                {
                    "table_doi": table_doi,
                    "name": "Table 14",
                    "description": "Limits",
                    "kind": "table",
                    "resource_type": "",
                    "embedding": VECTOR,
                }
            ],
            "products": products,
        },
    )


def test_parse_earliest_date_handles_partial_dates() -> None:
    assert parse_earliest_date("2021-12-22") == dt.date(2021, 12, 22)
    assert parse_earliest_date("2021-03") == dt.date(2021, 3, 1)
    assert parse_earliest_date("2021") == dt.date(2021, 1, 1)


def test_fingerprint_depends_on_description() -> None:
    a = _product("same caption")
    b = {**a, "description": "A different product sharing the caption"}
    assert fingerprint(a) != fingerprint(b)


@pytest.mark.django_db
def test_store_loads_papers_products_tables_and_gaps(tmp_path: Path) -> None:
    published = _product("published caption", "published", "10.17182/hepdata.1.v2/t1")
    _write(tmp_path, products=[published, _product("missing span")])
    counts = store_corpus(CORPUS, tmp_path)
    assert counts["papers"] == 2 and counts["reconciled"] == 1

    paper = Paper.objects.get(inspire_id=9)
    assert paper.processing_status == "reconciled"
    assert paper.readiness_score == 50.0 and paper.hepdata_record_id == 1
    assert Paper.objects.get(inspire_id=10).processing_status == "no_arxiv_id"
    assert Paper.objects.get(inspire_id=10).collaboration == "ATLAS; CMS"
    assert PublishedTable.objects.count() == 1
    gaps = {
        g.declared_product.evidence_span: g for g in Gap.objects.select_related("matched_table")
    }
    assert gaps["published caption"].matched_table is not None
    assert gaps["missing span"].status == "missing" and gaps["missing span"].severity == 2


@pytest.mark.django_db
def test_shared_table_and_repeated_product_become_one_row_each(tmp_path: Path) -> None:
    """A batched upsert cannot touch one row twice, so duplicates are merged before writing.

    Seen live: two papers listed the same HEPData table DOI (1774 parts, 1773 rows).
    """
    _write(tmp_path, products=[_product("a span")])
    paper_9 = json.loads((tmp_path / "reconcile" / "c" / "9.json").read_text(encoding="utf-8"))
    published = _product("shared span", "published", "10.17182/hepdata.1.v2/t1")
    paper_10 = {**paper_9, "products": [published, published]}
    (tmp_path / "reconcile" / "c" / "10.json").write_text(json.dumps(paper_10), encoding="utf-8")

    counts = store_corpus(CORPUS, tmp_path)
    assert counts["tables"] == 2 and PublishedTable.objects.count() == 1
    assert DeclaredProduct.objects.filter(paper_id=10).count() == 1
    gap = Gap.objects.get(declared_product__paper_id=10)
    assert gap.matched_table is not None and gap.matched_table.table_doi.endswith("/t1")


@pytest.mark.django_db
def test_store_is_idempotent(tmp_path: Path) -> None:
    _write(tmp_path, products=[_product("a span"), _product("b span")])
    store_corpus(CORPUS, tmp_path)
    ids = sorted(DeclaredProduct.objects.values_list("id", flat=True))
    store_corpus(CORPUS, tmp_path)
    assert sorted(DeclaredProduct.objects.values_list("id", flat=True)) == ids
    assert Gap.objects.count() == 2 and Paper.objects.count() == 2


@pytest.mark.django_db
def test_reviewed_products_survive_a_new_extraction(tmp_path: Path) -> None:
    _write(tmp_path, products=[_product("reviewed span"), _product("unreviewed span")])
    store_corpus(CORPUS, tmp_path)
    reviewed = DeclaredProduct.objects.get(evidence_span="reviewed span")
    Review.objects.create(declared_product=reviewed, verdict="accept", reviewer="curator")

    _write(tmp_path, products=[_product("new span")])  # neither old product is produced now
    counts = store_corpus(CORPUS, tmp_path)

    spans = dict(DeclaredProduct.objects.values_list("evidence_span", "is_current"))
    assert spans == {"reviewed span": False, "new span": True}  # unreviewed one deleted
    assert counts["superseded"] == 1
    assert Review.objects.count() == 1


@pytest.mark.django_db
def test_database_refuses_to_delete_reviewed_products(tmp_path: Path) -> None:
    _write(tmp_path, products=[_product("reviewed span")])
    store_corpus(CORPUS, tmp_path)
    product = DeclaredProduct.objects.get()
    Review.objects.create(declared_product=product, verdict="reject", reviewer="curator")
    with pytest.raises(ProtectedError):
        product.delete()


def test_missing_harvest_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="harvest output missing"):
        store_corpus(CORPUS, tmp_path)
