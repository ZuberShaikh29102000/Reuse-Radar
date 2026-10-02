"""Markdown rendering of eval metrics (printed by run_eval and attached to CI runs)."""

from __future__ import annotations

from typing import Any


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def render_markdown(metrics: dict[str, Any], thresholds: dict[str, Any]) -> str:
    o = metrics["overall"]
    lines = [
        "# Reuse Radar extraction eval",
        "",
        f"Source: `{metrics['source']}` · papers: {metrics['papers']} · gold items: "
        f"{metrics['gold_items']} · predicted: {metrics['predicted']}",
        "",
    ]
    if not metrics.get("gold_reviewed_by_human"):
        lines += [
            "> **Gold labels are a draft by an AI assistant and have not been reviewed by a "
            "physicist.** Treat these numbers as estimates.",
            "",
        ]
    lines += [
        "| Metric | Value | Gate |",
        "|---|---|---|",
        f"| Precision | {_pct(o['precision'])} | {_pct(thresholds.get('min_precision'))} |",
        f"| Recall | {_pct(o['recall'])} | {_pct(thresholds.get('min_recall'))} |",
        f"| F1 | {_pct(o['f1'])} | {_pct(thresholds.get('min_f1'))} |",
        f"| Type accuracy (of matched) | {_pct(metrics['type_accuracy'])} | |",
        f"| Duplicates | {o['duplicates']} | |",
        "",
        "## By product type (gold type for recall, predicted type for precision)",
        "",
        "| Type | Precision | Recall | F1 | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
    ]
    for product_type, row in metrics["by_type"].items():
        lines.append(
            f"| {product_type} | {_pct(row['precision'])} | {_pct(row['recall'])} | "
            f"{_pct(row['f1'])} | {row['tp']} | {row['fp']} | {row['fn']} |"
        )
    gap = metrics["gap_detection"]
    if gap["tp"] + gap["fp"] + gap["fn"] + gap["tn"] + gap["uncertain"]:
        lines += [
            "",
            "## Gap detection (matched items with a known HEPData status)",
            "",
            f"Precision {_pct(gap['precision'])} · recall {_pct(gap['recall'])} · "
            f"TP {gap['tp']} · FP {gap['fp']} · FN {gap['fn']} · TN {gap['tn']} · "
            f"left uncertain {gap['uncertain']}",
        ]
    cost, latency = metrics["cost"], metrics["latency_s"]
    p95 = "n/a" if latency["p95"] is None else f"{latency['p95']:.1f} s"
    p50 = "n/a" if latency["p50"] is None else f"{latency['p50']:.1f} s"
    lines += [
        "",
        "## Cost and latency",
        "",
        f"{cost['requests']} requests · {cost['input_tokens']:,} input + "
        f"{cost['output_tokens']:,} output tokens · ${cost['usd']:.4f} at the configured price",
        f"Latency p50 {p50} · p95 {p95} (from {latency['measured_requests']} live calls; "
        "calls recorded before latency tracking show none)",
        "",
        "## Per paper",
        "",
        "| INSPIRE | Gold | Predicted | Precision | Recall | Missed | Dup. |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in metrics["per_paper"]:
        lines.append(
            f"| {p['inspire_id']} | {p['gold']} | {p['predicted']} | {_pct(p['precision'])} | "
            f"{_pct(p['recall'])} | {', '.join(p['missed']) or '-'} | {p['duplicates']} |"
        )
    return "\n".join(lines) + "\n"
