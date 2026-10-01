"""Filter stage: cut a paper's LaTeX down to the passages likely to describe reusable data products.

Pure Python, no model calls. Given a paper's .tex files, `filter_latex` returns candidates
(section_name, text), each an exact slice of the cleaned document (inputs expanded, comments
removed). Phase 3 verifies evidence spans against these same strings, so candidates are never
paraphrased or reformatted.

What is kept (see docs/adr/0002 for the reasoning and rejected alternatives):

1. every figure and table caption: captions are where limits, cross-sections and efficiency
   maps are named;
2. whole sections whose title marks them as data statements (data availability, auxiliary or
   supplementary material, HEPData);
3. any paragraph, anywhere, with availability language (HEPData, Rivet, pyhf, "available at",
   "provided in", URLs, auxiliary *material*, cut-flows, covariance or correlation matrices);
4. paragraphs in results-like sections (and appendices) that name a product (upper limits,
   cross-sections, efficiencies, likelihood scans...);
5. appendix section headers, so the extractor knows which supplementary tables exist.

The reduction ratio is measured against the document body with the bibliography and author list
removed, so boilerplate cannot inflate it. Tokens are approximated as ceil(chars / 4).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from opentelemetry import metrics, trace

from reuse_radar.clients.arxiv import ArxivClient, ArxivSourceError, PaperSource
from reuse_radar.config import Settings
from reuse_radar.log import configure_logging
from reuse_radar.pipeline.harvest import CorpusConfig

# Bump whenever the selection rules change, so outputs can be re-run and diffed.
FILTER_VERSION = "1"

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_ratio_histogram = _meter.create_histogram(
    "filter_token_reduction_ratio", description="Body tokens / candidate tokens, per paper"
)
_tokens_counter = _meter.create_counter(
    "filter_tokens_total", description="Approximate tokens before and after filtering"
)
_status_counter = _meter.create_counter(
    "filter_papers_total", description="Papers processed by the filter stage, by status"
)


class FilterError(ValueError):
    """The LaTeX cannot be filtered (no main document found, unbalanced structure)."""


CandidateKind = Literal["caption", "data_statement_section", "paragraph", "appendix_header"]


@dataclass(frozen=True)
class Candidate:
    section_name: str
    text: str
    kind: CandidateKind

    def as_pair(self) -> tuple[str, str]:
        return (self.section_name, self.text)


@dataclass(frozen=True)
class FilterResult:
    main_file: str
    document: str  # cleaned, expanded source; every candidate.text is a substring of it
    candidates: list[Candidate]
    body_tokens: int
    candidate_tokens: int
    missing_inputs: list[str] = field(default_factory=list)
    skipped_inputs: list[str] = field(default_factory=list)

    @property
    def reduction_ratio(self) -> float:
        if self.candidate_tokens == 0:
            return math.inf
        return self.body_tokens / self.candidate_tokens


# --- selection vocabulary -------------------------------------------------------------------

_DATA_STATEMENT_TITLE = re.compile(
    r"data\s+availability|auxiliary\s+material|supplementa|hepdata|open\s+data|"
    r"reinterpretation\s+material|additional\s+material",
    re.IGNORECASE,
)
_RESULTS_TITLE = re.compile(
    r"result|limit|interpretation|implication|conclusion|summary|combination|"
    r"cross[\s-]*section|discussion|outlook|statistical",
    re.IGNORECASE,
)
# Sections describing the apparatus and setup rather than results.
_SETUP_TITLE = re.compile(
    r"introduction|detector|simulat|samples|monte\s+carlo|object|reconstruction|"
    r"identification|acknowledg",
    re.IGNORECASE,
)
# Availability language: evidence that a product is (or should be) published. Deliberately
# narrow: bare "is provided"/"are available" mostly describes detectors and MC samples.
_AVAILABILITY = re.compile(
    r"hepdata|rivet|pyhf|simpleanalysis|"
    r"auxiliary\s+(material|figure|table)s?|supplementa|additional\s+(material|figure|table)s?|"
    r"\b(publicly|electronically|online)\s+available\b|"
    r"\bavailable\s+(at|from|on|via|as)\s|\bprovided\s+(on|as|at)\s|"
    r"\bcan\s+be\s+found\s+(at|on|in\s+the\s+(auxiliary|supplementa|appendix))|"
    r"https?://|\\url\b|\\href\b|"
    r"cut[\s-]*flows?|efficiency\s+maps?|covariance\s+matri|correlation\s+matri|"
    r"(full|public|statistical)\s+likelihoods?|likelihoods?\s+(is|are)\s+(published|released)",
    re.IGNORECASE,
)
# Product vocabulary, applied only inside results-like sections and appendices.
_PRODUCT = re.compile(
    r"upper\s+limits?|exclusion\s+(limit|contour|region)s?|\b(are|is)\s+excluded\b|"
    r"(fiducial|differential|inclusive|total|production)\s+cross[\s-]*sections?|"
    r"cross[\s-]*sections?\s+(is|are|was|were)\s+measured|"
    r"unfolded\s+(data|distributions?|cross[\s-]*sections?|results?)|likelihood\s+scan|"
    # Bare "signal/trigger efficiency" is excluded: it mostly appears in setup prose ("MC is used
    # to estimate the signal efficiencies"). Efficiency results are caught by their captions.
    r"acceptance\s*(times|\\times|\N{MULTIPLICATION SIGN})?\s*efficienc",
    re.IGNORECASE,
)
# Captions are kept unless they sit in a setup section *and* carry no data vocabulary, or
# describe a diagram. Results-figure captions almost always correspond to HEPData tables.
_CAPTION_DATA = re.compile(
    r"distribution|\bdata\b|observed|measured|yields?\b|limits?\b|cross[\s-]*sections?|"
    r"efficienc|acceptance|likelihood|covariance|correlation|cut[\s-]*flow|exclu|unfold|"
    r"fiducial|best[\s-]*fit|uncertaint",
    re.IGNORECASE,
)
_CAPTION_FEYNMAN = re.compile(r"feynman", re.IGNORECASE)
_CAPTION_DIAGRAM = re.compile(r"diagram|schematic|illustrat|sketch", re.IGNORECASE)

_FLOAT_ENVS = r"figure\*?|table\*?|sidewaysfigure|sidewaystable|wrapfigure|wraptable"
_FLOAT = re.compile(r"\\begin\{(" + _FLOAT_ENVS + r")\}.*?\\end\{\1\}", re.DOTALL)
_SECTION_CMD = re.compile(r"\\(part|section|subsection|subsubsection)\*?\s*(?=[\[{])")
_SECTION_LEVEL = {"part": 0, "section": 1, "subsection": 2, "subsubsection": 3}
_APPENDIX = re.compile(r"\\appendix\b")
_CAPTION_CMD = re.compile(r"\\caption(?:of\{[^}]*\})?\s*(?=[\[{])")
_INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
_AUTHOR_LIST = re.compile(r"auth(or)?s?_?list", re.IGNORECASE)
_COMMENT_ENV = re.compile(r"\\begin\{comment\}.*?\\end\{comment\}", re.DOTALL)
_UNESCAPED_PERCENT = re.compile(r"(?<!\\)((?:\\\\)*)%.*")
_BEGIN_DOCUMENT = re.compile(r"\\begin\{document\}")
_END_DOCUMENT = re.compile(r"\\end\{document\}")
_BIBLIOGRAPHY = re.compile(
    r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}|"
    r"\\printbibliography(\[[^\]]*\])?|\\bibliography\{[^}]*\}",
    re.DOTALL,
)
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_MAX_INPUT_DEPTH = 20


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


# --- LaTeX helpers ----------------------------------------------------------------------------


def strip_comments(text: str) -> str:
    """Remove % comments (respecting \\%) and comment environments.

    Lines that held only a comment are dropped entirely: in LaTeX they do not end a paragraph,
    so keeping them as blank lines would split paragraphs that the author wrote as one.
    """
    text = _COMMENT_ENV.sub("", text)
    out: list[str] = []
    for line in text.split("\n"):
        stripped = _UNESCAPED_PERCENT.sub(r"\1", line)
        if stripped.strip() == "" and line.strip() != "":
            continue  # comment-only line
        out.append(stripped.rstrip())
    return "\n".join(out)


def _group_end(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """`text[start]` is `open_ch`; return the index just past its matching `close_ch`."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2  # skip escaped character, e.g. \{ or \}
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise FilterError(f"unbalanced {open_ch}{close_ch} starting at offset {start}")


def _command_argument(text: str, pos: int) -> tuple[str, int]:
    """Parse `[optional]{required}` at `pos`; return (required content, end index)."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos < len(text) and text[pos] == "[":
        pos = _group_end(text, pos, "[", "]")
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if pos >= len(text) or text[pos] != "{":
        raise FilterError(f"expected '{{' at offset {pos}")
    end = _group_end(text, pos, "{", "}")
    return text[pos + 1 : end - 1], end


def _find_main_file(tex_files: Mapping[str, str]) -> str:
    with_document = [n for n, t in tex_files.items() if _BEGIN_DOCUMENT.search(strip_comments(t))]
    if not with_document:
        raise FilterError("no .tex file contains \\begin{document}")
    with_class = [n for n in with_document if "\\documentclass" in tex_files[n]]
    pool = with_class or with_document
    return max(pool, key=lambda n: len(tex_files[n]))


def _expand_inputs(
    name: str,
    tex_files: Mapping[str, str],
    missing: list[str],
    skipped: list[str],
    stack: tuple[str, ...] = (),
) -> str:
    if len(stack) > _MAX_INPUT_DEPTH:
        raise FilterError(f"\\input nesting deeper than {_MAX_INPUT_DEPTH}: {' -> '.join(stack)}")
    text = strip_comments(tex_files[name])
    base = PurePosixPath(name).parent

    def resolve(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        if _AUTHOR_LIST.search(target):
            skipped.append(target)
            return ""
        for cand in (target, f"{target}.tex"):
            # LaTeX resolves inputs against the working directory (the main file's), so try
            # relative to the including file first, then relative to the archive root.
            for path in (base / cand, PurePosixPath(cand)):
                key = str(path)  # PurePosixPath normalises "./a/b.tex" to "a/b.tex"
                if key in tex_files:
                    if key in stack or key == name:
                        raise FilterError(f"\\input cycle: {' -> '.join((*stack, name, key))}")
                    return _expand_inputs(key, tex_files, missing, skipped, (*stack, name))
        missing.append(target)
        return ""

    return _INPUT.sub(resolve, text)


# --- section model ----------------------------------------------------------------------------


@dataclass
class _Section:
    level: int
    title: str
    path: str
    start: int  # offset of the section's content (after the header)
    end: int
    header_start: int
    in_appendix: bool
    results_like: bool
    data_statement: bool
    setup_like: bool = False


def _sections(document: str, body_start: int, body_end: int) -> list[_Section]:
    appendix_at = None
    if m := _APPENDIX.search(document, body_start, body_end):
        appendix_at = m.start()

    headers: list[tuple[int, int, int, str]] = []  # (header_start, content_start, level, title)
    for m in _SECTION_CMD.finditer(document, body_start, body_end):
        title, end = _command_argument(document, m.end())
        headers.append((m.start(), end, _SECTION_LEVEL[m.group(1)], " ".join(title.split())))

    sections: list[_Section] = []
    # Content before the first header (front matter of the body) is its own pseudo-section.
    first = headers[0][0] if headers else body_end
    # Letters often have no sections (only "Acknowledgements"); then this pseudo-section is the
    # whole paper, results included, and must be scanned like a results section.
    is_letter = not any(not re.search("acknowledg", h[3], re.IGNORECASE) for h in headers)
    name = "Body" if is_letter else "Front matter"
    sections.append(_Section(0, name, name, body_start, first, body_start, False, is_letter, False))
    ancestors: list[_Section] = []
    for i, (h_start, c_start, level, title) in enumerate(headers):
        end = headers[i + 1][0] if i + 1 < len(headers) else body_end
        ancestors = [a for a in ancestors if a.level < level]
        in_appendix = appendix_at is not None and h_start > appendix_at
        prefix = "Appendix > " if in_appendix and not ancestors else ""
        path = prefix + " > ".join([*(a.title for a in ancestors), title])
        section = _Section(
            level=level,
            title=title,
            path=path,
            start=c_start,
            end=end,
            header_start=h_start,
            in_appendix=in_appendix,
            results_like=bool(_RESULTS_TITLE.search(title))
            or any(a.results_like for a in ancestors),
            data_statement=bool(_DATA_STATEMENT_TITLE.search(title))
            or any(a.data_statement for a in ancestors),
            setup_like=bool(_SETUP_TITLE.search(title)) or any(a.setup_like for a in ancestors),
        )
        sections.append(section)
        ancestors.append(section)
    return sections


def _trimmed(document: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    while start < end and document[start].isspace():
        start += 1
    while end > start and document[end - 1].isspace():
        end -= 1
    if end > start:
        yield (start, end)


def _paragraph_spans(
    document: str, start: int, end: int, excluded: list[tuple[int, int]]
) -> Iterable[tuple[int, int]]:
    """Yield (start, end) of paragraphs in [start, end), skipping `excluded` spans (sorted)."""
    pieces: list[tuple[int, int]] = []
    cursor = start
    for x_start, x_end in excluded:
        if x_end <= start or x_start >= end:
            continue
        if x_start > cursor:
            pieces.append((cursor, x_start))
        cursor = max(cursor, x_end)
    if cursor < end:
        pieces.append((cursor, end))

    for p_start, p_end in pieces:
        pos = p_start
        for brk in _PARAGRAPH_BREAK.finditer(document, p_start, p_end):
            yield from _trimmed(document, pos, brk.start())
            pos = brk.end()
        yield from _trimmed(document, pos, p_end)


def _captions(document: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    for m in _CAPTION_CMD.finditer(document, start, end):
        _, arg_end = _command_argument(document, m.end())
        yield (m.start(), arg_end)


# --- public API -------------------------------------------------------------------------------


def filter_latex(tex_files: Mapping[str, str]) -> FilterResult:
    """Select candidate passages from a paper's LaTeX. Pure: no I/O, no network, no models."""
    if not tex_files:
        raise FilterError("no .tex files")
    main = _find_main_file(tex_files)
    missing: list[str] = []
    skipped: list[str] = []
    document = _expand_inputs(main, tex_files, missing, skipped)

    begin = _BEGIN_DOCUMENT.search(document)
    if begin is None:  # pragma: no cover - _find_main_file guarantees it
        raise FilterError("no \\begin{document}")
    end_match = _END_DOCUMENT.search(document, begin.end())
    body_start, body_end = begin.end(), end_match.start() if end_match else len(document)

    bibliography = [
        (m.start(), m.end()) for m in _BIBLIOGRAPHY.finditer(document, body_start, body_end)
    ]
    body_chars = (body_end - body_start) - sum(e - s for s, e in bibliography)
    floats = [(m.start(), m.end()) for m in _FLOAT.finditer(document, body_start, body_end)]
    # Paragraph scanning skips floats (their captions are taken separately) and the
    # bibliography (full of URLs and DOIs that would match availability language).
    excluded = sorted(floats + bibliography)

    spans: dict[tuple[int, int], tuple[str, CandidateKind]] = {}

    def add(span: tuple[int, int], section: str, kind: CandidateKind) -> None:
        spans.setdefault(span, (section, kind))

    for section in _sections(document, body_start, body_end):
        if section.data_statement:
            add((section.start, section.end), section.path, "data_statement_section")
            continue
        if section.in_appendix:
            add((section.header_start, section.start), section.path, "appendix_header")
        for span in _captions(document, section.start, section.end):
            caption = document[span[0] : span[1]]
            if _CAPTION_FEYNMAN.search(caption):
                continue  # Feynman diagrams are never data products
            weak = section.setup_like or _CAPTION_DIAGRAM.search(caption)
            if weak and not _CAPTION_DATA.search(caption):
                continue
            add(span, section.path, "caption")
        for p_start, p_end in _paragraph_spans(document, section.start, section.end, excluded):
            text = document[p_start:p_end]
            if _AVAILABILITY.search(text) or (
                (section.results_like or section.in_appendix) and _PRODUCT.search(text)
            ):
                add((p_start, p_end), section.path, "paragraph")

    candidates = [
        Candidate(section, document[start:end], kind)
        for (start, end), (section, kind) in sorted(spans.items())
    ]
    return FilterResult(
        main_file=main,
        document=document,
        candidates=candidates,
        body_tokens=math.ceil(body_chars / 4),
        candidate_tokens=sum(approx_tokens(c.text) for c in candidates),
        missing_inputs=missing,
        skipped_inputs=skipped,
    )


# --- stage runner -----------------------------------------------------------------------------


class SourceProvider(Protocol):
    def get_source(self, arxiv_id: str) -> PaperSource: ...


FilterStatus = Literal["ok", "no_arxiv_id", "no_latex", "source_error", "filter_error"]


@dataclass(frozen=True)
class PaperFilterRecord:
    inspire_id: int
    arxiv_id: str | None
    status: FilterStatus
    filter_version: str
    detail: str | None = None
    main_file: str | None = None
    body_tokens: int | None = None
    candidate_tokens: int | None = None
    reduction_ratio: float | None = None
    missing_inputs: list[str] = field(default_factory=list)
    candidates: list[dict[str, str]] = field(default_factory=list)


def filter_paper(
    inspire_id: int, arxiv_id: str | None, client: SourceProvider
) -> PaperFilterRecord:
    """Fetch and filter one paper. Per-paper source problems become a status, never a crash;
    access-denied and other fatal client errors propagate and stop the stage."""
    log_extra = {"inspire_id": inspire_id, "arxiv_id": arxiv_id}
    if arxiv_id is None:
        return PaperFilterRecord(inspire_id, None, "no_arxiv_id", FILTER_VERSION)
    try:
        source = client.get_source(arxiv_id)
    except ArxivSourceError as exc:
        logger.warning("arxiv source unusable", extra={**log_extra, "error": str(exc)})
        return PaperFilterRecord(inspire_id, arxiv_id, "source_error", FILTER_VERSION, str(exc))
    if source.kind == "pdf_only":
        return PaperFilterRecord(inspire_id, arxiv_id, "no_latex", FILTER_VERSION)
    try:
        result = filter_latex(source.tex_files)
    except FilterError as exc:
        logger.warning("latex could not be filtered", extra={**log_extra, "error": str(exc)})
        return PaperFilterRecord(inspire_id, arxiv_id, "filter_error", FILTER_VERSION, str(exc))

    _ratio_histogram.record(result.reduction_ratio)
    _tokens_counter.add(result.body_tokens, {"stage": "before"})
    _tokens_counter.add(result.candidate_tokens, {"stage": "after"})
    if result.missing_inputs:
        logger.warning(
            "latex inputs missing from source",
            extra={**log_extra, "missing": result.missing_inputs},
        )
    logger.info(
        "paper filtered",
        extra={
            **log_extra,
            "candidates": len(result.candidates),
            "reduction_ratio": round(result.reduction_ratio, 2),
        },
    )
    return PaperFilterRecord(
        inspire_id=inspire_id,
        arxiv_id=arxiv_id,
        status="ok",
        filter_version=FILTER_VERSION,
        main_file=result.main_file,
        body_tokens=result.body_tokens,
        candidate_tokens=result.candidate_tokens,
        reduction_ratio=round(result.reduction_ratio, 3),
        missing_inputs=result.missing_inputs,
        candidates=[
            {"section_name": c.section_name, "kind": c.kind, "text": c.text}
            for c in result.candidates
        ],
    )


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run_filter(
    client: SourceProvider, corpus: CorpusConfig, data_dir: Path, *, limit: int | None = None
) -> dict[str, object]:
    """Filter every harvested paper of the corpus. Entry point for Airflow and GitHub Actions.

    Writes `<data_dir>/filter/<corpus>/<inspire_id>.json` per paper and `_summary.json`.
    """
    with tracer.start_as_current_span("filter") as span:
        span.set_attribute("corpus", corpus.name)
        harvest_dir = data_dir / "harvest" / corpus.name
        out_dir = data_dir / "filter" / corpus.name
        papers: list[dict[str, object]] = []
        for year in corpus.years:
            path = harvest_dir / f"{year}.jsonl"
            if not path.exists():
                raise FileNotFoundError(f"harvest output missing: {path}; run harvest first")
            papers.extend(
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            )
        if limit is not None:
            papers = papers[:limit]

        statuses: dict[str, int] = {}
        ratios: list[float] = []
        for paper in papers:
            inspire_id = paper["inspire_id"]
            arxiv_id = paper["arxiv_id"]
            assert isinstance(inspire_id, int)
            assert arxiv_id is None or isinstance(arxiv_id, str)
            with tracer.start_as_current_span("filter.paper") as paper_span:
                paper_span.set_attribute("inspire_id", inspire_id)
                record = filter_paper(inspire_id, arxiv_id, client)
            _status_counter.add(1, {"status": record.status})
            statuses[record.status] = statuses.get(record.status, 0) + 1
            if record.reduction_ratio is not None:
                ratios.append(record.reduction_ratio)
            _write_json_atomic(out_dir / f"{inspire_id}.json", asdict(record))

        summary: dict[str, object] = {
            "corpus": corpus.name,
            "filter_version": FILTER_VERSION,
            "papers": len(papers),
            "statuses": statuses,
            "reduction_ratio_median": round(statistics.median(ratios), 2) if ratios else None,
            "reduction_ratio_min": round(min(ratios), 2) if ratios else None,
        }
        _write_json_atomic(out_dir / "_summary.json", summary)
        logger.info("filter stage finished", extra=summary)
        return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Filter harvested papers' LaTeX.")
    parser.add_argument("--corpus", type=Path, default=Path("config/corpus.toml"))
    parser.add_argument("--limit", type=int, default=None, help="only the first N papers")
    args = parser.parse_args(argv)

    configure_logging()
    settings = Settings.from_env()
    client = ArxivClient(contact_email=settings.inspire_contact_email, cache_dir=settings.cache_dir)
    run_filter(client, CorpusConfig.from_toml(args.corpus), settings.data_dir, limit=args.limit)


if __name__ == "__main__":
    main()
