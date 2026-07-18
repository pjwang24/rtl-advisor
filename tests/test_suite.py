from collections import Counter
from pathlib import Path

from rtl_advisor.corpus import FAMILY_REGISTRY
from rtl_advisor.suite import (
    generate_suite,
    load_suite_manifest,
    suite_case_specs,
)


def test_suite_allocations_match_v1_case_counts() -> None:
    development = suite_case_specs("development")
    heldout = suite_case_specs("heldout")

    assert len(development) == 32
    assert len(heldout) == 36
    assert set(item["family"] for item in development) == set(FAMILY_REGISTRY)
    assert Counter(item["family"] for item in heldout) == {
        family: 4 for family in FAMILY_REGISTRY
    }
    assert len({item["case_id"] for item in development}) == 32
    assert len({item["case_id"] for item in heldout}) == 36


def test_heldout_specs_are_opaque_and_disjoint() -> None:
    development = suite_case_specs("development")
    heldout = suite_case_specs("heldout")
    development_parameters = {
        (item["width"], item["seed"]) for item in development
    }
    heldout_parameters = {(item["width"], item["seed"]) for item in heldout}

    assert development_parameters.isdisjoint(heldout_parameters)
    assert all(item["case_id"].startswith("h_") for item in heldout)
    assert all(
        item["family"] not in item["case_id"]
        for item in heldout
    )


def test_generate_suite_is_deterministic_and_uses_five_variants(
    tmp_path: Path,
) -> None:
    first_path = generate_suite(tmp_path / "first", "development")
    second_path = generate_suite(tmp_path / "second", "development")
    first = load_suite_manifest(first_path)
    second = load_suite_manifest(second_path)

    first.pop("manifest_path")
    second.pop("manifest_path")
    assert first == second
    assert first["case_count"] == 32
    assert all(case["variant_count"] == 5 for case in first["cases"])
