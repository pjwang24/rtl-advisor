from __future__ import annotations

import pytest

from rtl_advisor.diagnostic_v22 import _binary_ranking_metrics, classify_case_v22


def _candidate(
    template: str,
    probability: float,
    *,
    eligible: bool,
    predicted_utility: float,
    measured_utility: float,
) -> dict:
    return {
        "template_id": template,
        "eligibility_probability": probability,
        "eligible": eligible,
        "predicted_utility": predicted_utility,
        "measured_utility": measured_utility,
    }


def test_diagnostic_classifies_unsupported_and_threshold_misses() -> None:
    candidates = [
        _candidate(
            "v1", 0.8, eligible=True, predicted_utility=1.0, measured_utility=2.0
        ),
        _candidate(
            "v2", 0.1, eligible=False, predicted_utility=0.0, measured_utility=0.0
        ),
    ]
    unsupported = classify_case_v22(candidates, supported=False, threshold=1.0)
    threshold = classify_case_v22(candidates, supported=True, threshold=0.9)
    assert unsupported["category"] == "unsupported_family"
    assert threshold["category"] == "no_candidate_clears_threshold"
    assert threshold["eligible_probability_margin_to_threshold"] == pytest.approx(0.1)


def test_diagnostic_separates_ranking_failure_from_covered_best() -> None:
    candidates = [
        _candidate(
            "v1", 0.9, eligible=True, predicted_utility=1.0, measured_utility=5.0
        ),
        _candidate(
            "v2", 0.95, eligible=False, predicted_utility=2.0, measured_utility=-1.0
        ),
    ]
    ranking = classify_case_v22(candidates, supported=True, threshold=0.8)
    assert ranking["category"] == "ranking_selected_ineligible"
    assert ranking["oracle_rank_with_frozen_thresholds_covered"] is True

    candidates[1]["predicted_utility"] = 0.0
    covered = classify_case_v22(candidates, supported=True, threshold=0.8)
    assert covered["category"] == "covered_best"
    assert covered["selected_template"] == "v1"


def test_binary_ranking_metrics_handle_perfect_and_constant_scores() -> None:
    perfect = _binary_ranking_metrics([True, True, False, False], [0.9, 0.8, 0.2, 0.1])
    constant = _binary_ranking_metrics([True, False], [0.5, 0.5])
    assert perfect["roc_auc"] == 1.0
    assert perfect["average_precision"] == 1.0
    assert constant["roc_auc"] == 0.5
    assert constant["average_precision"] == 0.5
