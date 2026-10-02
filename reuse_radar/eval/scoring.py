"""Scoring extraction (and, when available, gap status) against the gold set.

Matching is by evidence location, not wording: an extracted product matches a gold item when
their evidence overlaps in the paper (one contains the other after removing whitespace, or they
share a run of at least MIN_OVERLAP characters). Descriptions are free text and would make the
match depend on phrasing; the evidence span is verbatim, so location is objective.

Each gold item can be matched once. Further extracted products pointing at an already-matched
item are reported as duplicates (counted correct for precision, since they are real products,
but listed so the duplicate rate is visible).
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reuse_radar.llm.schemas import ProductType

MIN_OVERLAP = 40
GAP_STATUSES = frozenset({"missing", "no_record"})


class GoldItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    product_type: ProductType
    accept_types: list[ProductType] = Field(min_length=1)
    description: str
    # Every place in the paper that states this result (caption, referring paragraph, conclusion).
    evidence: list[str] = Field(min_length=1)
    on_hepdata: bool | None  # None = genuinely ambiguous; excluded from gap accuracy
    hepdata_table: str | None = None


class GoldPaper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inspire_id: int
    arxiv_id: str
    labeled_by: str
    reviewed_by_human: bool
    items: list[GoldItem]
    revision: int = 1
    revision_note: str = ""


def load_gold(path: Path) -> list[GoldPaper]:
    papers = [
        GoldPaper.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [p.inspire_id for p in papers]
    if len(ids) != len(set(ids)):
        raise ValueError("gold set lists a paper twice")
    return papers


def _squash(text: str) -> str:
    return "".join(text.split())


def overlap(a: str, b: str) -> int:
    """Characters of evidence shared by two spans (whitespace ignored)."""
    x, y = _squash(a), _squash(b)
    if not x or not y:
        return 0
    if x in y or y in x:
        return min(len(x), len(y))
    match = difflib.SequenceMatcher(None, x, y, autojunk=False).find_longest_match(
        0, len(x), 0, len(y)
    )
    return match.size


def evidence_in_document(gold: GoldPaper, candidates: Iterable[dict[str, Any]]) -> list[str]:
    """Gold item ids whose evidence is not in the filtered text (a broken gold label)."""
    haystack = "".join(_squash(c["text"]) for c in candidates)
    return [
        f"{item.id}: {quote[:40]}"
        for item in gold.items
        for quote in item.evidence
        if len(_squash(quote)) < 20 or _squash(quote) not in haystack
    ]


def item_overlap(span: str, item: GoldItem) -> int:
    """Best overlap of a predicted span with any of the item's evidence quotes, or 0 if below
    the item's matching threshold."""
    best = 0
    for quote in item.evidence:
        size = overlap(span, quote)
        if size >= min(MIN_OVERLAP, len(_squash(quote))):
            best = max(best, size)
    return best


@dataclass
class PaperScore:
    inspire_id: int
    gold_total: int
    predicted_total: int
    matched_gold: dict[str, int] = field(default_factory=dict)  # gold id -> predicted index
    duplicates: int = 0
    false_positives: list[str] = field(default_factory=list)  # descriptions
    missed: list[str] = field(default_factory=list)  # gold ids
    type_correct: int = 0
    by_type: dict[str, dict[str, int]] = field(default_factory=dict)
    gap: dict[str, int] = field(default_factory=dict)

    def _bump(self, product_type: str, key: str) -> None:
        self.by_type.setdefault(product_type, {"tp": 0, "fp": 0, "fn": 0})[key] += 1


def score_paper(
    gold: GoldPaper,
    products: list[dict[str, Any]],
    statuses: dict[tuple[str, str], str] | None = None,
) -> PaperScore:
    """Score one paper. `statuses` maps (evidence_span, description) -> gap status."""
    result = PaperScore(gold.inspire_id, len(gold.items), len(products))
    assigned: dict[int, str] = {}
    # Pair each prediction with its best-overlapping gold item; best pairs claim items first.
    pairs = sorted(
        (
            (item_overlap(p["evidence_span"], g), i, g.id)
            for i, p in enumerate(products)
            for g in gold.items
        ),
        reverse=True,
    )
    for size, i, gid in pairs:
        if size == 0 or i in assigned:
            continue
        if gid in result.matched_gold:
            continue
        assigned[i] = gid
        result.matched_gold[gid] = i
    # Unassigned predictions that still overlap a matched item are duplicates, not errors.
    for i, product in enumerate(products):
        if i in assigned:
            continue
        best = max(
            ((item_overlap(product["evidence_span"], g), g.id) for g in gold.items),
            default=(0, ""),
        )
        if best[0] > 0 and best[1] in result.matched_gold:
            result.duplicates += 1
        else:
            result.false_positives.append(product["description"])
            result._bump(product["product_type"], "fp")

    items = {g.id: g for g in gold.items}
    for gid, item in items.items():
        if gid not in result.matched_gold:
            result.missed.append(gid)
            result._bump(item.product_type, "fn")
            continue
        product = products[result.matched_gold[gid]]
        result._bump(item.product_type, "tp")
        if product["product_type"] in item.accept_types:
            result.type_correct += 1
        if statuses is not None and item.on_hepdata is not None:
            status = statuses.get((product["evidence_span"], product["description"]))
            if status is None:
                continue
            truth_gap = not item.on_hepdata
            if status == "uncertain":
                result.gap["uncertain"] = result.gap.get("uncertain", 0) + 1
                continue
            predicted_gap = status in GAP_STATUSES
            key = {
                (True, True): "tp",
                (False, True): "fp",
                (True, False): "fn",
                (False, False): "tn",
            }[(truth_gap, predicted_gap)]
            result.gap[key] = result.gap.get(key, 0) + 1
    return result


def prf(tp: int, fp: int, fn: int) -> dict[str, float | None]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def aggregate(
    scores: list[PaperScore],
    request_stats: list[dict[str, Any]],
    price_per_million: dict[str, float],
) -> dict[str, Any]:
    tp = sum(len(s.matched_gold) for s in scores)
    fp = sum(len(s.false_positives) for s in scores)
    fn = sum(len(s.missed) for s in scores)
    duplicates = sum(s.duplicates for s in scores)
    by_type: dict[str, dict[str, int]] = {}
    for s in scores:
        for product_type, counts in s.by_type.items():
            row = by_type.setdefault(product_type, {"tp": 0, "fp": 0, "fn": 0})
            for key, value in counts.items():
                row[key] += value
    gap = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "uncertain": 0}
    for s in scores:
        for key, value in s.gap.items():
            gap[key] += value

    latencies = [float(r["latency_s"]) for r in request_stats if float(r["latency_s"]) > 0]
    tokens_in = sum(int(r["input_tokens"]) for r in request_stats)
    tokens_out = sum(int(r["output_tokens"]) for r in request_stats)
    cost = (
        tokens_in * price_per_million.get("input", 0.0)
        + tokens_out * price_per_million.get("output", 0.0)
    ) / 1_000_000
    return {
        "papers": len(scores),
        "gold_items": sum(s.gold_total for s in scores),
        "predicted": sum(s.predicted_total for s in scores),
        "overall": {**prf(tp, fp, fn), "tp": tp, "fp": fp, "fn": fn, "duplicates": duplicates},
        "type_accuracy": (sum(s.type_correct for s in scores) / tp) if tp else None,
        "by_type": {t: {**prf(c["tp"], c["fp"], c["fn"]), **c} for t, c in sorted(by_type.items())},
        "gap_detection": {**prf(gap["tp"], gap["fp"], gap["fn"]), **gap},
        "cost": {
            "requests": len(request_stats),
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "usd": round(cost, 4),
            "price_per_million": price_per_million,
        },
        "latency_s": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "measured_requests": len(latencies),
        },
        "per_paper": [
            {
                "inspire_id": s.inspire_id,
                "gold": s.gold_total,
                "predicted": s.predicted_total,
                **prf(len(s.matched_gold), len(s.false_positives), len(s.missed)),
                "missed": s.missed,
                "false_positives": s.false_positives,
                "duplicates": s.duplicates,
            }
            for s in scores
        ],
    }


def dump(metrics: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=1), encoding="utf-8")
