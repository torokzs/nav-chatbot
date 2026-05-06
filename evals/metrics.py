from __future__ import annotations

from collections.abc import Mapping

THRESHOLDS = {
    "citation_correctness": 0.2,
}

# Optional GPT-based thresholds (only enforced when evaluators are available)
OPTIONAL_THRESHOLDS = {
    "groundedness": 3.5,
    "relevance": 3.5,
    "fluency": 3.5,
}


def metric_passes(metric_name: str, value: float | int | None, thresholds: Mapping[str, float] = THRESHOLDS) -> bool:
    if value is None:
        return False
    threshold = thresholds[metric_name]
    return float(value) >= float(threshold)



def summarize_thresholds(summary: Mapping[str, float | int | None], thresholds: Mapping[str, float] = THRESHOLDS) -> dict[str, dict[str, float | bool | None]]:
    return {
        metric_name: {
            "value": None if summary.get(metric_name) is None else float(summary[metric_name]),
            "threshold": float(threshold),
            "passed": metric_passes(metric_name, summary.get(metric_name), thresholds),
        }
        for metric_name, threshold in thresholds.items()
    }



def all_metrics_pass(summary: Mapping[str, float | int | None], thresholds: Mapping[str, float] = THRESHOLDS) -> bool:
    return all(metric_passes(metric_name, summary.get(metric_name), thresholds) for metric_name in thresholds)
