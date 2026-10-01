"""Extract stage: ask an LLM which data products each paper declares, keep only verified claims.

Input: the filter stage's candidates per paper. Output: `<data_dir>/extract/<corpus>/<id>.json`.

Correctness gates (SPEC section 2, item 3, and Phase 3):
- The response must parse and validate against `ExtractionOutput`. On failure we retry exactly
  once, showing the model its invalid output and the validation error. A second failure marks
  the paper `schema_error`; no partial products are written for it.
- Every product's `evidence_span` must occur in the passage text. We accept an exact substring
  or, failing that, a match where only runs of whitespace differ (models routinely reflow line
  breaks); the span stored is always the slice copied from the source, never the model's text.
  Anything else is dropped and `evidence_span_verification_failures_total` incremented.
- Spans shorter than MIN_SPAN_CHARS non-space characters are dropped as non-identifying.

Large papers are split into several requests so each stays inside Groq's free-tier per-minute
token budget; a single candidate larger than the budget is sent alone (Gemini accepts it).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, Protocol

from opentelemetry import metrics, trace
from pydantic import ValidationError

from reuse_radar.config import Settings
from reuse_radar.llm.router import (
    AllProvidersExhaustedError,
    LLMRequest,
    LLMResult,
    Role,
    router_from_env,
)
from reuse_radar.llm.schemas import EXTRACTION_JSON_SCHEMA, ExtractedProduct, ExtractionOutput
from reuse_radar.log import configure_logging
from reuse_radar.pipeline.filter import FILTER_VERSION
from reuse_radar.pipeline.harvest import CorpusConfig

PROMPT_VERSION = "extract_v1"
# Changing the prompt, the filter or the schema must produce a new, diffable version string.
EXTRACTION_VERSION = f"{PROMPT_VERSION}+filter_{FILTER_VERSION}"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "llm" / "prompts" / f"{PROMPT_VERSION}.md"

# Sized for Groq's free tier (8K tokens/minute, budgeted at 7.6K): <=2K passage tokens (chars/4;
# ~2.3K by the router's chars/3.5 estimate) + ~1.6K of system prompt and schema + 3K output fits
# one minute's budget: the worst request over 31 real papers estimates at 7.3K. A candidate over
# the budget is split (seen live: one 8.3K request could not go to Groq, its fallback was
# overloaded, and the whole run stopped). Changing this measure moves chunk boundaries and so
# invalidates cached LLM answers: only change it together with the prompt version.
MAX_PASSAGE_TOKENS_PER_REQUEST = 2_000
CHARS_PER_TOKEN = 4.0
MAX_OUTPUT_TOKENS = 3_000
MIN_SPAN_CHARS = 20

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_schema_failures = _meter.create_counter(
    "extraction_schema_failures_total", description="LLM outputs failing schema validation"
)
_span_failures = _meter.create_counter(
    "evidence_span_verification_failures_total",
    description="Extracted products dropped because their evidence span failed verification",
)
_products_counter = _meter.create_counter(
    "extracted_products_total", description="Verified products, by type"
)


class Completer(Protocol):
    """Anything with the Router's `complete` method (lets tests inject a scripted fake)."""

    def complete(self, request: LLMRequest) -> LLMResult: ...


@dataclass(frozen=True)
class Passage:
    passage_id: int
    section_name: str
    kind: str
    text: str


@dataclass(frozen=True)
class DroppedProduct:
    """A model claim rejected by evidence verification, kept for auditing (never served)."""

    product_type: str
    description: str
    evidence_span: str  # as the model wrote it
    passage_id: int
    reason: Literal["too_short", "not_found"]


@dataclass(frozen=True)
class VerifiedProduct:
    product_type: str
    description: str
    evidence_span: str  # verbatim slice of the source passage
    evidence_section: str
    evidence_kind: str
    confidence: float
    match: Literal["exact", "whitespace"]
    provider: str
    model: str


ExtractStatus = Literal["ok", "schema_error", "skipped"]


@dataclass(frozen=True)
class PaperExtraction:
    inspire_id: int
    arxiv_id: str | None
    status: ExtractStatus
    extraction_version: str
    detail: str | None = None
    products: list[VerifiedProduct] = field(default_factory=list)
    dropped_spans: int = 0
    dropped: list[DroppedProduct] = field(default_factory=list)
    requests: int = 0
    cached_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class SchemaFailureError(RuntimeError):
    pass


# --- prompt construction ----------------------------------------------------------------------


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def chunk_passages(
    candidates: Sequence[dict[str, str]], budget: int = MAX_PASSAGE_TOKENS_PER_REQUEST
) -> list[list[Passage]]:
    """Group candidates into requests of at most `budget` passage tokens, preserving order.

    Passage ids restart at 1 in every chunk; they only need to be unique within a request.
    """
    chunks: list[list[Passage]] = []
    current: list[Passage] = []
    used = 0
    for candidate in candidates:
        for piece in split_text(candidate["text"], budget):
            tokens = passage_tokens(piece)
            if current and used + tokens > budget:
                chunks.append(current)
                current, used = [], 0
            current.append(
                Passage(
                    passage_id=len(current) + 1,
                    section_name=candidate["section_name"],
                    kind=candidate["kind"],
                    text=piece,
                )
            )
            used += tokens
    if current:
        chunks.append(current)
    return chunks


def passage_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# Preferred break points, coarsest first: paragraph, line, sentence end.
_BREAKS = (re.compile(r"\n[ \t]*\n"), re.compile(r"\n"), re.compile(r"(?<=[.;])\s+"))


def split_text(text: str, max_tokens: int, level: int = 0) -> list[str]:
    """Split `text` into pieces of at most `max_tokens`, each a verbatim (stripped) slice.

    Tries paragraph breaks, then line breaks, then sentence ends; only an unbroken run longer
    than the budget is cut at a fixed width. Pieces stay exact substrings of the source, so
    evidence spans can still be verified against them.
    """
    if passage_tokens(text) <= max_tokens:
        return [text]
    if level >= len(_BREAKS):
        width = int(max_tokens * CHARS_PER_TOKEN)
        return [p for i in range(0, len(text), width) if (p := text[i : i + width].strip())]
    cuts = [m.end() for m in _BREAKS[level].finditer(text)]
    bounds = [0, *cuts, len(text)]
    pieces: list[str] = []
    start = end = 0
    for a, b in pairwise(bounds):
        if end > start and passage_tokens(text[start:b]) > max_tokens:
            pieces.append(text[start:end])
            start = a
        end = b
    pieces.append(text[start:end])
    return [
        part
        for piece in pieces
        if piece.strip()
        for part in split_text(piece.strip(), max_tokens, level + 1)
    ]


def render_passages(passages: Sequence[Passage]) -> str:
    blocks = [
        f"### Passage {p.passage_id} ({p.kind}; section: {p.section_name})\n{p.text}"
        for p in passages
    ]
    return "Passages from one paper follow. List its reusable data products.\n\n" + "\n\n".join(
        blocks
    )


def build_request(
    system: str, passages: Sequence[Passage], retry: tuple[str, str] | None = None
) -> LLMRequest:
    messages: list[tuple[Role, str]] = [("user", render_passages(passages))]
    if retry is not None:
        bad_output, error = retry
        messages.append(("assistant", bad_output))
        messages.append(
            (
                "user",
                "That output did not validate against the required schema:\n"
                f"{error}\n\nReturn the complete answer again as JSON that matches the schema.",
            )
        )
    return LLMRequest(
        system=system,
        messages=tuple(messages),
        schema_name="declared_products",
        json_schema=EXTRACTION_JSON_SCHEMA,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )


# --- validation and verification --------------------------------------------------------------


def parse_output(result: LLMResult) -> ExtractionOutput:
    if result.finish_reason not in ("stop", ""):
        raise SchemaFailureError(f"output incomplete (finish_reason={result.finish_reason})")
    try:
        return ExtractionOutput.model_validate_json(result.text)
    except ValidationError as exc:
        raise SchemaFailureError(str(exc)[:1500]) from exc


def locate_span(span: str, text: str) -> tuple[str, Literal["exact", "whitespace"]] | None:
    """Find `span` in `text`. Returns the verbatim slice of `text` and how it matched, or None.

    Only whitespace may differ between the model's span and the source; every other character
    must match exactly.
    """
    stripped = span.strip()
    if not stripped:
        return None
    if stripped in text:
        return stripped, "exact"

    # Compare with all whitespace removed from both sides, then map back to the source. This
    # accepts whitespace inserted, deleted or changed anywhere (models write "space.}" where the
    # source has "space.\n}") and nothing else: every other character must match in order.
    needle = "".join(stripped.split())
    kept: list[int] = []  # kept[i] = index in `text` of the i-th non-whitespace character
    chars: list[str] = []
    for index, ch in enumerate(text):
        if not ch.isspace():
            kept.append(index)
            chars.append(ch)
    position = "".join(chars).find(needle)
    if position < 0:
        return None
    start, end = kept[position], kept[position + len(needle) - 1] + 1
    return text[start:end], "whitespace"


def verify(
    product: ExtractedProduct, passages: Sequence[Passage], result: LLMResult
) -> VerifiedProduct | DroppedProduct:
    span = product.evidence_span

    def dropped(reason: Literal["too_short", "not_found"]) -> DroppedProduct:
        _span_failures.add(1, {"reason": reason})
        return DroppedProduct(
            product.product_type, product.description, span, product.passage_id, reason
        )

    if len("".join(span.split())) < MIN_SPAN_CHARS:
        return dropped("too_short")
    by_id = {p.passage_id: p for p in passages}
    cited = by_id.get(product.passage_id)
    # The cited passage first; if the model mis-numbered it, any passage of this request.
    order = ([cited] if cited else []) + [p for p in passages if p is not cited]
    for passage in order:
        found = locate_span(span, passage.text)
        if found is not None:
            verbatim, how = found
            return VerifiedProduct(
                product_type=product.product_type,
                description=product.description.strip(),
                evidence_span=verbatim,
                evidence_section=passage.section_name,
                evidence_kind=passage.kind,
                confidence=product.confidence,
                match=how,
                provider=result.provider,
                model=result.model,
            )
    return dropped("not_found")


# --- per paper --------------------------------------------------------------------------------


@dataclass
class _Tally:
    requests: int = 0
    cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    dropped: list[DroppedProduct] = field(default_factory=list)

    def add(self, result: LLMResult) -> None:
        self.requests += 1
        self.cached += int(result.cached)
        if not result.cached:
            self.input_tokens += result.input_tokens
            self.output_tokens += result.output_tokens


def _extract_chunk(
    router: Completer, system: str, passages: Sequence[Passage], tally: _Tally
) -> list[VerifiedProduct]:
    request = build_request(system, passages)
    result = router.complete(request)
    tally.add(result)
    try:
        output = parse_output(result)
    except SchemaFailureError as first:
        _schema_failures.add(1, {"attempt": "first"})
        retry = router.complete(build_request(system, passages, (result.text, str(first))))
        tally.add(retry)
        try:
            output = parse_output(retry)
        except SchemaFailureError as second:
            _schema_failures.add(1, {"attempt": "retry"})
            raise SchemaFailureError(f"failed after retry: {second}") from second
        result = retry

    verified: list[VerifiedProduct] = []
    for product in output.products:
        checked = verify(product, passages, result)
        if isinstance(checked, DroppedProduct):
            tally.dropped.append(checked)
        else:
            verified.append(checked)
    return verified


def _dedupe(products: list[VerifiedProduct]) -> list[VerifiedProduct]:
    """Drop exact repeats. One caption can legitimately support two products (e.g. a cross-section
    and a cross-section ratio), so the description is part of the key."""
    best: dict[tuple[str, str, str], VerifiedProduct] = {}
    for p in products:
        key = (p.product_type, p.evidence_span, " ".join(p.description.lower().split()))
        if key not in best or p.confidence > best[key].confidence:
            best[key] = p
    return list(best.values())


def extract_paper(router: Completer, system: str, filter_record: dict[str, Any]) -> PaperExtraction:
    inspire_id = int(filter_record["inspire_id"])
    arxiv_id = filter_record.get("arxiv_id")
    if filter_record["status"] != "ok":
        return PaperExtraction(
            inspire_id,
            arxiv_id,
            "skipped",
            EXTRACTION_VERSION,
            detail=f"filter status {filter_record['status']}",
        )
    log_extra = {"inspire_id": inspire_id, "arxiv_id": arxiv_id}
    tally = _Tally()
    products: list[VerifiedProduct] = []
    try:
        for chunk in chunk_passages(filter_record["candidates"]):
            products.extend(_extract_chunk(router, system, chunk, tally))
    except SchemaFailureError as exc:
        logger.warning("extraction schema failure", extra={**log_extra, "error": str(exc)[:300]})
        return PaperExtraction(
            inspire_id,
            arxiv_id,
            "schema_error",
            EXTRACTION_VERSION,
            detail=str(exc)[:1000],
            requests=tally.requests,
            cached_requests=tally.cached,
            input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
        )

    products = _dedupe(products)
    for p in products:
        _products_counter.add(1, {"product_type": p.product_type})
    logger.info(
        "paper extracted",
        extra={
            **log_extra,
            "products": len(products),
            "dropped_spans": len(tally.dropped),
            "requests": tally.requests,
            "cached_requests": tally.cached,
        },
    )
    return PaperExtraction(
        inspire_id=inspire_id,
        arxiv_id=arxiv_id,
        status="ok",
        extraction_version=EXTRACTION_VERSION,
        products=products,
        dropped_spans=len(tally.dropped),
        dropped=tally.dropped,
        requests=tally.requests,
        cached_requests=tally.cached,
        input_tokens=tally.input_tokens,
        output_tokens=tally.output_tokens,
    )


# --- stage runner -----------------------------------------------------------------------------


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run_extract(
    router: Completer,
    corpus: CorpusConfig,
    data_dir: Path,
    *,
    limit: int | None = None,
    only: Sequence[int] | None = None,
) -> dict[str, object]:
    """Extract every filtered paper of the corpus. Entry point for Airflow and GitHub Actions.

    Stops with AllProvidersExhaustedError when every provider's quota is spent; papers finished
    so far are already written, and the next run replays them from the cache at no cost.
    """
    with tracer.start_as_current_span("extract") as span:
        span.set_attribute("corpus", corpus.name)
        filter_dir = data_dir / "filter" / corpus.name
        out_dir = data_dir / "extract" / corpus.name
        if not filter_dir.is_dir():
            raise FileNotFoundError(f"filter output missing: {filter_dir}; run filter first")
        paths = sorted(p for p in filter_dir.glob("*.json") if not p.name.startswith("_"))
        if only is not None:
            wanted = {str(i) for i in only}
            paths = [p for p in paths if p.stem in wanted]
        if limit is not None:
            paths = paths[:limit]

        system = load_system_prompt()
        statuses: dict[str, int] = {}
        totals = {"products": 0, "dropped_spans": 0, "input_tokens": 0, "output_tokens": 0}
        stopped: str | None = None
        for path in paths:
            record = json.loads(path.read_text(encoding="utf-8"))
            with tracer.start_as_current_span("extract.paper") as paper_span:
                paper_span.set_attribute("inspire_id", int(record["inspire_id"]))
                try:
                    result = extract_paper(router, system, record)
                except AllProvidersExhaustedError as exc:
                    stopped = str(exc)
                    logger.error(
                        "llm quota exhausted; stopping extract stage",
                        extra={"inspire_id": record["inspire_id"]},
                    )
                    break
            statuses[result.status] = statuses.get(result.status, 0) + 1
            totals["products"] += len(result.products)
            totals["dropped_spans"] += result.dropped_spans
            totals["input_tokens"] += result.input_tokens
            totals["output_tokens"] += result.output_tokens
            _write_json_atomic(out_dir / f"{result.inspire_id}.json", asdict(result))

        summary: dict[str, object] = {
            "corpus": corpus.name,
            "extraction_version": EXTRACTION_VERSION,
            "papers_attempted": len(paths),
            "statuses": statuses,
            **totals,
            "stopped_early": stopped,
        }
        _write_json_atomic(out_dir / "_summary.json", summary)
        logger.info("extract stage finished", extra=summary)
        if stopped is not None:
            raise AllProvidersExhaustedError(
                f"{stopped}; {sum(statuses.values())} of {len(paths)} papers done, rerun later"
            )
        return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Extract declared data products with an LLM.")
    parser.add_argument("--corpus", type=Path, default=Path("config/corpus.toml"))
    parser.add_argument("--limit", type=int, default=None, help="only the first N papers")
    parser.add_argument("--only", type=int, nargs="*", help="only these INSPIRE ids")
    args = parser.parse_args(argv)

    configure_logging()
    settings = Settings.from_env()
    router = router_from_env(settings.cache_dir)
    run_extract(
        router,
        CorpusConfig.from_toml(args.corpus),
        settings.data_dir,
        limit=args.limit,
        only=args.only,
    )


if __name__ == "__main__":
    main()
