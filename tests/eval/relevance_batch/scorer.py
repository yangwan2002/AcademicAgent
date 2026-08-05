"""Pure scoring functions for batch-vs-per-item relevance evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


VERDICTS = ("relevant", "weakly_relevant", "unrelated")


@dataclass(frozen=True)
class Prediction:
    case_id: str
    status: str
    verdict: str | None
    basis: str = ""
    error_kind: str | None = None


def _accepted(prediction: Prediction) -> bool:
    return (
        prediction.status == "SUCCESS"
        and prediction.verdict == "relevant"
        and prediction.basis != "title_only"
    )


def _gold_accepted(case: dict) -> bool:
    return (
        case["label"]["verdict"] == "relevant"
        and bool(expand_abstract(case))
    )


def expand_abstract(case: dict) -> str:
    candidate = case["candidate"]
    abstract = str(candidate.get("abstract", ""))
    repeat = int(candidate.get("abstract_repeat", 1))
    return " ".join(abstract for _ in range(max(1, repeat))) if abstract else ""


def _precision(cases: Sequence[dict], predictions: Mapping[str, Prediction]) -> float:
    accepted = [case for case in cases if _accepted(predictions[case["id"]])]
    if not accepted:
        return 1.0
    true_positive = sum(_gold_accepted(case) for case in accepted)
    return true_positive / len(accepted)


def _false_acceptance_rate(
    cases: Sequence[dict], predictions: Mapping[str, Prediction]
) -> float:
    negatives = [case for case in cases if not _gold_accepted(case)]
    if not negatives:
        return 0.0
    false_accepts = sum(_accepted(predictions[case["id"]]) for case in negatives)
    return false_accepts / len(negatives)


def score_predictions(
    cases: Sequence[dict],
    per_item: Mapping[str, Prediction],
    batch: Mapping[str, Prediction],
    transformed_batches: Sequence[Mapping[str, Prediction]] = (),
) -> dict:
    ids = [case["id"] for case in cases]
    if set(ids) != set(per_item) or set(ids) != set(batch):
        raise ValueError("prediction ids must exactly match manifest case ids")

    verdict_matches = sum(
        per_item[case_id].status == "SUCCESS"
        and batch[case_id].status == "SUCCESS"
        and per_item[case_id].verdict == batch[case_id].verdict
        for case_id in ids
    )
    acceptance_matches = sum(
        per_item[case_id].status == "SUCCESS"
        and batch[case_id].status == "SUCCESS"
        and _accepted(per_item[case_id]) == _accepted(batch[case_id])
        for case_id in ids
    )
    comparisons = len(ids) * len(transformed_batches)
    transformation_matches = sum(
        batch[case_id].status == "SUCCESS"
        and variant[case_id].status == "SUCCESS"
        and _accepted(batch[case_id]) == _accepted(variant[case_id])
        for variant in transformed_batches
        for case_id in ids
    )

    per_precision = _precision(cases, per_item)
    batch_precision = _precision(cases, batch)
    per_far = _false_acceptance_rate(cases, per_item)
    batch_far = _false_acceptance_rate(cases, batch)
    n = len(ids)
    return {
        "n_cases": n,
        "verdict_consistency": verdict_matches / n if n else 1.0,
        "acceptance_consistency": acceptance_matches / n if n else 1.0,
        "per_item_accepted_precision": per_precision,
        "batch_accepted_precision": batch_precision,
        "precision_drop_pp": (per_precision - batch_precision) * 100,
        "per_item_false_acceptance_rate": per_far,
        "batch_false_acceptance_rate": batch_far,
        "false_acceptance_increase_pp": (batch_far - per_far) * 100,
        "window_transformation_acceptance_consistency": (
            transformation_matches / comparisons if comparisons else None
        ),
        "per_item_error_rate": sum(
            per_item[case_id].status != "SUCCESS" for case_id in ids
        ) / n if n else 0.0,
        "batch_error_rate": sum(
            batch[case_id].status != "SUCCESS" for case_id in ids
        ) / n if n else 0.0,
    }


def threshold_results(metrics: dict) -> dict:
    metamorphic = metrics["window_transformation_acceptance_consistency"]
    return {
        "verdict_consistency": metrics["verdict_consistency"] >= 0.90,
        "acceptance_consistency": metrics["acceptance_consistency"] >= 0.95,
        "precision_drop": metrics["precision_drop_pp"] <= 2.0,
        "false_acceptance_increase": metrics["false_acceptance_increase_pp"] <= 2.0,
        "window_transformation": metamorphic is not None and metamorphic >= 0.95,
    }


__all__ = [
    "Prediction",
    "VERDICTS",
    "expand_abstract",
    "score_predictions",
    "threshold_results",
]
