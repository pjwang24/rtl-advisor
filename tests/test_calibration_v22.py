from __future__ import annotations

from rtl_advisor.calibration_v22 import (
    family_opportunity_count_v22,
    select_joint_family_policy_v22,
)


def _option(
    threshold: float,
    *,
    opportunities: int,
    nonopportunities: int,
    correct: int,
    abstained: int,
    harmful: int,
) -> dict:
    recommendations = correct + harmful
    recall = correct / opportunities if opportunities else 1.0
    specificity = abstained / nonopportunities if nonopportunities else 1.0
    harmful_rate = harmful / recommendations if recommendations else 0.0
    return {
        "threshold": threshold,
        "opportunity_count": opportunities,
        "non_opportunity_count": nonopportunities,
        "correct_opportunity_count": correct,
        "abstained_nonopportunity_count": abstained,
        "recommendation_count": recommendations,
        "harmful_count": harmful,
        "opportunity_recall": recall,
        "abstention_specificity": specificity,
        "harmful_recommendation_rate": harmful_rate,
        "balanced_actionable_accuracy": (recall + specificity) / 2.0,
        "family_constraints_passed": True,
    }


def test_family_opportunity_support_counts_cases_not_candidates() -> None:
    rows = [
        {"case_id": "a", "eligible": template == "v1"}
        for template in ("v1", "v2", "v3")
    ] + [
        {"case_id": "b", "eligible": False}
        for _ in ("v1", "v2", "v3")
    ]
    assert family_opportunity_count_v22(rows) == 1


def test_joint_policy_selects_maximum_recall_under_frozen_constraints() -> None:
    options = {
        "family_a": [
            _option(
                1.0,
                opportunities=10,
                nonopportunities=40,
                correct=0,
                abstained=40,
                harmful=0,
            ),
            _option(
                0.8,
                opportunities=10,
                nonopportunities=40,
                correct=8,
                abstained=39,
                harmful=0,
            ),
        ],
        "family_b": [
            _option(
                1.0,
                opportunities=10,
                nonopportunities=40,
                correct=0,
                abstained=40,
                harmful=0,
            ),
            _option(
                0.7,
                opportunities=10,
                nonopportunities=40,
                correct=8,
                abstained=39,
                harmful=0,
            ),
        ],
    }
    policy = select_joint_family_policy_v22(options)
    assert policy["feasible"] is True
    assert policy["aggregate"]["opportunity_recall"] == 0.8
    assert policy["selected"]["family_a"]["threshold"] == 0.8
    assert policy["selected"]["family_b"]["threshold"] == 0.7


def test_joint_policy_retains_safety_frontier_when_balanced_gate_fails() -> None:
    options = {
        "family_a": [
            _option(
                1.0,
                opportunities=20,
                nonopportunities=80,
                correct=0,
                abstained=80,
                harmful=0,
            ),
            _option(
                0.9,
                opportunities=20,
                nonopportunities=80,
                correct=4,
                abstained=80,
                harmful=0,
            ),
        ]
    }
    policy = select_joint_family_policy_v22(options)
    assert policy["feasible"] is False
    assert policy["selection_kind"] == "safety_frontier"
    assert policy["aggregate"]["harmful_recommendation_rate"] == 0.0
    assert policy["aggregate"]["abstention_specificity"] == 1.0
    assert policy["selected"]["family_a"]["threshold"] == 0.9
