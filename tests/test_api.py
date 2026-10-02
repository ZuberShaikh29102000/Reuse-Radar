from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.test import Client, override_settings

from reuse_radar.api.models import DeclaredProduct, Gap, Paper, PublishedTable

pytestmark = pytest.mark.django_db
API = override_settings(ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False)


def _vector(i: int) -> list[float]:
    v = [0.0] * 384
    v[i % 384] = 1.0
    v[(i + 1) % 384] = 0.5
    return v


def _paper(
    inspire_id: int, year: int, *, record: int | None = 1, score: float | None = 50.0
) -> Paper:
    return Paper.objects.create(
        inspire_id=inspire_id,
        arxiv_id=f"{year % 100:02d}01.0000{inspire_id % 10}",
        title=f"Search for something {inspire_id}",
        collaboration="ATLAS",
        earliest_date=dt.date(year, 6, 1),
        hepdata_record_id=record,
        readiness_score=score,
        processing_status="reconciled",
    )


def _product(
    paper: Paper,
    product_type: str,
    description: str,
    *,
    status: str,
    severity: int,
    confidence: float = 0.9,
    vector: int = 0,
    current: bool = True,
    table: PublishedTable | None = None,
) -> DeclaredProduct:
    product = DeclaredProduct.objects.create(
        paper=paper,
        product_type=product_type,
        description=description,
        evidence_span=f"Evidence for {description}",
        evidence_section="Results",
        evidence_kind="caption",
        confidence=confidence,
        embedding=_vector(vector),
        fingerprint=f"{paper.inspire_id}-{description}",
        is_current=current,
        extraction_version="extract_v1+filter_1",
    )
    Gap.objects.create(
        declared_product=product,
        status=status,
        severity=severity,
        matched_table=table,
        match_score=0.8 if table else None,
        embedding_similarity=0.7 if table else None,
        caption_overlap=0.9 if table else None,
        reconcile_version="1",
    )
    return product


@pytest.fixture
def corpus() -> dict[str, Any]:
    p2024 = _paper(100, 2024)
    p2021 = _paper(200, 2021, record=None, score=0.0)
    table = PublishedTable.objects.create(
        hepdata_record_id=1,
        table_doi="10.17182/hepdata.1.v1/t1",
        name="Table 1",
        description="Upper limits",
        embedding=_vector(0),
    )
    items = {
        "likelihood": _product(
            p2024, "likelihood", "Full likelihood of the search",
            status="missing", severity=3, vector=10,
        ),
        "limit": _product(
            p2024, "upper_limit", "Upper limits on the WW cross section",
            status="published", severity=0, vector=0, table=table,
        ),
        "yields": _product(
            p2024, "other", "Event yields in signal regions",
            status="uncertain", severity=1, confidence=0.6, vector=20,
        ),
        "xsec": _product(
            p2021, "cross_section", "Differential cross section in jet multiplicity",
            status="no_record", severity=2, vector=30,
        ),
        "old": _product(
            p2021, "cutflow", "Superseded cutflow",
            status="missing", severity=3, current=False, vector=40,
        ),
    }  # fmt: skip
    return {"papers": (p2024, p2021), "table": table, **items}


def get(url: str) -> Any:
    with API:
        response = Client().get(url)
    assert response.status_code == 200, (url, response.content[:300])
    return response.json()


def status(url: str) -> int:
    with API:
        return Client().get(url).status_code


# --- triage queue -----------------------------------------------------------------------------


def test_gaps_default_to_open_and_most_severe_first(corpus: dict[str, Any]) -> None:
    data = get("/api/gaps")
    assert data["count"] == 3  # published and superseded products are excluded
    assert [g["severity"] for g in data["results"]] == [3, 2, 1]
    first = data["results"][0]
    assert first["product"]["product_type"] == "likelihood"
    assert first["paper"]["inspire_url"] == "https://inspirehep.net/literature/100"
    assert first["paper"]["hepdata_url"] == "https://www.hepdata.net/record/ins100"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("product_type=likelihood", ["likelihood"]),
        ("product_type=cross_section,other", ["cross_section", "other"]),
        ("year=2021", ["cross_section"]),
        ("severity=1", ["other"]),
        ("min_severity=2", ["likelihood", "cross_section"]),
        ("status=published", ["upper_limit"]),
        ("status=published,missing&year=2024", ["likelihood", "upper_limit"]),
    ],
)
def test_gap_filters(corpus: dict[str, Any], query: str, expected: list[str]) -> None:
    data = get(f"/api/gaps?{query}")
    assert [g["product"]["product_type"] for g in data["results"]] == expected


def test_published_gap_shows_its_table_and_scores(corpus: dict[str, Any]) -> None:
    [gap] = get("/api/gaps?status=published")["results"]
    assert gap["matched_table"]["doi_url"] == "https://doi.org/10.17182/hepdata.1.v1/t1"
    assert gap["match"] == {"score": 0.8, "embedding_similarity": 0.7, "caption_overlap": 0.9}


@pytest.mark.parametrize(
    "query",
    ["product_type=banana", "year=abc", "year=1800", "severity=7", "status=done"],
)
def test_bad_filters_are_400(corpus: dict[str, Any], query: str) -> None:
    assert status(f"/api/gaps?{query}") == 400


def test_pagination(corpus: dict[str, Any]) -> None:
    data = get("/api/gaps?page_size=2")
    assert data["count"] == 3 and len(data["results"]) == 2 and data["next"]


# --- papers -----------------------------------------------------------------------------------


def test_paper_gaps_lists_all_current_products(corpus: dict[str, Any]) -> None:
    data = get("/api/papers/100/gaps")
    assert data["paper"]["year"] == 2024
    assert [g["status"] for g in data["gaps"]] == ["missing", "uncertain", "published"]
    assert (
        get("/api/papers/100/gaps?status=missing")["gaps"][0]["product"]["product_type"]
        == "likelihood"
    )


def test_unknown_paper_is_404(corpus: dict[str, Any]) -> None:
    assert status("/api/papers/999/gaps") == 404


def test_paper_list_filters_and_ordering(corpus: dict[str, Any]) -> None:
    assert [p["inspire_id"] for p in get("/api/papers?ordering=readiness")["results"]] == [200, 100]
    assert [p["inspire_id"] for p in get("/api/papers?has_hepdata=false")["results"]] == [200]
    assert get("/api/papers?year=2024")["results"][0]["arxiv_url"].startswith(
        "https://arxiv.org/abs/"
    )
    assert status("/api/papers?ordering=title") == 400
    assert status("/api/papers?has_hepdata=maybe") == 400


# --- stats, search, similarity ----------------------------------------------------------------


def test_stats(corpus: dict[str, Any]) -> None:
    data = get("/api/stats")
    assert data["papers"]["total"] == 2
    assert data["papers"]["with_hepdata_record"] == 1
    assert data["products"]["total"] == 4  # superseded excluded
    assert data["gaps"]["open"] == 3
    assert data["gaps"]["by_status"] == {
        "missing": 1,
        "no_record": 1,
        "published": 1,
        "uncertain": 1,
    }
    assert data["gaps"]["by_severity"] == {"1": 1, "2": 1, "3": 1}


def test_full_text_search_returns_only_matches(corpus: dict[str, Any]) -> None:
    """Regression: ts_rank gives non-matches a tiny positive rank; all rows came back."""
    data = get("/api/search?q=likelihood")
    assert [r["product_type"] for r in data["results"]] == ["likelihood"]
    assert get("/api/search?q=%22cross%20section%22")["count"] == 2
    assert get("/api/search?q=nonexistentword")["count"] == 0
    assert status("/api/search?q=x") == 400


def test_similar_uses_stored_embeddings(corpus: dict[str, Any]) -> None:
    limit = corpus["limit"]
    data = get(f"/api/products/{limit.pk}/similar?limit=2")
    assert len(data) == 2
    assert limit.pk not in [d["id"] for d in data]
    assert data[0]["distance"] <= data[1]["distance"]
    assert all(d["description"] != "Superseded cutflow" for d in data)
    assert status(f"/api/products/{corpus['old'].pk}/similar") == 404


# --- operations -------------------------------------------------------------------------------


def test_health() -> None:
    assert get("/healthz") == {"status": "ok"}


def test_openapi_schema_lists_every_endpoint() -> None:
    paths = set(get("/api/schema?format=json")["paths"])
    assert {"/api/gaps", "/api/papers", "/api/papers/{inspire_id}/gaps", "/api/stats",
            "/api/search", "/api/products/{id}/similar"} <= paths  # fmt: skip


def test_api_is_read_only(corpus: dict[str, Any]) -> None:
    with API:
        assert Client().post("/api/gaps", {}).status_code == 405


def test_request_path_never_imports_models_or_the_llm_router() -> None:
    """SPEC section 2, item 2: no LLM (or any model) call in the request path."""
    forbidden = (
        "reuse_radar.llm.router",
        "reuse_radar.llm.cache",
        "reuse_radar.pipeline",
        "fastembed",
        "onnxruntime",
        "httpx",
    )
    code = (
        "import os, sys, django;"
        "os.environ['DJANGO_SETTINGS_MODULE'] = 'reuse_radar.api.settings';"
        "django.setup();"
        "import reuse_radar.api.urls, reuse_radar.api.views;"
        "from django.urls import get_resolver; get_resolver().url_patterns;"
        f"print([m for m in sys.modules if m.startswith({forbidden!r})])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).parent.parent,
    )
    assert out.stdout.strip() == "[]", out.stdout
