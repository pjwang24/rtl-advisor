from __future__ import annotations

from rtl_advisor.calibration_v21 import (
    _direction_label,
    select_direction_threshold,
    select_risk_threshold,
)


def _row(case_id: str, template: str, eligible: bool) -> dict:
    return {
        "case_id": case_id,
        "template_id": template,
        "eligible": eligible,
    }


def _prediction(probability: float, utility: float) -> dict:
    return {
        "eligibility_probability": probability,
        "regression": {
            "delay": utility,
            "area": 0.0,
            "cell_count": 0.0,
        },
    }


def test_direction_neutral_band_is_plus_or_minus_one_percent() -> None:
    assert _direction_label(1.01) == "improve"
    assert _direction_label(1.0) == "neutral"
    assert _direction_label(-1.0) == "neutral"
    assert _direction_label(-1.01) == "degrade"


def test_risk_threshold_is_selected_only_from_grouped_oof_policy_metrics() -> None:
    rows = []
    oof = []
    for case_id, opportunity, positive_probability in (
        ("opp-a", True, 0.9),
        ("opp-b", True, 0.8),
        ("no-a", False, 0.1),
        ("no-b", False, 0.2),
    ):
        for template in ("v1", "v2", "v3"):
            eligible = opportunity and template == "v1"
            rows.append(_row(case_id, template, eligible))
            oof.append(
                _prediction(
                    positive_probability if eligible else 0.05,
                    10.0 if template == "v1" else 0.0,
                )
            )
    policy = select_risk_threshold(rows, oof)
    assert policy["feasible"] is True
    assert policy["selected"]["opportunity_recall"] == 1.0
    assert policy["selected"]["abstention_specificity"] == 1.0
    assert policy["selected"]["harmful_recommendation_rate"] == 0.0


def test_direction_threshold_reports_accuracy_and_coverage_constraints() -> None:
    true = ["improve"] * 9 + ["degrade"]
    predicted = ["improve"] * 9 + ["neutral"]
    confidence = [0.9] * 9 + [0.2]
    policy = select_direction_threshold(true, predicted, confidence)
    assert policy["feasible"] is True
    assert policy["selected"]["coverage"] >= 0.9
    assert policy["selected"]["accuracy"] >= 0.7


def test_infeasible_risk_policy_persists_safe_frontier() -> None:
    rows = []
    oof = []
    for index in range(10):
        rows.append(_row(f"opp-{index}", "v1", True))
        oof.append(_prediction(0.9 if index == 0 else 0.4, 10.0))
    for index in range(2):
        rows.append(_row(f"negative-{index}", "v1", False))
        oof.append(_prediction(0.4, 10.0))
    policy = select_risk_threshold(rows, oof)
    assert policy["feasible"] is False
    assert policy["safety_constraints_feasible"] is True
    assert policy["selected"]["harmful_recommendation_rate"] <= 0.05
    assert policy["selected"]["abstention_specificity"] >= 0.9
