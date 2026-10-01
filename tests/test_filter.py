from __future__ import annotations

import json
from pathlib import Path

import pytest

from reuse_radar.clients.arxiv import ArxivAccessDeniedError, ArxivSourceError, PaperSource
from reuse_radar.pipeline.filter import (
    FILTER_VERSION,
    FilterError,
    FilterResult,
    filter_latex,
    filter_paper,
    run_filter,
    strip_comments,
)
from reuse_radar.pipeline.harvest import CorpusConfig

FIXTURES = Path(__file__).parent / "fixtures" / "arxiv"
PAPERS = ("2112.11876", "2401.05299", "2403.02793")


def _load(arxiv_id: str) -> dict[str, str]:
    root = FIXTURES / arxiv_id
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*.tex")
    }


@pytest.fixture(scope="module")
def results() -> dict[str, FilterResult]:
    return {arxiv_id: filter_latex(_load(arxiv_id)) for arxiv_id in PAPERS}


def _joined(result: FilterResult) -> str:
    return "\n".join(" ".join(c.text.split()) for c in result.candidates)


# --- real papers ------------------------------------------------------------------------------


@pytest.mark.parametrize("arxiv_id", PAPERS)
def test_candidates_are_verbatim_slices_of_the_document(
    results: dict[str, FilterResult], arxiv_id: str
) -> None:
    result = results[arxiv_id]
    assert result.candidates
    for candidate in result.candidates:
        assert candidate.text in result.document
        assert candidate.text == candidate.text.strip()


@pytest.mark.parametrize("arxiv_id", PAPERS)
def test_token_reduction_floor(results: dict[str, FilterResult], arxiv_id: str) -> None:
    # Regression guard. Measured 5.8x / 7.0x / 6.5x at FILTER_VERSION 1; see docs/adr/0002 for
    # why the 10x target is not forced at the cost of dropping results captions.
    assert results[arxiv_id].reduction_ratio >= 5.0


@pytest.mark.parametrize("arxiv_id", PAPERS)
def test_author_list_is_skipped_and_nothing_else_missing(
    results: dict[str, FilterResult], arxiv_id: str
) -> None:
    assert results[arxiv_id].skipped_inputs == ["atlas_authlist"]
    assert results[arxiv_id].missing_inputs == []


def test_keeps_hepdata_statements(results: dict[str, FilterResult]) -> None:
    text = _joined(results["2403.02793"])
    assert "All these distributions are available from HEPData" in text
    assert "Information about uncertainties and correlations is provided on HEPData" in text


def test_keeps_limit_and_likelihood_captions(results: dict[str, FilterResult]) -> None:
    captions = [c.text for c in results["2112.11876"].candidates if c.kind == "caption"]
    assert any(
        "Observed and expected limits at 95\\% CL on the cross section" in c for c in captions
    )
    assert any("negative log-profile-likelihood ratio" in c for c in captions)


def test_keeps_covariance_and_likelihood_scan(results: dict[str, FilterResult]) -> None:
    text = _joined(results["2401.05299"])
    assert "covariance matrix of the unfolded cross sections" in text
    assert "Two-dimensional likelihood scan" in text


def test_drops_diagrams_and_detector_boilerplate(results: dict[str, FilterResult]) -> None:
    for result in results.values():
        text = _joined(result)
        assert "Feynman diagram" not in text
        assert "electromagnetic calorimetry is provided by" not in text


def test_section_names_are_hierarchical(results: dict[str, FilterResult]) -> None:
    names = {c.section_name for c in results["2112.11876"].candidates}
    assert "Results > Nonresonant search results" in names


# --- comments ---------------------------------------------------------------------------------


def test_strip_comments_respects_escaped_percent() -> None:
    assert strip_comments("50\\% of events % a comment") == "50\\% of events"


def test_strip_comments_treats_escaped_backslash_then_percent_as_comment() -> None:
    assert strip_comments("line break\\\\% comment") == "line break\\\\"


def test_comment_only_lines_do_not_split_paragraphs() -> None:
    assert strip_comments("first half\n% note\nsecond half") == "first half\nsecond half"


def test_comment_environment_removed() -> None:
    assert strip_comments("a\\begin{comment}secret\\end{comment}b") == "ab"


# --- structure --------------------------------------------------------------------------------


def _doc(body: str, preamble: str = "\\documentclass{article}\n") -> str:
    return f"{preamble}\\begin{{document}}\n{body}\n\\end{{document}}\n"


def test_inputs_expand_recursively_and_missing_are_recorded() -> None:
    files = {
        "main.tex": _doc("\\input{sections/results}\n\\input{nowhere}"),
        "sections/results.tex": "\\section{Results}\nTables are available on HEPData.\n",
    }
    result = filter_latex(files)
    assert result.missing_inputs == ["nowhere"]
    assert [c.section_name for c in result.candidates] == ["Results"]


def test_commented_out_input_is_ignored() -> None:
    files = {"main.tex": _doc("% \\input{gone}\nText.")}
    assert filter_latex(files).missing_inputs == []


def test_input_cycle_raises() -> None:
    files = {"main.tex": _doc("\\input{a}"), "a.tex": "\\input{b}", "b.tex": "\\input{a}"}
    with pytest.raises(FilterError, match="cycle"):
        filter_latex(files)


def test_main_file_prefers_documentclass() -> None:
    files = {
        "main.tex": _doc("\\section{Results}\nSee HEPData."),
        "standalone_fig.tex": "\\begin{document}x\\end{document}" + " " * 10_000,
    }
    assert filter_latex(files).main_file == "main.tex"


def test_no_document_raises() -> None:
    with pytest.raises(FilterError, match="begin\\{document\\}"):
        filter_latex({"defs.tex": "\\newcommand{\\x}{y}"})


def test_unbalanced_section_title_raises() -> None:
    with pytest.raises(FilterError, match="unbalanced"):
        filter_latex({"main.tex": _doc("\\section{Results\nno closing brace")})


def test_data_availability_section_kept_whole() -> None:
    body = "\\section{Data availability}\nThe data are stored somewhere.\n\nSecond paragraph.\n"
    result = filter_latex({"main.tex": _doc(body + "\\section{Other}\nUnrelated.")})
    [candidate] = result.candidates
    assert candidate.kind == "data_statement_section"
    assert "Second paragraph." in candidate.text


def test_appendix_headers_and_product_paragraphs() -> None:
    body = (
        "\\section{Method}\nThe upper limits are computed later.\n"
        "\\appendix\n\\section{Additional limits}\nUpper limits per channel are listed.\n"
    )
    result = filter_latex({"main.tex": _doc(body)})
    kinds = [(c.kind, c.section_name) for c in result.candidates]
    # "upper limits" outside a results-like section is not enough; inside the appendix it is.
    assert kinds == [
        ("appendix_header", "Appendix > Additional limits"),
        ("paragraph", "Appendix > Additional limits"),
    ]


def test_bibliography_urls_are_not_candidates() -> None:
    body = (
        "\\section{Introduction}\nPlain text.\n"
        "\\begin{thebibliography}{9}\n\\bibitem{a} See https://example.org/x\n"
        "\\end{thebibliography}"
    )
    result = filter_latex({"main.tex": _doc(body)})
    assert result.candidates == []


def test_caption_with_optional_argument_and_nested_braces() -> None:
    body = (
        "\\section{Results}\n\\begin{figure}\n"
        "\\caption[short]{Observed {\\em upper} limits on $\\sigma_{\\text{fid}}$.}\n"
        "\\end{figure}\n"
    )
    [candidate] = filter_latex({"main.tex": _doc(body)}).candidates
    assert candidate.kind == "caption"
    assert candidate.text.endswith("$\\sigma_{\\text{fid}}$.}")


def test_letter_without_sections_is_scanned_as_results() -> None:
    """Regression (arXiv:2004.03540): a letter whose only header is Acknowledgements."""
    body = (
        "Upper limits on the cross-section are set.\n\n"
        "\\section*{Acknowledgements}\nWe thank CERN.\n"
    )
    [candidate] = filter_latex({"main.tex": _doc(body)}).candidates
    assert candidate.section_name == "Body"
    assert candidate.text.startswith("Upper limits")


def test_diagram_word_does_not_drop_a_data_caption() -> None:
    """Regression (arXiv:2004.03540): 'The inset triangle illustrates...' on unfolded data."""
    body = (
        "\\section{Results}\n\\begin{figure}\n"
        "\\caption{Unfolded data compared with MC. The inset illustrates the slice.}\n"
        "\\end{figure}\n\\begin{figure}\n\\caption{Schematic of the detector layout.}\n"
        "\\end{figure}\n"
    )
    captions = [c.text for c in filter_latex({"main.tex": _doc(body)}).candidates]
    assert len(captions) == 1 and "Unfolded data" in captions[0]


def test_float_bodies_are_not_scanned_as_paragraphs() -> None:
    body = (
        "\\section{Results}\n\\begin{table}\n\\begin{tabular}{c}\n"
        "see https://example.org \\\\\n\\end{tabular}\n\\end{table}\n"
    )
    assert filter_latex({"main.tex": _doc(body)}).candidates == []


# --- stage runner -----------------------------------------------------------------------------


class StubSource:
    def __init__(self, sources: dict[str, PaperSource | Exception]) -> None:
        self._sources = sources
        self.calls: list[str] = []

    def get_source(self, arxiv_id: str) -> PaperSource:
        self.calls.append(arxiv_id)
        value = self._sources[arxiv_id]
        if isinstance(value, Exception):
            raise value
        return value


def test_filter_paper_statuses() -> None:
    stub = StubSource(
        {
            "1111.11111": PaperSource("1111.11111", "pdf_only", {}),
            "2222.22222": ArxivSourceError("no e-print"),
            "3333.33333": PaperSource("3333.33333", "latex", {"a.tex": "no document here"}),
            "2403.02793": PaperSource("2403.02793", "latex", _load("2403.02793")),
        }
    )
    assert filter_paper(1, None, stub).status == "no_arxiv_id"
    assert filter_paper(2, "1111.11111", stub).status == "no_latex"
    assert filter_paper(3, "2222.22222", stub).status == "source_error"
    assert filter_paper(4, "3333.33333", stub).status == "filter_error"
    ok = filter_paper(5, "2403.02793", stub)
    assert ok.status == "ok"
    assert ok.filter_version == FILTER_VERSION
    assert ok.reduction_ratio is not None and ok.reduction_ratio >= 5
    assert {"section_name", "kind", "text"} == set(ok.candidates[0])


def test_access_denied_stops_the_stage() -> None:
    stub = StubSource({"1111.11111": ArxivAccessDeniedError("403")})
    with pytest.raises(ArxivAccessDeniedError):
        filter_paper(1, "1111.11111", stub)


def test_run_filter_writes_records_and_summary(tmp_path: Path) -> None:
    corpus = CorpusConfig(name="c", query="q", start_year=2024, end_year=2024)
    harvest_file = tmp_path / "harvest" / "c" / "2024.jsonl"
    harvest_file.parent.mkdir(parents=True)
    rows = [
        {"inspire_id": 10, "arxiv_id": "2403.02793"},
        {"inspire_id": 11, "arxiv_id": None},
    ]
    harvest_file.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    stub = StubSource({"2403.02793": PaperSource("2403.02793", "latex", _load("2403.02793"))})

    summary = run_filter(stub, corpus, tmp_path)
    out = tmp_path / "filter" / "c"
    assert summary["statuses"] == {"ok": 1, "no_arxiv_id": 1}
    record = json.loads((out / "10.json").read_text(encoding="utf-8"))
    assert record["status"] == "ok" and record["candidates"]
    first = (out / "10.json").read_bytes()

    run_filter(stub, corpus, tmp_path)
    assert (out / "10.json").read_bytes() == first  # idempotent


def test_run_filter_requires_harvest_output(tmp_path: Path) -> None:
    corpus = CorpusConfig(name="c", query="q", start_year=2024, end_year=2024)
    with pytest.raises(FileNotFoundError, match="run harvest first"):
        run_filter(StubSource({}), corpus, tmp_path)
