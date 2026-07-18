from pathlib import Path

import pytest

from rtl_advisor.corpus import (
    ADDER_ASSOCIATION_FAMILY,
    COMPARATOR_SELECTION_FAMILY,
    DECODE_FACTORING_FAMILY,
    MUX_PLACEMENT_FAMILY,
    PRIORITY_SELECTION_FAMILY,
    POPCOUNT_SATURATION_FAMILY,
    VARIABLE_SHIFT_FAMILY,
    WIDTH_SIGNEDNESS_FAMILY,
    CorpusError,
    default_case_id,
    default_suite_parameters,
    generate_case,
    generate_comparator_selection_case,
    generate_decode_factoring_case,
    generate_mux_placement_case,
    generate_priority_selection_case,
    generate_popcount_saturation_case,
    generate_adder_association_case,
    generate_resource_sharing_case,
    generate_variable_shift_case,
    generate_width_signedness_case,
    load_manifest,
)


def test_resource_sharing_generation_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_manifest = generate_resource_sharing_case(
        first,
        case_id="dev_rs_test",
        width=8,
        seed=42,
    )
    second_manifest = generate_resource_sharing_case(
        second,
        case_id="dev_rs_test",
        width=8,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    for variant_id in ("v0", "v1", "v2", "v3", "n0"):
        assert (first / "rtl" / f"{variant_id}.sv").read_bytes() == (
            second / "rtl" / f"{variant_id}.sv"
        ).read_bytes()

    manifest = load_manifest(first_manifest)
    assert manifest.baseline.variant_id == "v0"
    assert manifest.variant("v1").expected_equivalent is True
    assert manifest.variant("v2").expected_equivalent is True
    assert manifest.variant("v3").expected_equivalent is True
    assert manifest.variant("n0").expected_equivalent is False


def test_manifest_rejects_modified_rtl(tmp_path: Path) -> None:
    manifest_path = generate_resource_sharing_case(tmp_path / "case")
    variant = tmp_path / "case" / "rtl" / "v1.sv"
    variant.write_text(variant.read_text(encoding="utf-8") + "// changed\n")

    with pytest.raises(CorpusError, match="checksum mismatch"):
        load_manifest(manifest_path)


def test_generation_requires_force_for_different_content(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    generate_resource_sharing_case(case_dir, width=8)

    with pytest.raises(CorpusError, match="use --force"):
        generate_resource_sharing_case(case_dir, width=16)


def test_seed_changes_the_negative_control(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_resource_sharing_case(first, seed=1)
    generate_resource_sharing_case(second, seed=2)

    assert (first / "rtl/n0.sv").read_bytes() != (
        second / "rtl/n0.sv"
    ).read_bytes()


def test_adder_association_has_three_candidates_and_separate_control(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_adder_association_case(
        first,
        case_id="dev_aa_test",
        width=12,
        seed=17,
    )
    second_manifest = generate_adder_association_case(
        second,
        case_id="dev_aa_test",
        width=12,
        seed=17,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == ADDER_ASSOCIATION_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert [variant.role for variant in manifest.variants] == [
        "baseline",
        "candidate",
        "candidate",
        "candidate",
        "negative_control",
    ]
    assert all(manifest.variant(variant_id).expected_equivalent for variant_id in (
        "v1",
        "v2",
        "v3",
    ))
    assert manifest.variant("n0").expected_equivalent is False
    for variant_id in ("v0", "v1", "v2", "v3", "n0"):
        assert (first / "rtl" / f"{variant_id}.sv").read_bytes() == (
            second / "rtl" / f"{variant_id}.sv"
        ).read_bytes()


def test_heldout_default_case_id_is_stable_and_opaque() -> None:
    first = default_case_id(
        ADDER_ASSOCIATION_FAMILY,
        "heldout",
        width=16,
        seed=9001,
    )
    repeated = default_case_id(
        ADDER_ASSOCIATION_FAMILY,
        "heldout",
        width=16,
        seed=9001,
    )
    different = default_case_id(
        ADDER_ASSOCIATION_FAMILY,
        "heldout",
        width=16,
        seed=9002,
    )

    assert first == repeated
    assert first.startswith("h_")
    assert "adder" not in first
    assert "association" not in first
    assert different != first


def test_heldout_defaults_are_disjoint_from_development(tmp_path: Path) -> None:
    development_parameters = default_suite_parameters("development")
    heldout_parameters = default_suite_parameters("heldout")
    heldout_manifest_path = generate_case(
        tmp_path / "heldout",
        family=ADDER_ASSOCIATION_FAMILY,
        suite="heldout",
    )
    heldout_manifest = load_manifest(heldout_manifest_path)

    assert development_parameters != heldout_parameters
    assert (heldout_manifest.width, heldout_manifest.seed) == heldout_parameters
    assert heldout_manifest.case_id.startswith("h_")


def test_priority_selection_family_is_registered_and_deterministic(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_priority_selection_case(
        first,
        case_id="dev_pr_test",
        width=10,
        seed=41,
    )
    second_manifest = generate_priority_selection_case(
        second,
        case_id="dev_pr_test",
        width=10,
        seed=41,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == PRIORITY_SELECTION_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False


def test_mux_placement_family_has_three_equivalent_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_mux_placement_case(
        first,
        case_id="dev_mp_test",
        width=11,
        seed=42,
    )
    second_manifest = generate_mux_placement_case(
        second,
        case_id="dev_mp_test",
        width=11,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == MUX_PLACEMENT_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False


def test_decode_factoring_family_has_three_equivalent_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_decode_factoring_case(
        first,
        case_id="dev_df_test",
        width=9,
        seed=42,
    )
    second_manifest = generate_decode_factoring_case(
        second,
        case_id="dev_df_test",
        width=9,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == DECODE_FACTORING_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False


def test_comparator_selection_family_has_three_equivalent_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_comparator_selection_case(
        first,
        case_id="dev_cs_test",
        width=9,
        seed=42,
    )
    second_manifest = generate_comparator_selection_case(
        second,
        case_id="dev_cs_test",
        width=9,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == COMPARATOR_SELECTION_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False


def test_variable_shift_family_has_three_equivalent_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_variable_shift_case(
        first,
        case_id="dev_vs_test",
        width=9,
        seed=42,
    )
    second_manifest = generate_variable_shift_case(
        second,
        case_id="dev_vs_test",
        width=9,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == VARIABLE_SHIFT_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False


def test_width_signedness_family_has_three_equivalent_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_width_signedness_case(
        first,
        case_id="dev_ws_test",
        width=9,
        seed=42,
    )
    second_manifest = generate_width_signedness_case(
        second,
        case_id="dev_ws_test",
        width=9,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == WIDTH_SIGNEDNESS_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False


def test_popcount_saturation_family_has_three_equivalent_candidates(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_popcount_saturation_case(
        first,
        case_id="dev_pc_test",
        width=9,
        seed=42,
    )
    second_manifest = generate_popcount_saturation_case(
        second,
        case_id="dev_pc_test",
        width=9,
        seed=42,
    )

    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    manifest = load_manifest(first_manifest)
    assert manifest.family == POPCOUNT_SATURATION_FAMILY
    assert [variant.variant_id for variant in manifest.variants] == [
        "v0",
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert all(
        manifest.variant(variant_id).expected_equivalent
        for variant_id in ("v1", "v2", "v3")
    )
    assert manifest.variant("n0").expected_equivalent is False
