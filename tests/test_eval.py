from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reuse_radar.eval import run_eval
from reuse_radar.eval.scoring import (
    GoldItem,
    GoldPaper,
    aggregate,
    evidence_in_document,
    load_gold,
    overlap,
    percentile,
    score_paper,
)

CAPTION = (
    "Observed and expected limits at 95\\% CL on the cross section of nonresonant HH production"
)
PARAGRAPH = (
    "Upper limits on the $HH$ production cross section are also computed as a function of kappa"
)


def _gold(*items: GoldItem) -> GoldPaper:
    return GoldPaper(
        inspire_id=1,
        arxiv_id="2112.11876",
        labeled_by="test",
        reviewed_by_human=True,
        items=list(items),
    )


def _item(
    gid: str, evidence: list[str], ptype: str = "upper_limit", on_hepdata: bool | None = True
) -> GoldItem:
    return GoldItem.model_validate(
        {
            "id": gid,
            "product_type": ptype,
            "accept_types": [ptype],
            "description": gid,
            "evidence": evidence,
            "on_hepdata": on_hepdata,
        }
    )


def _pred(span: str, ptype: str = "upper_limit", description: str = "d") -> dict[str, Any]:
    return {"evidence_span": span, "product_type": ptype, "description": description}


def test_overlap_ignores_whitespace_and_finds_shared_runs() -> None:
    assert overlap("a  b\nc" * 10, "abc" * 10) == 30
    assert overlap(CAPTION + " more text", CAPTION) == len("".join(CAPTION.split()))
    assert overlap("completely different words here", CAPTION) < 15


def test_match_through_any_evidence_location() -> None:
    """A product quoted from the paragraph, not the caption, still matches the same item."""
    gold = _gold(_item("g1", [CAPTION, PARAGRAPH]))
    score = score_paper(gold, [_pred(PARAGRAPH + " for this purpose")])
    assert score.matched_gold == {"g1": 0}
    assert score.false_positives == [] and score.missed == []


def test_unmatched_predictions_and_gold_are_fp_and_fn() -> None:
    gold = _gold(_item("g1", [CAPTION]), _item("g2", [PARAGRAPH], ptype="cross_section"))
    score = score_paper(
        gold, [_pred(CAPTION), _pred("An invented sentence that is in no gold item at all.")]
    )
    assert list(score.matched_gold) == ["g1"]
    assert score.missed == ["g2"]
    assert len(score.false_positives) == 1


def test_second_prediction_of_same_item_is_a_duplicate_not_an_error() -> None:
    gold = _gold(_item("g1", [CAPTION, PARAGRAPH]))
    score = score_paper(gold, [_pred(CAPTION), _pred(PARAGRAPH)])
    assert score.duplicates == 1 and score.false_positives == []


def test_type_accuracy_uses_accept_types() -> None:
    gold = _gold(_item("g1", [CAPTION]))
    assert score_paper(gold, [_pred(CAPTION, ptype="other")]).type_correct == 0
    assert score_paper(gold, [_pred(CAPTION)]).type_correct == 1


def test_gap_detection_counts() -> None:
    gold = _gold(
        _item("g1", [CAPTION], on_hepdata=False),  # a real gap
        _item("g2", [PARAGRAPH], on_hepdata=True),  # published
    )
    preds = [_pred(CAPTION, description="a"), _pred(PARAGRAPH, description="b")]
    statuses = {(CAPTION, "a"): "missing", (PARAGRAPH, "b"): "uncertain"}
    score = score_paper(gold, preds, statuses)
    assert score.gap == {"tp": 1, "uncertain": 1}


def test_aggregate_and_percentiles() -> None:
    gold = _gold(_item("g1", [CAPTION]), _item("g2", [PARAGRAPH]))
    score = score_paper(gold, [_pred(CAPTION)])
    stats = [
        {"input_tokens": 100, "output_tokens": 50, "latency_s": 2.0},
        {"input_tokens": 100, "output_tokens": 50, "latency_s": 0.0},  # recorded before tracking
    ]
    metrics = aggregate([score], stats, {"input": 1.0, "output": 2.0})
    assert metrics["overall"]["precision"] == 1.0 and metrics["overall"]["recall"] == 0.5
    assert metrics["cost"]["usd"] == pytest.approx((200 * 1 + 100 * 2) / 1e6)
    assert metrics["latency_s"]["measured_requests"] == 1
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) in (2.0, 3.0)


def test_gold_set_is_valid_and_every_quote_is_in_its_paper() -> None:
    gold = load_gold(run_eval.GOLD_PATH)
    assert len(gold) >= 6 and sum(len(p.items) for p in gold) >= 50
    for paper in gold:
        candidates = run_eval._filter_record(paper.inspire_id, None)["candidates"]
        assert evidence_in_document(paper, candidates) == []


def test_gate_flags_metrics_below_threshold() -> None:
    metrics = {"overall": {"precision": 0.9, "recall": 0.5, "f1": 0.64}}
    failures = run_eval.gate(metrics, {"min_precision": 0.85, "min_recall": 0.6})
    assert failures == ["recall = 0.5 is below the gate 0.6"]


def test_replay_eval_passes_the_gate(tmp_path: Path) -> None:
    """The CI gate itself: current extraction code, replayed from committed fixtures."""
    assert run_eval.main(["--out", str(tmp_path)]) == 0
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# Reuse Radar")


def test_replay_never_calls_a_provider(tmp_path: Path) -> None:
    router = run_eval.ReplayRouter(tmp_path / "empty", run_eval._template_router())
    from reuse_radar.llm.router import LLMRequest

    with pytest.raises(run_eval.ReplayMiss, match="Re-record"):
        router.complete(LLMRequest("s", (("user", "u"),), "n", {}))
