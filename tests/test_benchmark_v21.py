from __future__ import annotations

import json
from pathlib import Path

from rtl_advisor.benchmark_v21 import (
    aggregate_scores_v21,
    benchmark_run_plan,
    model_call_plan,
    score_v21_analysis,
)


def _comparison(template: str, delay: float, area: float, cells: float) -> dict:
    return {
        "candidate_id": template,
        "critical_delay_ps": {"improvement_percent": delay},
        "area_total": {"improvement_percent": area},
        "cell_count": {"improvement_percent": cells},
    }


def test_v21_score_is_tie_aware_and_regret_is_conditional() -> None:
    synthesis = {
        "comparisons": [
            _comparison("v1", 10.0, 0.0, 0.0),
            _comparison("v2", 9.0, 1.0, 0.0),
            _comparison("v3", -5.0, -5.0, 0.0),
        ]
    }
    analysis = {
        "decision": "recommend",
        "selected_candidate_id": "candidate-v2",
        "candidates": [
            {
                "candidate_id": "candidate-v2",
                "template_id": "v2",
                "predicted_improvement_percent": {
                    "delay": {"estimate": 5.0, "direction": "improve"},
                    "area": {"estimate": 1.0, "direction": "uncertain"},
                    "cell_count": {"estimate": 0.0, "direction": "uncertain"},
                },
            }
        ],
    }
    score = score_v21_analysis(analysis, synthesis)
    assert score["best_candidate_ids"] == ["v1", "v2"]
    assert score["tie_aware_exact_best"] is True
    assert score["conditional_normalized_ranking_regret"] == 0.0
    assert len(score["direction_pairs"]) == 1


def test_v21_direction_coverage_denominator_is_recommended_metric_slots() -> None:
    opportunity = {
        "family": "family-a",
        "score": {
            "recommended": True,
            "opportunity": True,
            "opportunity_covered": True,
            "true_abstention": False,
            "harmful_recommendation": False,
            "tie_aware_exact_best": True,
            "conditional_normalized_ranking_regret": 0.0,
            "direction_pairs": [
                {"metric": "delay", "predicted": "improve", "observed": "improve"}
            ],
        },
    }
    negative = {
        "family": "family-a",
        "score": {
            "recommended": False,
            "opportunity": False,
            "opportunity_covered": False,
            "true_abstention": True,
            "harmful_recommendation": False,
            "tie_aware_exact_best": False,
            "conditional_normalized_ranking_regret": 0.0,
            "direction_pairs": [],
        },
    }
    metrics = aggregate_scores_v21([opportunity, negative])
    assert metrics["micro"]["balanced_actionable_accuracy"] == 1.0
    assert metrics["micro"]["direction"]["possible_recommended_metric_slots"] == 3
    assert metrics["micro"]["direction"]["coverage"] == 1 / 3


def test_v21_run_and_model_call_counts_remain_frozen() -> None:
    suite = json.loads(Path("corpus/heldout-v21/suite.json").read_text())
    assert len(benchmark_run_plan(suite)) == 480
    assert len(model_call_plan(suite)) == 264
