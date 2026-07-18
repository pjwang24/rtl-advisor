import pytest

from rtl_advisor.synthesis_robustness_full import (
    SynthesisRobustnessFullError,
    _validate_full_population,
    classify_robust_candidate,
    direction_label,
    directions_compatible,
)


def _comparison(delay: float, area: float, cells: float = 0.0) -> dict:
    return {
        "critical_delay_ps": {"improvement_percent": delay},
        "area_total": {"improvement_percent": area},
        "cell_count": {"improvement_percent": cells},
    }


def test_direction_label_uses_frozen_one_percent_neutral_band() -> None:
    assert direction_label(1.01) == "improve"
    assert direction_label(1.0) == "neutral"
    assert direction_label(-1.0) == "neutral"
    assert direction_label(-1.01) == "degrade"


def test_direction_compatibility_allows_neutral_but_not_sign_flip() -> None:
    assert directions_compatible("improve", "improve") is True
    assert directions_compatible("improve", "neutral") is True
    assert directions_compatible("neutral", "degrade") is True
    assert directions_compatible("improve", "degrade") is False
    with pytest.raises(SynthesisRobustnessFullError):
        directions_compatible("unknown", "neutral")


@pytest.mark.parametrize(
    ("standard", "stronger", "signatures_equal", "expected"),
    (
        (
            _comparison(4.0, 0.0),
            _comparison(5.0, 0.5),
            False,
            "robust_useful",
        ),
        (
            _comparison(4.0, 0.0),
            _comparison(-1.5, 6.0),
            False,
            "flow_conflict",
        ),
        (
            _comparison(4.0, 0.0),
            _comparison(0.2, 0.1),
            True,
            "absorbed_by_stronger_synthesis",
        ),
        (
            _comparison(0.2, 0.1),
            _comparison(4.0, 0.0),
            False,
            "stronger_recipe_only",
        ),
        (
            _comparison(0.2, 0.1),
            _comparison(0.2, -0.2, 0.0),
            True,
            "synthesis_absorbed",
        ),
        (
            _comparison(-5.0, 2.0),
            _comparison(-4.0, 3.0, 2.0),
            False,
            "not_useful",
        ),
    ),
)
def test_candidate_classification_is_mutually_exclusive(
    standard: dict,
    stronger: dict,
    signatures_equal: bool,
    expected: str,
) -> None:
    result = classify_robust_candidate(
        standard,
        stronger,
        stronger_cell_signatures_equal=signatures_equal,
    )

    assert result["classification"] == expected
    assert result["robust_eligible"] is (expected == "robust_useful")


def test_full_population_requires_104_cases_for_each_of_nine_families() -> None:
    cases = [
        {"case_id": f"case_{family}_{index}", "family": f"family_{family}"}
        for family in range(9)
        for index in range(104)
    ]

    _validate_full_population(cases)

    with pytest.raises(SynthesisRobustnessFullError):
        _validate_full_population(cases[:-1])
