from copy import deepcopy

import pytest

from rtl_advisor.family_measure_reproducibility import (
    FamilyMeasureReproducibilityError,
    compare_family_m01_repeats,
    merge_family_m01_evidence,
)


def _evidence(repeat_id: str) -> dict[str, object]:
    return {
        "study_id": "study",
        "family_id": "family",
        "repeat_id": repeat_id,
        "preppa_semantic_hash": "a" * 64,
        "results": [
            {
                "reference_id": "reference",
                "configuration_id": "n04",
                "candidate_id": "candidate",
                "formal_semantic_hash": "b" * 64,
                "decision": "measured_improvement",
                "profiles": {
                    "M0": {"classification": "improved", "metrics": [1, 2]},
                    "M1": {"classification": "improved", "metrics": [1, 2]},
                },
            }
        ],
        "failures": [
            {
                "reference_id": "reference",
                "configuration_id": "n01",
                "candidate_id": "candidate-small",
                "error": {"code": "invalid_synthesis_metrics"},
            }
        ],
    }


def test_exact_results_and_repeated_limitations_pass() -> None:
    result = compare_family_m01_repeats(
        _evidence("repeat-1"),
        _evidence("repeat-2"),
    )

    assert result["status"] == "passed"
    assert result["summary"] == {
        "cohort_configuration_count": 2,
        "exact_measured_result_count": 1,
        "repeated_measurement_limitation_count": 1,
        "mismatch_count": 0,
        "measured_exact_match_rate": 1.0,
        "cohort_reproducibility_rate": 1.0,
    }


def test_changed_normalized_profile_fails() -> None:
    first = _evidence("repeat-1")
    second = deepcopy(_evidence("repeat-2"))
    second["results"][0]["profiles"]["M0"]["metrics"][0] = 9

    result = compare_family_m01_repeats(first, second)

    assert result["status"] == "failed"
    assert result["summary"]["mismatch_count"] == 1
    assert result["checks"][1]["status"] == "result_mismatch"


def test_supplemental_retry_must_be_hash_linked() -> None:
    first = _evidence("repeat-1")
    supplemental = {
        **_evidence("repeat-1"),
        "preppa_semantic_hash": "c" * 64,
    }

    with pytest.raises(
        FamilyMeasureReproducibilityError,
        match="different pre-PPA",
    ):
        compare_family_m01_repeats(
            first,
            _evidence("repeat-2"),
            first_supplemental=[supplemental],
        )


def test_hash_matched_retry_replaces_only_recorded_failure() -> None:
    primary = _evidence("repeat-1")
    retry = {
        **_evidence("repeat-1"),
        "results": [
            {
                "reference_id": "reference",
                "configuration_id": "n01",
                "candidate_id": "candidate-small",
                "formal_semantic_hash": "c" * 64,
                "decision": "synthesis_handles",
                "profiles": {
                    "M0": {"classification": "neutral"},
                    "M1": {"classification": "neutral"},
                },
            }
        ],
        "failures": [],
        "semantic_hash": "d" * 64,
    }

    merged = merge_family_m01_evidence(primary, [retry])

    assert merged["summary"]["measured_configuration_count"] == 2
    assert merged["summary"]["failure_count"] == 0
    assert merged["derivation"]["supplemental_semantic_hashes"] == [
        "d" * 64
    ]
