from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from rtl_advisor.realistic_openroad import compare_m2_repeats
from rtl_advisor.realistic_result import publish_realistic_result


def _repeat() -> dict[str, object]:
    return {
        "configurations": [
            {
                "configuration_id": configuration_id,
                "usable": True,
                "classification": "improved",
                "direction_agreement": True,
                "comparison": {
                    "critical_delay_ps": {
                        "baseline": delay,
                        "candidate": delay * 0.75,
                    },
                    "area_total": {
                        "baseline": area,
                        "candidate": area * 0.98,
                    },
                },
            }
            for configuration_id, delay, area in (
                ("n08-dw32", 800.0, 900.0),
                ("n16-dw32", 1000.0, 1500.0),
            )
        ]
    }


def test_m2_repeats_require_direction_and_two_percent_drift(
    tmp_path: Path,
) -> None:
    first = _repeat()
    second = deepcopy(first)
    second["configurations"][0]["comparison"]["critical_delay_ps"][
        "candidate"
    ] *= 1.01

    result = compare_m2_repeats(
        first,
        second,
        output_path=tmp_path / "pass.json",
    )

    assert result["status"] == "passed"
    assert all(
        row["repeat_direction_agreement"]
        and row["m01_direction_agreement"]
        and row["within_2_percent"]
        for row in result["configurations"]
    )


def test_m2_repeats_separate_cross_flow_disagreement_from_reproducibility(
    tmp_path: Path,
) -> None:
    first = _repeat()
    disagreed = deepcopy(first)
    disagreed["configurations"][0]["direction_agreement"] = False
    direction = compare_m2_repeats(
        first,
        disagreed,
        output_path=tmp_path / "direction.json",
    )
    assert direction["status"] == "passed"
    assert direction["configurations"][0]["m01_direction_agreement"] is False

    drifted = deepcopy(first)
    drifted["configurations"][1]["comparison"]["area_total"][
        "candidate"
    ] *= 1.021
    drift = compare_m2_repeats(
        first,
        drifted,
        output_path=tmp_path / "drift.json",
    )
    assert drift["status"] == "failed"
    assert drift["configurations"][1]["within_2_percent"] is False


def test_final_result_publishes_confirmed_and_disagreed_points(
    tmp_path: Path,
) -> None:
    study = {
        "study_id": "realistic-rtl-evidence-slice-v1",
        "status": "completed",
        "normalized": {
            "reference_id": "reference",
            "reference_manifest_hash": "a" * 64,
            "transformation_registry_hash": "b" * 64,
            "controls": {"status": "passed"},
            "configurations": [
                {
                    "configuration_id": configuration_id,
                    "parameters": {"N": width},
                    "formal": {
                        "status": "formal_passed",
                        "safe": True,
                    },
                    "measurement": {
                        "decision": "measured_improvement",
                    },
                }
                for configuration_id, width in (
                    ("n08-dw32", 8),
                    ("n16-dw32", 16),
                )
            ],
        },
    }
    m2 = _repeat()
    m2["study_id"] = "realistic-rtl-evidence-slice-v1"
    m2["status"] = "completed"
    for item in m2["configurations"]:
        item["direction_agreement"] = item["configuration_id"] == "n08-dw32"
        if item["configuration_id"] == "n16-dw32":
            item["classification"] = "neutral"
    m2_reproduction = {
        "study_id": "realistic-rtl-evidence-slice-v1",
        "status": "passed",
        "configurations": [
            {
                "configuration_id": configuration_id,
                "available": True,
                "repeat_direction_agreement": True,
                "m01_direction_agreement": configuration_id == "n08-dw32",
                "within_2_percent": True,
            }
            for configuration_id in ("n08-dw32", "n16-dw32")
        ],
    }
    study_reproduction = {
        "study_id": "realistic-rtl-evidence-slice-v1",
        "status": "passed",
    }

    result = publish_realistic_result(
        study,
        deepcopy(study),
        study_reproduction,
        m2,
        deepcopy(m2),
        m2_reproduction,
        output_path=tmp_path / "result.json",
    )

    assert result["status"] == "completed"
    assert result["configurations"][0]["conclusion"] == (
        "openroad_confirmed_improvement"
    )
    assert result["configurations"][0]["recommend_candidate"] is True
    assert result["configurations"][1]["conclusion"] == (
        "cross_flow_disagreement"
    )
    assert result["configurations"][1]["recommend_candidate"] is False
    assert result["family_gate"]["passed"] is False
