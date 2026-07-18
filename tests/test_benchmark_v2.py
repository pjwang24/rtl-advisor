from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rtl_advisor.advisor_explanation_v2 import (
    ADVISOR_PROMPT_VERSION,
    ADVISOR_RESPONSE_CONTRACT_VERSION,
)
from rtl_advisor.advisor_v2 import FEATURE_ORDER, FEATURE_SCHEMA_HASH
from rtl_advisor.benchmark_v2 import (
    BenchmarkV2Error,
    _json_hash,
    _profile_hash,
    aggregate_scores,
    model_call_plan,
    score_v2_analysis,
    verify_benchmark_lock,
)
from rtl_advisor.benchmark_runner_v2 import (
    adapt_v1_analysis,
    benchmark_run_plan,
    challenger_analysis,
)
from rtl_advisor.rules import RULESET_VERSION
from rtl_advisor.v2_corpus import all_descriptors


def _blind_suite() -> dict:
    return {
        "cases": [
            descriptor.to_dict()
            for descriptor in all_descriptors()
            if descriptor.split == "heldout-v2"
        ]
    }


def _summary() -> dict:
    def comparison(candidate_id: str, delay: float, area: float, cells: float) -> dict:
        return {
            "candidate_id": candidate_id,
            "critical_delay_ps": {"improvement_percent": delay},
            "area_total": {"improvement_percent": area},
            "cell_count": {"improvement_percent": cells},
        }

    return {
        "comparisons": [
            comparison("v1", 6.0, 1.0, 2.0),
            comparison("v2", 2.0, 7.0, 4.0),
            comparison("v3", -4.0, 1.0, 1.0),
        ]
    }


def test_v2_model_call_plan_is_exactly_264() -> None:
    calls = model_call_plan(_blind_suite())
    assert len(calls) == 264
    assert sum(call["repeat_index"] == 0 for call in calls) == 216
    assert sum(call["repeat_index"] > 0 for call in calls) == 48


def test_v2_execution_plan_has_480_records_and_264_model_calls() -> None:
    runs = benchmark_run_plan(_blind_suite())

    assert len(runs) == 480
    assert sum(run["arm"] in {
        "v1_codex_xhigh",
        "v1_hybrid_xhigh",
        "v2_safe_advisor_xhigh",
    } for run in runs) == 264
    assert sum(run["repeat_index"] == 0 for run in runs) == 432
    assert sum(run["repeat_index"] > 0 for run in runs) == 48


def test_v2_score_separates_action_and_rank_accuracy() -> None:
    analysis = {
        "decision": "recommend",
        "selected_candidate_id": "candidate",
        "candidates": [
            {
                "candidate_id": "candidate",
                "template_id": "v2",
                "predicted_improvement_percent": {
                    "delay": {"estimate": 2.0},
                    "area": {"estimate": 7.0},
                    "cell_count": {"estimate": 4.0},
                },
            }
        ],
    }
    score = score_v2_analysis(analysis, _summary())

    assert score["actionable_correct"] is True
    assert score["harmful_recommendation"] is False
    assert score["exact_best"] is True
    assert score["normalized_ranking_regret"] == 0.0


def test_v2_aggregate_harmful_rate_uses_recommendations() -> None:
    rows = [
        {
            "recommended": True,
            "actionable_correct": True,
            "harmful_recommendation": False,
            "opportunity": True,
            "opportunity_covered": True,
            "exact_best": True,
            "normalized_ranking_regret": 0.0,
            "direction_pairs": [],
        },
        {
            "recommended": False,
            "actionable_correct": True,
            "harmful_recommendation": False,
            "opportunity": False,
            "opportunity_covered": False,
            "exact_best": False,
            "normalized_ranking_regret": 0.0,
            "direction_pairs": [],
        },
    ]
    summary = aggregate_scores(rows)

    assert summary["actionable_accuracy"] == 1.0
    assert summary["harmful_recommendation_rate"] == 0.0
    assert summary["opportunity_coverage"] == 1.0


def test_v1_adapter_uses_canonical_v1_template_and_known_directions() -> None:
    raw = {
        "findings": [
            {
                "transformation_id": "share_arithmetic_by_muxing_inputs",
                "predicted_effect": {
                    "delay": "uncertain",
                    "area": "improve",
                    "cell_count": "improve",
                },
            }
        ]
    }

    analysis = adapt_v1_analysis(raw, "arithmetic_resource_sharing")

    assert analysis["decision"] == "recommend"
    assert analysis["candidates"][0]["template_id"] == "v1"
    predictions = analysis["candidates"][0]["predicted_improvement_percent"]
    assert "delay" not in predictions
    assert predictions["area"]["estimate"] == 2.0


class _FakeEstimator:
    def predict(self, rows: list[list[float]]) -> list[list[float]]:
        template_code = rows[0][FEATURE_ORDER.index("template_code")]
        return [[template_code, template_code * 2.0, 0.0]]


def test_challenger_ranks_registered_templates_from_point_predictions() -> None:
    candidates = []
    for index, template_id in enumerate(("v1", "v2", "v3"), start=1):
        features = {feature: 0.0 for feature in FEATURE_ORDER}
        features["template_code"] = float(index)
        candidates.append(
            {
                "candidate_id": template_id,
                "template_id": template_id,
                "features": features,
            }
        )
    challenger = {
        "estimator": _FakeEstimator(),
        "family_codes": {"arithmetic_resource_sharing": 1.0},
    }

    analysis = challenger_analysis(
        {"candidates": candidates},
        "arithmetic_resource_sharing",
        challenger,
    )

    assert analysis["decision"] == "recommend"
    assert analysis["selected_candidate_id"] == "v3"
    assert analysis["candidates"][0]["template_id"] == "v3"


def test_benchmark_lock_detects_dependency_tampering(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency.json"
    dependency.write_text("locked\n", encoding="utf-8")
    digest = hashlib.sha256(dependency.read_bytes()).hexdigest()
    item = {"path": str(dependency), "file_sha256": digest}
    core = {
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "profile_hash": _profile_hash(),
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "response_contract_version": ADVISOR_RESPONSE_CONTRACT_VERSION,
        "ruleset_version": RULESET_VERSION,
        "calibration_suite": item,
        "blind_suite": item,
        "gate_model": item,
        "model_artifacts": {"challenger": item},
    }
    lock_path = tmp_path / "benchmark-lock.json"
    lock_path.write_text(
        json.dumps({**core, "lock_hash": _json_hash(core)}),
        encoding="utf-8",
    )

    assert verify_benchmark_lock(lock_path)["lock_hash"] == _json_hash(core)

    dependency.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BenchmarkV2Error, match="dependency changed"):
        verify_benchmark_lock(lock_path)
