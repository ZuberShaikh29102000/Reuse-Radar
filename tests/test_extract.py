from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reuse_radar.llm.router import AllProvidersExhaustedError, LLMRequest, LLMResult
from reuse_radar.llm.schemas import (
    EXTRACTION_JSON_SCHEMA,
    PRODUCT_TYPES,
    ExtractedProduct,
    ExtractionOutput,
)
from reuse_radar.pipeline.extract import (
    EXTRACTION_VERSION,
    MIN_SPAN_CHARS,
    PROMPT_PATH,
    DroppedProduct,
    Passage,
    VerifiedProduct,
    chunk_passages,
    extract_paper,
    load_system_prompt,
    locate_span,
    run_extract,
    verify,
)
from reuse_radar.pipeline.harvest import CorpusConfig

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
CAPTION = (
    "\\caption{Observed and expected limits at 95\\% CL on the cross section of nonresonant\n"
    "Higgs boson pair production as a function of $\\kappa_\\lambda$.}"
)
PARAGRAPH = "All these distributions are available from HEPData~\\cite{Maguire:2017ypu}."


def _result(text: str, finish: str = "stop", provider: str = "groq") -> LLMResult:
    return LLMResult(provider, "m", text, 10, 10, False, finish)


def _products(*items: dict[str, Any]) -> str:
    return json.dumps({"products": list(items)})


def _product(span: str, passage_id: int = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "product_type": "upper_limit",
        "description": "Limits on HH cross section vs kappa_lambda.",
        "evidence_span": span,
        "passage_id": passage_id,
        "confidence": 0.9,
        **overrides,
    }


class ScriptedRouter:
    """Returns queued results in order and records every request."""

    def __init__(self, results: list[LLMResult | Exception]) -> None:
        self.results = list(results)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _record(inspire_id: int = 1, *candidates: tuple[str, str, str]) -> dict[str, Any]:
    cands = candidates or (("Results", "caption", CAPTION), ("Results", "paragraph", PARAGRAPH))
    return {
        "inspire_id": inspire_id,
        "arxiv_id": "2112.11876",
        "status": "ok",
        "candidates": [{"section_name": s, "kind": k, "text": t} for s, k, t in cands],
    }


# --- schema and prompt ------------------------------------------------------------------------


def test_json_schema_matches_pydantic_model() -> None:
    item = EXTRACTION_JSON_SCHEMA["properties"]["products"]["items"]
    assert set(item["properties"]) == set(ExtractedProduct.model_fields)
    assert set(item["required"]) == set(ExtractedProduct.model_fields)
    assert item["properties"]["product_type"]["enum"] == list(PRODUCT_TYPES)
    assert item["additionalProperties"] is False
    assert EXTRACTION_JSON_SCHEMA["additionalProperties"] is False


def test_spec_product_types_are_all_present() -> None:
    assert set(PRODUCT_TYPES) == {
        "cross_section", "upper_limit", "efficiency_map", "likelihood", "covariance_matrix",
        "acceptance_table", "cutflow", "correlation_matrix", "other",
    }  # fmt: skip


def test_prompt_defines_every_product_type() -> None:
    prompt = load_system_prompt()
    for product_type in PRODUCT_TYPES:
        assert f"- {product_type}:" in prompt
    assert PROMPT_PATH.stem in EXTRACTION_VERSION


def test_recorded_provider_outputs_validate() -> None:
    groq = json.loads((FIXTURES / "groq_ok.json").read_text(encoding="utf-8"))
    gemini = json.loads((FIXTURES / "gemini_ok.json").read_text(encoding="utf-8"))
    ExtractionOutput.model_validate_json(groq["choices"][0]["message"]["content"])
    ExtractionOutput.model_validate_json(gemini["candidates"][0]["content"]["parts"][-1]["text"])


# --- span location ----------------------------------------------------------------------------


def test_locate_exact() -> None:
    assert locate_span("available from HEPData", PARAGRAPH) == ("available from HEPData", "exact")


def test_locate_tolerates_only_whitespace_differences() -> None:
    model_span = "cross section of nonresonant Higgs boson pair production as a function"
    found = locate_span(model_span, CAPTION)
    assert found is not None
    verbatim, how = found
    assert how == "whitespace"
    assert "nonresonant\nHiggs" in verbatim  # the stored span is the source's text


def test_locate_tolerates_deleted_whitespace() -> None:
    """Seen live: model wrote 'space.}' where the source has 'space.\\n}'."""
    source = "constructed for all processes in the fiducial phase space.\n}"
    found = locate_span("in the fiducial phase space.}", source)
    assert found == ("in the fiducial phase space.\n}", "whitespace")


@pytest.mark.parametrize(
    "altered",
    [
        "Observed and expected limits at 95% CL on the cross section",  # dropped backslash
        "Observed and expected limits at 95\\% C.L. on the cross section",  # changed text
        "Observed and expected limits ... on the cross section",  # ellipsis join
        "Observed & expected limits at 95\\% CL on the cross section",
    ],
)
def test_locate_rejects_any_non_whitespace_change(altered: str) -> None:
    assert locate_span(altered, CAPTION) is None


# --- verification -----------------------------------------------------------------------------

PASSAGES = [
    Passage(1, "Results", "caption", CAPTION),
    Passage(2, "Conclusions", "paragraph", PARAGRAPH),
]


def _verify(span: str, passage_id: int = 1) -> VerifiedProduct | DroppedProduct:
    product = ExtractedProduct.model_validate(_product(span, passage_id))
    return verify(product, PASSAGES, _result(""))


def test_verify_records_section_and_kind() -> None:
    verified = _verify("Observed and expected limits at 95\\% CL")
    assert isinstance(verified, VerifiedProduct)
    assert (verified.evidence_section, verified.evidence_kind) == ("Results", "caption")


def test_verify_accepts_span_from_a_misnumbered_passage() -> None:
    verified = _verify("All these distributions are available from HEPData", passage_id=1)
    assert isinstance(verified, VerifiedProduct)
    assert verified.evidence_section == "Conclusions"


def test_verify_drops_short_spans() -> None:
    short = "x" * (MIN_SPAN_CHARS - 1)
    dropped = _verify(short)
    assert isinstance(dropped, DroppedProduct) and dropped.reason == "too_short"
    dropped = _verify("cross section")  # real example from a live Groq probe
    assert isinstance(dropped, DroppedProduct) and dropped.reason == "too_short"


def test_verify_drops_hallucinated_spans() -> None:
    dropped = _verify("Upper limits on the WW cross section are shown in Figure 9.")
    assert isinstance(dropped, DroppedProduct) and dropped.reason == "not_found"


# --- per paper --------------------------------------------------------------------------------


def test_extract_paper_happy_path() -> None:
    router = ScriptedRouter(
        [
            _result(
                _products(
                    _product("Observed and expected limits at 95\\% CL on the cross section"),
                    _product("totally invented sentence that is not in the paper", 2),
                )
            )
        ]
    )
    result = extract_paper(router, "sys", _record())
    assert result.status == "ok"
    assert [p.product_type for p in result.products] == ["upper_limit"]
    assert result.dropped_spans == 1 and result.dropped[0].reason == "not_found"
    assert result.extraction_version == EXTRACTION_VERSION


def test_schema_failure_retries_once_with_the_error() -> None:
    router = ScriptedRouter(
        [
            _result('{"products": [{"product_type": "banana"}]}'),
            _result(_products(_product("All these distributions are available from HEPData", 2))),
        ]
    )
    result = extract_paper(router, "sys", _record())
    assert result.status == "ok" and len(result.products) == 1
    retry = router.requests[1]
    assert [role for role, _ in retry.messages] == ["user", "assistant", "user"]
    assert "banana" in retry.messages[1][1]
    assert "did not validate" in retry.messages[2][1]


def test_second_schema_failure_marks_paper_without_partial_products() -> None:
    router = ScriptedRouter([_result("not json"), _result("still not json")])
    result = extract_paper(router, "sys", _record())
    assert result.status == "schema_error"
    assert result.products == []
    assert len(router.requests) == 2


def test_truncated_output_counts_as_schema_failure() -> None:
    router = ScriptedRouter([_result('{"products": [', finish="length"), _result(_products())])
    assert extract_paper(router, "sys", _record()).status == "ok"
    assert len(router.requests) == 2


def test_two_products_may_share_one_caption() -> None:
    """Seen live: one caption gave both an inclusive cross-section and a cross-section ratio."""
    span = "Observed and expected limits at 95\\% CL on the cross section"
    router = ScriptedRouter(
        [
            _result(
                _products(
                    _product(span, description="Inclusive cross-section."),
                    _product(span, description="Cross-section ratio."),
                    _product(span, description="Inclusive cross-section.", confidence=0.5),
                )
            )
        ]
    )
    result = extract_paper(router, "sys", _record())
    assert sorted(p.description for p in result.products) == [
        "Cross-section ratio.",
        "Inclusive cross-section.",
    ]
    assert max(p.confidence for p in result.products) == 0.9


def test_non_ok_filter_records_are_skipped_without_calls() -> None:
    router = ScriptedRouter([])
    result = extract_paper(router, "sys", {"inspire_id": 5, "status": "no_latex"})
    assert result.status == "skipped" and router.requests == []


# --- chunking ---------------------------------------------------------------------------------


def test_chunks_respect_budget_and_restart_ids() -> None:
    cands = [{"section_name": "S", "kind": "paragraph", "text": "x" * 400} for _ in range(5)]
    chunks = chunk_passages(cands, budget=250)  # 100 tokens each -> 2 per chunk
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert [p.passage_id for p in chunks[1]] == [1, 2]


def test_oversized_candidate_gets_its_own_chunk() -> None:
    cands = [
        {"section_name": "S", "kind": "paragraph", "text": "a" * 40},
        {"section_name": "S", "kind": "paragraph", "text": "b" * 4000},
    ]
    assert [len(c) for c in chunk_passages(cands, budget=100)] == [1, 1]


# --- stage runner -----------------------------------------------------------------------------

CORPUS = CorpusConfig(name="c", query="q", start_year=2021, end_year=2021)
OK = _products(_product("All these distributions are available from HEPData", 2))


def _write_filter(tmp_path: Path, *records: dict[str, Any]) -> None:
    out = tmp_path / "filter" / "c"
    out.mkdir(parents=True)
    for record in records:
        (out / f"{record['inspire_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    (out / "_summary.json").write_text("{}", encoding="utf-8")


def test_run_extract_writes_records_and_summary(tmp_path: Path) -> None:
    _write_filter(tmp_path, _record(1), {"inspire_id": 2, "status": "no_arxiv_id"})
    summary = run_extract(ScriptedRouter([_result(OK)]), CORPUS, tmp_path)
    assert summary["statuses"] == {"ok": 1, "skipped": 1}
    record = json.loads((tmp_path / "extract" / "c" / "1.json").read_text(encoding="utf-8"))
    assert record["products"][0]["evidence_section"] == "Results"


def test_quota_exhaustion_keeps_finished_papers_and_raises(tmp_path: Path) -> None:
    _write_filter(tmp_path, _record(1), _record(2), _record(3))
    router = ScriptedRouter([_result(OK), AllProvidersExhaustedError("quota")])
    with pytest.raises(AllProvidersExhaustedError, match="1 of 3 papers done"):
        run_extract(router, CORPUS, tmp_path)
    out = tmp_path / "extract" / "c"
    assert (out / "1.json").exists() and not (out / "2.json").exists()
    summary = json.loads((out / "_summary.json").read_text(encoding="utf-8"))
    assert summary["stopped_early"]


def test_only_filter_selects_papers(tmp_path: Path) -> None:
    _write_filter(tmp_path, _record(1), _record(2))
    summary = run_extract(ScriptedRouter([_result(OK)]), CORPUS, tmp_path, only=[2])
    assert summary["papers_attempted"] == 1


def test_run_extract_requires_filter_output(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run filter first"):
        run_extract(ScriptedRouter([]), CORPUS, tmp_path)
