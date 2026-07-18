from pathlib import Path
import shutil

import pytest

from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.corpus import (
    generate_adder_association_case,
    generate_comparator_selection_case,
    generate_decode_factoring_case,
    generate_mux_placement_case,
    generate_priority_selection_case,
    generate_popcount_saturation_case,
    generate_resource_sharing_case,
    generate_variable_shift_case,
    generate_width_signedness_case,
)
from rtl_advisor.verification import lint_case, prove_case_candidates


VERILATOR = shutil.which("verilator")
YOSYS = shutil.which("yosys")


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator=VERILATOR or "verilator",
            yosys=YOSYS or "yosys",
            codex="codex",
            timeout_seconds=30,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="unused",
            path=tmp_path / "unused.lib",
            url="https://example.invalid/unused.lib",
            sha256="a" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="unused",
        ),
    )


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_generated_case_lints_and_proves_expected_outcomes(tmp_path: Path) -> None:
    manifest_path = generate_resource_sharing_case(
        tmp_path / "corpus" / "development" / "dev_rs_test",
        case_id="dev_rs_test",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)
    assert proof_results[0].counterexample_path is None
    assert proof_results[-1].counterexample_path is not None
    assert Path(proof_results[-1].counterexample_path).is_file()


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_adder_association_candidates_are_equivalent_and_control_is_not(
    tmp_path: Path,
) -> None:
    manifest_path = generate_adder_association_case(
        tmp_path / "corpus/development/dev_aa_test",
        case_id="dev_aa_test",
        width=8,
        seed=43,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.candidate_id for result in proof_results] == [
        "v1",
        "v2",
        "v3",
        "n0",
    ]
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)
    assert proof_results[-1].counterexample_path is not None


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_priority_selection_preserves_low_index_priority(tmp_path: Path) -> None:
    manifest_path = generate_priority_selection_case(
        tmp_path / "corpus/development/dev_pr_test",
        case_id="dev_pr_test",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_mux_placement_candidates_preserve_arithmetic_result(tmp_path: Path) -> None:
    manifest_path = generate_mux_placement_case(
        tmp_path / "corpus/development/dev_mp_test",
        case_id="dev_mp_test",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_decode_factoring_candidates_preserve_outputs(tmp_path: Path) -> None:
    manifest_path = generate_decode_factoring_case(
        tmp_path / "corpus/development/dev_df_test",
        case_id="dev_df_test",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_comparator_selection_candidates_preserve_unsigned_result(
    tmp_path: Path,
) -> None:
    manifest_path = generate_comparator_selection_case(
        tmp_path / "corpus/development/dev_cs_test",
        case_id="dev_cs_test",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_variable_shift_candidates_preserve_out_of_range_behavior(
    tmp_path: Path,
) -> None:
    manifest_path = generate_variable_shift_case(
        tmp_path / "corpus/development/dev_vs_test",
        case_id="dev_vs_test",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_width_signedness_candidates_preserve_signed_order(tmp_path: Path) -> None:
    manifest_path = generate_width_signedness_case(
        tmp_path / "corpus/development/dev_ws_test",
        case_id="dev_ws_test",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for the integration proof",
)
def test_popcount_candidates_count_every_input_bit(tmp_path: Path) -> None:
    manifest_path = generate_popcount_saturation_case(
        tmp_path / "corpus/development/dev_pc_test",
        case_id="dev_pc_test",
        width=8,
        seed=41,
    )
    config = make_config(tmp_path)

    lint_results = lint_case(config, manifest_path)
    proof_results = prove_case_candidates(config, manifest_path)

    assert all(result.ok for result in lint_results)
    assert [result.status for result in proof_results] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proof_results)
