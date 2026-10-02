"""Evaluate extraction against eval/gold_set.jsonl and gate on the result (SPEC Phase 6).

Modes:
  --source replay (default; what CI runs)
      Re-runs the *current* extraction code on the gold papers, answering every LLM request
      from the committed fixture cache (eval/fixtures/llm). No network, no API keys, zero cost,
      deterministic. If the prompt, schema, chunking or model changed, requests miss the cache
      and the run fails with instructions to re-record: a prompt change must come with fresh,
      reviewed eval numbers.
  --source stored
      Scores whatever the pipeline last wrote to data/extract (and data/reconcile, which adds
      gap-detection accuracy).
  --record
      Runs extraction for the gold papers with the real router (cache first, live calls only on
      a miss), then copies the filter outputs and every LLM cache entry used into the fixtures.

Exit status 1 when a metric falls below eval/thresholds.json (unless --no-gate).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from reuse_radar.eval.report import render_markdown
from reuse_radar.eval.scoring import (
    GoldPaper,
    PaperScore,
    aggregate,
    dump,
    evidence_in_document,
    load_gold,
    score_paper,
)
from reuse_radar.llm.cache import CachedResponse, LLMCache
from reuse_radar.llm.router import AllProvidersExhaustedError, LLMRequest, LLMResult, Router

EVAL_DIR = Path(__file__).resolve().parent
GOLD_PATH = EVAL_DIR / "gold_set.jsonl"
THRESHOLDS_PATH = EVAL_DIR / "thresholds.json"
FIXTURES = EVAL_DIR / "fixtures"
CORPUS = "atlas-published-2020-2025"


class ReplayMiss(RuntimeError):
    pass


class ReplayRouter:
    """Answers only from a fixed cache; any miss is an error (never a network call)."""

    def __init__(self, cache_dir: Path, template: Router) -> None:
        self._cache = LLMCache(cache_dir)
        self._providers = template.providers

    def complete(self, request: LLMRequest) -> LLMResult:
        from reuse_radar.llm.cache import canonical_prompt

        for provider in self._providers:
            hit = self._cache.get(
                provider.name, provider.model, canonical_prompt(provider.payload(request))
            )
            if hit is not None:
                return LLMResult(
                    hit.provider,
                    hit.model,
                    hit.text,
                    hit.input_tokens,
                    hit.output_tokens,
                    True,
                    latency_s=hit.latency_s,
                )
        raise ReplayMiss(
            "eval fixture cache has no answer for this request: the prompt, schema, chunking or "
            "model changed. Re-record with `python -m reuse_radar.eval.run_eval --record` "
            "(needs API keys), review the new numbers, and commit eval/fixtures."
        )


class TeeCache(LLMCache):
    """Normal cache that also copies every entry it serves or stores into a second directory."""

    def __init__(self, root: Path, copy_to: Path) -> None:
        super().__init__(root)
        self._copy = LLMCache(copy_to)

    def get(self, provider: str, model: str, prompt: str) -> CachedResponse | None:
        hit = super().get(provider, model, prompt)
        if hit is not None:
            self._copy.set(prompt, hit)
        return hit

    def set(self, prompt: str, response: CachedResponse) -> None:
        super().set(prompt, response)
        self._copy.set(prompt, response)


def _template_router() -> Router:
    """The production provider chain, used only for its payload shapes (no keys needed)."""
    from reuse_radar.llm.router import DEFAULT_GEMINI_MODELS, GeminiProvider, GroqProvider

    providers: list[Any] = [GroqProvider("replay-only")]
    providers += [GeminiProvider("replay-only", m) for m in DEFAULT_GEMINI_MODELS]
    return Router(providers=providers, cache=LLMCache(FIXTURES / "unused"))


def _filter_record(inspire_id: int, data_dir: Path | None) -> dict[str, Any]:
    path = (
        FIXTURES / "filter" / f"{inspire_id}.json"
        if data_dir is None
        else data_dir / "filter" / CORPUS / f"{inspire_id}.json"
    )
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return record


def _check_gold(gold: list[GoldPaper], data_dir: Path | None) -> None:
    problems = {}
    for paper in gold:
        missing = evidence_in_document(
            paper, _filter_record(paper.inspire_id, data_dir)["candidates"]
        )
        if missing:
            problems[paper.inspire_id] = missing
    if problems:
        raise SystemExit(f"gold evidence not found in the filtered text: {problems}")


def evaluate_replay(gold: list[GoldPaper]) -> tuple[list[PaperScore], list[dict[str, Any]]]:
    from reuse_radar.pipeline.extract import extract_paper, load_system_prompt

    _check_gold(gold, None)
    router = ReplayRouter(FIXTURES / "llm", _template_router())
    system = load_system_prompt()
    scores, stats = [], []
    for paper in gold:
        extraction = extract_paper(router, system, _filter_record(paper.inspire_id, None))
        products = [p.__dict__ for p in extraction.products]
        scores.append(score_paper(paper, products))
        stats.extend(extraction.request_stats)
    return scores, stats


def evaluate_stored(
    gold: list[GoldPaper], data_dir: Path
) -> tuple[list[PaperScore], list[dict[str, Any]]]:
    _check_gold(gold, data_dir)
    scores: list[PaperScore] = []
    stats: list[dict[str, Any]] = []
    for paper in gold:
        extract_path = data_dir / "extract" / CORPUS / f"{paper.inspire_id}.json"
        if not extract_path.exists():
            raise SystemExit(f"no extraction output for gold paper {paper.inspire_id}")
        extraction = json.loads(extract_path.read_text(encoding="utf-8"))
        statuses = None
        rec_path = data_dir / "reconcile" / CORPUS / f"{paper.inspire_id}.json"
        if rec_path.exists():
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            statuses = {
                (p["evidence_span"], p["description"]): p["status"] for p in rec["products"]
            }
        scores.append(score_paper(paper, extraction.get("products") or [], statuses))
        stats.extend(extraction.get("request_stats") or [])
    return scores, stats


def record(gold: list[GoldPaper], data_dir: Path, cache_dir: Path) -> None:
    from reuse_radar.config import Settings
    from reuse_radar.llm.router import router_from_env
    from reuse_radar.pipeline.extract import extract_paper, load_system_prompt

    Settings.from_env()  # loads .env (API keys) like the pipeline entry points do
    llm_fixtures = FIXTURES / "llm"
    if llm_fixtures.exists():
        shutil.rmtree(llm_fixtures)  # only entries the current code actually uses
    (FIXTURES / "filter").mkdir(parents=True, exist_ok=True)
    router = router_from_env(cache_dir)
    router.cache = TeeCache(cache_dir / "llm", llm_fixtures)
    system = load_system_prompt()
    for paper in gold:
        src = data_dir / "filter" / CORPUS / f"{paper.inspire_id}.json"
        shutil.copyfile(src, FIXTURES / "filter" / f"{paper.inspire_id}.json")
        result = extract_paper(router, system, _filter_record(paper.inspire_id, data_dir))
        print(f"recorded {paper.inspire_id}: {result.status}, {len(result.products)} products")


def gate(metrics: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    overall = metrics["overall"]
    failures = []
    for name, minimum in thresholds.items():
        if not name.startswith("min_"):
            continue
        key = name.removeprefix("min_")
        value = overall.get(key) if key in overall else metrics.get(key)
        if value is None or value < minimum:
            failures.append(f"{key} = {value} is below the gate {minimum}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--source", choices=["replay", "stored"], default="replay")
    parser.add_argument("--record", action="store_true", help="refresh eval/fixtures (live calls)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--out", type=Path, default=Path("eval-report"))
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args(argv)

    gold = load_gold(GOLD_PATH)
    thresholds = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    if args.record:
        record(gold, args.data_dir, args.cache_dir)
        return 0
    try:
        scores, stats = (
            evaluate_replay(gold)
            if args.source == "replay"
            else evaluate_stored(gold, args.data_dir)
        )
    except (ReplayMiss, AllProvidersExhaustedError) as exc:
        print(f"EVAL FAILED: {exc}", file=sys.stderr)
        return 1
    metrics = aggregate(scores, stats, thresholds.get("price_per_million", {}))
    metrics["source"] = args.source
    metrics["gold_reviewed_by_human"] = all(p.reviewed_by_human for p in gold)
    dump(metrics, args.out / "metrics.json")
    report = render_markdown(metrics, thresholds)
    (args.out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    failures = [] if args.no_gate else gate(metrics, thresholds)
    for failure in failures:
        print(f"GATE FAILED: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
