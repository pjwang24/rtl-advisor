from __future__ import annotations

from collections import Counter

from rtl_advisor.v2_corpus import (
    BLIND_CASES_PER_FAMILY,
    CALIBRATION_CASES_PER_FAMILY,
    TOPOLOGY_DOMAINS,
    all_descriptors,
    family_descriptors,
    suite_statistics,
)


def test_v2_descriptor_counts_and_split_are_frozen() -> None:
    descriptors = all_descriptors()
    assert len(descriptors) == 9 * 48
    counts = Counter(descriptor.split for descriptor in descriptors)
    assert counts == {"calibration-v2": 360, "heldout-v2": 72}


def test_v2_family_selection_is_deterministic_and_unique() -> None:
    family = next(iter(TOPOLOGY_DOMAINS))
    first = family_descriptors(family)
    second = family_descriptors(family)
    assert first == second
    assert len({item.topology_signature for item in first}) == 48
    assert sum(item.split == "calibration-v2" for item in first) == (
        CALIBRATION_CASES_PER_FAMILY
    )
    assert sum(item.split == "heldout-v2" for item in first) == (
        BLIND_CASES_PER_FAMILY
    )


def test_v2_pairwise_coverage_exceeds_floor() -> None:
    stats = suite_statistics(all_descriptors())
    assert stats["family_count"] == 9
    for family in stats["families"].values():
        assert family["pairwise_coverage"] >= 0.85
