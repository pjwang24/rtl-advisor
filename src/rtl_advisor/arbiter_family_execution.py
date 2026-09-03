from __future__ import annotations

from dataclasses import asdict
import difflib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_registry import (
    CorpusRegistryError,
    ReferenceManifestV1,
    parse_reference_manifest,
)
from rtl_advisor.mvp_rewriter import (
    CANDIDATE_DOCUMENT_TYPE,
    FORMAL_DOCUMENT_TYPE,
    MVPRewriteError,
    _design_from_mapping,
    _design_integrity,
    _pyslang_lint,
    _verilator_lint,
    candidate_design_from_record,
    verify_addition_candidate,
)
from rtl_advisor.mvp_schema import (
    RUN_SCHEMA_ID,
    RUN_SCHEMA_VERSION,
    compile_context_snapshot,
    file_sha256,
    read_hashed_json,
    source_integrity,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.rtl_input import DesignInputV2, normalize_design_input
from rtl_advisor.sequential_equivalence import (
    SequentialEquivalenceError,
    collect_p2_tool_identity,
    run_p2_proof,
)
from rtl_advisor.tools import ToolExecutionError, run_command
from rtl_advisor.transformation_executor import ExecutorSpec
from rtl_advisor.transformation_registry import (
    ARBITER_TRANSFORMATION_ID,
    DEFAULT_TRANSFORMATION_REGISTRY,
)


class ArbiterFamilyExecutionError(RuntimeError):
    """Raised when a frozen family pair cannot be executed faithfully."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "arbiter_family_execution_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


_REVISIONS = {
    "lowrisc-opentitan": "99fb7bd3b2330a1bda61b1d3382198d0c7fee8d7",
    "pulp-common-cells": "1281545696eb3fcba50ec5b4275993476a3c710e",
    "alexforencich-verilog-axis": "48ff7a7e2ef782cf778d47910cf85835c64b1bce",
    "basejump-stl": "b48037e28544425839dbd617d45b1a82631bc1a9",
}

_CONFIGURATIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "opentitan-prim-arbiter-fixed": tuple(
        {
            "configuration_id": f"n{width:02d}-dw32",
            "N": width,
            "DW": 32,
            "EnDataPort": 1,
        }
        for width in (1, 4, 8, 16)
    ),
    "pulp-common-cells-rr-arb-tree": tuple(
        {
            "configuration_id": f"numin{width:02d}-dw32",
            "NumIn": width,
            "DataWidth": 32,
            "FairArb": 1,
            "LockIn": 1,
        }
        for width in (1, 4, 8, 16)
    ),
    "pulp-common-cells-stream-arbiter-flushable": tuple(
        {
            "configuration_id": f"nin{width:02d}-dw32",
            "N_INP": width,
            "DATA_WIDTH": 32,
            "ARBITER": "rr",
        }
        for width in (1, 4, 8, 16)
    ),
    "verilog-axis-arbiter": tuple(
        {
            "configuration_id": f"ports{width:02d}",
            "PORTS": width,
            "ARB_TYPE_ROUND_ROBIN": 1,
            "ARB_BLOCK": 1,
            "ARB_BLOCK_ACK": 1,
        }
        for width in (2, 4, 8, 16)
    ),
    "verilog-axis-axis-arb-mux": tuple(
        {
            "configuration_id": f"scount{width:02d}-dw32",
            "S_COUNT": width,
            "DATA_WIDTH": 32,
            "ARB_TYPE_ROUND_ROBIN": 1,
        }
        for width in (2, 4, 8, 16)
    ),
    "basejump-bsg-arb-fixed": tuple(
        {
            "configuration_id": f"width{width:02d}",
            "width_p": width,
            "lo_to_hi_p": 0,
        }
        for width in (2, 4, 8, 16)
    ),
    "basejump-bsg-arb-round-robin": tuple(
        {"configuration_id": f"width{width:02d}", "width_p": width}
        for width in (2, 4, 8, 16)
    ),
    "basejump-bsg-locking-arb-fixed": tuple(
        {
            "configuration_id": f"width{width:02d}",
            "width_p": width,
            "lo_to_hi_p": 0,
        }
        for width in (2, 4, 8, 16)
    ),
    "basejump-bsg-round-robin-n-to-1": tuple(
        {
            "configuration_id": f"width{width:02d}-dw32",
            "num_in_p": width,
            "width_p": 32,
            "strict_p": 0,
        }
        for width in (2, 4, 8, 16)
    ),
}

_VARIANTS = {
    "opentitan-prim-arbiter-fixed": "opentitan-prim-arbiter-fixed-flat-prefix",
    "pulp-common-cells-rr-arb-tree": "pulp-rr-arb-tree-rotated-mask",
    "pulp-common-cells-stream-arbiter-flushable": (
        "pulp-stream-arbiter-alternative-cone"
    ),
    "verilog-axis-arbiter": "verilog-axis-arbiter-hierarchical",
    "verilog-axis-axis-arb-mux": "verilog-axis-axis-arb-mux-balanced",
    "basejump-bsg-arb-fixed": "basejump-bsg-arb-fixed-balanced",
    "basejump-bsg-arb-round-robin": (
        "basejump-bsg-arb-round-robin-rotated-mask"
    ),
    "basejump-bsg-locking-arb-fixed": (
        "basejump-bsg-locking-arb-fixed-alternative"
    ),
    "basejump-bsg-round-robin-n-to-1": (
        "basejump-bsg-round-robin-n-to-1-scan"
    ),
}

_HAS_PAYLOAD = {
    "opentitan-prim-arbiter-fixed",
    "pulp-common-cells-rr-arb-tree",
    "pulp-common-cells-stream-arbiter-flushable",
    "verilog-axis-axis-arb-mux",
    "basejump-bsg-round-robin-n-to-1",
}

_M2 = {
    ("opentitan-prim-arbiter-fixed", "n16-dw32"),
    ("pulp-common-cells-rr-arb-tree", "numin16-dw32"),
    ("verilog-axis-axis-arb-mux", "scount08-dw32"),
    ("basejump-bsg-arb-round-robin", "width16"),
}

_COMPILE_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "opentitan-prim-arbiter-fixed": {
        "sources": ("hw/ip/prim/rtl/prim_arbiter_fixed.sv",),
        "include_dirs": ("hw/ip/prim/rtl",),
        "defines": ("SYNTHESIS",),
    },
    "pulp-common-cells-rr-arb-tree": {
        "sources": (
            "src/cf_math_pkg.sv",
            "src/lzc.sv",
            "src/rr_arb_tree.sv",
        ),
        "include_dirs": ("include",),
        "defines": ("SYNTHESIS", "COMMON_CELLS_ASSERTS_OFF"),
    },
    "pulp-common-cells-stream-arbiter-flushable": {
        "sources": (
            "src/cf_math_pkg.sv",
            "src/lzc.sv",
            "src/rr_arb_tree.sv",
            "src/stream_arbiter_flushable.sv",
        ),
        "include_dirs": ("include",),
        "defines": ("SYNTHESIS", "COMMON_CELLS_ASSERTS_OFF"),
    },
    "verilog-axis-arbiter": {
        "sources": ("rtl/priority_encoder.v", "rtl/arbiter.v"),
        "include_dirs": (),
        "defines": (),
    },
    "verilog-axis-axis-arb-mux": {
        "sources": (
            "rtl/priority_encoder.v",
            "rtl/arbiter.v",
            "rtl/axis_arb_mux.v",
        ),
        "include_dirs": (),
        "defines": (),
    },
    "basejump-bsg-arb-fixed": {
        "sources": (
            "bsg_misc/bsg_defines.sv",
            "bsg_misc/bsg_scan.sv",
            "bsg_misc/bsg_priority_encode_one_hot_out.sv",
            "bsg_misc/bsg_arb_fixed.sv",
        ),
        "include_dirs": ("bsg_misc",),
        "defines": ("SYNTHESIS",),
    },
    "basejump-bsg-arb-round-robin": {
        "sources": (
            "bsg_misc/bsg_defines.sv",
            "bsg_misc/bsg_scan.sv",
            "bsg_misc/bsg_arb_round_robin.sv",
        ),
        "include_dirs": ("bsg_misc",),
        "defines": ("SYNTHESIS",),
    },
    "basejump-bsg-locking-arb-fixed": {
        "sources": (
            "bsg_misc/bsg_defines.sv",
            "bsg_misc/bsg_scan.sv",
            "bsg_misc/bsg_priority_encode_one_hot_out.sv",
            "bsg_misc/bsg_arb_fixed.sv",
            "bsg_misc/bsg_dff_reset_en.sv",
            "bsg_misc/bsg_locking_arb_fixed.sv",
        ),
        "include_dirs": ("bsg_misc",),
        "defines": ("SYNTHESIS",),
    },
    "basejump-bsg-round-robin-n-to-1": {
        "sources": (
            "bsg_misc/bsg_defines.sv",
            "bsg_misc/bsg_scan.sv",
            "bsg_misc/bsg_arb_round_robin.sv",
            "bsg_misc/bsg_encode_one_hot.sv",
            "bsg_misc/bsg_round_robin_arb.sv",
            "bsg_misc/bsg_mux_one_hot.sv",
            "bsg_misc/bsg_crossbar_o_by_i.sv",
            "bsg_dataflow/bsg_round_robin_n_to_1.sv",
        ),
        "include_dirs": ("bsg_misc",),
        "defines": ("SYNTHESIS",),
    },
}


def family_configurations(reference_id: str) -> tuple[dict[str, Any], ...]:
    try:
        return tuple(dict(item) for item in _CONFIGURATIONS[reference_id])
    except KeyError as exc:
        raise ArbiterFamilyExecutionError(
            f"no frozen configurations for {reference_id}",
            code="unsupported_configuration",
        ) from exc


def family_compile_spec(reference_id: str) -> dict[str, tuple[str, ...]]:
    try:
        spec = _COMPILE_SPECS[reference_id]
    except KeyError as exc:
        raise ArbiterFamilyExecutionError(
            f"no frozen compile specification for {reference_id}",
            code="unsupported_reference",
        ) from exc
    return {key: tuple(value) for key, value in spec.items()}


def _read_manifest(path: str | Path) -> tuple[dict[str, Any], ReferenceManifestV1]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "semantic_hash" in raw:
            raw = read_hashed_json(manifest_path)
        manifest = parse_reference_manifest(raw)
    except (OSError, json.JSONDecodeError, CorpusRegistryError) as exc:
        raise ArbiterFamilyExecutionError(
            f"invalid corpus reference manifest {manifest_path}: {exc}",
            code=getattr(exc, "code", "invalid_reference_manifest"),
        ) from exc
    return raw, manifest


def load_family_reference(
    config: ProjectConfig,
    manifest_path: str | Path,
    spec: ExecutorSpec,
) -> dict[str, Any]:
    raw, manifest = _read_manifest(manifest_path)
    if manifest.reference_id not in spec.reference_ids:
        raise ArbiterFamilyExecutionError(
            f"executor {spec.executor_id} does not support {manifest.reference_id}",
            code="unsupported_reference",
        )
    if (
        manifest.qualification.status != "active"
        or manifest.qualification.state
        not in {"reference_qualified", "variant_eligible"}
    ):
        raise ArbiterFamilyExecutionError(
            f"reference {manifest.reference_id} is not qualified",
            code="reference_not_qualified",
        )
    if manifest.lineage.upstream_project_id != spec.upstream_project_id:
        raise ArbiterFamilyExecutionError(
            "reference lineage does not match its frozen executor",
            code="stale_reference_lineage",
        )
    if manifest.provenance.revision != _REVISIONS[spec.upstream_project_id]:
        raise ArbiterFamilyExecutionError(
            "reference revision does not match the frozen family study",
            code="stale_reference_revision",
        )
    source_root = (config.corpus_dir / spec.source_root).resolve()
    for relative, expected in manifest.source_hashes:
        source = (source_root / relative).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise ArbiterFamilyExecutionError(
                f"reference source escapes its frozen root: {relative}",
                code="unsafe_path",
            ) from exc
        if not source.is_file() or file_sha256(source) != expected:
            raise ArbiterFamilyExecutionError(
                f"reference source hash mismatch: {relative}",
                code="stale_source_hashes",
            )
    return {
        "manifest": manifest,
        "manifest_payload": raw,
        "manifest_path": str(Path(manifest_path).expanduser().resolve()),
        "manifest_semantic_hash": raw.get("semantic_hash")
        or stable_hash(manifest.to_dict()),
        "source_root": str(source_root),
    }


def family_findings(
    reference: Mapping[str, Any],
    spec: ExecutorSpec,
    *,
    executor_registry_hash: str,
) -> list[dict[str, Any]]:
    manifest = reference["manifest"]
    assert isinstance(manifest, ReferenceManifestV1)
    transformation = DEFAULT_TRANSFORMATION_REGISTRY.get(
        ARBITER_TRANSFORMATION_ID
    )
    configurations = _CONFIGURATIONS.get(manifest.reference_id)
    if not configurations:
        raise ArbiterFamilyExecutionError(
            f"no frozen parameter matrix for {manifest.reference_id}",
            code="unsupported_configuration",
        )
    source_path = Path(str(reference["source_root"])) / dict(
        manifest.source_hashes
    ).keys().__iter__().__next__()
    findings = []
    for configuration in configurations:
        identity = {
            "reference_id": manifest.reference_id,
            "variant_id": _VARIANTS[manifest.reference_id],
            "configuration": configuration,
            "executor_id": spec.executor_id,
            "executor_version": spec.version,
            "reference_manifest_hash": reference["manifest_semantic_hash"],
        }
        findings.append(
            {
                "finding_id": f"arbsite_{stable_hash(identity)[:16]}",
                "status": "candidate_available",
                "transformation_id": transformation.transformation_id,
                "transformation_version": transformation.version,
                "transformation_registry_hash": (
                    DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
                ),
                "executor_id": spec.executor_id,
                "executor_version": spec.version,
                "executor_registry_hash": executor_registry_hash,
                "candidate_origin": spec.candidate_origin,
                "reference_id": manifest.reference_id,
                "variant_id": _VARIANTS[manifest.reference_id],
                "configuration": dict(configuration),
                "proof_level": spec.proof_levels[0],
                "measurement_levels": (
                    ["M0", "M1", "M2"]
                    if (
                        manifest.reference_id,
                        configuration["configuration_id"],
                    )
                    in _M2
                    else ["M0", "M1"]
                ),
                "source": {
                    "path": str(source_path),
                    "sha256": file_sha256(source_path),
                    "line": 1,
                },
                "summary": (
                    f"Evaluate the frozen {_VARIANTS[manifest.reference_id]} "
                    f"architecture at {configuration['configuration_id']}."
                ),
                "objective": "timing",
            }
        )
    return findings


def write_family_reference_input(
    path: Path,
    reference: Mapping[str, Any],
    spec: ExecutorSpec,
    *,
    executor_registry_hash: str,
) -> dict[str, Any]:
    manifest = reference["manifest"]
    assert isinstance(manifest, ReferenceManifestV1)
    source_root = Path(str(reference["source_root"]))
    source_hashes = {
        relative: file_sha256(source_root / relative)
        for relative, _ in manifest.source_hashes
    }
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.run.reference-input",
        "kind": "qualified_reference",
        "reference_id": manifest.reference_id,
        "manifest_path": reference["manifest_path"],
        "manifest_semantic_hash": reference["manifest_semantic_hash"],
        "source_root": str(source_root),
        "source_hashes": source_hashes,
        "compile_context": manifest.to_dict()["compile_context"],
        "provenance": {
            "project": manifest.provenance.project,
            "revision": manifest.provenance.revision,
            "license_expression": manifest.provenance.license.expression,
        },
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": spec.executor_id,
        "executor_version": spec.version,
        "executor_registry_hash": executor_registry_hash,
    }
    expected = {**payload, "semantic_hash": stable_hash(payload)}
    if path.is_file():
        current = read_hashed_json(
            path,
            document_type="rtl-advisor.run.reference-input",
            schema_version=1,
        )
        if current != expected:
            raise ArbiterFamilyExecutionError(
                "append-only reference input conflicts with the frozen executor",
                code="append_only_conflict",
            )
        return current
    return write_hashed_json(path, payload, exclusive=True)


def validate_family_reference_input(
    record: Mapping[str, Any],
    spec: ExecutorSpec,
    *,
    executor_registry_hash: str,
) -> None:
    core = {key: value for key, value in record.items() if key != "semantic_hash"}
    if record.get("semantic_hash") != stable_hash(core):
        raise ArbiterFamilyExecutionError(
            "reference input semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    if (
        record.get("document_type") != "rtl-advisor.run.reference-input"
        or record.get("kind") != "qualified_reference"
        or record.get("reference_id") not in spec.reference_ids
    ):
        raise ArbiterFamilyExecutionError(
            "invalid family reference input",
            code="invalid_reference_input",
        )
    if (
        record.get("executor_id") != spec.executor_id
        or record.get("executor_version") != spec.version
    ):
        raise ArbiterFamilyExecutionError(
            "reference input uses a stale executor",
            code="stale_executor_version",
        )
    if record.get("executor_registry_hash") != executor_registry_hash:
        raise ArbiterFamilyExecutionError(
            "reference input uses a stale executor registry",
            code="stale_executor_registry",
        )
    source_root = Path(str(record.get("source_root", ""))).resolve()
    hashes = record.get("source_hashes")
    if not isinstance(hashes, Mapping):
        raise ArbiterFamilyExecutionError("reference input lacks source hashes")
    for relative, expected in hashes.items():
        path = (source_root / str(relative)).resolve()
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise ArbiterFamilyExecutionError(
                "reference input contains an unsafe path",
                code="unsafe_path",
            ) from exc
        if not path.is_file() or file_sha256(path) != expected:
            raise ArbiterFamilyExecutionError(
                f"reference source changed after review: {relative}",
                code="stale_source_hashes",
            )


def _design_mapping(design: DesignInputV2) -> dict[str, Any]:
    return {
        "schema_version": design.schema_version,
        "top": design.top,
        "files": [asdict(item) for item in design.files],
        "include_dirs": list(design.include_dirs),
        "defines": list(design.defines),
        "filelists": list(design.filelists),
        "design_hash": design.design_hash,
    }


def _opentitan_fixed_sources(configuration: Mapping[str, Any]) -> tuple[str, str]:
    n = int(configuration["N"])
    dw = int(configuration["DW"])
    idxw = max(1, (n - 1).bit_length())
    baseline_idx = ".idx_o()" if n == 1 else ".idx_o(idx_o)"
    wrapper_ports = f"""\
  input logic clk_i,
  input logic rst_ni,
  input logic [{n - 1}:0] req_i,
  input logic [{dw - 1}:0] data_i [{n}],
  input logic ready_i,
  output logic [{n - 1}:0] gnt_o,
  output logic [{idxw - 1}:0] idx_o,
  output logic valid_o,
  output logic [{dw - 1}:0] data_o
"""
    baseline = f"""\
module rtl_advisor_family_top (
{wrapper_ports});
  prim_arbiter_fixed #(.N({n}), .DW({dw}), .EnDataPort(1'b1)) dut (
    .clk_i, .rst_ni, .req_i, .data_i, .gnt_o, {baseline_idx},
    .valid_o, .data_o, .ready_i
  );
  if ({n} == 1) assign idx_o = '0;
endmodule
"""
    candidate = f"""\
module rtl_advisor_fixed_flat #(
  parameter int N = {n},
  parameter int DW = {dw},
  parameter bit EnDataPort = 1,
  parameter int IdxW = (N > 1) ? $clog2(N) : 1
) (
  input logic [N-1:0] req_i,
  input logic [DW-1:0] data_i [N],
  input logic ready_i,
  output logic [N-1:0] gnt_o,
  output logic [IdxW-1:0] idx_o,
  output logic valid_o,
  output logic [DW-1:0] data_o
);
  logic [N-1:0] selected;
  logic found;
  always_comb begin
    selected = '0;
    idx_o = '0;
    data_o = data_i[0];
    found = 1'b0;
    for (int i = 0; i < N; i++) begin
      if (!found && req_i[i]) begin
        selected[i] = 1'b1;
        idx_o = IdxW'(i);
        data_o = data_i[i];
        found = 1'b1;
      end
    end
    valid_o = found;
    gnt_o = selected & {{N{{ready_i}}}};
    if (!EnDataPort) data_o = '1;
  end
endmodule

module rtl_advisor_family_top (
{wrapper_ports});
  rtl_advisor_fixed_flat #(.N({n}), .DW({dw}), .EnDataPort(1'b1)) dut (
    .req_i, .data_i, .ready_i, .gnt_o, .idx_o, .valid_o, .data_o
  );
endmodule
"""
    return baseline, candidate


def _basejump_fixed_sources(configuration: Mapping[str, Any]) -> tuple[str, str]:
    width = int(configuration["width_p"])
    lo_to_hi = int(configuration["lo_to_hi_p"])
    baseline = f"""\
module rtl_advisor_family_top (
  input logic ready_then_i,
  input logic [{width - 1}:0] reqs_i,
  output logic [{width - 1}:0] grants_o
);
  bsg_arb_fixed #(.inputs_p({width}), .lo_to_hi_p({lo_to_hi})) dut (
    .ready_then_i, .reqs_i, .grants_o
  );
endmodule
"""
    candidate = f"""\
module rtl_advisor_bsg_fixed_flat #(
  parameter int inputs_p = {width},
  parameter bit lo_to_hi_p = {lo_to_hi}
) (
  input logic ready_then_i,
  input logic [inputs_p-1:0] reqs_i,
  output logic [inputs_p-1:0] grants_o
);
  logic found;
  always_comb begin
    grants_o = '0;
    found = 1'b0;
    if (lo_to_hi_p) begin
      for (int i = 0; i < inputs_p; i++) begin
        if (!found && reqs_i[i]) begin
          grants_o[i] = ready_then_i;
          found = 1'b1;
        end
      end
    end else begin
      for (int i = inputs_p-1; i >= 0; i--) begin
        if (!found && reqs_i[i]) begin
          grants_o[i] = ready_then_i;
          found = 1'b1;
        end
      end
    end
  end
endmodule

module rtl_advisor_family_top (
  input logic ready_then_i,
  input logic [{width - 1}:0] reqs_i,
  output logic [{width - 1}:0] grants_o
);
  rtl_advisor_bsg_fixed_flat #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) dut (.ready_then_i, .reqs_i, .grants_o);
endmodule
"""
    return baseline, candidate


def _pulp_rr_alt_module(
    *,
    num_in: int,
    data_width: int,
    axi_valid_ready: int,
    lock_in: int,
    module_name: str = "rtl_advisor_pulp_rr_alt",
) -> str:
    idx_width = max(1, (num_in - 1).bit_length())
    # rr_arb_tree intentionally implements NumIn=1 as a pure pass-through:
    # gnt_o mirrors gnt_i even when req_i is low. Preserve that upstream
    # corner-case behavior instead of applying the multi-input grant gate.
    grant_condition = (
        "gnt_i"
        if num_in == 1
        else "gnt_i && (AxiVldRdy || req_d[selected_idx])"
    )
    return f"""\
module {module_name} (
  input logic clk_i,
  input logic rst_ni,
  input logic flush_i,
  input logic [{num_in - 1}:0] req_i,
  output logic [{num_in - 1}:0] gnt_o,
  input logic [{num_in - 1}:0][{data_width - 1}:0] data_i,
  output logic req_o,
  input logic gnt_i,
  output logic [{data_width - 1}:0] data_o,
  output logic [{idx_width - 1}:0] idx_o
);
  localparam int NumIn = {num_in};
  localparam int IdxWidth = {idx_width};
  localparam bit AxiVldRdy = {axi_valid_ready};
  localparam bit LockIn = {lock_in};
  logic [IdxWidth-1:0] rr_q, rr_d;
  logic lock_q, lock_d;
  logic [NumIn-1:0] req_q, req_d;
  logic found, next_found;
  logic [IdxWidth-1:0] selected_idx, next_idx;

  always_comb begin
    req_d = (LockIn && lock_q) ? req_q : req_i;
    req_o = |req_d;
    found = 1'b0;
    selected_idx = rr_q;
    for (int priority_key = 0; priority_key < NumIn; priority_key++) begin
      if (
        !found &&
        req_d[IdxWidth'(priority_key) ^ rr_q]
      ) begin
        selected_idx = IdxWidth'(priority_key) ^ rr_q;
        found = 1'b1;
      end
    end
    if (!found && AxiVldRdy) selected_idx = IdxWidth'(NumIn-1);

    idx_o = selected_idx;
    data_o = data_i[selected_idx];
    gnt_o = '0;
    if ({grant_condition}) begin
      gnt_o[selected_idx] = 1'b1;
    end

    next_found = 1'b0;
    next_idx = rr_q;
    for (int next_candidate = 0; next_candidate < NumIn; next_candidate++) begin
      if (
        !next_found &&
        next_candidate > int'(rr_q) &&
        req_d[next_candidate]
      ) begin
        next_idx = IdxWidth'(next_candidate);
        next_found = 1'b1;
      end
    end
    for (int next_candidate = 0; next_candidate < NumIn; next_candidate++) begin
      if (
        !next_found &&
        next_candidate <= int'(rr_q) &&
        req_d[next_candidate]
      ) begin
        next_idx = IdxWidth'(next_candidate);
        next_found = 1'b1;
      end
    end
    rr_d = (gnt_i && req_o) ? next_idx : rr_q;
    lock_d = LockIn && req_o && !gnt_i;
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      rr_q <= '0;
      lock_q <= 1'b0;
      req_q <= '0;
    end else if (flush_i) begin
      rr_q <= '0;
      lock_q <= 1'b0;
      req_q <= '0;
    end else begin
      rr_q <= rr_d;
      lock_q <= lock_d;
      req_q <= req_d;
    end
  end
endmodule
"""


def _pulp_rr_sources(configuration: Mapping[str, Any]) -> tuple[str, str]:
    num_in = int(configuration["NumIn"])
    data_width = int(configuration["DataWidth"])
    idx_width = max(1, (num_in - 1).bit_length())
    baseline = f"""\
module rtl_advisor_family_top (
  input logic clk_i,
  input logic rst_ni,
  input logic flush_i,
  input logic [{num_in - 1}:0] req_i,
  output logic [{num_in - 1}:0] gnt_o,
  input logic [{num_in - 1}:0][{data_width - 1}:0] data_i,
  output logic req_o,
  input logic gnt_i,
  output logic [{data_width - 1}:0] data_o,
  output logic [{idx_width - 1}:0] idx_o
);
  rr_arb_tree #(
    .NumIn({num_in}), .DataWidth({data_width}), .ExtPrio(1'b0),
    .AxiVldRdy(1'b0), .LockIn(1'b1), .FairArb(1'b1)
  ) dut (
    .clk_i, .rst_ni, .flush_i, .rr_i('0), .req_i, .gnt_o, .data_i,
    .req_o, .gnt_i, .data_o, .idx_o
  );
endmodule
"""
    candidate = _pulp_rr_alt_module(
        num_in=num_in,
        data_width=data_width,
        axi_valid_ready=0,
        lock_in=1,
    ) + f"""\

module rtl_advisor_family_top (
  input logic clk_i,
  input logic rst_ni,
  input logic flush_i,
  input logic [{num_in - 1}:0] req_i,
  output logic [{num_in - 1}:0] gnt_o,
  input logic [{num_in - 1}:0][{data_width - 1}:0] data_i,
  output logic req_o,
  input logic gnt_i,
  output logic [{data_width - 1}:0] data_o,
  output logic [{idx_width - 1}:0] idx_o
);
  rtl_advisor_pulp_rr_alt dut (
    .clk_i, .rst_ni, .flush_i, .req_i, .gnt_o, .data_i,
    .req_o, .gnt_i, .data_o, .idx_o
  );
endmodule
"""
    return baseline, candidate


def _pulp_stream_sources(configuration: Mapping[str, Any]) -> tuple[str, str]:
    num_in = int(configuration["N_INP"])
    data_width = int(configuration["DATA_WIDTH"])
    baseline = f"""\
module rtl_advisor_family_top (
  input logic clk_i,
  input logic rst_ni,
  input logic flush_i,
  input logic [{num_in - 1}:0][{data_width - 1}:0] inp_data_i,
  input logic [{num_in - 1}:0] inp_valid_i,
  output logic [{num_in - 1}:0] inp_ready_o,
  output logic [{data_width - 1}:0] oup_data_o,
  output logic oup_valid_o,
  input logic oup_ready_i
);
  stream_arbiter_flushable #(
    .DATA_T(logic [{data_width - 1}:0]), .N_INP({num_in}), .ARBITER("rr")
  ) dut (
    .clk_i, .rst_ni, .flush_i, .inp_data_i, .inp_valid_i,
    .inp_ready_o, .oup_data_o, .oup_valid_o, .oup_ready_i
  );
endmodule
"""
    candidate = _pulp_rr_alt_module(
        num_in=num_in,
        data_width=data_width,
        axi_valid_ready=1,
        lock_in=1,
        module_name="rtl_advisor_pulp_stream_rr_alt",
    ) + f"""\

module rtl_advisor_family_top (
  input logic clk_i,
  input logic rst_ni,
  input logic flush_i,
  input logic [{num_in - 1}:0][{data_width - 1}:0] inp_data_i,
  input logic [{num_in - 1}:0] inp_valid_i,
  output logic [{num_in - 1}:0] inp_ready_o,
  output logic [{data_width - 1}:0] oup_data_o,
  output logic oup_valid_o,
  input logic oup_ready_i
);
  logic [{max(1, (num_in - 1).bit_length()) - 1}:0] unused_idx;
  rtl_advisor_pulp_stream_rr_alt dut (
    .clk_i, .rst_ni, .flush_i, .req_i(inp_valid_i),
    .gnt_o(inp_ready_o), .data_i(inp_data_i), .req_o(oup_valid_o),
    .gnt_i(oup_ready_i), .data_o(oup_data_o), .idx_o(unused_idx)
  );
endmodule
"""
    return baseline, candidate


def _verilog_axis_arbiter_alt_module(*, ports: int) -> str:
    return f"""\
module rtl_advisor_axis_arbiter_alt #(
  parameter integer PORTS = {ports},
  parameter ARB_TYPE_ROUND_ROBIN = 1,
  parameter ARB_BLOCK = 1,
  parameter ARB_BLOCK_ACK = 1,
  parameter ARB_LSB_HIGH_PRIORITY = 0,
  parameter integer INDEX_WIDTH = (PORTS > 1) ? $clog2(PORTS) : 1
) (
  input wire clk,
  input wire rst,
  input wire [PORTS-1:0] request,
  input wire [PORTS-1:0] acknowledge,
  output wire [PORTS-1:0] grant,
  output wire grant_valid,
  output wire [INDEX_WIDTH-1:0] grant_encoded
);
  reg [PORTS-1:0] grant_reg = 0;
  reg [PORTS-1:0] grant_next;
  reg grant_valid_reg = 0;
  reg grant_valid_next;
  reg [INDEX_WIDTH-1:0] grant_encoded_reg = 0;
  reg [INDEX_WIDTH-1:0] grant_encoded_next;
  reg [PORTS-1:0] mask_reg = 0;
  reg [PORTS-1:0] mask_next;
  reg [PORTS-1:0] request_selected;
  reg [PORTS-1:0] masked_selected;
  reg [INDEX_WIDTH-1:0] request_index;
  reg [INDEX_WIDTH-1:0] masked_index;
  reg request_found;
  reg masked_found;
  integer i;

  assign grant = grant_reg;
  assign grant_valid = grant_valid_reg;
  assign grant_encoded = grant_encoded_reg;

  always @* begin
    request_selected = 0;
    masked_selected = 0;
    request_index = 0;
    masked_index = 0;
    request_found = 1'b0;
    masked_found = 1'b0;
    if (ARB_LSB_HIGH_PRIORITY) begin
      for (i = 0; i < PORTS; i = i+1) begin
        if (!request_found && request[i]) begin
          request_selected[i] = 1'b1;
          request_index = i[INDEX_WIDTH-1:0];
          request_found = 1'b1;
        end
        if (!masked_found && request[i] && mask_reg[i]) begin
          masked_selected[i] = 1'b1;
          masked_index = i[INDEX_WIDTH-1:0];
          masked_found = 1'b1;
        end
      end
    end else begin
      for (i = PORTS-1; i >= 0; i = i-1) begin
        if (!request_found && request[i]) begin
          request_selected[i] = 1'b1;
          request_index = i[INDEX_WIDTH-1:0];
          request_found = 1'b1;
        end
        if (!masked_found && request[i] && mask_reg[i]) begin
          masked_selected[i] = 1'b1;
          masked_index = i[INDEX_WIDTH-1:0];
          masked_found = 1'b1;
        end
      end
    end

    grant_next = 0;
    grant_valid_next = 0;
    grant_encoded_next = 0;
    mask_next = mask_reg;
    if (ARB_BLOCK && !ARB_BLOCK_ACK && |(grant_reg & request)) begin
      grant_next = grant_reg;
      grant_valid_next = grant_valid_reg;
      grant_encoded_next = grant_encoded_reg;
    end else if (
      ARB_BLOCK && ARB_BLOCK_ACK && grant_valid_reg &&
      !(|(grant_reg & acknowledge))
    ) begin
      grant_next = grant_reg;
      grant_valid_next = grant_valid_reg;
      grant_encoded_next = grant_encoded_reg;
    end else if (request_found) begin
      grant_valid_next = 1'b1;
      if (ARB_TYPE_ROUND_ROBIN && masked_found) begin
        grant_next = masked_selected;
        grant_encoded_next = masked_index;
        if (ARB_LSB_HIGH_PRIORITY)
          mask_next = {{PORTS{{1'b1}}}} << (integer'(masked_index)+1);
        else
          mask_next = {{PORTS{{1'b1}}}} >> (PORTS-integer'(masked_index));
      end else begin
        grant_next = request_selected;
        grant_encoded_next = request_index;
        if (ARB_TYPE_ROUND_ROBIN) begin
          if (ARB_LSB_HIGH_PRIORITY)
            mask_next = {{PORTS{{1'b1}}}} << (integer'(request_index)+1);
          else
            mask_next = {{PORTS{{1'b1}}}} >> (PORTS-integer'(request_index));
        end
      end
    end
  end

  always @(posedge clk) begin
    grant_reg <= grant_next;
    grant_valid_reg <= grant_valid_next;
    grant_encoded_reg <= grant_encoded_next;
    mask_reg <= mask_next;
    if (rst) begin
      grant_reg <= 0;
      grant_valid_reg <= 0;
      grant_encoded_reg <= 0;
      mask_reg <= 0;
    end
  end
endmodule
"""


def _verilog_axis_arbiter_sources(
    configuration: Mapping[str, Any],
) -> tuple[str, str]:
    ports = int(configuration["PORTS"])
    index_width = max(1, (ports - 1).bit_length())
    baseline = f"""\
module rtl_advisor_family_top (
  input wire clk,
  input wire rst,
  input wire [{ports - 1}:0] request,
  input wire [{ports - 1}:0] acknowledge,
  output wire [{ports - 1}:0] grant,
  output wire grant_valid,
  output wire [{index_width - 1}:0] grant_encoded
);
  arbiter #(
    .PORTS({ports}), .ARB_TYPE_ROUND_ROBIN(1), .ARB_BLOCK(1),
    .ARB_BLOCK_ACK(1), .ARB_LSB_HIGH_PRIORITY(0)
  ) dut (
    .clk, .rst, .request, .acknowledge, .grant, .grant_valid,
    .grant_encoded
  );
endmodule
"""
    candidate = _verilog_axis_arbiter_alt_module(ports=ports) + f"""\
module rtl_advisor_family_top (
  input wire clk,
  input wire rst,
  input wire [{ports - 1}:0] request,
  input wire [{ports - 1}:0] acknowledge,
  output wire [{ports - 1}:0] grant,
  output wire grant_valid,
  output wire [{index_width - 1}:0] grant_encoded
);
  rtl_advisor_axis_arbiter_alt #(.PORTS({ports})) dut (
    .clk, .rst, .request, .acknowledge, .grant, .grant_valid,
    .grant_encoded
  );
endmodule
"""
    return baseline, candidate


def _verilog_axis_mux_wrapper(
    *,
    module_name: str,
    ports: int,
    data_width: int,
) -> str:
    keep_width = (data_width + 7) // 8
    id_width = 8
    dest_width = 8
    user_width = 1
    output_id_width = id_width + max(1, (ports - 1).bit_length())
    return f"""\
module rtl_advisor_family_top (
  input wire clk,
  input wire rst,
  input wire [{ports * data_width - 1}:0] s_axis_tdata,
  input wire [{ports * keep_width - 1}:0] s_axis_tkeep,
  input wire [{ports - 1}:0] s_axis_tvalid,
  output wire [{ports - 1}:0] s_axis_tready,
  input wire [{ports - 1}:0] s_axis_tlast,
  input wire [{ports * id_width - 1}:0] s_axis_tid,
  input wire [{ports * dest_width - 1}:0] s_axis_tdest,
  input wire [{ports * user_width - 1}:0] s_axis_tuser,
  output wire [{data_width - 1}:0] m_axis_tdata,
  output wire [{keep_width - 1}:0] m_axis_tkeep,
  output wire m_axis_tvalid,
  input wire m_axis_tready,
  output wire m_axis_tlast,
  output wire [{output_id_width - 1}:0] m_axis_tid,
  output wire [{dest_width - 1}:0] m_axis_tdest,
  output wire [{user_width - 1}:0] m_axis_tuser
);
  {module_name} #(
    .S_COUNT({ports}), .DATA_WIDTH({data_width}),
    .KEEP_ENABLE(1), .KEEP_WIDTH({keep_width}),
    .ID_ENABLE(0), .S_ID_WIDTH({id_width}),
    .M_ID_WIDTH({output_id_width}), .DEST_ENABLE(0),
    .DEST_WIDTH({dest_width}), .USER_ENABLE(1),
    .USER_WIDTH({user_width}), .LAST_ENABLE(1), .UPDATE_TID(0),
    .ARB_TYPE_ROUND_ROBIN(1), .ARB_LSB_HIGH_PRIORITY(1)
  ) dut (
    .clk, .rst, .s_axis_tdata, .s_axis_tkeep, .s_axis_tvalid,
    .s_axis_tready, .s_axis_tlast, .s_axis_tid, .s_axis_tdest,
    .s_axis_tuser, .m_axis_tdata, .m_axis_tkeep, .m_axis_tvalid,
    .m_axis_tready, .m_axis_tlast, .m_axis_tid, .m_axis_tdest,
    .m_axis_tuser
  );
endmodule
"""


def _verilog_axis_mux_sources(
    configuration: Mapping[str, Any],
    source_root: Path,
) -> tuple[str, str]:
    ports = int(configuration["S_COUNT"])
    data_width = int(configuration["DATA_WIDTH"])
    upstream_path = source_root / "rtl/axis_arb_mux.v"
    try:
        upstream = upstream_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArbiterFamilyExecutionError(
            f"cannot read pinned axis_arb_mux source: {exc}",
            code="missing_candidate_source",
        ) from exc
    renamed = upstream.replace(
        "module axis_arb_mux #",
        "module rtl_advisor_axis_arb_mux_alt #",
        1,
    ).replace(
        "arbiter #(",
        "rtl_advisor_axis_arbiter_alt #(",
        1,
    )
    original_mux = (
        "wire [DATA_WIDTH-1:0] current_s_tdata  = "
        "s_axis_tdata_reg[grant_encoded*DATA_WIDTH +: DATA_WIDTH];"
    )
    balanced_mux = """\
reg [DATA_WIDTH-1:0] current_s_tdata;
integer rtl_advisor_data_port;
always @* begin
    current_s_tdata =
        s_axis_tdata_reg[grant_encoded*DATA_WIDTH +: DATA_WIDTH];
    if (grant_valid) begin
        current_s_tdata = {DATA_WIDTH{1'b0}};
        for (
            rtl_advisor_data_port = 0;
            rtl_advisor_data_port < S_COUNT;
            rtl_advisor_data_port = rtl_advisor_data_port + 1
        ) begin
            if (grant[rtl_advisor_data_port])
                current_s_tdata = current_s_tdata |
                    s_axis_tdata_reg[
                        rtl_advisor_data_port*DATA_WIDTH +: DATA_WIDTH
                    ];
        end
    end
end"""
    if original_mux not in renamed:
        raise ArbiterFamilyExecutionError(
            "pinned axis_arb_mux payload selection no longer matches the "
            "registered candidate transformation",
            code="stale_candidate_transform",
        )
    renamed = renamed.replace(original_mux, balanced_mux, 1)
    if renamed == upstream:
        raise ArbiterFamilyExecutionError(
            "axis_arb_mux candidate is not structurally distinct",
            code="candidate_not_distinct",
        )
    baseline = _verilog_axis_mux_wrapper(
        module_name="axis_arb_mux",
        ports=ports,
        data_width=data_width,
    )
    candidate = (
        _verilog_axis_arbiter_alt_module(ports=ports)
        + "\n"
        + renamed
        + "\n"
        + _verilog_axis_mux_wrapper(
            module_name="rtl_advisor_axis_arb_mux_alt",
            ports=ports,
            data_width=data_width,
        )
    )
    return baseline, candidate


def _basejump_round_robin_sources(
    configuration: Mapping[str, Any],
) -> tuple[str, str]:
    width = int(configuration["width_p"])
    index_width = max(1, (width - 1).bit_length())
    baseline = f"""\
module rtl_advisor_family_top (
  input logic clk_i,
  input logic reset_i,
  input logic [{width - 1}:0] reqs_i,
  output logic [{width - 1}:0] grants_o,
  input logic yumi_i
);
  bsg_arb_round_robin #(.width_p({width})) dut (
    .clk_i, .reset_i, .reqs_i, .grants_o, .yumi_i
  );
endmodule
"""
    candidate = f"""\
module rtl_advisor_bsg_rr_rotated #(
  parameter int width_p = {width},
  parameter int index_width_lp = (width_p > 1) ? $clog2(width_p) : 1
) (
  input logic clk_i,
  input logic reset_i,
  input logic [width_p-1:0] reqs_i,
  output logic [width_p-1:0] grants_o,
  input logic yumi_i
);
  logic [index_width_lp-1:0] start_q, selected_index;
  logic found;
  integer offset;
  integer candidate_index;

  always_comb begin
    grants_o = '0;
    selected_index = start_q;
    found = 1'b0;
    for (offset = 0; offset < width_p; offset = offset+1) begin
      candidate_index = integer'(start_q) - offset;
      if (candidate_index < 0) candidate_index = candidate_index + width_p;
      if (!found && reqs_i[candidate_index]) begin
        grants_o[candidate_index] = 1'b1;
        selected_index = index_width_lp'(candidate_index);
        found = 1'b1;
      end
    end
  end

  always_ff @(posedge clk_i) begin
    if (reset_i)
      start_q <= index_width_lp'(width_p-1);
    else if (yumi_i) begin
      if (!found || selected_index == 0)
        start_q <= index_width_lp'(width_p-1);
      else
        start_q <= selected_index - 1'b1;
    end
  end
endmodule

module rtl_advisor_family_top (
  input logic clk_i,
  input logic reset_i,
  input logic [{width - 1}:0] reqs_i,
  output logic [{width - 1}:0] grants_o,
  input logic yumi_i
);
  rtl_advisor_bsg_rr_rotated #(.width_p({width})) dut (
    .clk_i, .reset_i, .reqs_i, .grants_o, .yumi_i
  );
endmodule
"""
    return baseline, candidate


def _basejump_locking_fixed_sources(
    configuration: Mapping[str, Any],
) -> tuple[str, str]:
    width = int(configuration["width_p"])
    lo_to_hi = int(configuration["lo_to_hi_p"])
    baseline = f"""\
module rtl_advisor_family_top (
  input logic clk_i,
  input logic ready_then_i,
  input logic unlock_i,
  input logic [{width - 1}:0] reqs_i,
  output logic [{width - 1}:0] grants_o
);
  bsg_locking_arb_fixed #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) dut (
    .clk_i, .ready_then_i, .unlock_i, .reqs_i, .grants_o
  );
endmodule
"""
    candidate = f"""\
module rtl_advisor_bsg_locking_fixed_alt #(
  parameter int inputs_p = {width},
  parameter bit lo_to_hi_p = {lo_to_hi}
) (
  input logic clk_i,
  input logic ready_then_i,
  input logic unlock_i,
  input logic [inputs_p-1:0] reqs_i,
  output logic [inputs_p-1:0] grants_o
);
  logic [inputs_p-1:0] not_req_mask_q;
  logic [inputs_p-1:0] req_mask;
  logic found;
  integer i;

  assign req_mask = ~not_req_mask_q;
  always_comb begin
    grants_o = '0;
    found = 1'b0;
    if (lo_to_hi_p) begin
      for (i = 0; i < inputs_p; i = i+1) begin
        if (!found && reqs_i[i] && req_mask[i]) begin
          grants_o[i] = ready_then_i;
          found = 1'b1;
        end
      end
    end else begin
      for (i = inputs_p-1; i >= 0; i = i-1) begin
        if (!found && reqs_i[i] && req_mask[i]) begin
          grants_o[i] = ready_then_i;
          found = 1'b1;
        end
      end
    end
  end

  always_ff @(posedge clk_i) begin
    if (unlock_i)
      not_req_mask_q <= '0;
    else if ((&req_mask) && (|grants_o))
      not_req_mask_q <= ~grants_o;
  end
endmodule

module rtl_advisor_family_top (
  input logic clk_i,
  input logic ready_then_i,
  input logic unlock_i,
  input logic [{width - 1}:0] reqs_i,
  output logic [{width - 1}:0] grants_o
);
  rtl_advisor_bsg_locking_fixed_alt #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) dut (
    .clk_i, .ready_then_i, .unlock_i, .reqs_i, .grants_o
  );
endmodule
"""
    return baseline, candidate


def _basejump_n_to_1_wrapper(
    *,
    width: int,
    data_width: int,
    use_scan: int,
) -> str:
    tag_width = max(1, (width - 1).bit_length())
    return f"""\
module rtl_advisor_family_top (
  input logic clk_i,
  input logic reset_i,
  input logic [{width - 1}:0][{data_width - 1}:0] data_i,
  input logic [{width - 1}:0] v_i,
  output logic [{width - 1}:0] yumi_o,
  output logic v_o,
  output logic [{data_width - 1}:0] data_o,
  output logic [{tag_width - 1}:0] tag_o,
  input logic yumi_i
);
  bsg_round_robin_n_to_1 #(
    .width_p({data_width}), .num_in_p({width}), .strict_p(0),
    .use_scan_p({use_scan}), .tag_width_lp({tag_width})
  ) dut (
    .clk_i, .reset_i, .data_i, .v_i, .yumi_o, .v_o, .data_o,
    .tag_o, .yumi_i
  );
endmodule
"""


def _basejump_n_to_1_candidate(*, width: int, data_width: int) -> str:
    tag_width = max(1, (width - 1).bit_length())
    return f"""\
module rtl_advisor_bsg_n_to1_scan_alt (
  input logic clk_i,
  input logic reset_i,
  input logic [{width - 1}:0][{data_width - 1}:0] data_i,
  input logic [{width - 1}:0] v_i,
  output logic [{width - 1}:0] yumi_o,
  output logic v_o,
  output logic [{data_width - 1}:0] data_o,
  output logic [{tag_width - 1}:0] tag_o,
  input logic yumi_i
);
  logic [{width - 1}:0][{data_width - 1}:0] scan_data;
  logic [{width - 1}:0] scan_valid, scan_yumi;
  logic [{tag_width - 1}:0] scan_tag;
  genvar port_index;
  for (port_index = 0; port_index < {width}; port_index++) begin: permute
    localparam int original_index = ({width}-port_index) % {width};
    assign scan_valid[port_index] = v_i[original_index];
    assign scan_data[port_index] = data_i[original_index];
    assign yumi_o[original_index] = scan_yumi[port_index];
  end
  bsg_round_robin_n_to_1 #(
    .width_p({data_width}), .num_in_p({width}), .strict_p(0),
    .use_scan_p(1), .tag_width_lp({tag_width})
  ) dut (
    .clk_i, .reset_i, .data_i(scan_data), .v_i(scan_valid),
    .yumi_o(scan_yumi), .v_o, .data_o, .tag_o(scan_tag),
    .yumi_i(yumi_i & (|v_i))
  );
  assign tag_o = -scan_tag;
endmodule

module rtl_advisor_family_top (
  input logic clk_i,
  input logic reset_i,
  input logic [{width - 1}:0][{data_width - 1}:0] data_i,
  input logic [{width - 1}:0] v_i,
  output logic [{width - 1}:0] yumi_o,
  output logic v_o,
  output logic [{data_width - 1}:0] data_o,
  output logic [{tag_width - 1}:0] tag_o,
  input logic yumi_i
);
  rtl_advisor_bsg_n_to1_scan_alt dut (
    .clk_i, .reset_i, .data_i, .v_i, .yumi_o, .v_o, .data_o,
    .tag_o, .yumi_i
  );
endmodule
"""


def _basejump_n_to_1_sources(
    configuration: Mapping[str, Any],
) -> tuple[str, str]:
    width = int(configuration["num_in_p"])
    data_width = int(configuration["width_p"])
    return (
        _basejump_n_to_1_wrapper(
            width=width,
            data_width=data_width,
            use_scan=0,
        ),
        _basejump_n_to_1_candidate(
            width=width,
            data_width=data_width,
        ),
    )


def _candidate_sources(
    reference_id: str,
    configuration: Mapping[str, Any],
    *,
    source_root: Path,
) -> tuple[str, str]:
    if reference_id == "opentitan-prim-arbiter-fixed":
        return _opentitan_fixed_sources(configuration)
    if reference_id == "basejump-bsg-arb-fixed":
        return _basejump_fixed_sources(configuration)
    if reference_id == "pulp-common-cells-rr-arb-tree":
        return _pulp_rr_sources(configuration)
    if reference_id == "pulp-common-cells-stream-arbiter-flushable":
        return _pulp_stream_sources(configuration)
    if reference_id == "verilog-axis-arbiter":
        return _verilog_axis_arbiter_sources(configuration)
    if reference_id == "verilog-axis-axis-arb-mux":
        return _verilog_axis_mux_sources(configuration, source_root)
    if reference_id == "basejump-bsg-arb-round-robin":
        return _basejump_round_robin_sources(configuration)
    if reference_id == "basejump-bsg-locking-arb-fixed":
        return _basejump_locking_fixed_sources(configuration)
    if reference_id == "basejump-bsg-round-robin-n-to-1":
        return _basejump_n_to_1_sources(configuration)
    raise ArbiterFamilyExecutionError(
        f"{reference_id} is frozen but its reviewed P2 candidate has not "
        "completed source/formal qualification",
        code="candidate_qualification_pending",
    )


def candidate_source_status(reference_id: str) -> str:
    if reference_id in {
        "opentitan-prim-arbiter-fixed",
        "basejump-bsg-arb-fixed",
        "pulp-common-cells-rr-arb-tree",
        "pulp-common-cells-stream-arbiter-flushable",
        "verilog-axis-arbiter",
        "verilog-axis-axis-arb-mux",
        "basejump-bsg-arb-round-robin",
        "basejump-bsg-locking-arb-fixed",
        "basejump-bsg-round-robin-n-to-1",
    }:
        return "implemented"
    return "formal_qualification_pending"


def prepare_family_candidate(
    config: ProjectConfig,
    reference_input: Mapping[str, Any],
    finding: Mapping[str, Any],
    artifact_root: str | Path,
    spec: ExecutorSpec,
    *,
    executor_registry_hash: str,
) -> dict[str, Any]:
    validate_family_reference_input(
        reference_input,
        spec,
        executor_registry_hash=executor_registry_hash,
    )
    if finding.get("reference_id") not in spec.reference_ids:
        raise ArbiterFamilyExecutionError(
            "finding does not belong to the selected executor",
            code="invalid_finding",
        )
    configuration = finding.get("configuration")
    if not isinstance(configuration, Mapping) or dict(configuration) not in [
        dict(item) for item in _CONFIGURATIONS[str(finding["reference_id"])]
    ]:
        raise ArbiterFamilyExecutionError(
            "finding uses an unfrozen configuration",
            code="unsupported_configuration",
        )
    source_root = Path(str(reference_input["source_root"]))
    baseline_source, candidate_source = _candidate_sources(
        str(finding["reference_id"]),
        configuration,
        source_root=source_root,
    )
    if stable_hash({"source": baseline_source}) == stable_hash(
        {"source": candidate_source}
    ):
        raise ArbiterFamilyExecutionError(
            "candidate is not structurally distinct",
            code="candidate_not_distinct",
        )
    proof_level = spec.proof_levels[0]
    p2_assumptions = [
        "reset asserted initially, released after one clock, and never reasserted",
        "requests and payloads remain stable while blocked when required",
        "ready, acknowledge, or yumi inputs are otherwise unconstrained",
    ]
    if finding["reference_id"] in {
        "pulp-common-cells-rr-arb-tree",
        "pulp-common-cells-stream-arbiter-flushable",
    }:
        p2_assumptions.append("flush may assert under arbitrary legal traffic")
    if finding["reference_id"] == "basejump-bsg-locking-arb-fixed":
        p2_assumptions = [
            "unlock initializes both state machines before comparison",
            "later unlock assertions remain unconstrained and are compared",
            "request and ready inputs remain unconstrained",
        ]
    reference_id = str(finding["reference_id"])
    if reference_id.startswith("verilog-axis-"):
        reset_contract = {
            "clock": "clk",
            "signal": "rst",
            "sequence": (
                "asserted at formal time zero, released after one clock, "
                "and never reasserted"
            ),
        }
    elif reference_id == "basejump-bsg-locking-arb-fixed":
        reset_contract = {
            "clock": "clk_i",
            "signal": "unlock_i",
            "sequence": (
                "asserted at formal time zero; later operational unlock "
                "assertions remain unconstrained"
            ),
        }
    else:
        reset_contract = {
            "clock": "clk_i",
            "signal": (
                "rst_ni"
                if reference_id.startswith("pulp-common-cells-")
                else "reset_i"
            ),
            "sequence": (
                "asserted at formal time zero, released after one clock, "
                "and never reasserted"
            ),
        }
    proof_contract = {
        "schema": "rtl-advisor-proof-v1",
        "level": proof_level,
        "kind": (
            "combinational_rtl_equivalence"
            if proof_level == "P1"
            else "cycle_aligned_sequential_equivalence"
        ),
        "latency_relation": "combinational" if proof_level == "P1" else "same_cycle",
        **({"reset": reset_contract} if proof_level == "P2" else {}),
        "assumptions": (
            []
            if proof_level == "P1"
            else p2_assumptions
        ),
        "observables": [
            "all grants and handshake outputs every cycle",
            "index and payload whenever semantically valid",
        ],
    }
    core = {
        "finding_id": finding["finding_id"],
        "reference_manifest_hash": reference_input["manifest_semantic_hash"],
        "configuration": dict(configuration),
        "executor_id": spec.executor_id,
        "executor_version": spec.version,
        "proof_contract_hash": stable_hash(proof_contract),
    }
    candidate_id = f"arbcand_{stable_hash(core)[:16]}"
    candidate_dir = Path(artifact_root).expanduser().resolve() / candidate_id
    record_path = candidate_dir / "candidate-core.json"
    if record_path.is_file():
        cached = read_hashed_json(
            record_path,
            document_type=CANDIDATE_DOCUMENT_TYPE,
            schema_version=RUN_SCHEMA_VERSION,
        )
        validate_family_candidate(
            cached,
            spec,
            executor_registry_hash=executor_registry_hash,
        )
        return cached
    baseline_root = candidate_dir / "baseline"
    alternative_root = candidate_dir / "candidate"
    baseline_root.mkdir(parents=True, exist_ok=True)
    alternative_root.mkdir(parents=True, exist_ok=True)
    baseline_wrapper = baseline_root / "rtl_advisor_family_top.sv"
    candidate_wrapper = alternative_root / "rtl_advisor_family_top.sv"
    baseline_wrapper.write_text(baseline_source, encoding="utf-8")
    candidate_wrapper.write_text(candidate_source, encoding="utf-8")

    shared_sources = [
        source_root / str(relative)
        for relative in reference_input["source_hashes"]
    ]
    compile_context = reference_input.get("compile_context") or {}
    if finding["reference_id"] == "opentitan-prim-arbiter-fixed":
        # The upstream include directory contains the complete OpenTitan
        # primitive library. Hashing that unrelated tree makes this one-module
        # study both slow and over-broad, so freeze only the exact include
        # closure used by prim_arbiter_fixed in the isolated workspace.
        qualified_include = candidate_dir / "qualified-include"
        qualified_include.mkdir(parents=True, exist_ok=True)
        upstream_include = source_root / "hw/ip/prim/rtl"
        for include_name in (
            "prim_assert.sv",
            "prim_assert_dummy_macros.svh",
            "prim_assert_sec_cm.svh",
            "prim_flop_macros.sv",
        ):
            shutil.copy2(
                upstream_include / include_name,
                qualified_include / include_name,
            )
        include_dirs = [qualified_include]
    else:
        include_dirs = [
            source_root / str(relative)
            for relative in compile_context.get("include_dirs", [])
        ]
    defines = tuple(str(item) for item in compile_context.get("defines", []))
    baseline = normalize_design_input(
        top="rtl_advisor_family_top",
        files=(*shared_sources, baseline_wrapper),
        include_dirs=include_dirs,
        defines=defines,
        base=config.root,
    )
    candidate_design = normalize_design_input(
        top="rtl_advisor_family_top",
        files=(*shared_sources, candidate_wrapper),
        include_dirs=include_dirs,
        defines=defines,
        base=config.root,
    )
    diff = "".join(
        difflib.unified_diff(
            baseline_source.splitlines(keepends=True),
            candidate_source.splitlines(keepends=True),
            fromfile="reference/rtl_advisor_family_top.sv",
            tofile="candidate/rtl_advisor_family_top.sv",
        )
    )
    diff_path = candidate_dir / "candidate.diff"
    diff_path.write_text(diff, encoding="utf-8")
    record = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": RUN_SCHEMA_ID,
        "document_type": CANDIDATE_DOCUMENT_TYPE,
        "status": "candidate_prepared",
        "candidate_id": candidate_id,
        "finding_id": finding["finding_id"],
        "transformation_id": ARBITER_TRANSFORMATION_ID,
        "transformation_version": DEFAULT_TRANSFORMATION_REGISTRY.get(
            ARBITER_TRANSFORMATION_ID
        ).version,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": spec.executor_id,
        "executor_version": spec.version,
        "executor_registry_hash": executor_registry_hash,
        "candidate_origin": spec.candidate_origin,
        "reference_id": finding["reference_id"],
        "reference_manifest_hash": reference_input["manifest_semantic_hash"],
        "variant_id": finding["variant_id"],
        "configuration": dict(configuration),
        "configuration_id": configuration["configuration_id"],
        "proof_contract": proof_contract,
        "proof_contract_hash": stable_hash(proof_contract),
        "measurement_levels": finding["measurement_levels"],
        "has_payload": finding["reference_id"] in _HAS_PAYLOAD,
        "baseline_design": _design_mapping(baseline),
        "candidate_design": _design_mapping(candidate_design),
        "baseline_compile_context": compile_context_snapshot(baseline),
        "candidate_compile_context": compile_context_snapshot(candidate_design),
        "finding": dict(finding),
        "artifact_dir": str(candidate_dir),
        "record_path": str(record_path),
        "diff_path": str(diff_path),
        "diff_sha256": file_sha256(diff_path),
        "source_integrity": {
            "reference": source_integrity(
                {
                    "path": str(source_root / relative),
                    "sha256": digest,
                }
                for relative, digest in reference_input["source_hashes"].items()
            ),
            "baseline": source_integrity(asdict(item) for item in baseline.files),
            "candidate": source_integrity(
                asdict(item) for item in candidate_design.files
            ),
        },
        "lint": {"status": "not_run"},
        "formal": {"status": "not_run", "safe": False},
        "limitations": [
            "The isolated candidate is not safe until its declared proof passes.",
            "Measurements remain limited to the pinned open-source flows.",
        ],
    }
    return write_hashed_json(record_path, record, exclusive=True)


def validate_family_candidate(
    candidate: Mapping[str, Any],
    spec: ExecutorSpec,
    *,
    executor_registry_hash: str,
) -> None:
    if candidate.get("executor_id") != spec.executor_id:
        raise ArbiterFamilyExecutionError(
            "candidate belongs to another executor",
            code="missing_executor",
        )
    if candidate.get("executor_version") != spec.version:
        raise ArbiterFamilyExecutionError(
            "candidate executor version is stale",
            code="stale_executor_version",
        )
    if candidate.get("executor_registry_hash") != executor_registry_hash:
        raise ArbiterFamilyExecutionError(
            "candidate executor registry is stale",
            code="stale_executor_registry",
        )
    try:
        DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(candidate)
        candidate_design_from_record(candidate)
    except Exception as exc:
        raise ArbiterFamilyExecutionError(
            str(exc),
            code=getattr(exc, "code", "invalid_candidate"),
        ) from exc
    diff_path = Path(str(candidate.get("diff_path", ""))).resolve()
    candidate_dir = Path(str(candidate.get("artifact_dir", ""))).resolve()
    try:
        diff_path.relative_to(candidate_dir)
    except ValueError as exc:
        raise ArbiterFamilyExecutionError(
            "candidate diff escapes the isolated workspace",
            code="unsafe_path",
        ) from exc
    if not diff_path.is_file() or file_sha256(diff_path) != candidate.get(
        "diff_sha256"
    ):
        raise ArbiterFamilyExecutionError(
            "candidate diff changed after preparation",
            code="stale_candidate",
        )


def verify_family_candidate(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    artifact_root: str | Path,
    spec: ExecutorSpec,
) -> dict[str, Any]:
    validate_family_candidate(
        candidate,
        spec,
        executor_registry_hash=str(candidate.get("executor_registry_hash")),
    )
    if spec.proof_levels == ("P1",):
        if candidate.get("reference_id") in {
            "opentitan-prim-arbiter-fixed",
            "basejump-bsg-arb-fixed",
        }:
            return _verify_family_p1_candidate(
                config,
                candidate,
                artifact_root,
            )
        try:
            return verify_addition_candidate(config, candidate, artifact_root)
        except MVPRewriteError as exc:
            raise ArbiterFamilyExecutionError(str(exc), code=exc.code) from exc
    if candidate.get("reference_id") not in {
        "pulp-common-cells-rr-arb-tree",
        "pulp-common-cells-stream-arbiter-flushable",
        "verilog-axis-arbiter",
        "verilog-axis-axis-arb-mux",
        "basejump-bsg-arb-round-robin",
        "basejump-bsg-locking-arb-fixed",
        "basejump-bsg-round-robin-n-to-1",
    }:
        raise ArbiterFamilyExecutionError(
            "the P2 source is pre-registered but its pair-specific miter has "
            "not completed formal qualification",
            code="formal_qualification_pending",
        )
    return _verify_pulp_p2_candidate(config, candidate, artifact_root)


def _project_relative(config: ProjectConfig, path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(config.root.resolve()).as_posix()
    except ValueError as exc:
        raise ArbiterFamilyExecutionError(
            f"formal input must stay inside the project workspace: {resolved}",
            code="unsafe_path",
        ) from exc


def _reversed_signal(signal: str, width: int) -> str:
    return "{" + ", ".join(f"{signal}[{index}]" for index in range(width)) + "}"


def _pulp_rr_miter(
    configuration: Mapping[str, Any],
    *,
    stream: bool,
) -> str:
    if stream:
        num_in = int(configuration["N_INP"])
        data_width = int(configuration["DATA_WIDTH"])
        return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input logic clk_i,
  input logic flush_i,
  input logic [{num_in - 1}:0] inp_valid_i,
  input logic [{num_in - 1}:0][{data_width - 1}:0] inp_data_i,
  input logic oup_ready_i
);
  localparam int Mutation = `MUTATION_KIND;
  logic started = 1'b0;
  wire rst_ni = started;
  logic [{num_in - 1}:0] gold_ready, gate_ready_raw;
  logic gold_valid, gate_valid;
  logic [{data_width - 1}:0] gold_data, gate_data_raw;

  stream_arbiter_flushable #(
    .DATA_T(logic [{data_width - 1}:0]), .N_INP({num_in}), .ARBITER("rr")
  ) gold (
    .clk_i, .rst_ni, .flush_i, .inp_data_i, .inp_valid_i,
    .inp_ready_o(gold_ready), .oup_data_o(gold_data),
    .oup_valid_o(gold_valid), .oup_ready_i
  );
  logic [{max(1, (num_in - 1).bit_length()) - 1}:0] unused_idx;
  wire gate_rst_ni = Mutation == 3 ? 1'b1 : rst_ni;
  wire [{num_in - 1}:0] gate_valid_input =
      Mutation == 1 ? {_reversed_signal("inp_valid_i", num_in)} : inp_valid_i;
  rtl_advisor_pulp_stream_rr_alt gate (
    .clk_i, .rst_ni(gate_rst_ni), .flush_i, .req_i(gate_valid_input),
    .gnt_o(gate_ready_raw), .data_i(inp_data_i), .req_o(gate_valid),
    .gnt_i(oup_ready_i), .data_o(gate_data_raw), .idx_o(unused_idx)
  );
  wire [{num_in - 1}:0] gate_ready =
      Mutation == 2 ? '0 : gate_ready_raw;
  wire [{data_width - 1}:0] gate_data =
      Mutation == 4 && gate_valid
          ? gate_data_raw ^ {{{{({data_width}-1){{1'b0}}}}, 1'b1}}
          : gate_data_raw;

  logic [{num_in - 1}:0] previous_valid, previous_ready;
  logic [{num_in - 1}:0][{data_width - 1}:0] previous_data;
  always_ff @(posedge clk_i) begin
    started <= 1'b1;
    if (started) begin
      for (int i = 0; i < {num_in}; i++) begin
        if (previous_valid[i] && !previous_ready[i]) begin
          assume (inp_valid_i[i]);
          assume (inp_data_i[i] == previous_data[i]);
        end
      end
    end
    previous_valid <= inp_valid_i;
    previous_ready <= gold_ready;
    previous_data <= inp_data_i;
  end
  always_comb if (started) begin
    assert (gold_ready == gate_ready);
    assert (gold_valid == gate_valid);
    if (gold_valid && gate_valid) assert (gold_data == gate_data);
  end
endmodule
"""
    num_in = int(configuration["NumIn"])
    data_width = int(configuration["DataWidth"])
    idx_width = max(1, (num_in - 1).bit_length())
    return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input logic clk_i,
  input logic flush_i,
  input logic [{num_in - 1}:0] req_i,
  input logic [{num_in - 1}:0][{data_width - 1}:0] data_i,
  input logic gnt_i
);
  localparam int Mutation = `MUTATION_KIND;
  logic started = 1'b0;
  wire rst_ni = started;
  logic [{num_in - 1}:0] gold_gnt, gate_gnt_raw;
  logic gold_req, gate_req;
  logic [{data_width - 1}:0] gold_data, gate_data_raw;
  logic [{idx_width - 1}:0] gold_idx, gate_idx;
  rr_arb_tree #(
    .NumIn({num_in}), .DataWidth({data_width}), .ExtPrio(1'b0),
    .AxiVldRdy(1'b0), .LockIn(1'b1), .FairArb(1'b1)
  ) gold (
    .clk_i, .rst_ni, .flush_i, .rr_i('0), .req_i, .gnt_o(gold_gnt),
    .data_i, .req_o(gold_req), .gnt_i, .data_o(gold_data), .idx_o(gold_idx)
  );
  wire gate_rst_ni = Mutation == 3 ? 1'b1 : rst_ni;
  wire [{num_in - 1}:0] gate_req_input =
      Mutation == 1 ? {_reversed_signal("req_i", num_in)} : req_i;
  rtl_advisor_pulp_rr_alt gate (
    .clk_i, .rst_ni(gate_rst_ni), .flush_i, .req_i(gate_req_input),
    .gnt_o(gate_gnt_raw), .data_i, .req_o(gate_req), .gnt_i,
    .data_o(gate_data_raw), .idx_o(gate_idx)
  );
  wire [{num_in - 1}:0] gate_gnt = Mutation == 2 ? '0 : gate_gnt_raw;
  wire [{data_width - 1}:0] gate_data =
      Mutation == 4 && gate_req
          ? gate_data_raw ^ {{{{({data_width}-1){{1'b0}}}}, 1'b1}}
          : gate_data_raw;

  logic [{num_in - 1}:0] previous_req, previous_gnt;
  logic [{num_in - 1}:0][{data_width - 1}:0] previous_data;
  always_ff @(posedge clk_i) begin
    started <= 1'b1;
    if (started) begin
      for (int i = 0; i < {num_in}; i++) begin
        if (previous_req[i] && !previous_gnt[i]) begin
          assume (req_i[i]);
          assume (data_i[i] == previous_data[i]);
        end
      end
    end
    previous_req <= req_i;
    previous_gnt <= gold_gnt;
    previous_data <= data_i;
  end
  always_comb if (started) begin
    assert (gold_gnt == gate_gnt);
    assert (gold_req == gate_req);
    if (gold_req && gate_req) begin
      assert (gold_idx == gate_idx);
      assert (gold_data == gate_data);
    end
  end
endmodule
"""


def _pulp_p2_sby(*, mutation_kind: int) -> str:
    mutation = (
        "" if mutation_kind == 0 else f" -D MUTATION_KIND={mutation_kind}"
    )
    return f"""\
[tasks]
positive

[options]
mode prove
depth 24
expect pass

[engines]
abc pdr

[script]
plugin -i /opt/oss-cad-suite/share/yosys/plugins/slang.so
read_slang --top rtl_advisor_family_p2_miter -D SYNTHESIS -D COMMON_CELLS_ASSERTS_OFF{mutation} -I . cf_math_pkg.sv lzc.sv rr_arb_tree.sv stream_arbiter_flushable.sv rtl_advisor_family_top.sv rtl_advisor_family_p2_miter.sv
prep -top rtl_advisor_family_p2_miter

[files]
"""


def _verilog_axis_arbiter_miter(
    configuration: Mapping[str, Any],
) -> str:
    ports = int(configuration["PORTS"])
    index_width = max(1, (ports - 1).bit_length())
    return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input wire clk,
  input wire [{ports - 1}:0] request,
  input wire [{ports - 1}:0] acknowledge
);
  localparam integer Mutation = `MUTATION_KIND;
  reg started = 1'b0;
  wire rst = !started;
  wire [{ports - 1}:0] gold_grant;
  wire gold_valid;
  wire [{index_width - 1}:0] gold_encoded;
  wire [{ports - 1}:0] gate_grant_raw;
  wire gate_valid;
  wire [{index_width - 1}:0] gate_encoded_raw;
  wire gate_rst = Mutation == 3 ? 1'b0 : rst;
  wire [{ports - 1}:0] gate_request =
      Mutation == 1 ? {_reversed_signal("request", ports)} : request;

  arbiter #(
    .PORTS({ports}), .ARB_TYPE_ROUND_ROBIN(1), .ARB_BLOCK(1),
    .ARB_BLOCK_ACK(1), .ARB_LSB_HIGH_PRIORITY(0)
  ) gold (
    .clk, .rst, .request, .acknowledge, .grant(gold_grant),
    .grant_valid(gold_valid), .grant_encoded(gold_encoded)
  );
  rtl_advisor_axis_arbiter_alt #(.PORTS({ports})) gate (
    .clk, .rst(gate_rst), .request(gate_request), .acknowledge,
    .grant(gate_grant_raw), .grant_valid(gate_valid),
    .grant_encoded(gate_encoded_raw)
  );
  wire [{ports - 1}:0] gate_grant =
      Mutation == 2 ? {{{ports}{{1'b0}}}} : gate_grant_raw;
  wire [{index_width - 1}:0] gate_encoded =
      Mutation == 4 && gate_valid
          ? gate_encoded_raw ^ {{{index_width}{{1'b1}}}}
          : gate_encoded_raw;

  always @(posedge clk) started <= 1'b1;
  always @* if (started) begin
    assert (gold_grant == gate_grant);
    assert (gold_valid == gate_valid);
    if (gold_valid && gate_valid) assert (gold_encoded == gate_encoded);
  end
endmodule
"""


def _verilog_axis_mux_miter(
    configuration: Mapping[str, Any],
) -> str:
    ports = int(configuration["S_COUNT"])
    data_width = int(configuration["DATA_WIDTH"])
    keep_width = (data_width + 7) // 8
    id_width = 8
    dest_width = 8
    user_width = 1
    output_id_width = id_width + max(1, (ports - 1).bit_length())
    common_parameters = f"""\
    .S_COUNT({ports}), .DATA_WIDTH({data_width}),
    .KEEP_ENABLE(1), .KEEP_WIDTH({keep_width}),
    .ID_ENABLE(0), .S_ID_WIDTH({id_width}),
    .M_ID_WIDTH({output_id_width}), .DEST_ENABLE(0),
    .DEST_WIDTH({dest_width}), .USER_ENABLE(1),
    .USER_WIDTH({user_width}), .LAST_ENABLE(1), .UPDATE_TID(0),
    .ARB_TYPE_ROUND_ROBIN(1), .ARB_LSB_HIGH_PRIORITY(1)"""
    return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input wire clk,
  input wire [{ports * data_width - 1}:0] s_axis_tdata,
  input wire [{ports * keep_width - 1}:0] s_axis_tkeep,
  input wire [{ports - 1}:0] s_axis_tvalid,
  input wire [{ports - 1}:0] s_axis_tlast,
  input wire [{ports * id_width - 1}:0] s_axis_tid,
  input wire [{ports * dest_width - 1}:0] s_axis_tdest,
  input wire [{ports * user_width - 1}:0] s_axis_tuser,
  input wire m_axis_tready
);
  localparam integer Mutation = `MUTATION_KIND;
  reg started = 1'b0;
  wire rst = !started;
  wire gate_rst = Mutation == 3 ? 1'b0 : rst;
  wire [{ports - 1}:0] gate_valid_input =
      Mutation == 1 ? {_reversed_signal("s_axis_tvalid", ports)} : s_axis_tvalid;
  wire [{ports - 1}:0] gold_ready;
  wire [{ports - 1}:0] gate_ready_raw;
  wire [{ports - 1}:0] gate_ready =
      Mutation == 2 ? {{{ports}{{1'b0}}}} : gate_ready_raw;
  wire [{data_width - 1}:0] gold_data;
  wire [{data_width - 1}:0] gate_data_raw;
  wire [{data_width - 1}:0] gate_data =
      Mutation == 4 && gate_valid
          ? gate_data_raw ^ {{{{({data_width}-1){{1'b0}}}}, 1'b1}}
          : gate_data_raw;
  wire [{keep_width - 1}:0] gold_keep, gate_keep;
  wire gold_valid, gate_valid;
  wire gold_last, gate_last;
  wire [{output_id_width - 1}:0] gold_id, gate_id;
  wire [{dest_width - 1}:0] gold_dest, gate_dest;
  wire [{user_width - 1}:0] gold_user, gate_user;

  axis_arb_mux #(
{common_parameters}
  ) gold (
    .clk, .rst, .s_axis_tdata, .s_axis_tkeep, .s_axis_tvalid,
    .s_axis_tready(gold_ready), .s_axis_tlast, .s_axis_tid,
    .s_axis_tdest, .s_axis_tuser, .m_axis_tdata(gold_data),
    .m_axis_tkeep(gold_keep), .m_axis_tvalid(gold_valid),
    .m_axis_tready, .m_axis_tlast(gold_last), .m_axis_tid(gold_id),
    .m_axis_tdest(gold_dest), .m_axis_tuser(gold_user)
  );
  rtl_advisor_axis_arb_mux_alt #(
{common_parameters}
  ) gate (
    .clk, .rst(gate_rst), .s_axis_tdata, .s_axis_tkeep,
    .s_axis_tvalid(gate_valid_input), .s_axis_tready(gate_ready_raw),
    .s_axis_tlast, .s_axis_tid, .s_axis_tdest, .s_axis_tuser,
    .m_axis_tdata(gate_data_raw), .m_axis_tkeep(gate_keep),
    .m_axis_tvalid(gate_valid), .m_axis_tready,
    .m_axis_tlast(gate_last), .m_axis_tid(gate_id),
    .m_axis_tdest(gate_dest), .m_axis_tuser(gate_user)
  );

  reg [{ports - 1}:0] previous_valid, previous_ready;
  reg [{ports * data_width - 1}:0] previous_data;
  reg [{ports * keep_width - 1}:0] previous_keep;
  reg [{ports - 1}:0] previous_last;
  reg [{ports * id_width - 1}:0] previous_id;
  reg [{ports * dest_width - 1}:0] previous_dest;
  reg [{ports * user_width - 1}:0] previous_user;
  integer source_port;
  always @(posedge clk) begin
    started <= 1'b1;
    if (started) begin
      for (source_port = 0; source_port < {ports}; source_port = source_port+1) begin
        if (previous_valid[source_port] && !previous_ready[source_port]) begin
          assume (s_axis_tvalid[source_port]);
          assume (
            s_axis_tdata[source_port*{data_width} +: {data_width}] ==
            previous_data[source_port*{data_width} +: {data_width}]
          );
          assume (
            s_axis_tkeep[source_port*{keep_width} +: {keep_width}] ==
            previous_keep[source_port*{keep_width} +: {keep_width}]
          );
          assume (s_axis_tlast[source_port] == previous_last[source_port]);
          assume (
            s_axis_tid[source_port*{id_width} +: {id_width}] ==
            previous_id[source_port*{id_width} +: {id_width}]
          );
          assume (
            s_axis_tdest[source_port*{dest_width} +: {dest_width}] ==
            previous_dest[source_port*{dest_width} +: {dest_width}]
          );
          assume (
            s_axis_tuser[source_port*{user_width} +: {user_width}] ==
            previous_user[source_port*{user_width} +: {user_width}]
          );
        end
      end
    end
    previous_valid <= s_axis_tvalid;
    previous_ready <= gold_ready;
    previous_data <= s_axis_tdata;
    previous_keep <= s_axis_tkeep;
    previous_last <= s_axis_tlast;
    previous_id <= s_axis_tid;
    previous_dest <= s_axis_tdest;
    previous_user <= s_axis_tuser;
  end

  always @* if (started) begin
    assert (gold_ready == gate_ready);
    assert (gold_valid == gate_valid);
    if (gold_valid && gate_valid) begin
      assert (gold_data == gate_data);
      assert (gold_keep == gate_keep);
      assert (gold_last == gate_last);
      assert (gold_id == gate_id);
      assert (gold_dest == gate_dest);
      assert (gold_user == gate_user);
    end
  end
endmodule
"""


def _verilog_axis_p2_sby(*, mutation_kind: int, mux: bool) -> str:
    mutation = (
        "" if mutation_kind == 0 else f" -D MUTATION_KIND={mutation_kind}"
    )
    axis_source = " axis_arb_mux.v" if mux else ""
    return f"""\
[tasks]
positive

[options]
mode prove
depth 24
expect pass

[engines]
abc pdr

[script]
read_verilog -sv{mutation} priority_encoder.v arbiter.v{axis_source} rtl_advisor_family_top.sv rtl_advisor_family_p2_miter.sv
prep -top rtl_advisor_family_p2_miter

[files]
"""


def _create_verilog_axis_p2_plan(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    *,
    mutation_kind: int = 0,
) -> Path:
    reference_id = str(candidate["reference_id"])
    suffix = "positive" if mutation_kind == 0 else f"bad-{mutation_kind}"
    formal_root = Path(str(candidate["artifact_dir"])) / "formal" / "plans" / suffix
    plan_path = formal_root / "plan.json"
    if plan_path.is_file():
        return plan_path
    is_mux = reference_id == "verilog-axis-axis-arb-mux"
    formal_root.mkdir(parents=True, exist_ok=True)
    miter_path = formal_root / "rtl_advisor_family_p2_miter.sv"
    sby_path = formal_root / "rtl_advisor_family_p2.sby"
    miter_path.write_text(
        (
            _verilog_axis_mux_miter(candidate["configuration"])
            if is_mux
            else _verilog_axis_arbiter_miter(candidate["configuration"])
        ),
        encoding="utf-8",
    )
    source_files = {
        Path(str(item["path"])).name: Path(str(item["path"]))
        for item in candidate["baseline_design"]["files"]
    }
    try:
        upstream = tuple(
            source_files[name]
            for name in (
                "priority_encoder.v",
                "arbiter.v",
                *(("axis_arb_mux.v",) if is_mux else ()),
            )
        )
    except KeyError as exc:
        raise ArbiterFamilyExecutionError(
            "verilog-axis proof input is incomplete",
            code="missing_formal_input",
        ) from exc
    candidate_source = Path(
        str(candidate["candidate_design"]["files"][-1]["path"])
    )
    sby_path.write_text(
        _verilog_axis_p2_sby(mutation_kind=mutation_kind, mux=is_mux)
        + "\n".join(
            _project_relative(config, path)
            for path in (*upstream, candidate_source, miter_path)
        )
        + "\n",
        encoding="utf-8",
    )
    inputs = (sby_path, miter_path, candidate_source, *upstream)
    proof_contract = candidate["proof_contract"]
    expected = "equivalent" if mutation_kind == 0 else "inequivalent_control"
    plan = {
        "schema_version": 1,
        "schema": "rtl-advisor-p2-proof-plan-v1",
        "document_type": "rtl-advisor.p2-proof-plan",
        "proof_id": (
            f"{reference_id}-{candidate['configuration_id']}-{suffix}-p2-v2"
        ),
        "variant_id": (
            f"{candidate['variant_id']}-{candidate['configuration_id']}-{suffix}"
        ),
        "parent_reference_id": reference_id,
        "tranche_id": "arbiter-family-credibility-v1",
        "tranche_semantic_hash": stable_hash(
            {
                "family_id": "same-cycle-arbiter-topology-v1",
                "reference_id": reference_id,
            }
        ),
        "expected_relation": expected,
        "registration": {
            "variant_lineage_id": (
                f"{candidate['variant_id']}-{candidate['configuration_id']}-{suffix}"
            ),
            "display_name": (
                f"{candidate['variant_id']} {candidate['configuration_id']} {suffix}"
            ),
            "origin": {
                "kind": (
                    candidate["candidate_origin"]
                    if mutation_kind == 0
                    else "negative_control"
                ),
                "description": (
                    "Frozen reviewed family candidate."
                    if mutation_kind == 0
                    else "Deliberately incorrect formal control."
                ),
                "generator": None,
                "generator_version": None,
            },
            "source_locations": [
                _project_relative(config, candidate_source),
                _project_relative(config, miter_path),
            ],
            "source_changes": [
                {
                    "path": _project_relative(config, candidate_source),
                    "before_sha256": file_sha256(
                        Path(str(candidate["baseline_design"]["files"][-1]["path"]))
                    ),
                    "after_sha256": file_sha256(candidate_source),
                }
            ],
        },
        "backend": "sby_miter",
        "formal_config": _project_relative(config, sby_path),
        "task": "positive",
        "contract": proof_contract,
        "tools": {
            "yosys_version": "0.63",
            "sby_version": "SBY v0.63",
            "make_version": "GNU Make 4.3",
        },
        "inputs": [
            {"path": _project_relative(config, path), "sha256": file_sha256(path)}
            for path in inputs
        ],
    }
    write_hashed_json(plan_path, plan, exclusive=True)
    return plan_path


def _basejump_p2_miter(candidate: Mapping[str, Any]) -> str:
    reference_id = str(candidate["reference_id"])
    configuration = candidate["configuration"]
    if reference_id == "basejump-bsg-arb-round-robin":
        width = int(configuration["width_p"])
        return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input logic clk_i,
  input logic [{width - 1}:0] reqs_i,
  input logic yumi_i
);
  localparam int Mutation = `MUTATION_KIND;
  logic started = 1'b0;
  wire reset_i = !started;
  wire gate_reset = Mutation == 3 ? 1'b0 : reset_i;
  wire [{width - 1}:0] gate_reqs =
      Mutation == 1 ? {_reversed_signal("reqs_i", width)} : reqs_i;
  wire [{width - 1}:0] gold_grants, gate_grants_raw;
  wire [{width - 1}:0] gate_grants =
      Mutation == 2 ? '0 : gate_grants_raw;
  bsg_arb_round_robin #(.width_p({width})) gold (
    .clk_i, .reset_i, .reqs_i, .grants_o(gold_grants), .yumi_i
  );
  rtl_advisor_bsg_rr_rotated #(.width_p({width})) gate (
    .clk_i, .reset_i(gate_reset), .reqs_i(gate_reqs),
    .grants_o(gate_grants_raw), .yumi_i
  );
  always_ff @(posedge clk_i) started <= 1'b1;
  always_comb if (started) assert (gold_grants == gate_grants);
endmodule
"""
    if reference_id == "basejump-bsg-locking-arb-fixed":
        width = int(configuration["width_p"])
        lo_to_hi = int(configuration["lo_to_hi_p"])
        return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input logic clk_i,
  input logic ready_then_i,
  input logic unlock_i,
  input logic [{width - 1}:0] reqs_i
);
  localparam int Mutation = `MUTATION_KIND;
  logic started = 1'b0;
  wire effective_unlock = !started || unlock_i;
  wire gate_unlock = Mutation == 3 ? unlock_i : effective_unlock;
  wire [{width - 1}:0] gate_reqs =
      Mutation == 1 ? {_reversed_signal("reqs_i", width)} : reqs_i;
  wire [{width - 1}:0] gold_grants, gate_grants_raw;
  wire [{width - 1}:0] gate_grants =
      Mutation == 2 ? '0 : gate_grants_raw;
  bsg_locking_arb_fixed #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) gold (
    .clk_i, .ready_then_i, .unlock_i(effective_unlock),
    .reqs_i, .grants_o(gold_grants)
  );
  rtl_advisor_bsg_locking_fixed_alt #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) gate (
    .clk_i, .ready_then_i, .unlock_i(gate_unlock),
    .reqs_i(gate_reqs), .grants_o(gate_grants_raw)
  );
  always_ff @(posedge clk_i) started <= 1'b1;
  always_comb if (started) assert (gold_grants == gate_grants);
endmodule
"""
    if reference_id == "basejump-bsg-round-robin-n-to-1":
        width = int(configuration["num_in_p"])
        data_width = int(configuration["width_p"])
        tag_width = max(1, (width - 1).bit_length())
        return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p2_miter (
  input logic clk_i,
  input logic [{width - 1}:0][{data_width - 1}:0] data_i,
  input logic [{width - 1}:0] v_i,
  input logic yumi_i
);
  localparam int Mutation = `MUTATION_KIND;
  logic started = 1'b0;
  wire reset_i = !started;
  wire gate_reset = Mutation == 3 ? 1'b0 : reset_i;
  wire [{width - 1}:0] gate_valid_input =
      Mutation == 1 ? {_reversed_signal("v_i", width)} : v_i;
  wire [{width - 1}:0] gold_yumi, gate_yumi_raw;
  wire [{width - 1}:0] gate_yumi =
      Mutation == 2 ? '0 : gate_yumi_raw;
  wire gold_valid, gate_valid;
  wire [{data_width - 1}:0] gold_data, gate_data_raw;
  wire [{data_width - 1}:0] gate_data =
      Mutation == 4 && gate_valid
          ? gate_data_raw ^ {{{{({data_width}-1){{1'b0}}}}, 1'b1}}
          : gate_data_raw;
  wire [{tag_width - 1}:0] gold_tag, gate_tag;
  bsg_round_robin_n_to_1 #(
    .width_p({data_width}), .num_in_p({width}), .strict_p(0),
    .use_scan_p(0), .tag_width_lp({tag_width})
  ) gold (
    .clk_i, .reset_i, .data_i, .v_i, .yumi_o(gold_yumi),
    .v_o(gold_valid), .data_o(gold_data), .tag_o(gold_tag), .yumi_i
  );
  rtl_advisor_bsg_n_to1_scan_alt gate (
    .clk_i, .reset_i(gate_reset), .data_i, .v_i(gate_valid_input),
    .yumi_o(gate_yumi_raw), .v_o(gate_valid), .data_o(gate_data_raw),
    .tag_o(gate_tag), .yumi_i
  );

  logic [{width - 1}:0] previous_valid, previous_yumi;
  logic [{width - 1}:0][{data_width - 1}:0] previous_data;
  integer source_port;
  always_ff @(posedge clk_i) begin
    started <= 1'b1;
    if (started) begin
      for (source_port = 0; source_port < {width}; source_port = source_port+1) begin
        if (previous_valid[source_port] && !previous_yumi[source_port]) begin
          assume (v_i[source_port]);
          assume (data_i[source_port] == previous_data[source_port]);
        end
      end
    end
    previous_valid <= v_i;
    previous_yumi <= gold_yumi;
    previous_data <= data_i;
  end
  always_comb if (started) begin
    assert (gold_yumi == gate_yumi);
    assert (gold_valid == gate_valid);
    if (gold_valid && gate_valid) begin
      assert (gold_tag == gate_tag);
      assert (gold_data == gate_data);
    end
  end
endmodule
"""
    raise ArbiterFamilyExecutionError(
        f"no BaseJump P2 miter for {reference_id}",
        code="formal_qualification_pending",
    )


def _basejump_p2_sby(
    *,
    mutation_kind: int,
    source_names: tuple[str, ...],
) -> str:
    mutation = (
        "" if mutation_kind == 0 else f" -D MUTATION_KIND={mutation_kind}"
    )
    names = " ".join(source_names)
    return f"""\
[tasks]
positive

[options]
mode prove
depth 24
expect pass

[engines]
abc pdr

[script]
plugin -i /opt/oss-cad-suite/share/yosys/plugins/slang.so
read_slang --top rtl_advisor_family_p2_miter -D SYNTHESIS{mutation} -I . {names} rtl_advisor_family_top.sv rtl_advisor_family_p2_miter.sv
prep -top rtl_advisor_family_p2_miter

[files]
"""


def _create_basejump_p2_plan(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    *,
    mutation_kind: int = 0,
) -> Path:
    reference_id = str(candidate["reference_id"])
    suffix = "positive" if mutation_kind == 0 else f"bad-{mutation_kind}"
    formal_root = Path(str(candidate["artifact_dir"])) / "formal" / "plans" / suffix
    plan_path = formal_root / "plan.json"
    if plan_path.is_file():
        return plan_path
    formal_root.mkdir(parents=True, exist_ok=True)
    miter_path = formal_root / "rtl_advisor_family_p2_miter.sv"
    sby_path = formal_root / "rtl_advisor_family_p2.sby"
    miter_path.write_text(_basejump_p2_miter(candidate), encoding="utf-8")
    candidate_source = Path(
        str(candidate["candidate_design"]["files"][-1]["path"])
    )
    baseline_wrapper = Path(
        str(candidate["baseline_design"]["files"][-1]["path"])
    )
    upstream = tuple(
        Path(str(item["path"]))
        for item in candidate["baseline_design"]["files"][:-1]
    )
    source_names = tuple(path.name for path in upstream)
    if len(source_names) != len(set(source_names)):
        raise ArbiterFamilyExecutionError(
            "BaseJump proof inputs contain duplicate basenames",
            code="ambiguous_formal_input",
        )
    sby_path.write_text(
        _basejump_p2_sby(
            mutation_kind=mutation_kind,
            source_names=source_names,
        )
        + "\n".join(
            f"{path.name} {_project_relative(config, path)}"
            for path in upstream
        )
        + "\n"
        + "\n".join(
            _project_relative(config, path)
            for path in (candidate_source, miter_path)
        )
        + "\n",
        encoding="utf-8",
    )
    inputs = (sby_path, miter_path, candidate_source, *upstream)
    expected = "equivalent" if mutation_kind == 0 else "inequivalent_control"
    proof_contract = candidate["proof_contract"]
    plan = {
        "schema_version": 1,
        "schema": "rtl-advisor-p2-proof-plan-v1",
        "document_type": "rtl-advisor.p2-proof-plan",
        "proof_id": (
            f"{reference_id}-{candidate['configuration_id']}-{suffix}-p2-v2"
        ),
        "variant_id": (
            f"{candidate['variant_id']}-{candidate['configuration_id']}-{suffix}"
        ),
        "parent_reference_id": reference_id,
        "tranche_id": "arbiter-family-credibility-v1",
        "tranche_semantic_hash": stable_hash(
            {
                "family_id": "same-cycle-arbiter-topology-v1",
                "reference_id": reference_id,
            }
        ),
        "expected_relation": expected,
        "registration": {
            "variant_lineage_id": (
                f"{candidate['variant_id']}-{candidate['configuration_id']}-{suffix}"
            ),
            "display_name": (
                f"{candidate['variant_id']} {candidate['configuration_id']} {suffix}"
            ),
            "origin": {
                "kind": (
                    candidate["candidate_origin"]
                    if mutation_kind == 0
                    else "negative_control"
                ),
                "description": (
                    "Frozen reviewed family candidate."
                    if mutation_kind == 0
                    else "Deliberately incorrect formal control."
                ),
                "generator": None,
                "generator_version": None,
            },
            "source_locations": [
                _project_relative(config, candidate_source),
                _project_relative(config, miter_path),
            ],
            "source_changes": [
                {
                    "path": _project_relative(config, candidate_source),
                    "before_sha256": file_sha256(baseline_wrapper),
                    "after_sha256": file_sha256(candidate_source),
                }
            ],
        },
        "backend": "sby_miter",
        "formal_config": _project_relative(config, sby_path),
        "task": "positive",
        "contract": proof_contract,
        "tools": {
            "yosys_version": "0.63",
            "sby_version": "SBY v0.63",
            "make_version": "GNU Make 4.3",
        },
        "inputs": [
            {"path": _project_relative(config, path), "sha256": file_sha256(path)}
            for path in inputs
        ],
    }
    write_hashed_json(plan_path, plan, exclusive=True)
    return plan_path


def _create_pulp_p2_plan(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    *,
    mutation_kind: int = 0,
) -> Path:
    reference_id = str(candidate["reference_id"])
    stream = reference_id == "pulp-common-cells-stream-arbiter-flushable"
    suffix = "positive" if mutation_kind == 0 else f"bad-{mutation_kind}"
    formal_root = Path(str(candidate["artifact_dir"])) / "formal" / "plans" / suffix
    plan_path = formal_root / "plan.json"
    if plan_path.is_file():
        return plan_path
    formal_root.mkdir(parents=True, exist_ok=True)
    miter_path = formal_root / "rtl_advisor_family_p2_miter.sv"
    sby_path = formal_root / "rtl_advisor_family_p2.sby"
    miter_path.write_text(
        _pulp_rr_miter(candidate["configuration"], stream=stream),
        encoding="utf-8",
    )
    source_root = Path(
        str(candidate["baseline_design"]["files"][0]["path"])
    ).parents[1]
    if source_root.name == "src":
        source_root = source_root.parent
    upstream = [
        source_root / "src/cf_math_pkg.sv",
        source_root / "src/lzc.sv",
        source_root / "src/rr_arb_tree.sv",
        source_root / "src/stream_arbiter_flushable.sv",
    ]
    include_files = [
        source_root / "include/common_cells/assertions.svh",
        source_root / "include/common_cells/registers.svh",
    ]
    candidate_source = Path(
        str(candidate["candidate_design"]["files"][-1]["path"])
    )
    sby_path.write_text(
        _pulp_p2_sby(mutation_kind=mutation_kind)
        + "\n".join(
            (
                f"common_cells/{path.name} {_project_relative(config, path)}"
                for path in include_files
            )
        )
        + "\n"
        + "\n".join(
            _project_relative(config, path)
            for path in (*upstream, candidate_source, miter_path)
        )
        + "\n",
        encoding="utf-8",
    )
    inputs = (sby_path, miter_path, candidate_source, *upstream, *include_files)
    proof_contract = candidate["proof_contract"]
    expected = "equivalent" if mutation_kind == 0 else "inequivalent_control"
    plan = {
        "schema_version": 1,
        "schema": "rtl-advisor-p2-proof-plan-v1",
        "document_type": "rtl-advisor.p2-proof-plan",
        "proof_id": (
            f"{reference_id}-{candidate['configuration_id']}-{suffix}-p2-v1"
        ),
        "variant_id": (
            f"{candidate['variant_id']}-{candidate['configuration_id']}-{suffix}"
        ),
        "parent_reference_id": reference_id,
        "tranche_id": "arbiter-family-credibility-v1",
        "tranche_semantic_hash": stable_hash(
            {
                "family_id": "same-cycle-arbiter-topology-v1",
                "reference_id": reference_id,
            }
        ),
        "expected_relation": expected,
        "registration": {
            "variant_lineage_id": (
                f"{candidate['variant_id']}-{candidate['configuration_id']}-{suffix}"
            ),
            "display_name": (
                f"{candidate['variant_id']} {candidate['configuration_id']} {suffix}"
            ),
            "origin": {
                "kind": (
                    candidate["candidate_origin"]
                    if mutation_kind == 0
                    else "negative_control"
                ),
                "description": (
                    "Frozen reviewed family candidate."
                    if mutation_kind == 0
                    else "Deliberately incorrect formal control."
                ),
                "generator": None,
                "generator_version": None,
            },
            "source_locations": [
                _project_relative(config, candidate_source),
                _project_relative(config, miter_path),
            ],
            "source_changes": [
                {
                    "path": _project_relative(config, candidate_source),
                    "before_sha256": file_sha256(
                        Path(str(candidate["baseline_design"]["files"][-1]["path"]))
                    ),
                    "after_sha256": file_sha256(candidate_source),
                }
            ],
        },
        "backend": "sby_miter",
        "formal_config": _project_relative(config, sby_path),
        "task": "positive",
        "contract": proof_contract,
        "tools": {
            "yosys_version": "0.63",
            "sby_version": "SBY v0.63",
            "make_version": "GNU Make 4.3",
        },
        "inputs": [
            {"path": _project_relative(config, path), "sha256": file_sha256(path)}
            for path in inputs
        ],
    }
    write_hashed_json(plan_path, plan, exclusive=True)
    return plan_path


def _family_p1_miter(
    candidate: Mapping[str, Any],
) -> str:
    reference_id = str(candidate["reference_id"])
    configuration = candidate["configuration"]
    if reference_id == "opentitan-prim-arbiter-fixed":
        width = int(configuration["N"])
        data_width = int(configuration["DW"])
        index_width = max(1, (width - 1).bit_length())
        gold_index_connection = (
            ".idx_o()" if width == 1 else ".idx_o(gold_idx)"
        )
        gold_index_tieoff = (
            "assign gold_idx = '0;" if width == 1 else ""
        )
        return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p1_miter (
  input logic clk_i,
  input logic rst_ni,
  input logic [{width - 1}:0] req_i,
  input logic [{data_width - 1}:0] data_i [{width}],
  input logic ready_i
);
  localparam int Mutation = `MUTATION_KIND;
  wire [{width - 1}:0] gate_req =
      Mutation == 1 ? {_reversed_signal("req_i", width)} : req_i;
  wire [{width - 1}:0] gold_gnt, gate_gnt_raw;
  wire [{width - 1}:0] gate_gnt =
      Mutation == 2 ? '0 : gate_gnt_raw;
  wire [{index_width - 1}:0] gold_idx, gate_idx;
  wire gold_valid, gate_valid;
  wire [{data_width - 1}:0] gold_data, gate_data_raw;
  wire [{data_width - 1}:0] gate_data =
      Mutation == 4 && gate_valid
          ? gate_data_raw ^ {{{{({data_width}-1){{1'b0}}}}, 1'b1}}
          : gate_data_raw;

  prim_arbiter_fixed #(
    .N({width}), .DW({data_width}), .EnDataPort(1'b1)
  ) gold (
    .clk_i, .rst_ni, .req_i, .data_i, .gnt_o(gold_gnt),
    {gold_index_connection}, .valid_o(gold_valid),
    .data_o(gold_data), .ready_i
  );
  {gold_index_tieoff}
  rtl_advisor_fixed_flat #(
    .N({width}), .DW({data_width}), .EnDataPort(1'b1)
  ) gate (
    .req_i(gate_req), .data_i, .ready_i, .gnt_o(gate_gnt_raw),
    .idx_o(gate_idx), .valid_o(gate_valid), .data_o(gate_data_raw)
  );

  always_comb begin
    assert (gold_gnt == gate_gnt);
    assert (gold_valid == gate_valid);
    if (gold_valid && gate_valid) begin
      assert (gold_idx == gate_idx);
      assert (gold_data == gate_data);
    end
  end
endmodule
"""
    if reference_id == "basejump-bsg-arb-fixed":
        width = int(configuration["width_p"])
        lo_to_hi = int(configuration["lo_to_hi_p"])
        return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif
module rtl_advisor_family_p1_miter (
  input logic ready_then_i,
  input logic [{width - 1}:0] reqs_i
);
  localparam int Mutation = `MUTATION_KIND;
  wire [{width - 1}:0] gate_reqs =
      Mutation == 1 ? {_reversed_signal("reqs_i", width)} : reqs_i;
  wire [{width - 1}:0] gold_grants, gate_grants_raw;
  wire [{width - 1}:0] gate_grants =
      Mutation == 2 ? '0 : gate_grants_raw;
  bsg_arb_fixed #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) gold (
    .ready_then_i, .reqs_i, .grants_o(gold_grants)
  );
  rtl_advisor_bsg_fixed_flat #(
    .inputs_p({width}), .lo_to_hi_p({lo_to_hi})
  ) gate (
    .ready_then_i, .reqs_i(gate_reqs), .grants_o(gate_grants_raw)
  );
  always_comb assert (gold_grants == gate_grants);
endmodule
"""
    raise ArbiterFamilyExecutionError(
        f"no P1 miter for {reference_id}",
        code="formal_qualification_pending",
    )


def _create_family_p1_sby(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    *,
    mutation_kind: int,
) -> tuple[Path, tuple[Path, ...]]:
    suffix = "positive" if mutation_kind == 0 else f"bad-{mutation_kind}"
    formal_root = (
        Path(str(candidate["artifact_dir"])) / "formal" / "plans" / suffix
    )
    formal_root.mkdir(parents=True, exist_ok=True)
    miter_path = formal_root / "rtl_advisor_family_p1_miter.sv"
    sby_path = formal_root / "rtl_advisor_family_p1.sby"
    miter_path.write_text(_family_p1_miter(candidate), encoding="utf-8")

    upstream = tuple(
        Path(str(item["path"]))
        for item in candidate["baseline_design"]["files"][:-1]
    )
    candidate_source = Path(
        str(candidate["candidate_design"]["files"][-1]["path"])
    )
    include_files: tuple[Path, ...] = ()
    if candidate["reference_id"] == "opentitan-prim-arbiter-fixed":
        source_dir = upstream[0].parent
        include_files = (
            source_dir / "prim_assert.sv",
            source_dir / "prim_assert_dummy_macros.svh",
            source_dir / "prim_assert_sec_cm.svh",
            source_dir / "prim_flop_macros.sv",
        )
    source_files = (*upstream, *include_files)
    source_names = tuple(path.name for path in source_files)
    if len(source_names) != len(set(source_names)):
        raise ArbiterFamilyExecutionError(
            "P1 proof inputs contain duplicate basenames",
            code="ambiguous_formal_input",
        )
    mutation = (
        "" if mutation_kind == 0 else f" -D MUTATION_KIND={mutation_kind}"
    )
    sby_path.write_text(
        f"""\
[tasks]
positive

[options]
mode prove
depth 1
expect pass

[engines]
abc pdr

[script]
plugin -i /opt/oss-cad-suite/share/yosys/plugins/slang.so
read_slang --top rtl_advisor_family_p1_miter -D SYNTHESIS{mutation} -I . {" ".join(source_names)} rtl_advisor_family_top.sv rtl_advisor_family_p1_miter.sv
prep -top rtl_advisor_family_p1_miter

[files]
"""
        + "\n".join(
            f"{path.name} {_project_relative(config, path)}"
            for path in source_files
        )
        + "\n"
        + "\n".join(
            _project_relative(config, path)
            for path in (candidate_source, miter_path)
        )
        + "\n",
        encoding="utf-8",
    )
    return sby_path, (
        sby_path,
        miter_path,
        candidate_source,
        *source_files,
    )


def _classify_family_p1_sby(
    returncode: int | None,
    output_dir: Path,
    transcript: str,
) -> str:
    if (
        returncode == 0
        and (output_dir / "PASS").is_file()
        and "DONE (PASS, rc=0)" in transcript
    ):
        return "equivalent"
    if (
        (output_dir / "FAIL").is_file()
        and "DONE (FAIL" in transcript
        and (
            "Status returned by engine: FAIL" in transcript
            or "Assert failed" in transcript
        )
    ):
        return "inequivalent"
    return "inconclusive"


def _run_family_p1_sby(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    *,
    mutation_kind: int,
) -> dict[str, Any]:
    sby_path, inputs = _create_family_p1_sby(
        config,
        candidate,
        mutation_kind=mutation_kind,
    )
    pinned_runner = config.root / "scripts/run_pinned_sby.py"
    formal_command = str(pinned_runner) if pinned_runner.is_file() else "sby"
    tool_identity = collect_p2_tool_identity(formal_command)
    versions = {
        name: str(tool_identity.get(name, {}).get("version", ""))
        for name in ("yosys", "sby", "make")
    }
    if (
        not versions["yosys"].startswith("Yosys 0.63")
        or versions["sby"] != "SBY v0.63"
        or versions["make"] != "GNU Make 4.3"
    ):
        raise ArbiterFamilyExecutionError(
            "P1 proof tools do not match the pinned contract",
            code="formal_tool_identity_mismatch",
        )
    proof_core = {
        "reference_id": candidate["reference_id"],
        "configuration_id": candidate["configuration_id"],
        "candidate_id": candidate["candidate_id"],
        "proof_contract_hash": candidate["proof_contract_hash"],
        "mutation_kind": mutation_kind,
        "inputs": [
            {
                "path": _project_relative(config, path),
                "sha256": file_sha256(path),
            }
            for path in inputs
        ],
        "tool_identity_hash": tool_identity["identity_hash"],
    }
    proof_hash = stable_hash(proof_core)
    suffix = "positive" if mutation_kind == 0 else f"bad-{mutation_kind}"
    proof_id = (
        f"{candidate['reference_id']}-{candidate['configuration_id']}-"
        f"{suffix}-p1-v1"
    )
    artifact_root = config.artifacts_dir / "formal" / "p1" / proof_id / proof_hash
    result_path = artifact_root / "result.json"
    if result_path.is_file():
        return read_hashed_json(
            result_path,
            document_type="rtl-advisor.p1-proof-result",
            schema_version=1,
        )
    artifact_root.mkdir(parents=True, exist_ok=True)
    formal_output = artifact_root / "formal"
    with tempfile.TemporaryDirectory(prefix="rtl-advisor-p1-") as temporary:
        execution_output = Path(temporary) / "formal"
        command = (
            formal_command,
            "-f",
            "-d",
            str(execution_output),
            str(sby_path),
            "positive",
        )
        try:
            completed = run_command(
                command,
                timeout_seconds=config.tools.timeout_seconds,
                cwd=config.root,
            )
            transcript = "\n".join(
                part
                for part in (completed.stdout, completed.stderr)
                if part
            )
            returncode: int | None = completed.returncode
        except ToolExecutionError as exc:
            transcript = str(exc)
            returncode = None
        if execution_output.is_dir():
            shutil.copytree(execution_output, formal_output)
    transcript_path = artifact_root / "transcript.log"
    transcript_path.write_text(
        transcript + ("\n" if transcript else ""),
        encoding="utf-8",
    )
    relation = _classify_family_p1_sby(
        returncode,
        formal_output,
        transcript,
    )
    expected_relation = (
        "equivalent" if mutation_kind == 0 else "inequivalent"
    )
    expectation_met = relation == expected_relation
    counterexample = formal_output / "engine_0" / "trace.vcd"
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.p1-proof-result",
        "proof_id": proof_id,
        "proof_hash": proof_hash,
        "candidate_id": candidate["candidate_id"],
        "reference_id": candidate["reference_id"],
        "configuration_id": candidate["configuration_id"],
        "mutation_kind": mutation_kind,
        "expected_relation": expected_relation,
        "observed_relation": relation,
        "expectation_met": expectation_met,
        "status": (
            "formal_passed"
            if relation == "equivalent"
            else "formal_failed"
            if relation == "inequivalent"
            else "formal_inconclusive"
        ),
        "safe": mutation_kind == 0 and relation == "equivalent",
        "proof_contract": candidate["proof_contract"],
        "proof_contract_hash": candidate["proof_contract_hash"],
        "inputs": proof_core["inputs"],
        "tool_identity": tool_identity,
        "command": list(command),
        "returncode": returncode,
        "transcript_path": str(transcript_path),
        "transcript_sha256": file_sha256(transcript_path),
        "counterexample": (
            {
                "path": str(counterexample),
                "sha256": file_sha256(counterexample),
            }
            if counterexample.is_file()
            else None
        ),
    }
    return write_hashed_json(result_path, payload, exclusive=True)


def _verify_family_p1_candidate(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    artifact_root: str | Path,
) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    expected_dir = Path(artifact_root).expanduser().resolve() / candidate_id
    if Path(str(candidate["artifact_dir"])).resolve() != expected_dir:
        raise ArbiterFamilyExecutionError(
            "candidate does not belong to the requested artifact root",
            code="artifact_root_mismatch",
        )
    baseline = _design_from_mapping(candidate["baseline_design"])
    alternative = _design_from_mapping(candidate["candidate_design"])
    before = {
        "baseline": _design_integrity(
            baseline,
            candidate["baseline_compile_context"],
        ),
        "candidate": _design_integrity(
            alternative,
            candidate["candidate_compile_context"],
        ),
    }
    if not all(item["ok"] for item in before.values()):
        raise ArbiterFamilyExecutionError(
            "source or compile context changed before P1",
            code="stale_candidate",
        )
    output_dir = expected_dir / "formal"
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "formal.json"
    if result_path.is_file():
        return read_hashed_json(
            result_path,
            document_type=FORMAL_DOCUMENT_TYPE,
            schema_version=RUN_SCHEMA_VERSION,
        )
    baseline_lint = _verilator_lint(
        config,
        baseline,
        output_dir,
        "baseline",
    )
    candidate_lint = _verilator_lint(
        config,
        alternative,
        output_dir,
        "candidate",
    )
    lint = {
        "verilator": {
            "baseline": baseline_lint,
            "candidate": candidate_lint,
        },
        "pyslang": {
            "baseline": _pyslang_lint(baseline),
            "candidate": _pyslang_lint(alternative),
        },
    }
    p1_result: Mapping[str, Any] | None = None
    if {baseline_lint["status"], candidate_lint["status"]} != {"passed"}:
        formal = {
            "backend": "sby-p1-combinational-family-v1",
            "semantics": "combinational two-state bit-vector equivalence",
            "status": "inconclusive",
            "detail": "baseline or candidate did not pass Verilator lint",
        }
    else:
        try:
            p1_result = _run_family_p1_sby(
                config,
                candidate,
                mutation_kind=0,
            )
        except (ArbiterFamilyExecutionError, SequentialEquivalenceError) as exc:
            formal = {
                "backend": "sby-p1-combinational-family-v1",
                "semantics": "combinational two-state bit-vector equivalence",
                "status": "inconclusive",
                "detail": str(exc),
                "error_code": getattr(exc, "code", "formal_inconclusive"),
            }
        else:
            formal = {
                "backend": "sby-p1-combinational-family-v1",
                "semantics": "combinational two-state bit-vector equivalence",
                "status": (
                    "passed"
                    if p1_result["status"] == "formal_passed"
                    and p1_result["safe"] is True
                    else "failed"
                    if p1_result["status"] == "formal_failed"
                    else "inconclusive"
                ),
                "observed_relation": p1_result["observed_relation"],
                "expectation_met": p1_result["expectation_met"],
                "tool_identity": p1_result["tool_identity"],
                "p1_result_semantic_hash": p1_result["semantic_hash"],
                "p1_transcript_path": p1_result["transcript_path"],
                "p1_transcript_sha256": p1_result["transcript_sha256"],
                "detail": None,
            }
    after = {
        "baseline": _design_integrity(
            baseline,
            candidate["baseline_compile_context"],
        ),
        "candidate": _design_integrity(
            alternative,
            candidate["candidate_compile_context"],
        ),
    }
    current = all(item["ok"] for item in after.values())
    if not current:
        formal = {
            **formal,
            "status": "inconclusive",
            "detail": "source integrity changed during P1",
        }
    safe = formal["status"] == "passed" and current
    state = {
        "passed": "formal_passed",
        "failed": "formal_failed",
        "inconclusive": "formal_inconclusive",
    }[formal["status"]]
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": RUN_SCHEMA_ID,
        "document_type": FORMAL_DOCUMENT_TYPE,
        "status": state,
        "safe": safe,
        "candidate_id": candidate_id,
        "reference_id": candidate["reference_id"],
        "configuration_id": candidate["configuration_id"],
        "transformation_id": ARBITER_TRANSFORMATION_ID,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": candidate["executor_id"],
        "executor_version": candidate["executor_version"],
        "executor_registry_hash": candidate["executor_registry_hash"],
        "proof_contract": candidate["proof_contract"],
        "proof_contract_hash": candidate["proof_contract_hash"],
        "baseline_design_hash": baseline.design_hash,
        "candidate_design_hash": alternative.design_hash,
        "compile_context": {
            "baseline": candidate["baseline_compile_context"],
            "candidate": candidate["candidate_compile_context"],
        },
        "source_integrity": after,
        "lint": lint,
        "formal": formal,
        "p1_result": p1_result,
        "artifacts": {"root": str(output_dir), "formal": str(result_path)},
        "record_path": str(result_path),
        "limitations": [
            "P1 proves combinational two-state bit-vector equivalence only.",
            "The proof does not establish four-state X/Z equivalence.",
        ],
    }
    return write_hashed_json(result_path, payload, exclusive=True)


def _verify_pulp_p2_candidate(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    artifact_root: str | Path,
) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    expected_dir = Path(artifact_root).expanduser().resolve() / candidate_id
    if Path(str(candidate["artifact_dir"])).resolve() != expected_dir:
        raise ArbiterFamilyExecutionError(
            "candidate does not belong to the requested artifact root",
            code="artifact_root_mismatch",
        )
    baseline = _design_from_mapping(candidate["baseline_design"])
    alternative = _design_from_mapping(candidate["candidate_design"])
    before = {
        "baseline": _design_integrity(
            baseline,
            candidate["baseline_compile_context"],
        ),
        "candidate": _design_integrity(
            alternative,
            candidate["candidate_compile_context"],
        ),
    }
    if not all(item["ok"] for item in before.values()):
        raise ArbiterFamilyExecutionError(
            "source or compile context changed before P2",
            code="stale_candidate",
        )
    output_dir = expected_dir / "formal"
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "formal.json"
    if result_path.is_file():
        return read_hashed_json(
            result_path,
            document_type=FORMAL_DOCUMENT_TYPE,
            schema_version=RUN_SCHEMA_VERSION,
        )

    baseline_lint = _verilator_lint(
        config,
        baseline,
        output_dir,
        "baseline",
    )
    candidate_lint = _verilator_lint(
        config,
        alternative,
        output_dir,
        "candidate",
    )
    if str(candidate["reference_id"]).startswith("verilog-axis-"):
        qualified_warning_sites = (
            "priority_encoder.v:86:",
            "priority_encoder.v:87:",
            "arbiter.v:104:",
            "arbiter.v:109:",
            "current_s_tid",
        )

        def qualify_axis_warnings(result: Mapping[str, Any]) -> dict[str, Any]:
            warnings = result.get("blocking_warnings") or []
            if (
                result.get("returncode") == 0
                and warnings
                and all(
                    any(site in str(warning) for site in qualified_warning_sites)
                    for warning in warnings
                )
            ):
                return {
                    **result,
                    "status": "passed",
                    "waived_warnings": list(warnings),
                    "detail": (
                        "Pinned upstream width warnings were retained in the "
                        "artifact and accepted for this qualified reference."
                    ),
                }
            return dict(result)

        baseline_lint = qualify_axis_warnings(baseline_lint)
        candidate_lint = qualify_axis_warnings(candidate_lint)
    if candidate["reference_id"] == "basejump-bsg-round-robin-n-to-1":
        warnings = baseline_lint.get("blocking_warnings") or []
        if (
            baseline_lint.get("returncode") == 0
            and warnings
            and all(
                "bsg_round_robin_arb.sv:6486:" in str(warning)
                or "bsg_round_robin_arb.sv:6487:" in str(warning)
                for warning in warnings
            )
        ):
            baseline_lint = {
                **baseline_lint,
                "status": "passed",
                "waived_warnings": list(warnings),
                "detail": (
                    "Pinned upstream generated-arbiter width warnings were "
                    "retained and accepted for this qualified reference."
                ),
            }
    lint = {
        "verilator": {
            "baseline": baseline_lint,
            "candidate": candidate_lint,
        },
        "pyslang": {
            "baseline": _pyslang_lint(baseline),
            "candidate": _pyslang_lint(alternative),
        },
    }
    p2_result: Mapping[str, Any] | None = None
    if {baseline_lint["status"], candidate_lint["status"]} != {"passed"}:
        formal = {
            "backend": "sby-p2-same-cycle-family-v1",
            "semantics": "cycle-aligned P2 under the recorded protocol contract",
            "status": "inconclusive",
            "detail": "baseline or candidate did not pass Verilator lint",
        }
    else:
        try:
            if str(candidate["reference_id"]).startswith("verilog-axis-"):
                plan_path = _create_verilog_axis_p2_plan(config, candidate)
            elif str(candidate["reference_id"]).startswith("basejump-"):
                plan_path = _create_basejump_p2_plan(config, candidate)
            else:
                plan_path = _create_pulp_p2_plan(config, candidate)
            pinned_runner = config.root / "scripts/run_pinned_sby.py"
            p2_result = run_p2_proof(
                config,
                plan_path=plan_path,
                formal_command=(
                    str(pinned_runner) if pinned_runner.is_file() else "sby"
                ),
            )
        except (ArbiterFamilyExecutionError, SequentialEquivalenceError) as exc:
            formal = {
                "backend": "sby-p2-same-cycle-family-v1",
                "semantics": "cycle-aligned P2 under the recorded protocol contract",
                "status": "inconclusive",
                "detail": str(exc),
                "error_code": getattr(exc, "code", "formal_inconclusive"),
            }
        else:
            formal = {
                "backend": "sby-p2-same-cycle-family-v1",
                "semantics": "cycle-aligned P2 under the recorded protocol contract",
                "status": (
                    "passed"
                    if p2_result["status"] == "formal_passed"
                    and p2_result["safe"] is True
                    else "failed"
                    if p2_result["status"] == "formal_failed"
                    else "inconclusive"
                ),
                "observed_relation": p2_result["observed_relation"],
                "expectation_met": p2_result["expectation_met"],
                "tool_identity": p2_result["tool_identity"],
                "p2_result_semantic_hash": p2_result["semantic_hash"],
                "p2_transcript_path": p2_result["transcript_path"],
                "p2_transcript_sha256": p2_result["transcript_sha256"],
                "success_marker_seen": (
                    p2_result["status"] == "formal_passed"
                    and p2_result["safe"] is True
                ),
                "detail": None,
            }
    after = {
        "baseline": _design_integrity(
            baseline,
            candidate["baseline_compile_context"],
        ),
        "candidate": _design_integrity(
            alternative,
            candidate["candidate_compile_context"],
        ),
    }
    current = all(item["ok"] for item in after.values())
    if not current:
        formal = {
            **formal,
            "status": "inconclusive",
            "detail": "source integrity changed during P2",
        }
    safe = formal["status"] == "passed" and current
    state = {
        "passed": "formal_passed",
        "failed": "formal_failed",
        "inconclusive": "formal_inconclusive",
    }[formal["status"]]
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": RUN_SCHEMA_ID,
        "document_type": FORMAL_DOCUMENT_TYPE,
        "status": state,
        "safe": safe,
        "candidate_id": candidate_id,
        "reference_id": candidate["reference_id"],
        "configuration_id": candidate["configuration_id"],
        "transformation_id": ARBITER_TRANSFORMATION_ID,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": candidate["executor_id"],
        "executor_version": candidate["executor_version"],
        "executor_registry_hash": candidate["executor_registry_hash"],
        "proof_contract": candidate["proof_contract"],
        "proof_contract_hash": candidate["proof_contract_hash"],
        "baseline_design_hash": baseline.design_hash,
        "candidate_design_hash": alternative.design_hash,
        "compile_context": {
            "baseline": candidate["baseline_compile_context"],
            "candidate": candidate["candidate_compile_context"],
        },
        "source_integrity": after,
        "lint": lint,
        "formal": formal,
        "p2_result": p2_result,
        "artifacts": {"root": str(output_dir), "formal": str(result_path)},
        "record_path": str(result_path),
        "limitations": [
            "P2 covers only the recorded reset and protocol assumptions.",
            "The proof does not establish four-state X/Z equivalence.",
        ],
    }
    return write_hashed_json(result_path, payload, exclusive=True)


def run_family_negative_controls(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    spec: ExecutorSpec,
) -> dict[str, Any]:
    validate_family_candidate(
        candidate,
        spec,
        executor_registry_hash=str(candidate.get("executor_registry_hash")),
    )
    if spec.proof_levels == ("P1",):
        if candidate.get("reference_id") not in {
            "opentitan-prim-arbiter-fixed",
            "basejump-bsg-arb-fixed",
        }:
            raise ArbiterFamilyExecutionError(
                "negative controls are not implemented for this P1 pair",
                code="formal_qualification_pending",
            )
        labels = {
            1: "bad_priority_direction",
            2: "bad_grant",
        }
        if candidate.get("has_payload") is True:
            labels[4] = "bad_data_selection"
        p1_results = {
            label: _run_family_p1_sby(
                config,
                candidate,
                mutation_kind=mutation_kind,
            )
            for mutation_kind, label in labels.items()
        }
        passed = all(
            result.get("observed_relation") == "inequivalent"
            and result.get("expectation_met") is True
            for result in p1_results.values()
        )
        return {
            "status": "passed" if passed else "failed",
            "results": p1_results,
        }
    if candidate.get("reference_id") not in {
        "pulp-common-cells-rr-arb-tree",
        "pulp-common-cells-stream-arbiter-flushable",
        "verilog-axis-arbiter",
        "verilog-axis-axis-arb-mux",
        "basejump-bsg-arb-round-robin",
        "basejump-bsg-locking-arb-fixed",
        "basejump-bsg-round-robin-n-to-1",
    }:
        raise ArbiterFamilyExecutionError(
            "negative controls are not implemented for this pair",
            code="formal_qualification_pending",
        )
    labels = {
        1: "bad_priority_direction",
        2: "bad_grant",
        3: "bad_reset_state_update",
    }
    if candidate.get("has_payload") is True:
        labels[4] = "bad_data_selection"
    results: dict[str, Any] = {}
    for mutation_kind, label in labels.items():
        if str(candidate["reference_id"]).startswith("verilog-axis-"):
            plan_path = _create_verilog_axis_p2_plan(
                config,
                candidate,
                mutation_kind=mutation_kind,
            )
        elif str(candidate["reference_id"]).startswith("basejump-"):
            plan_path = _create_basejump_p2_plan(
                config,
                candidate,
                mutation_kind=mutation_kind,
            )
        else:
            plan_path = _create_pulp_p2_plan(
                config,
                candidate,
                mutation_kind=mutation_kind,
            )
        pinned_runner = config.root / "scripts/run_pinned_sby.py"
        results[label] = run_p2_proof(
            config,
            plan_path=plan_path,
            formal_command=(
                str(pinned_runner) if pinned_runner.is_file() else "sby"
            ),
        )
    passed = all(
        result.get("observed_relation") == "inequivalent"
        and result.get("expectation_met") is True
        for result in results.values()
    )
    return {"status": "passed" if passed else "failed", "results": results}


def family_designs_from_candidate(
    candidate: Mapping[str, Any],
    spec: ExecutorSpec,
) -> tuple[DesignInputV2, DesignInputV2]:
    validate_family_candidate(
        candidate,
        spec,
        executor_registry_hash=str(candidate.get("executor_registry_hash")),
    )
    try:
        baseline = _design_from_mapping(candidate["baseline_design"])
        alternative = candidate_design_from_record(candidate)
    except (KeyError, MVPRewriteError) as exc:
        raise ArbiterFamilyExecutionError(
            str(exc),
            code=getattr(exc, "code", "invalid_candidate"),
        ) from exc
    baseline_context = candidate.get("baseline_compile_context")
    if not isinstance(baseline_context, Mapping) or not _design_integrity(
        baseline,
        baseline_context,
    )["ok"]:
        raise ArbiterFamilyExecutionError(
            "baseline design changed after proof",
            code="stale_source_hashes",
        )
    return baseline, alternative
