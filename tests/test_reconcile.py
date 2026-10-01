from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from reuse_radar.clients.hepdata import HepDataRecord, PublishedTable
from reuse_radar.pipeline.harvest import CorpusConfig
from reuse_radar.pipeline.reconcile import (
    EMBEDDING_DIM,
    Match,
    Vectors,
    caption_overlap,
    clean_text,
    gap_status,
    merge_duplicates,
    readiness_score,
    reconcile_paper,
    run_reconcile,
    severity,
)


class BagOfWordsEmbedder:
    """Deterministic stand-in for the real model: hashed bag of words, unit-normalised."""

    def embed(self, texts: Sequence[str]) -> Vectors:
        out = np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in clean_text(text).lower().split():
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                out[i, h % EMBEDDING_DIM] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        normalised: Vectors = (out / np.where(norms == 0, 1, norms)).astype(np.float32)
        return normalised


RECORD = HepDataRecord("10.17182/hepdata.1.v2", 1, 2, "https://www.hepdata.net/record/ins9", ())
LIMIT_CAPTION = (
    "Observed and expected limits at 95\\% CL on the cross section of nonresonant Higgs boson "
    "pair production as a function of $\\kappa_\\lambda$."
)
TABLES = [
    PublishedTable(
        "10.17182/hepdata.1.v2/t1", "Table 14", LIMIT_CAPTION.replace("\\%", "%"), RECORD.record_doi
    ),
    PublishedTable(
        "10.17182/hepdata.1.v2/t2",
        "Correlation matrix Fig. 7a",
        "Statistical correlations between bins of the measured distribution.",
        RECORD.record_doi,
    ),
    PublishedTable(
        "10.17182/hepdata.1.v2/r1",
        "likelihoods.tar.gz",
        "Full likelihoods in the HistFactory JSON format",
        RECORD.record_doi,
        kind="resource",
        resource_type="HS3 file",
    ),
]


def _product(
    product_type: str, description: str, span: str, confidence: float = 0.9
) -> dict[str, Any]:
    return {
        "product_type": product_type,
        "description": description,
        "evidence_span": span,
        "evidence_section": "Results",
        "evidence_kind": "caption",
        "confidence": confidence,
        "provider": "groq",
        "model": "m",
    }


# --- text helpers -----------------------------------------------------------------------------


def test_clean_text_strips_latex_and_html() -> None:
    assert (
        clean_text("$p_\\text{T}^\\text{miss}$ &lt; 5 \\cite{x} <sub>T</sub>")
        == "p text T text miss < 5 T"
    )


def test_caption_overlap_is_one_when_hepdata_copies_the_caption() -> None:
    assert caption_overlap(LIMIT_CAPTION, LIMIT_CAPTION.replace("\\%", "%")) == 1.0
    assert caption_overlap(LIMIT_CAPTION, "Bootstrap replicas of the control regions") < 0.2


# --- decisions --------------------------------------------------------------------------------


def _match(sim: float, overlap: float, bonus: float = 0.0) -> Match:
    return Match("doi", sim, overlap, bonus, round(0.5 * sim + 0.5 * overlap + bonus, 4))


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        (_match(0.42, 0.76), "published"),  # caption copied into HEPData (real case)
        (_match(0.75, 0.10), "published"),  # same meaning, different words
        (_match(0.58, 0.40, 0.1), "uncertain"),  # near miss: a curator decides (real case)
        (_match(0.47, 0.31), "missing"),  # nothing relevant (real case)
    ],
)
def test_gap_status_thresholds(match: Match, expected: str) -> None:
    assert gap_status("cross_section", match, None, True)[0] == expected


def test_structural_types_use_object_kind_not_caption() -> None:
    """Regression: covariance products were false gaps next to 66 correlation-matrix tables."""
    weak = _match(0.40, 0.0)
    typed_close = _match(0.53, 0.07, 0.1)
    typed_far = _match(0.31, 0.12, 0.1)
    assert gap_status("covariance_matrix", weak, typed_close, True) == ("published", typed_close)
    assert gap_status("likelihood", weak, typed_far, True) == ("uncertain", typed_far)
    assert gap_status("cross_section", weak, typed_close, True)[0] == "missing"


def test_no_record_overrides_everything() -> None:
    assert gap_status("upper_limit", _match(0.9, 1.0), None, False) == ("no_record", None)


def test_severity_levels() -> None:
    assert severity("likelihood", "missing", 0.9) == 3
    assert severity("likelihood", "uncertain", 0.9) == 2
    assert severity("likelihood", "missing", 0.4) == 2
    assert severity("cross_section", "no_record", 0.9) == 2
    assert severity("other", "uncertain", 0.3) == 1  # never below 1 for a gap
    assert severity("likelihood", "published", 0.9) == 0


def test_merge_duplicates_keeps_most_confident_of_same_type() -> None:
    products = [
        _product("upper_limit", "95% CL upper limit on HH cross section", "a" * 30, 0.8),
        _product("upper_limit", "95% CL upper limit on HH cross section", "b" * 30, 0.95),
        _product("likelihood", "95% CL upper limit on HH cross section", "c" * 30, 0.9),
    ]
    vectors = BagOfWordsEmbedder().embed([p["description"] for p in products])
    kept, _, merged = merge_duplicates(products, vectors)
    assert [p["evidence_span"][0] for p in kept] == ["b", "c"]  # document order, best kept
    assert merged == [1, 0]


# --- per paper --------------------------------------------------------------------------------


def _extraction(*products: dict[str, Any]) -> dict[str, Any]:
    return {
        "inspire_id": 9,
        "arxiv_id": "2112.11876",
        "status": "ok",
        "extraction_version": "extract_v1+filter_1",
        "products": list(products),
    }


def test_reconcile_paper_end_to_end() -> None:
    extraction = _extraction(
        _product(
            "upper_limit", "Limits on nonresonant HH cross section vs kappa lambda", LIMIT_CAPTION
        ),
        _product(
            "cross_section", "Unfolded differential ttW cross-section in jet multiplicity", "x" * 40
        ),
        _product("likelihood", "Full statistical likelihood of the fit", "y" * 40),
    )
    result = reconcile_paper(extraction, RECORD, TABLES, BagOfWordsEmbedder())
    statuses = {p.product_type: p.status for p in result.products}
    assert statuses["upper_limit"] == "published"
    assert statuses["cross_section"] == "missing"
    assert statuses["likelihood"] in ("published", "uncertain")  # HS3 file present
    assert result.hepdata_record_id == 1 and len(result.tables) == 3
    assert all(len(p.embedding) == EMBEDDING_DIM for p in result.products)
    assert result.readiness_score is not None and 0 < result.readiness_score < 100


def test_paper_without_record_is_all_no_record() -> None:
    extraction = _extraction(_product("upper_limit", "Limits", LIMIT_CAPTION))
    result = reconcile_paper(extraction, None, [], BagOfWordsEmbedder())
    assert [p.status for p in result.products] == ["no_record"]
    assert result.readiness_score == 0.0


def test_readiness_is_weighted_and_none_without_products() -> None:
    assert readiness_score([]) is None
    extraction = _extraction(
        _product("likelihood", "Full likelihood", "y" * 40),  # weight 3, missing here
        _product(
            "upper_limit", "Limits on nonresonant HH cross section vs kappa lambda", LIMIT_CAPTION
        ),
    )
    result = reconcile_paper(extraction, RECORD, TABLES[:1], BagOfWordsEmbedder())
    assert result.readiness_score == pytest.approx(100 * 2 / 5)


# --- stage runner -----------------------------------------------------------------------------


class StubSource:
    def __init__(self, record: HepDataRecord | None) -> None:
        self.record = record

    def find_record(self, inspire_id: int) -> HepDataRecord | None:
        return self.record

    def list_tables(self, record: HepDataRecord) -> list[PublishedTable]:
        return TABLES


def test_run_reconcile_writes_outputs(tmp_path: Path) -> None:
    corpus = CorpusConfig("c", "q", 2021, 2021)
    extract_dir = tmp_path / "extract" / "c"
    extract_dir.mkdir(parents=True)
    ok = _extraction(_product("upper_limit", "Limits", LIMIT_CAPTION))
    skipped = {"inspire_id": 10, "status": "skipped", "extraction_version": "v"}
    (extract_dir / "9.json").write_text(json.dumps(ok), encoding="utf-8")
    (extract_dir / "10.json").write_text(json.dumps(skipped), encoding="utf-8")
    (extract_dir / "_summary.json").write_text("{}", encoding="utf-8")

    summary = run_reconcile(StubSource(RECORD), BagOfWordsEmbedder(), corpus, tmp_path)
    out = tmp_path / "reconcile" / "c"
    assert json.loads((out / "9.json").read_text(encoding="utf-8"))["status"] == "ok"
    assert json.loads((out / "10.json").read_text(encoding="utf-8"))["status"] == "skipped"
    assert summary["products_by_status"] == {"published": 1}


def test_run_reconcile_requires_extract_output(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run extract first"):
        run_reconcile(
            StubSource(None), BagOfWordsEmbedder(), CorpusConfig("c", "q", 2021, 2021), tmp_path
        )


def test_inspire_link_without_datacite_record_is_flagged_not_a_gap(tmp_path: Path) -> None:
    """Fail loudly: never report "no_record" gaps when INSPIRE says a HEPData record exists."""
    corpus = CorpusConfig("c", "q", 2021, 2021)
    harvest = tmp_path / "harvest" / "c" / "2021.jsonl"
    harvest.parent.mkdir(parents=True)
    row = {"inspire_id": 9, "inspire_links_hepdata": True}
    harvest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    extract_dir = tmp_path / "extract" / "c"
    extract_dir.mkdir(parents=True)
    ok = _extraction(_product("upper_limit", "Limits", LIMIT_CAPTION))
    (extract_dir / "9.json").write_text(json.dumps(ok), encoding="utf-8")

    summary = run_reconcile(StubSource(None), BagOfWordsEmbedder(), corpus, tmp_path)
    out = json.loads((tmp_path / "reconcile" / "c" / "9.json").read_text(encoding="utf-8"))
    assert out["status"] == "lookup_error" and out["products"] == []
    assert summary["lookup_errors"] == 1
