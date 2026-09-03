from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from rtl_advisor.arbiter_family_execution import (
    family_designs_from_candidate,
    prepare_family_candidate,
    validate_family_candidate,
    verify_family_candidate,
)
from rtl_advisor.config import load_config
from rtl_advisor.mvp_schema import file_sha256, stable_hash, write_hashed_json
from rtl_advisor.transformation_executor import DEFAULT_EXECUTOR_REGISTRY
from rtl_advisor.transformation_registry import DEFAULT_TRANSFORMATION_REGISTRY


ROOT = Path(__file__).resolve().parents[1]


def _input(
    tmp_path: Path,
    *,
    executor_id: str,
    reference_id: str,
    source_root: Path,
    sources: tuple[str, ...],
    include_dirs: tuple[str, ...],
    defines: tuple[str, ...] = (),
) -> dict:
    executor = DEFAULT_EXECUTOR_REGISTRY.get(executor_id, "1")
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.run.reference-input",
        "kind": "qualified_reference",
        "reference_id": reference_id,
        "manifest_path": str(tmp_path / "qualified-reference.json"),
        "manifest_semantic_hash": stable_hash(
            {"reference_id": reference_id, "state": "reference_qualified"}
        ),
        "source_root": str(source_root),
        "source_hashes": {
            source: file_sha256(source_root / source) for source in sources
        },
        "compile_context": {
            "include_dirs": list(include_dirs),
            "defines": list(defines),
        },
        "provenance": {
            "project": executor.spec.upstream_project_id,
            "revision": "pinned",
            "license_expression": "open-source",
        },
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": executor.spec.executor_id,
        "executor_version": executor.spec.version,
        "executor_registry_hash": DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    }
    return write_hashed_json(tmp_path / "input.json", payload, exclusive=True)


@pytest.mark.parametrize(
    (
        "executor_id",
        "reference_id",
        "source_root",
        "sources",
        "include_dirs",
        "defines",
        "configuration",
    ),
    (
        (
            "opentitan-fixed-flat-prefix",
            "opentitan-prim-arbiter-fixed",
            ROOT / "corpus/upstream/opentitan",
            ("hw/ip/prim/rtl/prim_arbiter_fixed.sv",),
            ("hw/ip/prim/rtl",),
            ("SYNTHESIS",),
            {"configuration_id": "n04-dw32", "N": 4, "DW": 32, "EnDataPort": 1},
        ),
        (
            "basejump-fixed-balanced-tree",
            "basejump-bsg-arb-fixed",
            ROOT / "corpus/upstream/basejump_stl",
            (
                "bsg_misc/bsg_defines.sv",
                "bsg_misc/bsg_scan.sv",
                "bsg_misc/bsg_priority_encode_one_hot_out.sv",
                "bsg_misc/bsg_arb_fixed.sv",
            ),
            ("bsg_misc",),
            (),
            {"configuration_id": "width04", "width_p": 4, "lo_to_hi_p": 0},
        ),
    ),
)
def test_p1_family_candidate_is_isolated_and_formally_equivalent(
    tmp_path: Path,
    executor_id: str,
    reference_id: str,
    source_root: Path,
    sources: tuple[str, ...],
    include_dirs: tuple[str, ...],
    defines: tuple[str, ...],
    configuration: dict,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )
    executor = DEFAULT_EXECUTOR_REGISTRY.get(executor_id, "1")
    reference_input = _input(
        tmp_path,
        executor_id=executor_id,
        reference_id=reference_id,
        source_root=source_root,
        sources=sources,
        include_dirs=include_dirs,
        defines=defines,
    )
    finding = {
        "finding_id": f"finding-{reference_id}",
        "transformation_id": "same_cycle_arbiter_topology",
        "executor_id": executor_id,
        "executor_version": "1",
        "reference_id": reference_id,
        "variant_id": f"{reference_id}-candidate",
        "configuration": configuration,
        "measurement_levels": ["M0", "M1"],
    }
    upstream_before = {
        source: (source_root / source).read_bytes() for source in sources
    }

    candidate = prepare_family_candidate(
        config,
        reference_input,
        finding,
        tmp_path / "candidates",
        executor.spec,
        executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    )
    validate_family_candidate(
        candidate,
        executor.spec,
        executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    )
    baseline, alternative = family_designs_from_candidate(
        candidate,
        executor.spec,
    )
    assert baseline.design_hash != alternative.design_hash
    assert Path(candidate["diff_path"]).read_text(encoding="utf-8")
    assert all(
        (source_root / source).read_bytes() == content
        for source, content in upstream_before.items()
    )

    result = verify_family_candidate(
        config,
        candidate,
        tmp_path / "candidates",
        executor.spec,
    )
    # The family P1 path uses the same pinned yosys-slang container as P2.
    assert result["status"] in {"formal_passed", "formal_inconclusive"}
    assert result["safe"] is (result["status"] == "formal_passed")
    assert result["lint"]["verilator"]["baseline"]["status"] == "passed"
    assert result["lint"]["verilator"]["candidate"]["status"] == "passed"


def test_p2_executor_blocks_before_unimplemented_miter_or_ppa(
    tmp_path: Path,
) -> None:
    executor = DEFAULT_EXECUTOR_REGISTRY.get(
        "pulp-rr-tree-rotated-mask",
        "1",
    )
    assert executor.spec.proof_levels == ("P2",)
    assert executor.negative_controls({"proof_contract": {"level": "P2"}})[:3] == (
        {"control_id": "bad_priority_direction", "mutation_kind": 1},
        {"control_id": "bad_grant", "mutation_kind": 2},
        {"control_id": "bad_reset_state_update", "mutation_kind": 3},
    )


@pytest.mark.parametrize(
    ("executor_id", "reference_id", "extra_source", "configuration"),
    (
        (
            "pulp-rr-tree-rotated-mask",
            "pulp-common-cells-rr-arb-tree",
            None,
            {
                "configuration_id": "numin04-dw32",
                "NumIn": 4,
                "DataWidth": 32,
                "FairArb": 1,
                "LockIn": 1,
            },
        ),
        (
            "pulp-stream-arbiter-alternative-cone",
            "pulp-common-cells-stream-arbiter-flushable",
            "src/stream_arbiter_flushable.sv",
            {
                "configuration_id": "nin04-dw32",
                "N_INP": 4,
                "DATA_WIDTH": 32,
                "ARBITER": "rr",
            },
        ),
    ),
)
def test_pulp_p2_candidates_prepare_and_lint_before_pinned_formal(
    tmp_path: Path,
    executor_id: str,
    reference_id: str,
    extra_source: str | None,
    configuration: dict,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )
    source_root = ROOT / "corpus/upstream/common_cells"
    sources = ["src/cf_math_pkg.sv", "src/lzc.sv", "src/rr_arb_tree.sv"]
    if extra_source:
        sources.append(extra_source)
    executor = DEFAULT_EXECUTOR_REGISTRY.get(executor_id, "1")
    reference_input = _input(
        tmp_path,
        executor_id=executor_id,
        reference_id=reference_id,
        source_root=source_root,
        sources=tuple(sources),
        include_dirs=("include",),
        defines=("SYNTHESIS", "COMMON_CELLS_ASSERTS_OFF"),
    )
    finding = {
        "finding_id": f"finding-{reference_id}",
        "transformation_id": "same_cycle_arbiter_topology",
        "executor_id": executor_id,
        "executor_version": "1",
        "reference_id": reference_id,
        "variant_id": f"{reference_id}-candidate",
        "configuration": configuration,
        "measurement_levels": ["M0", "M1"],
    }

    candidate = prepare_family_candidate(
        config,
        reference_input,
        finding,
        tmp_path / "candidates",
        executor.spec,
        executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    )
    result = verify_family_candidate(
        config,
        candidate,
        tmp_path / "candidates",
        executor.spec,
    )

    assert candidate["proof_contract"]["level"] == "P2"
    assert result["lint"]["verilator"]["baseline"]["status"] == "passed"
    assert result["lint"]["verilator"]["candidate"]["status"] == "passed"
    assert result["status"] == "formal_inconclusive"
    assert result["safe"] is False


def test_verilog_axis_p2_candidate_prepares_and_lints_before_pinned_formal(
    tmp_path: Path,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )
    executor_id = "verilog-axis-arbiter-hierarchical"
    reference_id = "verilog-axis-arbiter"
    source_root = ROOT / "corpus/upstream/verilog-axis"
    executor = DEFAULT_EXECUTOR_REGISTRY.get(executor_id, "1")
    reference_input = _input(
        tmp_path,
        executor_id=executor_id,
        reference_id=reference_id,
        source_root=source_root,
        sources=("rtl/priority_encoder.v", "rtl/arbiter.v"),
        include_dirs=(),
    )
    finding = {
        "finding_id": f"finding-{reference_id}",
        "transformation_id": "same_cycle_arbiter_topology",
        "executor_id": executor_id,
        "executor_version": "1",
        "reference_id": reference_id,
        "variant_id": f"{reference_id}-candidate",
        "configuration": {
            "configuration_id": "ports04",
            "PORTS": 4,
            "ARB_TYPE_ROUND_ROBIN": 1,
            "ARB_BLOCK": 1,
            "ARB_BLOCK_ACK": 1,
        },
        "measurement_levels": ["M0", "M1"],
    }

    candidate = prepare_family_candidate(
        config,
        reference_input,
        finding,
        tmp_path / "candidates",
        executor.spec,
        executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    )
    result = verify_family_candidate(
        config,
        candidate,
        tmp_path / "candidates",
        executor.spec,
    )

    assert candidate["proof_contract"]["level"] == "P2"
    assert result["lint"]["verilator"]["baseline"]["status"] == "passed"
    assert result["lint"]["verilator"]["candidate"]["status"] == "passed"
    assert result["status"] == "formal_inconclusive"
    assert result["safe"] is False


def test_verilog_axis_mux_p2_candidate_preserves_pipeline_and_lints(
    tmp_path: Path,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )
    executor_id = "verilog-axis-arb-mux-balanced"
    reference_id = "verilog-axis-axis-arb-mux"
    source_root = ROOT / "corpus/upstream/verilog-axis"
    executor = DEFAULT_EXECUTOR_REGISTRY.get(executor_id, "1")
    reference_input = _input(
        tmp_path,
        executor_id=executor_id,
        reference_id=reference_id,
        source_root=source_root,
        sources=(
            "rtl/priority_encoder.v",
            "rtl/arbiter.v",
            "rtl/axis_arb_mux.v",
        ),
        include_dirs=(),
    )
    finding = {
        "finding_id": f"finding-{reference_id}",
        "transformation_id": "same_cycle_arbiter_topology",
        "executor_id": executor_id,
        "executor_version": "1",
        "reference_id": reference_id,
        "variant_id": f"{reference_id}-candidate",
        "configuration": {
            "configuration_id": "scount04-dw32",
            "S_COUNT": 4,
            "DATA_WIDTH": 32,
            "ARB_TYPE_ROUND_ROBIN": 1,
        },
        "measurement_levels": ["M0", "M1"],
    }

    candidate = prepare_family_candidate(
        config,
        reference_input,
        finding,
        tmp_path / "candidates",
        executor.spec,
        executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    )
    result = verify_family_candidate(
        config,
        candidate,
        tmp_path / "candidates",
        executor.spec,
    )

    candidate_source = Path(
        candidate["candidate_design"]["files"][-1]["path"]
    ).read_text(encoding="utf-8")
    assert "module rtl_advisor_axis_arb_mux_alt" in candidate_source
    assert "rtl_advisor_axis_arbiter_alt" in candidate_source
    assert "rtl_advisor_data_port" in candidate_source
    assert result["lint"]["verilator"]["baseline"]["status"] == "passed"
    assert result["lint"]["verilator"]["candidate"]["status"] == "passed"
    assert result["status"] == "formal_inconclusive"
    assert result["safe"] is False


@pytest.mark.parametrize(
    ("executor_id", "reference_id", "sources", "configuration"),
    (
        (
            "basejump-round-robin-rotated-mask",
            "basejump-bsg-arb-round-robin",
            (
                "bsg_misc/bsg_defines.sv",
                "bsg_misc/bsg_scan.sv",
                "bsg_misc/bsg_arb_round_robin.sv",
            ),
            {"configuration_id": "width04", "width_p": 4},
        ),
        (
            "basejump-locking-fixed-alternative-cone",
            "basejump-bsg-locking-arb-fixed",
            (
                "bsg_misc/bsg_defines.sv",
                "bsg_misc/bsg_scan.sv",
                "bsg_misc/bsg_priority_encode_one_hot_out.sv",
                "bsg_misc/bsg_arb_fixed.sv",
                "bsg_misc/bsg_dff_reset_en.sv",
                "bsg_misc/bsg_locking_arb_fixed.sv",
            ),
            {
                "configuration_id": "width04",
                "width_p": 4,
                "lo_to_hi_p": 0,
            },
        ),
        (
            "basejump-n-to-1-upstream-parameter",
            "basejump-bsg-round-robin-n-to-1",
            (
                "bsg_misc/bsg_defines.sv",
                "bsg_misc/bsg_scan.sv",
                "bsg_misc/bsg_arb_round_robin.sv",
                "bsg_misc/bsg_encode_one_hot.sv",
                "bsg_misc/bsg_round_robin_arb.sv",
                "bsg_misc/bsg_mux_one_hot.sv",
                "bsg_misc/bsg_crossbar_o_by_i.sv",
                "bsg_dataflow/bsg_round_robin_n_to_1.sv",
            ),
            {
                "configuration_id": "width04-dw32",
                "num_in_p": 4,
                "width_p": 32,
                "strict_p": 0,
            },
        ),
    ),
)
def test_basejump_p2_candidates_prepare_and_lint_before_pinned_formal(
    tmp_path: Path,
    executor_id: str,
    reference_id: str,
    sources: tuple[str, ...],
    configuration: dict,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )
    source_root = ROOT / "corpus/upstream/basejump_stl"
    executor = DEFAULT_EXECUTOR_REGISTRY.get(executor_id, "1")
    reference_input = _input(
        tmp_path,
        executor_id=executor_id,
        reference_id=reference_id,
        source_root=source_root,
        sources=sources,
        include_dirs=("bsg_misc",),
        defines=("SYNTHESIS",),
    )
    finding = {
        "finding_id": f"finding-{reference_id}",
        "transformation_id": "same_cycle_arbiter_topology",
        "executor_id": executor_id,
        "executor_version": "1",
        "reference_id": reference_id,
        "variant_id": f"{reference_id}-candidate",
        "configuration": configuration,
        "measurement_levels": ["M0", "M1"],
    }

    candidate = prepare_family_candidate(
        config,
        reference_input,
        finding,
        tmp_path / "candidates",
        executor.spec,
        executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    )
    result = verify_family_candidate(
        config,
        candidate,
        tmp_path / "candidates",
        executor.spec,
    )

    assert candidate["proof_contract"]["level"] == "P2"
    assert result["lint"]["verilator"]["baseline"]["status"] == "passed"
    assert result["lint"]["verilator"]["candidate"]["status"] == "passed"
    assert result["status"] == "formal_inconclusive"
    assert result["safe"] is False
