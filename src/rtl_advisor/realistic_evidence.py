from __future__ import annotations

from dataclasses import asdict
import difflib
import json
from pathlib import Path
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
)
from rtl_advisor.mvp_schema import (
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
    run_p2_proof,
)
from rtl_advisor.transformation_registry import (
    ARBITER_TRANSFORMATION_ID,
    DEFAULT_TRANSFORMATION_REGISTRY,
    LEGACY_TRANSFORMATION_REGISTRY_HASHES,
)


REFERENCE_ID = "opentitan-prim-arbiter-ppc"
VARIANT_ID = "opentitan-prim-arbiter-tree-alternative"
UPSTREAM_PROJECT_ID = "lowrisc-opentitan"
UPSTREAM_RELATIVE_ROOT = Path("upstream/opentitan")
OPEN_TITAN_REVISION = "99fb7bd3b2330a1bda61b1d3382198d0c7fee8d7"
SLANG_FRONTEND = "yosys-slang"
SLANG_PLUGIN_PATH = "/opt/oss-cad-suite/share/yosys/plugins/slang.so"
ARBITER_CONFIGURATIONS = (
    {"configuration_id": "n01-dw32", "N": 1, "DW": 32, "EnDataPort": 1},
    {"configuration_id": "n04-dw32", "N": 4, "DW": 32, "EnDataPort": 1},
    {"configuration_id": "n08-dw32", "N": 8, "DW": 32, "EnDataPort": 1},
    {"configuration_id": "n16-dw32", "N": 16, "DW": 32, "EnDataPort": 1},
)
M2_CONFIGURATION_IDS = ("n08-dw32", "n16-dw32")
P2_BACKEND = "sby-p2-same-cycle-v5"
P2_SEMANTICS = (
    "Cycle-aligned sequential equivalence under the recorded reset and "
    "ready/request payload-stability protocol assumptions"
)
_EXPECTED_EXTRA_SOURCE_HASHES = {
    "hw/ip/prim/rtl/prim_arbiter_tree.sv": (
        "cad891e0479a377896ecbcddaf6dd7d5b9aa6e5b18ba23654b61795fcf3083e1"
    ),
    "hw/ip/prim/rtl/prim_assert.sv": (
        "d717d5dbcba3b5aa8a731ef9f8af18b036b49edef282f0a43a51f5ad2dd9bb40"
    ),
    "hw/ip/prim/rtl/prim_assert_dummy_macros.svh": (
        "cac4a930105da662547de873f0b80246074fb22df9a398663e1c8ac3e7998218"
    ),
    "hw/ip/prim/rtl/prim_assert_sec_cm.svh": (
        "25db89fe5f250c1bcbd808d0b4808206b9e85ac337b26e4ebb23f2ad569e1a13"
    ),
    "hw/ip/prim/rtl/prim_flop_macros.sv": (
        "2e8e6c2ee484899ae5d0020eaf0c9732c31537c60c30cb2a0a0acd2e92baee03"
    ),
}
_SOURCE_PATHS = (
    "hw/ip/prim/rtl/prim_util_pkg.sv",
    "hw/ip/prim/rtl/prim_leading_one_ppc.sv",
    "hw/ip/prim/rtl/prim_arbiter_ppc.sv",
    "hw/ip/prim/rtl/prim_arbiter_tree.sv",
)
_FORMAL_SUPPORT_PATHS = (
    "hw/ip/prim/rtl/prim_assert.sv",
    "hw/ip/prim/rtl/prim_assert_dummy_macros.svh",
    "hw/ip/prim/rtl/prim_assert_sec_cm.svh",
    "hw/ip/prim/rtl/prim_flop_macros.sv",
)


class RealisticEvidenceError(RuntimeError):
    """Raised when the frozen realistic-RTL study cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "realistic_evidence_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


def _read_reference_manifest(path: str | Path) -> tuple[dict[str, Any], ReferenceManifestV1]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RealisticEvidenceError(
            f"invalid corpus reference manifest {manifest_path}: {exc}",
            code="invalid_reference_manifest",
        ) from exc
    if not isinstance(raw, dict):
        raise RealisticEvidenceError(
            "corpus reference manifest must be an object",
            code="invalid_reference_manifest",
        )
    if "semantic_hash" in raw:
        try:
            raw = read_hashed_json(manifest_path)
        except Exception as exc:
            raise RealisticEvidenceError(
                f"reference manifest hash validation failed: {exc}",
                code=getattr(exc, "code", "artifact_hash_mismatch"),
            ) from exc
    try:
        manifest = parse_reference_manifest(raw)
    except CorpusRegistryError as exc:
        raise RealisticEvidenceError(str(exc), code=exc.code) from exc
    return raw, manifest


def is_qualified_reference_manifest(path: str | Path) -> bool:
    candidate = Path(path).expanduser()
    if not candidate.is_file() or candidate.suffix.lower() != ".json":
        return False
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(raw, dict)
        and raw.get("document_type") == "rtl-advisor.reference-manifest"
    )


def load_supported_reference(
    config: ProjectConfig,
    path: str | Path,
) -> dict[str, Any]:
    raw, manifest = _read_reference_manifest(path)
    if manifest.reference_id != REFERENCE_ID:
        raise RealisticEvidenceError(
            f"no registered realistic-RTL transformation supports "
            f"{manifest.reference_id!r}",
            code="unsupported_reference",
        )
    if (
        manifest.qualification.status != "active"
        or manifest.qualification.state not in {"reference_qualified", "variant_eligible"}
    ):
        raise RealisticEvidenceError(
            f"reference {manifest.reference_id} is not qualified",
            code="reference_not_qualified",
        )
    if manifest.provenance.revision != OPEN_TITAN_REVISION:
        raise RealisticEvidenceError(
            "OpenTitan reference revision does not match the frozen study",
            code="stale_reference_revision",
        )
    if manifest.lineage.upstream_project_id != UPSTREAM_PROJECT_ID:
        raise RealisticEvidenceError(
            "OpenTitan reference lineage does not match the frozen study",
            code="stale_reference_lineage",
        )
    source_root = (config.corpus_dir / UPSTREAM_RELATIVE_ROOT).resolve()
    expected_hashes = dict(manifest.source_hashes)
    for relative, expected in {
        **expected_hashes,
        **_EXPECTED_EXTRA_SOURCE_HASHES,
    }.items():
        source = (source_root / relative).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise RealisticEvidenceError(
                f"reference source escapes the pinned upstream root: {relative}",
                code="unsafe_path",
            ) from exc
        if not source.is_file() or file_sha256(source) != expected:
            raise RealisticEvidenceError(
                f"reference source hash mismatch: {relative}",
                code="stale_source_hashes",
            )
    manifest_hash = raw.get("semantic_hash") or stable_hash(manifest.to_dict())
    return {
        "manifest": manifest,
        "manifest_payload": raw,
        "manifest_path": str(Path(path).expanduser().resolve()),
        "manifest_semantic_hash": manifest_hash,
        "source_root": str(source_root),
    }


def arbiter_findings(reference: Mapping[str, Any]) -> list[dict[str, Any]]:
    manifest = reference["manifest"]
    assert isinstance(manifest, ReferenceManifestV1)
    spec = DEFAULT_TRANSFORMATION_REGISTRY.get(ARBITER_TRANSFORMATION_ID)
    ppc_path = Path(str(reference["source_root"])) / (
        "hw/ip/prim/rtl/prim_arbiter_ppc.sv"
    )
    findings: list[dict[str, Any]] = []
    for configuration in ARBITER_CONFIGURATIONS:
        core = {
            "reference_id": manifest.reference_id,
            "variant_id": VARIANT_ID,
            "configuration_id": configuration["configuration_id"],
            "transformation_id": spec.transformation_id,
            "transformation_version": spec.version,
            "reference_manifest_hash": reference["manifest_semantic_hash"],
            "transformation_registry_hash": (
                DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
            ),
        }
        findings.append(
            {
                "finding_id": f"arbsite_{stable_hash(core)[:16]}",
                "transformation_id": spec.transformation_id,
                "transformation_version": spec.version,
                "transformation_registry_hash": (
                    DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
                ),
                "candidate_origin": "upstream_alternative",
                "reference_id": manifest.reference_id,
                "variant_id": VARIANT_ID,
                "configuration": dict(configuration),
                "proof_level": "P2",
                "measurement_levels": (
                    ["M0", "M1", "M2"]
                    if configuration["configuration_id"] in M2_CONFIGURATION_IDS
                    else ["M0", "M1"]
                ),
                "source": {
                    "path": str(ppc_path),
                    "sha256": file_sha256(ppc_path),
                    "line": 1,
                },
                "summary": (
                    "Compare the frozen PPC and binary-tree arbiter "
                    f"implementations at N={configuration['N']}, "
                    f"DW={configuration['DW']} without changing cycle behavior."
                ),
                "objective": "timing",
            }
        )
    return findings


def write_reference_input(
    path: Path,
    reference: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = reference["manifest"]
    assert isinstance(manifest, ReferenceManifestV1)
    source_root = Path(str(reference["source_root"]))
    source_hashes = {
        relative: file_sha256(source_root / relative)
        for relative in _SOURCE_PATHS
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
        "provenance": {
            "project": manifest.provenance.project,
            "revision": manifest.provenance.revision,
            "license_expression": manifest.provenance.license.expression,
        },
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
    }
    expected = {**payload, "semantic_hash": stable_hash(payload)}
    if path.is_file():
        current = read_hashed_json(
            path,
            document_type="rtl-advisor.run.reference-input",
            schema_version=1,
        )
        if current != expected:
            raise RealisticEvidenceError(
                "append-only reference input conflicts with the current study",
                code="append_only_conflict",
            )
        return current
    return write_hashed_json(path, payload, exclusive=True)


def validate_reference_input(record: Mapping[str, Any]) -> None:
    expected_hash = record.get("semantic_hash")
    core = {key: value for key, value in record.items() if key != "semantic_hash"}
    if expected_hash != stable_hash(core):
        raise RealisticEvidenceError(
            "reference input semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    if (
        record.get("document_type") != "rtl-advisor.run.reference-input"
        or record.get("kind") != "qualified_reference"
        or record.get("reference_id") != REFERENCE_ID
    ):
        raise RealisticEvidenceError(
            "invalid realistic-RTL reference input",
            code="invalid_reference_input",
        )
    if record.get("transformation_registry_hash") not in {
        DEFAULT_TRANSFORMATION_REGISTRY.registry_hash,
        *LEGACY_TRANSFORMATION_REGISTRY_HASHES,
    }:
        raise RealisticEvidenceError(
            "reference input uses a stale transformation registry",
            code="stale_transformation_registry",
        )
    source_root = Path(str(record.get("source_root", ""))).expanduser().resolve()
    hashes = record.get("source_hashes")
    if not isinstance(hashes, Mapping):
        raise RealisticEvidenceError(
            "reference input lacks source hashes",
            code="invalid_reference_input",
        )
    for relative, expected in hashes.items():
        source = (source_root / str(relative)).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise RealisticEvidenceError(
                "reference input contains an unsafe source path",
                code="unsafe_path",
            ) from exc
        if not source.is_file() or file_sha256(source) != expected:
            raise RealisticEvidenceError(
                f"reference source changed after review: {relative}",
                code="stale_source_hashes",
            )


def _arbiter_wrapper(configuration: Mapping[str, Any], implementation: str) -> str:
    n = int(configuration["N"])
    dw = int(configuration["DW"])
    module = {
        "ppc": "prim_arbiter_ppc",
        "tree": "prim_arbiter_tree",
    }[implementation]
    index_width = max(1, (n - 1).bit_length())
    index_connection = (
        ".idx_o()" if n == 1 else ".idx_o(idx_internal)"
    )
    index_assignment = (
        "  assign idx_o = '0;\n"
        if n == 1
        else "  assign idx_o = idx_internal;\n"
    )
    return f"""\
module rtl_advisor_arbiter_evidence_top (
  input  logic clk_i,
  input  logic rst_ni,
  input  logic [{n - 1}:0] req_i,
  input  logic [{dw - 1}:0] data_i [{n}],
  input  logic ready_i,
  output logic [{n - 1}:0] gnt_o,
  output logic [{index_width - 1}:0] idx_o,
  output logic valid_o,
  output logic [{dw - 1}:0] data_o
);
  logic [{index_width - 1}:0] idx_internal;
  {module} #(
    .N({n}),
    .DW({dw}),
    .EnDataPort(1'b1)
  ) implementation_i (
    .clk_i,
    .rst_ni,
    .req_chk_i(1'b0),
    .req_i,
    .data_i,
    .gnt_o,
    {index_connection},
    .valid_o,
    .data_o,
    .ready_i
  );
{index_assignment}endmodule
"""


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


def prepare_arbiter_candidate(
    config: ProjectConfig,
    reference_input: Mapping[str, Any],
    finding: Mapping[str, Any],
    artifact_root: str | Path,
) -> dict[str, Any]:
    validate_reference_input(reference_input)
    if finding.get("transformation_id") != ARBITER_TRANSFORMATION_ID:
        raise RealisticEvidenceError(
            "finding does not select the arbiter transformation",
            code="invalid_finding",
        )
    configuration = finding.get("configuration")
    if not isinstance(configuration, Mapping) or dict(configuration) not in [
        dict(item) for item in ARBITER_CONFIGURATIONS
    ]:
        raise RealisticEvidenceError(
            "finding has an unsupported arbiter configuration",
            code="unsupported_configuration",
        )
    proof_contract = {
        "schema": "rtl-advisor-proof-v1",
        "level": "P2",
        "kind": "cycle_aligned_sequential_equivalence",
        "latency_relation": "same_cycle",
        "reset": {
            "clock": "clk_i",
            "signal": "rst_ni",
            "sequence": (
                "asserted at formal time zero, released after one clock, "
                "and never reasserted"
            ),
        },
        "assumptions": [
            "requests may arrive arbitrarily",
            "an asserted request remains asserted until its grant",
            "payload remains stable while its request is outstanding",
            "ready_i is unconstrained",
        ],
        "observables": [
            "valid_o and gnt_o every cycle",
            "idx_o and data_o whenever valid_o is asserted",
        ],
    }
    candidate_core = {
        "finding_id": finding["finding_id"],
        "reference_manifest_hash": reference_input["manifest_semantic_hash"],
        "configuration": dict(configuration),
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "proof_contract_hash": stable_hash(proof_contract),
    }
    candidate_id = f"arbcand_{stable_hash(candidate_core)[:16]}"
    candidate_dir = Path(artifact_root).expanduser().resolve() / candidate_id
    record_path = candidate_dir / "candidate-core.json"
    if record_path.is_file():
        cached = read_hashed_json(
            record_path,
            document_type=CANDIDATE_DOCUMENT_TYPE,
            schema_version=RUN_SCHEMA_VERSION,
        )
        DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(cached)
        return cached

    baseline_root = candidate_dir / "baseline"
    alternative_root = candidate_dir / "candidate"
    baseline_root.mkdir(parents=True, exist_ok=True)
    alternative_root.mkdir(parents=True, exist_ok=True)
    wrapper_name = "rtl_advisor_arbiter_evidence_top.sv"
    baseline_wrapper = baseline_root / wrapper_name
    alternative_wrapper = alternative_root / wrapper_name
    baseline_wrapper.write_text(
        _arbiter_wrapper(configuration, "ppc"),
        encoding="utf-8",
    )
    alternative_wrapper.write_text(
        _arbiter_wrapper(configuration, "tree"),
        encoding="utf-8",
    )
    source_root = Path(str(reference_input["source_root"]))
    shared_sources = [source_root / relative for relative in _SOURCE_PATHS]
    include_dir = source_root / "hw/ip/prim/rtl"
    baseline = normalize_design_input(
        top="rtl_advisor_arbiter_evidence_top",
        files=(*shared_sources, baseline_wrapper),
        include_dirs=(include_dir,),
        defines=("SYNTHESIS",),
        base=config.root,
    )
    candidate = normalize_design_input(
        top="rtl_advisor_arbiter_evidence_top",
        files=(*shared_sources, alternative_wrapper),
        include_dirs=(include_dir,),
        defines=("SYNTHESIS",),
        base=config.root,
    )
    baseline_context = compile_context_snapshot(baseline)
    candidate_context = compile_context_snapshot(candidate)
    diff = "".join(
        difflib.unified_diff(
            baseline_wrapper.read_text(encoding="utf-8").splitlines(keepends=True),
            alternative_wrapper.read_text(encoding="utf-8").splitlines(keepends=True),
            fromfile=f"reference/{wrapper_name}",
            tofile=f"candidate/{wrapper_name}",
        )
    )
    diff_path = candidate_dir / "candidate.diff"
    diff_path.write_text(diff, encoding="utf-8")
    spec = DEFAULT_TRANSFORMATION_REGISTRY.get(ARBITER_TRANSFORMATION_ID)
    record = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": CANDIDATE_DOCUMENT_TYPE,
        "status": "candidate_prepared",
        "candidate_id": candidate_id,
        "finding_id": finding["finding_id"],
        "transformation_id": spec.transformation_id,
        "transformation_version": spec.version,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": finding.get("executor_id", "opentitan-ppc-tree"),
        "executor_version": finding.get("executor_version", "1"),
        "executor_registry_hash": finding.get("executor_registry_hash"),
        "candidate_origin": "upstream_alternative",
        "reference_id": REFERENCE_ID,
        "reference_manifest_hash": reference_input["manifest_semantic_hash"],
        "variant_id": VARIANT_ID,
        "configuration": dict(configuration),
        "configuration_id": configuration["configuration_id"],
        "frontend": {
            "kind": SLANG_FRONTEND,
            "plugin_path": SLANG_PLUGIN_PATH,
        },
        "proof_contract": proof_contract,
        "proof_contract_hash": stable_hash(proof_contract),
        "measurement_levels": finding["measurement_levels"],
        "baseline_design": _design_mapping(baseline),
        "candidate_design": _design_mapping(candidate),
        "baseline_compile_context": baseline_context,
        "candidate_compile_context": candidate_context,
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
            "baseline": source_integrity(
                asdict(item) for item in baseline.files
            ),
            "candidate": source_integrity(
                asdict(item) for item in candidate.files
            ),
        },
        "lint": {"status": "not_run"},
        "formal": {"status": "not_run", "safe": False},
        "limitations": [
            "The candidate is isolated but unproven.",
            "The candidate selects a frozen upstream alternative; it is not "
            "an unseen-RTL rewrite.",
        ],
    }
    stored = write_hashed_json(record_path, record, exclusive=True)
    DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(stored)
    return stored


def validate_arbiter_candidate(candidate: Mapping[str, Any]) -> None:
    try:
        DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(candidate)
    except Exception as exc:
        raise RealisticEvidenceError(
            str(exc),
            code=getattr(exc, "code", "invalid_candidate"),
        ) from exc
    semantic_hash = candidate.get("semantic_hash")
    if semantic_hash != stable_hash(
        {key: value for key, value in candidate.items() if key != "semantic_hash"}
    ):
        raise RealisticEvidenceError(
            "arbiter candidate semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    candidate_dir = Path(str(candidate.get("artifact_dir", ""))).resolve()
    diff_path = Path(str(candidate.get("diff_path", ""))).resolve()
    try:
        diff_path.relative_to(candidate_dir)
    except ValueError as exc:
        raise RealisticEvidenceError(
            "arbiter candidate diff escapes its artifact workspace",
            code="unsafe_path",
        ) from exc
    if not diff_path.is_file() or file_sha256(diff_path) != candidate.get(
        "diff_sha256"
    ):
        raise RealisticEvidenceError(
            "arbiter candidate diff is stale",
            code="stale_candidate",
        )
    from rtl_advisor.mvp_rewriter import candidate_design_from_record

    try:
        candidate_design_from_record(candidate)
    except MVPRewriteError as exc:
        raise RealisticEvidenceError(str(exc), code=exc.code) from exc


def arbiter_designs_from_candidate(
    candidate: Mapping[str, Any],
) -> tuple[DesignInputV2, DesignInputV2]:
    """Return the hash-validated baseline and alternative design inputs."""

    validate_arbiter_candidate(candidate)
    try:
        return (
            _design_from_mapping(candidate["baseline_design"]),
            _design_from_mapping(candidate["candidate_design"]),
        )
    except (KeyError, MVPRewriteError) as exc:
        raise RealisticEvidenceError(
            f"invalid arbiter design record: {exc}",
            code=getattr(exc, "code", "invalid_candidate"),
        ) from exc


def _project_relative(config: ProjectConfig, path: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(config.root.resolve()).as_posix()
    except ValueError as exc:
        raise RealisticEvidenceError(
            f"P2 input must remain inside the project workspace: {resolved}",
            code="unsafe_path",
        ) from exc


def _p2_miter(configuration: Mapping[str, Any]) -> str:
    n = int(configuration["N"])
    dw = int(configuration["DW"])
    index_width = max(1, (n - 1).bit_length())
    tree_index_signal = (
        "  logic [IDX_W-1:0] gate_idx_raw;\n"
        if n > 1
        else ""
    )
    tree_index_connection = (
        ".idx_o(gate_idx_raw)" if n > 1 else ".idx_o()"
    )
    tree_index_assignment = (
        "  wire [IDX_W-1:0] gate_idx = gate_idx_raw;\n"
        if n > 1
        else "  wire [IDX_W-1:0] gate_idx = '0;\n"
    )
    return f"""\
`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif

module rtl_advisor_arbiter_p2_miter (
  input logic clk_i,
  input logic [{n - 1}:0] req_i,
  input logic [{dw - 1}:0] data_i [{n}],
  input logic ready_i
);
  localparam int N = {n};
  localparam int DW = {dw};
  localparam int IDX_W = {index_width};
  localparam int Mutation = `MUTATION_KIND;

  logic formal_started = 1'b0;
  wire formal_rst_ni = formal_started;

  logic [N-1:0] gold_gnt;
  logic [IDX_W-1:0] gold_idx;
  logic gold_valid;
  logic [DW-1:0] gold_data;
  prim_arbiter_ppc #(
    .N(N),
    .DW(DW),
    .EnDataPort(1'b1)
  ) gold_dut (
    .clk_i,
    .rst_ni(formal_rst_ni),
    .req_chk_i(1'b0),
    .req_i,
    .data_i,
    .gnt_o(gold_gnt),
    .idx_o(gold_idx),
    .valid_o(gold_valid),
    .data_o(gold_data),
    .ready_i
  );

  wire gate_rst_ni = Mutation == 1 ? 1'b1 : formal_rst_ni;
  wire gate_ready_i = Mutation == 2 ? ready_i ^ gold_valid : ready_i;
  logic [N-1:0] gate_gnt_raw;
{tree_index_signal}  logic gate_valid;
  logic [DW-1:0] gate_data_raw;
  prim_arbiter_tree #(
    .N(N),
    .DW(DW),
    .EnDataPort(1'b1)
  ) gate_dut (
    .clk_i,
    .rst_ni(gate_rst_ni),
    .req_chk_i(1'b0),
    .req_i,
    .data_i,
    .gnt_o(gate_gnt_raw),
    {tree_index_connection},
    .valid_o(gate_valid),
    .data_o(gate_data_raw),
    .ready_i(gate_ready_i)
  );
{tree_index_assignment}  wire [N-1:0] gate_gnt =
      Mutation == 3 ? '0 : gate_gnt_raw;
  wire [DW-1:0] gate_data =
      Mutation == 4 && gate_valid
          ? gate_data_raw ^ {{{{(DW-1){{1'b0}}}}, 1'b1}}
          : gate_data_raw;

  // Protocol contract: a request may appear at any time. Once present, it and
  // its payload remain stable until the reference arbiter grants it.
  logic [N-1:0] previous_req;
  logic [N-1:0] previous_gnt;
  logic [DW-1:0] previous_data [N];
  always_ff @(posedge clk_i) begin
    formal_started <= 1'b1;
    if (formal_started) begin
      for (int i = 0; i < N; i++) begin
        if (previous_req[i] && !previous_gnt[i]) begin
          assume (req_i[i]);
          assume (data_i[i] == previous_data[i]);
        end
      end
    end
    previous_req <= req_i;
    previous_gnt <= gold_gnt;
    for (int i = 0; i < N; i++) begin
      previous_data[i] <= data_i[i];
    end
  end

  always_comb begin
    if (formal_started) begin
      assert (gold_valid == gate_valid);
      assert (gold_gnt == gate_gnt);
      if (gold_valid && gate_valid) begin
        assert (gold_idx == gate_idx);
        assert (gold_data == gate_data);
      end
    end
  end
endmodule
"""


def _p2_sby(
    configuration: Mapping[str, Any],
    *,
    mutation_kind: int = 0,
) -> str:
    mutation_define = (
        ""
        if mutation_kind == 0
        else f" -D MUTATION_KIND={mutation_kind}"
    )
    return f"""\
[tasks]
positive

[options]
mode prove
depth 16
expect pass

[engines]
abc pdr

[script]
plugin -i {SLANG_PLUGIN_PATH}
read_slang --top rtl_advisor_arbiter_p2_miter -D SYNTHESIS{mutation_define} -I . prim_util_pkg.sv prim_leading_one_ppc.sv prim_arbiter_ppc.sv prim_arbiter_tree.sv rtl_advisor_arbiter_p2_miter.sv
prep -top rtl_advisor_arbiter_p2_miter

[files]
"""


def _create_p2_plan(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    *,
    mutation_kind: int = 0,
) -> Path:
    configuration = candidate["configuration"]
    configuration_id = str(candidate["configuration_id"])
    suffix = "positive" if mutation_kind == 0 else f"bad-{mutation_kind}"
    formal_root = (
        Path(str(candidate["artifact_dir"])) / "formal" / "plans" / suffix
    )
    formal_root.mkdir(parents=True, exist_ok=True)
    miter_path = formal_root / "rtl_advisor_arbiter_p2_miter.sv"
    sby_path = formal_root / "rtl_advisor_arbiter_p2.sby"
    miter_path.write_text(_p2_miter(configuration), encoding="utf-8")
    sby_text = _p2_sby(configuration, mutation_kind=mutation_kind)
    source_root = Path(
        str(
            Path(str(candidate["baseline_design"]["files"][0]["path"]))
            .parents[4]
        )
    )
    upstream_sources = [
        source_root / relative
        for relative in (*_SOURCE_PATHS, *_FORMAL_SUPPORT_PATHS)
    ]
    sby_path.write_text(
        sby_text
        + "\n".join(
            _project_relative(config, path)
            for path in (*upstream_sources, miter_path)
        )
        + "\n",
        encoding="utf-8",
    )
    input_paths = (sby_path, miter_path, *upstream_sources)
    baseline_wrapper = Path(
        str(candidate["baseline_design"]["files"][-1]["path"])
    )
    candidate_wrapper = Path(
        str(candidate["candidate_design"]["files"][-1]["path"])
    )
    expected_relation = (
        "equivalent" if mutation_kind == 0 else "inequivalent_control"
    )
    plan = {
        "schema_version": 1,
        "schema": "rtl-advisor-p2-proof-plan-v1",
        "document_type": "rtl-advisor.p2-proof-plan",
        "proof_id": (
            f"realistic-arbiter-{configuration_id}-{suffix}-p2-v5"
        ),
        "variant_id": (
            f"{VARIANT_ID}-{configuration_id}"
            if mutation_kind == 0
            else f"{VARIANT_ID}-{configuration_id}-{suffix}"
        ),
        "parent_reference_id": REFERENCE_ID,
        "tranche_id": "realistic-rtl-evidence-slice-v1",
        "tranche_semantic_hash": stable_hash(
            {
                "reference_id": REFERENCE_ID,
                "revision": OPEN_TITAN_REVISION,
                "configurations": [
                    dict(item) for item in ARBITER_CONFIGURATIONS
                ],
            }
        ),
        "expected_relation": expected_relation,
        "registration": {
            "variant_lineage_id": (
                f"opentitan-arbiter-topology-{configuration_id}-{suffix}"
            ),
            "display_name": (
                f"OpenTitan arbiter tree {configuration_id} {suffix}"
            ),
            "origin": {
                "kind": (
                    "upstream_alternative"
                    if mutation_kind == 0
                    else "negative_control"
                ),
                "description": (
                    "Frozen upstream tree alternative under the strengthened "
                    "same-cycle protocol contract."
                    if mutation_kind == 0
                    else "Deliberately incorrect P2 checker control."
                ),
                "generator": None,
                "generator_version": None,
            },
            "source_locations": [
                _project_relative(config, baseline_wrapper),
                _project_relative(config, candidate_wrapper),
            ],
            "source_changes": [
                {
                    "path": _project_relative(config, baseline_wrapper),
                    "before_sha256": file_sha256(baseline_wrapper),
                    "after_sha256": file_sha256(candidate_wrapper),
                }
            ],
        },
        "backend": "sby_miter",
        "formal_config": _project_relative(config, sby_path),
        "task": "positive",
        "contract": candidate["proof_contract"],
        "tools": {
            "yosys_version": "0.63",
            "sby_version": "SBY v0.63",
            "make_version": "GNU Make 4.3",
        },
        "inputs": [
            {
                "path": _project_relative(config, path),
                "sha256": file_sha256(path),
            }
            for path in input_paths
        ],
    }
    plan_path = formal_root / "plan.json"
    write_hashed_json(plan_path, plan)
    return plan_path


def verify_arbiter_candidate(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
    artifact_root: str | Path,
) -> dict[str, Any]:
    validate_arbiter_candidate(candidate)
    candidate_id = str(candidate["candidate_id"])
    expected_dir = Path(artifact_root).expanduser().resolve() / candidate_id
    if Path(str(candidate["artifact_dir"])).resolve() != expected_dir:
        raise RealisticEvidenceError(
            "arbiter candidate does not belong to the requested artifact root",
            code="artifact_root_mismatch",
        )
    baseline = _design_from_mapping(candidate["baseline_design"])
    alternative = _design_from_mapping(candidate["candidate_design"])
    baseline_context = candidate["baseline_compile_context"]
    candidate_context = candidate["candidate_compile_context"]
    before = {
        "baseline": _design_integrity(baseline, baseline_context),
        "candidate": _design_integrity(alternative, candidate_context),
    }
    if not all(item["ok"] for item in before.values()):
        raise RealisticEvidenceError(
            "arbiter source or compile context changed before proof",
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
    if {baseline_lint["status"], candidate_lint["status"]} != {"passed"}:
        formal = {
            "backend": P2_BACKEND,
            "semantics": P2_SEMANTICS,
            "status": "inconclusive",
            "detail": "baseline or candidate did not pass Verilator lint",
        }
        p2_result = None
    else:
        try:
            plan_path = _create_p2_plan(config, candidate)
            p2_result = run_p2_proof(config, plan_path=plan_path)
        except (RealisticEvidenceError, SequentialEquivalenceError) as exc:
            formal = {
                "backend": P2_BACKEND,
                "semantics": P2_SEMANTICS,
                "status": "inconclusive",
                "detail": str(exc),
                "error_code": getattr(exc, "code", "formal_inconclusive"),
            }
            p2_result = None
        else:
            formal = {
                "backend": P2_BACKEND,
                "semantics": P2_SEMANTICS,
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
                "p2_result_path": str(
                    config.artifacts_dir
                    / "formal"
                    / "p2"
                    / p2_result["proof_id"]
                    / p2_result["plan_semantic_hash"]
                    / "result.json"
                ),
                "p2_transcript_path": p2_result["transcript_path"],
                "p2_transcript_sha256": p2_result["transcript_sha256"],
                "success_marker_seen": (
                    p2_result["status"] == "formal_passed"
                    and p2_result["safe"] is True
                ),
                "detail": None,
            }
    after = {
        "baseline": _design_integrity(baseline, baseline_context),
        "candidate": _design_integrity(alternative, candidate_context),
    }
    current = all(item["ok"] for item in after.values())
    if not current:
        formal = {
            **formal,
            "status": "inconclusive",
            "detail": "source integrity changed during P2 verification",
        }
    safe = formal["status"] == "passed" and current
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": FORMAL_DOCUMENT_TYPE,
        "status": formal["status"],
        "safe": safe,
        "candidate_id": candidate_id,
        "reference_id": REFERENCE_ID,
        "configuration_id": candidate["configuration_id"],
        "transformation_id": ARBITER_TRANSFORMATION_ID,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "proof_contract": candidate["proof_contract"],
        "proof_contract_hash": candidate["proof_contract_hash"],
        "baseline_design_hash": baseline.design_hash,
        "candidate_design_hash": alternative.design_hash,
        "compile_context": {
            "baseline": baseline_context,
            "candidate": candidate_context,
        },
        "source_integrity": after,
        "lint": lint,
        "formal": formal,
        "p2_result": p2_result,
        "artifacts": {
            "root": str(output_dir),
            "formal": str(result_path),
        },
        "record_path": str(result_path),
        "limitations": [
            P2_SEMANTICS,
            "The proof does not establish four-state X/Z equivalence.",
        ],
    }
    return write_hashed_json(result_path, payload, exclusive=True)


def run_arbiter_negative_controls(
    config: ProjectConfig,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    validate_arbiter_candidate(candidate)
    labels = {
        1: "bad_reset",
        2: "bad_state",
        3: "bad_grant",
        4: "bad_data",
    }
    results: dict[str, Any] = {}
    for mutation_kind, label in labels.items():
        plan_path = _create_p2_plan(
            config,
            candidate,
            mutation_kind=mutation_kind,
        )
        results[label] = run_p2_proof(config, plan_path=plan_path)
    passed = all(
        item["observed_relation"] == "inequivalent"
        and item["expectation_met"] is True
        and item["safe"] is False
        for item in results.values()
    )
    return {
        "status": "passed" if passed else "failed",
        "configuration_id": candidate["configuration_id"],
        "results": results,
    }
