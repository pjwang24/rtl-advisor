from copy import deepcopy

from rtl_advisor.family_openroad import compare_family_m2_repeats


def _repeat(repeat_id: str) -> dict[str, object]:
    return {
        "study_id": "study",
        "family_id": "family",
        "repeat_id": repeat_id,
        "configurations": [
            {
                "pair_id": "pair-02",
                "configuration_id": "n16",
                "usable": True,
                "classification": "regressed",
                "comparison": {
                    "critical_delay_ps": {
                        "baseline": 1000.0,
                        "candidate": 1100.0,
                    },
                    "area_total": {
                        "baseline": 500.0,
                        "candidate": 510.0,
                    },
                },
            }
        ],
    }


def test_family_m2_repeat_comparison_accepts_two_percent_drift() -> None:
    first = _repeat("repeat-1")
    second = deepcopy(_repeat("repeat-2"))
    second["configurations"][0]["comparison"]["area_total"][
        "candidate"
    ] *= 1.02

    result = compare_family_m2_repeats(first, second)

    assert result["status"] == "passed"
    assert result["configurations"][0]["within_2_percent"] is True


def test_family_m2_repeat_comparison_rejects_direction_change() -> None:
    first = _repeat("repeat-1")
    second = deepcopy(_repeat("repeat-2"))
    second["configurations"][0]["classification"] = "neutral"

    result = compare_family_m2_repeats(first, second)

    assert result["status"] == "failed"
    assert (
        result["configurations"][0]["repeat_direction_agreement"] is False
    )
