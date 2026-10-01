"""Reconcile stage: compare what each paper declares with what HEPData actually holds.

For every extracted paper:
1. Embed its declared products locally (all-MiniLM-L6-v2, 384 dimensions, no API calls) and
   merge near-duplicates: the same result quoted from both Results and Conclusions.
2. Look up its HEPData record and list the record's tables and resources (via DataCite).
3. Score every product against every table with three deterministic signals:
   - meaning: cosine similarity of the embeddings;
   - caption overlap: HEPData descriptions are usually the paper's own captions, so word
     overlap with the product's evidence span is strong evidence;
   - type keywords: a likelihood product next to a HistFactory file, a covariance product next
     to a "Correlation matrix" table, and so on.
4. Assign each product a gap status, a severity, and the paper a readiness score.

Thresholds were calibrated on three hand-checked papers; see docs/adr/0004. Every score is kept
in the output so reviewers can see why a decision was made.

Output: `<data_dir>/reconcile/<corpus>/<inspire_id>.json`.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import numpy.typing as npt
from opentelemetry import metrics, trace

from reuse_radar.clients.hepdata import HepDataClient, HepDataRecord, PublishedTable
from reuse_radar.config import Settings
from reuse_radar.log import configure_logging
from reuse_radar.pipeline.harvest import CorpusConfig

RECONCILE_VERSION = "1"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Calibrated on 2112.11876, 2401.05299 and 2403.02793 (docs/adr/0004).
DUPLICATE_SIMILARITY = 0.90
PUBLISHED_OVERLAP = 0.60
PUBLISHED_EMBEDDING = 0.70
PUBLISHED_COMBINED = 0.60
UNCERTAIN_COMBINED = 0.42
TYPE_BONUS = 0.10
STRUCTURAL_PUBLISHED_EMBEDDING = 0.50
STRUCTURAL_TYPES = frozenset(
    {
        "likelihood",
        "covariance_matrix",
        "correlation_matrix",
        "cutflow",
        "efficiency_map",
        "acceptance_table",
    }
)

GapStatus = Literal["published", "uncertain", "missing", "no_record"]

# How much a missing product hurts reuse. Reinterpretation material (likelihoods, covariances,
# efficiencies, cut-flows) is what is most often missing and hardest to reconstruct later.
TYPE_WEIGHT: dict[str, int] = {
    "likelihood": 3,
    "covariance_matrix": 3,
    "correlation_matrix": 3,
    "efficiency_map": 3,
    "acceptance_table": 3,
    "cutflow": 3,
    "upper_limit": 2,
    "cross_section": 2,
    "other": 1,
}
_TYPE_KEYWORDS: dict[str, re.Pattern[str]] = {
    "upper_limit": re.compile(r"limit|exclu|contour", re.I),
    "likelihood": re.compile(r"likelihood|histfactory|hs3|pyhf|\bnll\b", re.I),
    "covariance_matrix": re.compile(r"covariance|correlation matri", re.I),
    "correlation_matrix": re.compile(r"correlation", re.I),
    "efficiency_map": re.compile(r"efficien|acceptance", re.I),
    "acceptance_table": re.compile(r"acceptance|efficien", re.I),
    "cutflow": re.compile(r"cut[\s-]*flow", re.I),
    "cross_section": re.compile(r"cross[\s-]*section|xsec|sigma", re.I),
}

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_gap_counter = _meter.create_counter("reconcile_gaps_total", description="Products by gap status")

Vectors = npt.NDArray[np.float32]


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> Vectors:
        """Unit-normalised embeddings, one row per text."""
        ...


class FastEmbedder:
    """all-MiniLM-L6-v2 through ONNX Runtime (fastembed): the sentence-transformers model
    without a PyTorch install. Downloaded once into the cache directory."""

    def __init__(self, cache_dir: Path, model: str = EMBEDDING_MODEL) -> None:
        self._cache_dir = cache_dir
        self._model_name = model
        self._model: Any = None

    def embed(self, texts: Sequence[str]) -> Vectors:
        if not texts:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        if self._model is None:
            from fastembed import TextEmbedding  # heavy import, only when needed

            self._model = TextEmbedding(self._model_name, cache_dir=str(self._cache_dir))
        vectors = np.array(list(self._model.embed(list(texts))), dtype=np.float32)
        if vectors.shape[1] != EMBEDDING_DIM:
            raise RuntimeError(f"{self._model_name} returned {vectors.shape[1]} dims")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        normalised: Vectors = (vectors / np.where(norms == 0, 1, norms)).astype(np.float32)
        return normalised


# --- text normalisation -----------------------------------------------------------------------

_LATEX_ARG_CMD = re.compile(r"\\(cite|ref|label|eqref|url|href)\{[^}]*\}")
_LATEX_CMD = re.compile(r"\\([A-Za-z]+)")
_HTML_TAG = re.compile(r"<[^>]+>")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_STOP = frozenset(
    (
        "the", "a", "an", "of", "in", "for", "and", "to", "with", "is", "are", "as", "by",
        "on", "at", "from", "or", "this", "that", "be", "its", "their", "which", "these",
        "shown", "figure", "table",
    )
)  # fmt: skip


def clean_text(text: str) -> str:
    """LaTeX/HTML-light plain text for embedding."""
    text = html.unescape(_HTML_TAG.sub(" ", text))
    text = _LATEX_ARG_CMD.sub(" ", text)
    text = _LATEX_CMD.sub(r"\1", text)
    text = re.sub(r"[{}$^_~\\]", " ", text)
    return " ".join(text.split())


def content_words(text: str) -> set[str]:
    words = _NON_WORD.sub(" ", clean_text(text).lower()).split()
    return {w for w in words if len(w) > 1 and w not in _STOP}


def caption_overlap(span: str, description: str) -> float:
    """Share of the smaller word set found in the other (1.0 = one contains the other)."""
    a, b = content_words(span), content_words(description)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def product_text(product: dict[str, Any]) -> str:
    return f"{product['product_type'].replace('_', ' ')}: {clean_text(product['description'])}"


def table_text(table: PublishedTable) -> str:
    return clean_text(f"{table.name}: {table.description}")


# --- results ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    table_doi: str
    embedding_similarity: float
    caption_overlap: float
    type_bonus: float
    score: float


@dataclass(frozen=True)
class ReconciledProduct:
    product_type: str
    description: str
    evidence_span: str
    evidence_section: str
    evidence_kind: str
    confidence: float
    provider: str
    model: str
    merged_duplicates: int
    embedding: list[float]
    status: GapStatus
    severity: int  # 0 = not a gap; 3 = most urgent
    best_match: Match | None


@dataclass(frozen=True)
class TableRecord:
    table_doi: str
    name: str
    description: str
    kind: str
    resource_type: str
    embedding: list[float]


@dataclass(frozen=True)
class PaperReconciliation:
    inspire_id: int
    arxiv_id: str | None
    status: Literal["ok", "skipped", "lookup_error"]
    reconcile_version: str
    extraction_version: str | None
    detail: str | None = None
    hepdata_record_doi: str | None = None
    hepdata_record_id: int | None = None
    hepdata_version: int | None = None
    hepdata_url: str | None = None
    readiness_score: float | None = None
    products: list[ReconciledProduct] = field(default_factory=list)
    tables: list[TableRecord] = field(default_factory=list)


# --- core logic -------------------------------------------------------------------------------


def merge_duplicates(
    products: list[dict[str, Any]], vectors: Vectors
) -> tuple[list[dict[str, Any]], Vectors, list[int]]:
    """Merge products of the same type whose embeddings are near-identical.

    Keeps the most confident of each group; returns (products, their vectors, merged counts).
    """
    order = sorted(range(len(products)), key=lambda i: -float(products[i]["confidence"]))
    kept: list[int] = []
    merged: dict[int, int] = {}
    for i in order:
        duplicate_of = next(
            (
                k
                for k in kept
                if products[k]["product_type"] == products[i]["product_type"]
                and float(vectors[i] @ vectors[k]) >= DUPLICATE_SIMILARITY
            ),
            None,
        )
        if duplicate_of is None:
            kept.append(i)
            merged[i] = 0
        else:
            merged[duplicate_of] += 1
    kept.sort()  # back to document order
    return [products[i] for i in kept], vectors[kept], [merged[i] for i in kept]


def best_matches(
    product: dict[str, Any],
    vector: Vectors,
    tables: Sequence[PublishedTable],
    table_vectors: Vectors,
) -> tuple[Match | None, Match | None]:
    """(best match overall, most similar part whose name/description fits the product type)."""
    if not tables:
        return None, None
    similarities = table_vectors @ vector
    keywords = _TYPE_KEYWORDS.get(product["product_type"])
    best: Match | None = None
    best_typed: Match | None = None
    for table, similarity in zip(tables, similarities, strict=True):
        overlap = caption_overlap(product["evidence_span"], table.description)
        typed = bool(
            keywords and keywords.search(f"{table.name} {table.description} {table.resource_type}")
        )
        bonus = TYPE_BONUS if typed else 0.0
        match = Match(
            table.table_doi,
            round(float(similarity), 4),
            round(overlap, 4),
            bonus,
            round(0.5 * float(similarity) + 0.5 * overlap + bonus, 4),
        )
        if best is None or match.score > best.score:
            best = match
        if typed and (best_typed is None or similarity > best_typed.embedding_similarity):
            best_typed = match
    return best, best_typed


def gap_status(
    product_type: str, match: Match | None, typed: Match | None, has_record: bool
) -> tuple[GapStatus, Match | None]:
    """Decide the status; returns it with the match that justified it."""
    if not has_record:
        return "no_record", None
    if match is None:
        return "missing", None
    if (
        match.caption_overlap >= PUBLISHED_OVERLAP
        or match.embedding_similarity >= PUBLISHED_EMBEDDING
        or match.score >= PUBLISHED_COMBINED
    ):
        return "published", match
    # Matrices, likelihoods, cut-flows and efficiency maps are usually declared in prose, not in
    # a caption, so caption overlap cannot work for them. What matters is whether the record
    # holds an object of that kind at all (seen live: 66 "Correlation matrix" tables were
    # invisible to the caption rule and became false gaps).
    if product_type in STRUCTURAL_TYPES and typed is not None:
        if typed.embedding_similarity >= STRUCTURAL_PUBLISHED_EMBEDDING:
            return "published", typed
        return "uncertain", typed
    if match.score >= UNCERTAIN_COMBINED:
        return "uncertain", match
    return "missing", match


def severity(product_type: str, status: GapStatus, confidence: float) -> int:
    if status == "published":
        return 0
    level = TYPE_WEIGHT.get(product_type, 1)
    if status == "uncertain":
        level -= 1
    if confidence < 0.5:
        level -= 1
    return max(1, level)


def readiness_score(products: Sequence[ReconciledProduct]) -> float | None:
    """0-100: weighted share of declared products found on HEPData (uncertain counts half)."""
    if not products:
        return None
    credit = {"published": 1.0, "uncertain": 0.5, "missing": 0.0, "no_record": 0.0}
    total = sum(TYPE_WEIGHT.get(p.product_type, 1) for p in products)
    earned = sum(TYPE_WEIGHT.get(p.product_type, 1) * credit[p.status] for p in products)
    return round(100.0 * earned / total, 1)


def reconcile_paper(
    extraction: dict[str, Any],
    record: HepDataRecord | None,
    tables: Sequence[PublishedTable],
    embedder: Embedder,
) -> PaperReconciliation:
    inspire_id = int(extraction["inspire_id"])
    raw_products: list[dict[str, Any]] = list(extraction.get("products") or [])
    vectors = embedder.embed([product_text(p) for p in raw_products])
    products, vectors, merged = merge_duplicates(raw_products, vectors)
    table_vectors = embedder.embed([table_text(t) for t in tables])

    reconciled: list[ReconciledProduct] = []
    for product, vector, merged_count in zip(products, vectors, merged, strict=True):
        overall, typed = best_matches(product, vector, tables, table_vectors)
        status, match = gap_status(product["product_type"], overall, typed, record is not None)
        _gap_counter.add(1, {"status": status})
        reconciled.append(
            ReconciledProduct(
                product_type=product["product_type"],
                description=product["description"],
                evidence_span=product["evidence_span"],
                evidence_section=product["evidence_section"],
                evidence_kind=product["evidence_kind"],
                confidence=float(product["confidence"]),
                provider=product["provider"],
                model=product["model"],
                merged_duplicates=merged_count,
                embedding=[round(float(x), 6) for x in vector],
                status=status,
                severity=severity(product["product_type"], status, float(product["confidence"])),
                best_match=match,
            )
        )
    return PaperReconciliation(
        inspire_id=inspire_id,
        arxiv_id=extraction.get("arxiv_id"),
        status="ok",
        reconcile_version=RECONCILE_VERSION,
        extraction_version=extraction.get("extraction_version"),
        hepdata_record_doi=record.record_doi if record else None,
        hepdata_record_id=record.hepdata_id if record else None,
        hepdata_version=record.version if record else None,
        hepdata_url=record.url if record else None,
        readiness_score=readiness_score(reconciled),
        products=reconciled,
        tables=[
            TableRecord(
                table_doi=t.table_doi,
                name=t.name,
                description=t.description,
                kind=t.kind,
                resource_type=t.resource_type,
                embedding=[round(float(x), 6) for x in v],
            )
            for t, v in zip(tables, table_vectors, strict=True)
        ],
    )


# --- stage runner -----------------------------------------------------------------------------


class RecordSource(Protocol):
    def find_record(self, inspire_id: int) -> HepDataRecord | None: ...

    def list_tables(self, record: HepDataRecord) -> list[PublishedTable]: ...


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _inspire_hepdata_links(corpus: CorpusConfig, data_dir: Path) -> dict[int, bool]:
    """inspire_id -> whether INSPIRE links a HEPData record (from the harvest output)."""
    links: dict[int, bool] = {}
    for year in corpus.years:
        path = data_dir / "harvest" / corpus.name / f"{year}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                links[int(row["inspire_id"])] = bool(row.get("inspire_links_hepdata"))
    return links


def run_reconcile(
    source: RecordSource,
    embedder: Embedder,
    corpus: CorpusConfig,
    data_dir: Path,
    *,
    only: Sequence[int] | None = None,
) -> dict[str, object]:
    """Reconcile every extracted paper. Entry point for Airflow and GitHub Actions."""
    with tracer.start_as_current_span("reconcile") as span:
        span.set_attribute("corpus", corpus.name)
        extract_dir = data_dir / "extract" / corpus.name
        out_dir = data_dir / "reconcile" / corpus.name
        if not extract_dir.is_dir():
            raise FileNotFoundError(f"extract output missing: {extract_dir}; run extract first")
        paths = sorted(p for p in extract_dir.glob("*.json") if not p.name.startswith("_"))
        if only is not None:
            wanted = {str(i) for i in only}
            paths = [p for p in paths if p.stem in wanted]

        inspire_links = _inspire_hepdata_links(corpus, data_dir)
        statuses: dict[str, int] = {}
        scores: list[float] = []
        lookup_errors = 0
        for path in paths:
            extraction = json.loads(path.read_text(encoding="utf-8"))
            inspire_id = int(extraction["inspire_id"])
            with tracer.start_as_current_span("reconcile.paper") as paper_span:
                paper_span.set_attribute("inspire_id", inspire_id)
                record = source.find_record(inspire_id) if extraction["status"] == "ok" else None
                if extraction["status"] != "ok":
                    result = PaperReconciliation(
                        inspire_id,
                        extraction.get("arxiv_id"),
                        "skipped",
                        RECONCILE_VERSION,
                        extraction.get("extraction_version"),
                        detail=f"extract status {extraction['status']}",
                    )
                elif record is None and inspire_links.get(inspire_id):
                    # INSPIRE says a HEPData record exists but the DataCite lookup found none.
                    # Reporting "no_record" gaps here would be confidently wrong; flag instead.
                    lookup_errors += 1
                    logger.error(
                        "INSPIRE links a HEPData record that DataCite does not return",
                        extra={"inspire_id": inspire_id},
                    )
                    result = PaperReconciliation(
                        inspire_id,
                        extraction.get("arxiv_id"),
                        "lookup_error",
                        RECONCILE_VERSION,
                        extraction.get("extraction_version"),
                        detail="INSPIRE links a HEPData record; DataCite returned none",
                    )
                else:
                    tables = source.list_tables(record) if record else []
                    result = reconcile_paper(extraction, record, tables, embedder)
                    for product in result.products:
                        statuses[product.status] = statuses.get(product.status, 0) + 1
                    if result.readiness_score is not None:
                        scores.append(result.readiness_score)
                    logger.info(
                        "paper reconciled",
                        extra={
                            "inspire_id": inspire_id,
                            "hepdata_record": result.hepdata_record_doi,
                            "tables": len(tables),
                            "products": len(result.products),
                            "readiness_score": result.readiness_score,
                        },
                    )
            _write_json_atomic(out_dir / f"{inspire_id}.json", asdict(result))

        summary: dict[str, object] = {
            "corpus": corpus.name,
            "reconcile_version": RECONCILE_VERSION,
            "papers": len(paths),
            "products_by_status": statuses,
            "lookup_errors": lookup_errors,
            "mean_readiness_score": round(sum(scores) / len(scores), 1) if scores else None,
        }
        _write_json_atomic(out_dir / "_summary.json", summary)
        logger.info("reconcile stage finished", extra=summary)
        return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Match declared products to HEPData tables.")
    parser.add_argument("--corpus", type=Path, default=Path("config/corpus.toml"))
    parser.add_argument("--only", type=int, nargs="*", help="only these INSPIRE ids")
    args = parser.parse_args(argv)

    configure_logging()
    settings = Settings.from_env()
    client = HepDataClient(
        contact_email=settings.inspire_contact_email, cache_dir=settings.cache_dir
    )
    embedder = FastEmbedder(settings.cache_dir / "models")
    run_reconcile(
        client, embedder, CorpusConfig.from_toml(args.corpus), settings.data_dir, only=args.only
    )


if __name__ == "__main__":
    main()
