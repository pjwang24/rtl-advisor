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
from rtl_advisor.synthesis import SynthesisError, synthesize_case
from rtl_advisor.tools import sha256_file
from rtl_advisor.verification import prove_case_candidates


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBERTY = (
    PROJECT_ROOT
    / "third_party"
    / "nangate45"
    / "NangateOpenCellLibrary_typical.lib"
)
YOSYS = shutil.which("yosys")


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator=shutil.which("verilator") or "verilator",
            yosys=YOSYS or "yosys",
            codex="codex",
            timeout_seconds=30,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="Nangate45 typical",
            path=LIBERTY,
            url="unused",
            sha256=sha256_file(LIBERTY),
            license_path=LIBERTY.parent / "LICENSE",
            license_url="unused",
            source_commit="test",
        ),
    )


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_proven_pair_synthesizes_and_caches(tmp_path: Path) -> None:
    manifest_path = generate_resource_sharing_case(
        tmp_path / "corpus/development/dev_rs_test",
        case_id="dev_rs_test",
        width=8,
        seed=42,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)
    cached_results, _ = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert all(result.status == "passed" for result in results)
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    assert all(result.metrics.area_total > 0 for result in results)
    assert all(result.metrics.cell_count > 0 for result in results)
    assert len(summary["comparisons"]) == 3
    assert all(result.cached for result in cached_results)

    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_adder_association_synthesizes_three_proven_candidates(
    tmp_path: Path,
) -> None:
    manifest_path = generate_adder_association_case(
        tmp_path / "corpus/development/dev_aa_synth",
        case_id="dev_aa_synth",
        width=12,
        seed=43,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    assert any(
        abs(comparison["critical_delay_ps"]["improvement_percent"]) > 0.01
        for comparison in summary["comparisons"]
    )
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_priority_selection_has_measurable_mapped_alternatives(
    tmp_path: Path,
) -> None:
    manifest_path = generate_priority_selection_case(
        tmp_path / "corpus/development/dev_pr_synth",
        case_id="dev_pr_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert any(
        abs(comparison["critical_delay_ps"]["improvement_percent"]) > 0.01
        for comparison in summary["comparisons"]
    )
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_mux_placement_has_measurable_area_timing_tradeoff(tmp_path: Path) -> None:
    manifest_path = generate_mux_placement_case(
        tmp_path / "corpus/development/dev_mp_synth",
        case_id="dev_mp_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert any(
        abs(comparison["critical_delay_ps"]["improvement_percent"]) > 0.01
        for comparison in summary["comparisons"]
    )
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_decode_factoring_records_mapped_neutral_or_tradeoff_results(
    tmp_path: Path,
) -> None:
    manifest_path = generate_decode_factoring_case(
        tmp_path / "corpus/development/dev_df_synth",
        case_id="dev_df_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_comparator_selection_records_mapped_tradeoffs(tmp_path: Path) -> None:
    manifest_path = generate_comparator_selection_case(
        tmp_path / "corpus/development/dev_cs_synth",
        case_id="dev_cs_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    assert all(result.metrics.area_total > 0 for result in results)
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_variable_shift_records_mapped_tradeoffs(tmp_path: Path) -> None:
    manifest_path = generate_variable_shift_case(
        tmp_path / "corpus/development/dev_vs_synth",
        case_id="dev_vs_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    assert all(result.metrics.area_total > 0 for result in results)
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_width_signedness_records_mapped_tradeoffs(tmp_path: Path) -> None:
    manifest_path = generate_width_signedness_case(
        tmp_path / "corpus/development/dev_ws_synth",
        case_id="dev_ws_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    assert all(result.metrics.area_total > 0 for result in results)
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")


@pytest.mark.skipif(
    YOSYS is None or not LIBERTY.is_file(),
    reason="Yosys and the pinned Nangate45 library are required",
)
def test_popcount_saturation_records_mapped_tradeoffs(tmp_path: Path) -> None:
    manifest_path = generate_popcount_saturation_case(
        tmp_path / "corpus/development/dev_pc_synth",
        case_id="dev_pc_synth",
        width=12,
        seed=41,
    )
    config = make_config(tmp_path)
    prove_case_candidates(config, manifest_path)

    results, summary = synthesize_case(config, manifest_path)

    assert [result.variant_id for result in results] == ["v0", "v1", "v2", "v3"]
    assert len(summary["comparisons"]) == 3
    assert all(result.metrics.critical_delay_ps > 0 for result in results)
    assert all(result.metrics.area_total > 0 for result in results)
    with pytest.raises(SynthesisError, match="successful equivalence proof"):
        synthesize_case(config, manifest_path, variant_id="n0")
