from __future__ import annotations

from collections import Counter
from pathlib import Path

from rtl_advisor.v2_corpus import all_descriptors as all_v2_descriptors
from rtl_advisor.v21_corpus import (
    ADDER_ASSOCIATION_FAMILY,
    PRIORITY_SELECTION_FAMILY,
    V21_CALIBRATION_CASES_PER_FAMILY,
    V21_BLIND_CASES_PER_FAMILY,
    all_descriptors,
    family_descriptors,
    suite_statistics,
)
from rtl_advisor.config import load_config
from rtl_advisor.v21_validation import V21ValidationError, validate_v21_suite
import pytest


ROWS = Path("artifacts/models/v2/calibration-rows.json")


def test_v21_descriptor_counts_and_v2_disjointness() -> None:
    descriptors = all_descriptors(propensity_rows_path=ROWS)
    assert len(descriptors) == 648
    assert Counter(item.split for item in descriptors) == {
        "calibration-v21": 576,
        "heldout-v21": 72,
    }
    signatures = {item.topology_signature for item in descriptors}
    assert len(signatures) == 648
    assert signatures.isdisjoint(
        {item.topology_signature for item in all_v2_descriptors()}
    )


def test_v21_selection_is_deterministic_and_balanced() -> None:
    family = ADDER_ASSOCIATION_FAMILY
    first = family_descriptors(family, propensity_rows_path=ROWS)
    second = family_descriptors(family, propensity_rows_path=ROWS)
    assert first == second
    assert sum(item.split == "calibration-v21" for item in first) == (
        V21_CALIBRATION_CASES_PER_FAMILY
    )
    assert sum(item.split == "heldout-v21" for item in first) == (
        V21_BLIND_CASES_PER_FAMILY
    )


def test_v21_expanded_sparse_values_are_selected() -> None:
    adders = family_descriptors(ADDER_ASSOCIATION_FAMILY, propensity_rows_path=ROWS)
    priorities = family_descriptors(PRIORITY_SELECTION_FAMILY, propensity_rows_path=ROWS)
    assert {item.topology["operand_count"] for item in adders} >= {5, 7, 10}
    assert {item.topology["width"] for item in adders} >= {10, 14, 20, 28}
    assert {item.topology["request_count"] for item in priorities} >= {6, 10, 20}
    assert {item.topology["width"] for item in priorities} >= {4, 12, 24}


def test_v21_pairwise_coverage_is_complete() -> None:
    stats = suite_statistics(all_descriptors(propensity_rows_path=ROWS))
    assert stats["family_count"] == 9
    assert all(
        family["pairwise_coverage"] == 1.0
        for family in stats["families"].values()
    )


def test_v21_blind_synthesis_is_forbidden_before_lock() -> None:
    with pytest.raises(V21ValidationError, match="forbidden"):
        validate_v21_suite(
            load_config("rtl-advisor.toml"),
            "heldout-v21",
            synthesize=True,
        )
