from rtl_advisor.synthesis_redundancy import (
    _aggressive_script,
    classify_candidate,
    select_case_records,
)


def _comparison(delay: float, area: float, cells: float = 0.0) -> dict:
    return {
        "critical_delay_ps": {"improvement_percent": delay},
        "area_total": {"improvement_percent": area},
        "cell_count": {"improvement_percent": cells},
    }


def test_selection_prefers_three_evidence_roles_per_family() -> None:
    cases = [
        {
            "case_id": f"case_{index}",
            "family": "family_a",
            "topology_signature": f"topology_{index}",
            "classification": {"category": category},
        }
        for index, category in enumerate(
            (
                "covered_best",
                "no_candidate_clears_threshold",
                "true_abstention",
                "true_abstention",
            )
        )
    ]

    selected = select_case_records(cases, seed=17)

    assert len(selected) == 3
    assert {case["selection_role"] for case in selected} == {
        "covered_best",
        "missed_improvement",
        "true_abstention",
    }


def test_selection_is_deterministic_when_a_role_needs_fill() -> None:
    cases = [
        {
            "case_id": f"case_{index}",
            "family": "family_a",
            "topology_signature": f"topology_{index}",
            "classification": {"category": "true_abstention"},
        }
        for index in range(5)
    ]

    first = select_case_records(cases, seed=17)
    second = select_case_records(list(reversed(cases)), seed=17)

    assert [case["case_id"] for case in first] == [
        case["case_id"] for case in second
    ]
    assert len(first) == 3


def test_candidate_classification_detects_surviving_and_absorbed_value() -> None:
    useful = _comparison(4.0, 0.0)
    neutral = _comparison(0.2, -0.3, 0.0)

    assert classify_candidate(
        useful, useful, aggressive_cell_signatures_equal=False
    ) == "survives_aggressive_synthesis"
    assert classify_candidate(
        useful, neutral, aggressive_cell_signatures_equal=True
    ) == "absorbed_by_aggressive_synthesis"
    assert classify_candidate(
        neutral, neutral, aggressive_cell_signatures_equal=True
    ) == "synthesis_absorbed"
    assert classify_candidate(
        neutral, useful, aggressive_cell_signatures_equal=False
    ) == "representation_sensitive_tradeoff"


def test_aggressive_recipe_adds_aggressive_sharing_before_same_mapping(tmp_path) -> None:
    script = _aggressive_script(
        source=tmp_path / "source.sv",
        top="test_top",
        liberty=tmp_path / "cells.lib",
        constraints=tmp_path / "abc.constr",
        stat_json=tmp_path / "stat.json",
        netlist=tmp_path / "mapped.v",
    )

    assert "synth -top test_top -flatten -noabc -run begin:fine" in script
    assert "share -aggressive" in script
    assert script.index("share -aggressive") < script.index("dfflibmap")
    assert "abc -liberty" in script
    assert "-constr" in script
