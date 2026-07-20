from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

from rtl_advisor.mvp_schema import (
    MVPSchemaError,
    read_hashed_json,
    write_hashed_json,
)


REGISTRY_SCHEMA_ID = "rtl-advisor-corpus-v1"
REFERENCE_SCHEMA_ID = "rtl-advisor-reference-v1"
VARIANT_SCHEMA_ID = "rtl-advisor-variant-v1"
PROOF_SCHEMA_ID = "rtl-advisor-proof-v1"
SCHEMA_VERSION = 1
REFERENCE_DOCUMENT_TYPE = "rtl-advisor.reference-manifest"
VARIANT_DOCUMENT_TYPE = "rtl-advisor.variant-manifest"

TIERS = ("A", "B", "C", "D")
CATEGORIES = (
    "arithmetic_datapath",
    "selection_arbitration",
    "buffering_flow_control",
    "decode_address_routing",
    "bus_adapter_interconnect",
    "pipeline_register_placement",
    "state_control_queue",
    "memory_request_tracking",
    "processor_accelerator_stage",
    "generated_hierarchy_cdc",
)
QUALIFICATION_STATES = (
    "discovered",
    "license_reviewed",
    "source_pinned",
    "build_reproduced",
    "behavior_baselined",
    "reference_qualified",
    "variant_eligible",
)
QUALIFICATION_STATUSES = ("active", "blocked", "rejected")
REVISION_KINDS = ("unresolved", "commit", "tag", "release")
LICENSE_DISPOSITIONS = ("pending", "approved", "conditional", "restricted")
SPLIT_ROLES = (
    "unassigned",
    "development",
    "calibration",
    "evaluation",
    "release_holdout",
)
PROOF_LEVELS = ("P0", "P1", "P2", "P3", "P4", "P5", "P6")
LATENCY_RELATIONS = (
    "not_applicable",
    "combinational",
    "same_cycle",
    "fixed_offset",
    "transaction_ordered",
    "structural_partition",
)
BEHAVIORAL_BASIS_KINDS = (
    "upstream_tests",
    "reference_model",
    "assertions",
    "specification",
    "documented_pair",
)
VARIANT_ORIGINS = (
    "deterministic_rewrite",
    "upstream_alternative",
    "upstream_parameter",
    "independent_implementation",
    "reviewed_codex_candidate",
    "negative_control",
)
VARIANT_STATES = (
    "declared",
    "prepared",
    "proof_passed",
    "proof_failed",
    "proof_inconclusive",
    "measured",
)
EXPECTED_RELATIONS = ("equivalent", "inequivalent_control", "unknown")

_PROOF_LATENCY_RELATIONS = {
    "P0": {"not_applicable"},
    "P1": {"combinational"},
    "P2": {"same_cycle"},
    "P3": {"fixed_offset", "transaction_ordered"},
    "P4": {"transaction_ordered", "structural_partition"},
    "P5": {"structural_partition"},
    "P6": {
        "combinational",
        "same_cycle",
        "fixed_offset",
        "transaction_ordered",
        "structural_partition",
    },
}

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CorpusRegistryError(ValueError):
    """Raised when a corpus registry record is invalid or inconsistent."""

    def __init__(self, message: str, *, code: str = "invalid_corpus_record") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QualificationV1:
    state: str
    status: str
    reason: str | None


@dataclass(frozen=True)
class LicenseV1:
    expression: str
    url: str
    sha256: str | None
    disposition: str


@dataclass(frozen=True)
class ProvenanceV1:
    project: str
    canonical_url: str
    revision: str | None
    revision_kind: str
    license: LicenseV1


@dataclass(frozen=True)
class LineageV1:
    upstream_project_id: str
    repository_lineage_id: str
    design_lineage_id: str
    containing_design_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClockV1:
    name: str
    edge: str
    period_ns: float | None


@dataclass(frozen=True)
class ResetV1:
    name: str
    polarity: str
    synchronous: bool


@dataclass(frozen=True)
class CompileContextV1:
    top: str
    sources: tuple[str, ...]
    filelist: str | None
    include_dirs: tuple[str, ...]
    defines: tuple[str, ...]
    parameters: tuple[tuple[str, bool | int | float | str], ...]
    generated_inputs: tuple[str, ...]
    clocks: tuple[ClockV1, ...]
    resets: tuple[ResetV1, ...]
    memories: tuple[str, ...]
    black_boxes: tuple[str, ...]
    frontend: str | None
    frontend_version: str | None
    build_commands: tuple[str, ...]
    lint_commands: tuple[str, ...]
    test_commands: tuple[str, ...]


@dataclass(frozen=True)
class CompileContextHashesV1:
    filelist_sha256: str | None
    include_tree_hashes: tuple[tuple[str, str], ...]
    generated_input_hashes: tuple[tuple[str, str], ...]
    compile_context_hash: str | None


@dataclass(frozen=True)
class BehavioralBasisV1:
    kind: str
    location: str
    description: str


@dataclass(frozen=True)
class ProofContractV1:
    schema: str
    level: str
    kind: str
    latency_relation: str
    assumptions: tuple[str, ...]
    observables: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceManifestV1:
    schema_version: int
    schema: str
    document_type: str
    reference_id: str
    display_name: str
    tier: str
    categories: tuple[str, ...]
    qualification: QualificationV1
    provenance: ProvenanceV1
    lineage: LineageV1
    compile_context: CompileContextV1
    compile_context_hashes: CompileContextHashesV1
    behavioral_basis: tuple[BehavioralBasisV1, ...]
    proof_contract: ProofContractV1
    source_hashes: tuple[tuple[str, str], ...]
    synthesis_profiles: tuple[str, ...]
    split: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["compile_context"]["parameters"] = {
            key: value for key, value in self.compile_context.parameters
        }
        payload["source_hashes"] = [
            {"path": path, "sha256": digest}
            for path, digest in self.source_hashes
        ]
        payload["compile_context_hashes"]["include_tree_hashes"] = [
            {"path": path, "sha256": digest}
            for path, digest in self.compile_context_hashes.include_tree_hashes
        ]
        payload["compile_context_hashes"]["generated_input_hashes"] = [
            {"path": path, "sha256": digest}
            for path, digest in self.compile_context_hashes.generated_input_hashes
        ]
        return json.loads(json.dumps(payload))


@dataclass(frozen=True)
class VariantOriginV1:
    kind: str
    description: str
    generator: str | None
    generator_version: str | None


@dataclass(frozen=True)
class SourceChangeV1:
    path: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class VariantManifestV1:
    schema_version: int
    schema: str
    document_type: str
    variant_id: str
    parent_reference_id: str
    variant_lineage_id: str
    display_name: str
    state: str
    origin: VariantOriginV1
    source_locations: tuple[str, ...]
    source_changes: tuple[SourceChangeV1, ...]
    expected_relation: str
    proof_contract: ProofContractV1
    artifact_hashes: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_hashes"] = {
            key: value for key, value in self.artifact_hashes
        }
        return json.loads(json.dumps(payload))


ManifestV1 = ReferenceManifestV1 | VariantManifestV1
CorpusReferenceManifestV1 = ReferenceManifestV1
CorpusVariantManifestV1 = VariantManifestV1
CorpusProofContractV1 = ProofContractV1


def _expect_fields(
    raw: Mapping[str, Any],
    *,
    allowed: Iterable[str],
    context: str,
) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise CorpusRegistryError(
            f"{context} contains unknown fields: {', '.join(unknown)}",
            code="unknown_fields",
        )


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CorpusRegistryError(f"{context} must be an object")
    return value


def _string(value: Any, context: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if nullable else ""
        raise CorpusRegistryError(f"{context} must be a non-empty string{suffix}")
    return value


def _identifier(value: Any, context: str) -> str:
    result = _string(value, context)
    assert result is not None
    if not _ID_PATTERN.fullmatch(result):
        raise CorpusRegistryError(
            f"{context} must match {_ID_PATTERN.pattern!r}",
            code="invalid_identifier",
        )
    return result


def _choice(value: Any, choices: Iterable[str], context: str) -> str:
    result = _string(value, context)
    assert result is not None
    allowed = tuple(choices)
    if result not in allowed:
        raise CorpusRegistryError(
            f"{context} must be one of {list(allowed)!r}",
            code="unsupported_value",
        )
    return result


def _string_tuple(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise CorpusRegistryError(f"{context} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise CorpusRegistryError(f"{context} must not contain duplicates")
    return tuple(value)


def _relative_path(value: Any, context: str, *, allow_dot: bool = False) -> str:
    result = _string(value, context)
    assert result is not None
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or (result == "." and not allow_dot):
        raise CorpusRegistryError(
            f"{context} must be a repository-relative path",
            code="unsafe_path",
        )
    return path.as_posix()


def _relative_paths(
    value: Any,
    context: str,
    *,
    allow_dot: bool = False,
) -> tuple[str, ...]:
    values = _string_tuple(value, context)
    return tuple(
        _relative_path(item, f"{context}[{index}]", allow_dot=allow_dot)
        for index, item in enumerate(values)
    )


def _sha256(value: Any, context: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    result = _string(value, context)
    assert result is not None
    if not _SHA256_PATTERN.fullmatch(result):
        raise CorpusRegistryError(
            f"{context} must be a lowercase 64-character SHA-256 digest",
            code="invalid_hash",
        )
    return result


def _parse_qualification(raw: Any) -> QualificationV1:
    data = _mapping(raw, "qualification")
    _expect_fields(
        data,
        allowed=("state", "status", "reason"),
        context="qualification",
    )
    status = _choice(data.get("status"), QUALIFICATION_STATUSES, "qualification.status")
    reason = _string(data.get("reason"), "qualification.reason", nullable=True)
    if status in {"blocked", "rejected"} and reason is None:
        raise CorpusRegistryError(
            "blocked or rejected qualification requires a reason",
            code="missing_status_reason",
        )
    if status == "active" and reason is not None:
        raise CorpusRegistryError("active qualification must not include a reason")
    return QualificationV1(
        state=_choice(data.get("state"), QUALIFICATION_STATES, "qualification.state"),
        status=status,
        reason=reason,
    )


def _parse_license(raw: Any) -> LicenseV1:
    data = _mapping(raw, "provenance.license")
    _expect_fields(
        data,
        allowed=("expression", "url", "sha256", "disposition"),
        context="provenance.license",
    )
    url = _string(data.get("url"), "provenance.license.url")
    assert url is not None
    if not url.startswith("https://"):
        raise CorpusRegistryError("provenance.license.url must use https")
    return LicenseV1(
        expression=str(_string(data.get("expression"), "provenance.license.expression")),
        url=url,
        sha256=_sha256(data.get("sha256"), "provenance.license.sha256", nullable=True),
        disposition=_choice(
            data.get("disposition"),
            LICENSE_DISPOSITIONS,
            "provenance.license.disposition",
        ),
    )


def _parse_provenance(raw: Any) -> ProvenanceV1:
    data = _mapping(raw, "provenance")
    _expect_fields(
        data,
        allowed=("project", "canonical_url", "revision", "revision_kind", "license"),
        context="provenance",
    )
    canonical_url = _string(data.get("canonical_url"), "provenance.canonical_url")
    assert canonical_url is not None
    if not canonical_url.startswith("https://"):
        raise CorpusRegistryError("provenance.canonical_url must use https")
    revision_kind = _choice(
        data.get("revision_kind"), REVISION_KINDS, "provenance.revision_kind"
    )
    revision = _string(data.get("revision"), "provenance.revision", nullable=True)
    if revision_kind == "unresolved" and revision is not None:
        raise CorpusRegistryError("unresolved provenance must not set a revision")
    if revision_kind != "unresolved" and revision is None:
        raise CorpusRegistryError("pinned provenance requires a revision")
    return ProvenanceV1(
        project=str(_string(data.get("project"), "provenance.project")),
        canonical_url=canonical_url,
        revision=revision,
        revision_kind=revision_kind,
        license=_parse_license(data.get("license")),
    )


def _parse_lineage(raw: Any) -> LineageV1:
    data = _mapping(raw, "lineage")
    _expect_fields(
        data,
        allowed=(
            "upstream_project_id",
            "repository_lineage_id",
            "design_lineage_id",
            "containing_design_ids",
        ),
        context="lineage",
    )
    return LineageV1(
        upstream_project_id=_identifier(
            data.get("upstream_project_id"), "lineage.upstream_project_id"
        ),
        repository_lineage_id=_identifier(
            data.get("repository_lineage_id"), "lineage.repository_lineage_id"
        ),
        design_lineage_id=_identifier(
            data.get("design_lineage_id"), "lineage.design_lineage_id"
        ),
        containing_design_ids=tuple(
            _identifier(item, f"lineage.containing_design_ids[{index}]")
            for index, item in enumerate(
                _string_tuple(
                    data.get("containing_design_ids"),
                    "lineage.containing_design_ids",
                )
            )
        ),
    )


def _parse_clock(raw: Any, index: int) -> ClockV1:
    context = f"compile_context.clocks[{index}]"
    data = _mapping(raw, context)
    _expect_fields(data, allowed=("name", "edge", "period_ns"), context=context)
    edge = _choice(data.get("edge"), ("rising", "falling"), f"{context}.edge")
    period = data.get("period_ns")
    if period is not None and (
        not isinstance(period, (int, float))
        or isinstance(period, bool)
        or float(period) <= 0
    ):
        raise CorpusRegistryError(f"{context}.period_ns must be positive or null")
    return ClockV1(
        name=str(_string(data.get("name"), f"{context}.name")),
        edge=edge,
        period_ns=float(period) if period is not None else None,
    )


def _parse_reset(raw: Any, index: int) -> ResetV1:
    context = f"compile_context.resets[{index}]"
    data = _mapping(raw, context)
    _expect_fields(
        data,
        allowed=("name", "polarity", "synchronous"),
        context=context,
    )
    synchronous = data.get("synchronous")
    if not isinstance(synchronous, bool):
        raise CorpusRegistryError(f"{context}.synchronous must be boolean")
    return ResetV1(
        name=str(_string(data.get("name"), f"{context}.name")),
        polarity=_choice(
            data.get("polarity"), ("active_high", "active_low"), f"{context}.polarity"
        ),
        synchronous=synchronous,
    )


def _parse_parameters(raw: Any) -> tuple[tuple[str, bool | int | float | str], ...]:
    data = _mapping(raw, "compile_context.parameters")
    parsed: list[tuple[str, bool | int | float | str]] = []
    for key in sorted(data):
        if not isinstance(key, str) or not key.strip():
            raise CorpusRegistryError("compile_context parameter names must be non-empty")
        value = data[key]
        if not isinstance(value, (bool, int, float, str)):
            raise CorpusRegistryError(
                f"compile_context parameter {key!r} must be a scalar"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise CorpusRegistryError(
                f"compile_context parameter {key!r} must be finite"
            )
        parsed.append((key, value))
    return tuple(parsed)


def _parse_compile_context(raw: Any) -> CompileContextV1:
    data = _mapping(raw, "compile_context")
    _expect_fields(
        data,
        allowed=(
            "top",
            "sources",
            "filelist",
            "include_dirs",
            "defines",
            "parameters",
            "generated_inputs",
            "clocks",
            "resets",
            "memories",
            "black_boxes",
            "frontend",
            "frontend_version",
            "build_commands",
            "lint_commands",
            "test_commands",
        ),
        context="compile_context",
    )
    filelist_raw = data.get("filelist")
    filelist = (
        _relative_path(filelist_raw, "compile_context.filelist")
        if filelist_raw is not None
        else None
    )
    clocks_raw = data.get("clocks")
    resets_raw = data.get("resets")
    if not isinstance(clocks_raw, list):
        raise CorpusRegistryError("compile_context.clocks must be an array")
    if not isinstance(resets_raw, list):
        raise CorpusRegistryError("compile_context.resets must be an array")
    frontend = _string(data.get("frontend"), "compile_context.frontend", nullable=True)
    frontend_version = _string(
        data.get("frontend_version"),
        "compile_context.frontend_version",
        nullable=True,
    )
    if (frontend is None) != (frontend_version is None):
        raise CorpusRegistryError(
            "compile_context frontend and frontend_version must be set together"
        )
    sources = _relative_paths(data.get("sources"), "compile_context.sources")
    if not sources and filelist is None:
        raise CorpusRegistryError(
            "compile_context requires at least one source or a filelist"
        )
    return CompileContextV1(
        top=str(_string(data.get("top"), "compile_context.top")),
        sources=sources,
        filelist=filelist,
        include_dirs=_relative_paths(
            data.get("include_dirs"),
            "compile_context.include_dirs",
            allow_dot=True,
        ),
        defines=_string_tuple(data.get("defines"), "compile_context.defines"),
        parameters=_parse_parameters(data.get("parameters")),
        generated_inputs=_relative_paths(
            data.get("generated_inputs"), "compile_context.generated_inputs"
        ),
        clocks=tuple(
            _parse_clock(item, index) for index, item in enumerate(clocks_raw)
        ),
        resets=tuple(
            _parse_reset(item, index) for index, item in enumerate(resets_raw)
        ),
        memories=_string_tuple(data.get("memories"), "compile_context.memories"),
        black_boxes=_string_tuple(
            data.get("black_boxes"), "compile_context.black_boxes"
        ),
        frontend=frontend,
        frontend_version=frontend_version,
        build_commands=_string_tuple(
            data.get("build_commands"), "compile_context.build_commands"
        ),
        lint_commands=_string_tuple(
            data.get("lint_commands"), "compile_context.lint_commands"
        ),
        test_commands=_string_tuple(
            data.get("test_commands"), "compile_context.test_commands"
        ),
    )


def _parse_behavioral_basis(raw: Any) -> tuple[BehavioralBasisV1, ...]:
    if not isinstance(raw, list):
        raise CorpusRegistryError("behavioral_basis must be an array")
    parsed: list[BehavioralBasisV1] = []
    for index, item in enumerate(raw):
        context = f"behavioral_basis[{index}]"
        data = _mapping(item, context)
        _expect_fields(
            data,
            allowed=("kind", "location", "description"),
            context=context,
        )
        parsed.append(
            BehavioralBasisV1(
                kind=_choice(
                    data.get("kind"), BEHAVIORAL_BASIS_KINDS, f"{context}.kind"
                ),
                location=str(_string(data.get("location"), f"{context}.location")),
                description=str(
                    _string(data.get("description"), f"{context}.description")
                ),
            )
        )
    return tuple(parsed)


def _parse_proof_contract(raw: Any) -> ProofContractV1:
    data = _mapping(raw, "proof_contract")
    _expect_fields(
        data,
        allowed=("schema", "level", "kind", "latency_relation", "assumptions", "observables"),
        context="proof_contract",
    )
    schema = _string(data.get("schema"), "proof_contract.schema")
    if schema != PROOF_SCHEMA_ID:
        raise CorpusRegistryError(
            f"proof_contract.schema must be {PROOF_SCHEMA_ID!r}",
            code="unsupported_schema",
        )
    level = _choice(data.get("level"), PROOF_LEVELS, "proof_contract.level")
    latency_relation = _choice(
        data.get("latency_relation"),
        LATENCY_RELATIONS,
        "proof_contract.latency_relation",
    )
    if latency_relation not in _PROOF_LATENCY_RELATIONS[level]:
        raise CorpusRegistryError(
            f"proof level {level} does not support latency relation "
            f"{latency_relation!r}",
            code="invalid_proof_contract",
        )
    observables = _string_tuple(
        data.get("observables"), "proof_contract.observables"
    )
    if level != "P0" and not observables:
        raise CorpusRegistryError(
            "P1+ proof contracts require at least one observable",
            code="invalid_proof_contract",
        )
    return ProofContractV1(
        schema=schema,
        level=level,
        kind=str(_string(data.get("kind"), "proof_contract.kind")),
        latency_relation=latency_relation,
        assumptions=_string_tuple(
            data.get("assumptions"), "proof_contract.assumptions"
        ),
        observables=observables,
    )


def _parse_source_hashes(raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise CorpusRegistryError("source_hashes must be an array")
    parsed: list[tuple[str, str]] = []
    for index, item in enumerate(raw):
        context = f"source_hashes[{index}]"
        data = _mapping(item, context)
        _expect_fields(data, allowed=("path", "sha256"), context=context)
        parsed.append(
            (
                _relative_path(data.get("path"), f"{context}.path"),
                str(_sha256(data.get("sha256"), f"{context}.sha256")),
            )
        )
    paths = [path for path, _ in parsed]
    if len(paths) != len(set(paths)):
        raise CorpusRegistryError("source_hashes must not contain duplicate paths")
    return tuple(parsed)


def _parse_path_hashes(
    raw: Any,
    context: str,
    *,
    allow_dot: bool = False,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise CorpusRegistryError(f"{context} must be an array")
    parsed: list[tuple[str, str]] = []
    for index, item in enumerate(raw):
        item_context = f"{context}[{index}]"
        data = _mapping(item, item_context)
        _expect_fields(data, allowed=("path", "sha256"), context=item_context)
        parsed.append(
            (
                _relative_path(
                    data.get("path"),
                    f"{item_context}.path",
                    allow_dot=allow_dot,
                ),
                str(_sha256(data.get("sha256"), f"{item_context}.sha256")),
            )
        )
    paths = [path for path, _ in parsed]
    if len(paths) != len(set(paths)):
        raise CorpusRegistryError(f"{context} must not contain duplicate paths")
    return tuple(parsed)


def _parse_compile_context_hashes(raw: Any) -> CompileContextHashesV1:
    data = _mapping(raw, "compile_context_hashes")
    _expect_fields(
        data,
        allowed=(
            "filelist_sha256",
            "include_tree_hashes",
            "generated_input_hashes",
            "compile_context_hash",
        ),
        context="compile_context_hashes",
    )
    return CompileContextHashesV1(
        filelist_sha256=_sha256(
            data.get("filelist_sha256"),
            "compile_context_hashes.filelist_sha256",
            nullable=True,
        ),
        include_tree_hashes=_parse_path_hashes(
            data.get("include_tree_hashes"),
            "compile_context_hashes.include_tree_hashes",
            allow_dot=True,
        ),
        generated_input_hashes=_parse_path_hashes(
            data.get("generated_input_hashes"),
            "compile_context_hashes.generated_input_hashes",
        ),
        compile_context_hash=_sha256(
            data.get("compile_context_hash"),
            "compile_context_hashes.compile_context_hash",
            nullable=True,
        ),
    )


def _state_at_least(state: str, required: str) -> bool:
    return QUALIFICATION_STATES.index(state) >= QUALIFICATION_STATES.index(required)


def _validate_reference_stage(manifest: ReferenceManifestV1) -> None:
    state = manifest.qualification.state
    license_record = manifest.provenance.license
    if _state_at_least(state, "license_reviewed"):
        if license_record.sha256 is None or license_record.disposition == "pending":
            raise CorpusRegistryError(
                "license_reviewed state requires a license hash and disposition",
                code="incomplete_qualification_stage",
            )
    if _state_at_least(state, "source_pinned"):
        if (
            manifest.provenance.revision is None
            or manifest.provenance.revision_kind == "unresolved"
        ):
            raise CorpusRegistryError(
                "source_pinned state requires an exact revision",
                code="incomplete_qualification_stage",
            )
        expected_paths = set(manifest.compile_context.sources)
        actual_paths = {path for path, _ in manifest.source_hashes}
        if not expected_paths or expected_paths != actual_paths:
            raise CorpusRegistryError(
                "source_pinned state requires a hash for every resolved source",
                code="incomplete_qualification_stage",
            )
        context = manifest.compile_context
        hashes = manifest.compile_context_hashes
        if (context.filelist is None) != (hashes.filelist_sha256 is None):
            raise CorpusRegistryError(
                "source_pinned state requires a filelist hash exactly when a filelist is used",
                code="incomplete_qualification_stage",
            )
        include_paths = {path for path, _ in hashes.include_tree_hashes}
        if include_paths != set(context.include_dirs):
            raise CorpusRegistryError(
                "source_pinned state requires a tree hash for every include directory",
                code="incomplete_qualification_stage",
            )
        generated_paths = {path for path, _ in hashes.generated_input_hashes}
        if generated_paths != set(context.generated_inputs):
            raise CorpusRegistryError(
                "source_pinned state requires a hash for every generated input",
                code="incomplete_qualification_stage",
            )
        if hashes.compile_context_hash is None:
            raise CorpusRegistryError(
                "source_pinned state requires a normalized compile-context hash",
                code="incomplete_qualification_stage",
            )
    if _state_at_least(state, "build_reproduced"):
        context = manifest.compile_context
        if (
            context.frontend is None
            or not context.build_commands
            or not context.lint_commands
        ):
            raise CorpusRegistryError(
                "build_reproduced state requires frontend, build, and lint commands",
                code="incomplete_qualification_stage",
            )
    if _state_at_least(state, "behavior_baselined"):
        if not manifest.behavioral_basis:
            raise CorpusRegistryError(
                "behavior_baselined state requires behavioral evidence",
                code="incomplete_qualification_stage",
            )
    if _state_at_least(state, "reference_qualified"):
        if manifest.proof_contract.level == "P0" or not manifest.synthesis_profiles:
            raise CorpusRegistryError(
                "reference_qualified state requires a P1+ contract and synthesis profiles",
                code="incomplete_qualification_stage",
            )


def parse_reference_manifest(raw: Mapping[str, Any]) -> ReferenceManifestV1:
    _expect_fields(
        raw,
        allowed=(
            "schema_version",
            "schema",
            "document_type",
            "reference_id",
            "display_name",
            "tier",
            "categories",
            "qualification",
            "provenance",
            "lineage",
            "compile_context",
            "compile_context_hashes",
            "behavioral_basis",
            "proof_contract",
            "source_hashes",
            "synthesis_profiles",
            "split",
            "semantic_hash",
        ),
        context="reference manifest",
    )
    if (
        raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("schema") != REFERENCE_SCHEMA_ID
    ):
        raise CorpusRegistryError(
            "unsupported reference manifest schema",
            code="unsupported_schema",
        )
    if raw.get("document_type") != REFERENCE_DOCUMENT_TYPE:
        raise CorpusRegistryError("invalid reference manifest document type")
    categories = _string_tuple(raw.get("categories"), "categories")
    if not categories:
        raise CorpusRegistryError("reference manifest requires at least one category")
    unsupported = sorted(set(categories) - set(CATEGORIES))
    if unsupported:
        raise CorpusRegistryError(
            f"unsupported categories: {', '.join(unsupported)}",
            code="unsupported_value",
        )
    manifest = ReferenceManifestV1(
        schema_version=SCHEMA_VERSION,
        schema=REFERENCE_SCHEMA_ID,
        document_type=REFERENCE_DOCUMENT_TYPE,
        reference_id=_identifier(raw.get("reference_id"), "reference_id"),
        display_name=str(_string(raw.get("display_name"), "display_name")),
        tier=_choice(raw.get("tier"), TIERS, "tier"),
        categories=categories,
        qualification=_parse_qualification(raw.get("qualification")),
        provenance=_parse_provenance(raw.get("provenance")),
        lineage=_parse_lineage(raw.get("lineage")),
        compile_context=_parse_compile_context(raw.get("compile_context")),
        compile_context_hashes=_parse_compile_context_hashes(
            raw.get("compile_context_hashes")
        ),
        behavioral_basis=_parse_behavioral_basis(raw.get("behavioral_basis")),
        proof_contract=_parse_proof_contract(raw.get("proof_contract")),
        source_hashes=_parse_source_hashes(raw.get("source_hashes")),
        synthesis_profiles=_string_tuple(
            raw.get("synthesis_profiles"), "synthesis_profiles"
        ),
        split=_choice(raw.get("split"), SPLIT_ROLES, "split"),
    )
    _validate_reference_stage(manifest)
    return manifest


def _parse_origin(raw: Any) -> VariantOriginV1:
    data = _mapping(raw, "origin")
    _expect_fields(
        data,
        allowed=("kind", "description", "generator", "generator_version"),
        context="origin",
    )
    generator = _string(data.get("generator"), "origin.generator", nullable=True)
    generator_version = _string(
        data.get("generator_version"), "origin.generator_version", nullable=True
    )
    if (generator is None) != (generator_version is None):
        raise CorpusRegistryError(
            "origin generator and generator_version must be set together"
        )
    return VariantOriginV1(
        kind=_choice(data.get("kind"), VARIANT_ORIGINS, "origin.kind"),
        description=str(_string(data.get("description"), "origin.description")),
        generator=generator,
        generator_version=generator_version,
    )


def _parse_source_changes(raw: Any) -> tuple[SourceChangeV1, ...]:
    if not isinstance(raw, list):
        raise CorpusRegistryError("source_changes must be an array")
    parsed: list[SourceChangeV1] = []
    for index, item in enumerate(raw):
        context = f"source_changes[{index}]"
        data = _mapping(item, context)
        _expect_fields(
            data,
            allowed=("path", "before_sha256", "after_sha256"),
            context=context,
        )
        before = _sha256(data.get("before_sha256"), f"{context}.before_sha256")
        after = _sha256(data.get("after_sha256"), f"{context}.after_sha256")
        assert before is not None and after is not None
        if before == after:
            raise CorpusRegistryError(f"{context} does not change source content")
        parsed.append(
            SourceChangeV1(
                path=_relative_path(data.get("path"), f"{context}.path"),
                before_sha256=before,
                after_sha256=after,
            )
        )
    return tuple(parsed)


def _parse_artifact_hashes(raw: Any) -> tuple[tuple[str, str], ...]:
    data = _mapping(raw, "artifact_hashes")
    parsed: list[tuple[str, str]] = []
    for key in sorted(data):
        _identifier(key, f"artifact_hashes key {key!r}")
        parsed.append((key, str(_sha256(data[key], f"artifact_hashes.{key}"))))
    return tuple(parsed)


def parse_variant_manifest(raw: Mapping[str, Any]) -> VariantManifestV1:
    _expect_fields(
        raw,
        allowed=(
            "schema_version",
            "schema",
            "document_type",
            "variant_id",
            "parent_reference_id",
            "variant_lineage_id",
            "display_name",
            "state",
            "origin",
            "source_locations",
            "source_changes",
            "expected_relation",
            "proof_contract",
            "artifact_hashes",
            "semantic_hash",
        ),
        context="variant manifest",
    )
    if (
        raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("schema") != VARIANT_SCHEMA_ID
    ):
        raise CorpusRegistryError(
            "unsupported variant manifest schema",
            code="unsupported_schema",
        )
    if raw.get("document_type") != VARIANT_DOCUMENT_TYPE:
        raise CorpusRegistryError("invalid variant manifest document type")
    manifest = VariantManifestV1(
        schema_version=SCHEMA_VERSION,
        schema=VARIANT_SCHEMA_ID,
        document_type=VARIANT_DOCUMENT_TYPE,
        variant_id=_identifier(raw.get("variant_id"), "variant_id"),
        parent_reference_id=_identifier(
            raw.get("parent_reference_id"), "parent_reference_id"
        ),
        variant_lineage_id=_identifier(
            raw.get("variant_lineage_id"), "variant_lineage_id"
        ),
        display_name=str(_string(raw.get("display_name"), "display_name")),
        state=_choice(raw.get("state"), VARIANT_STATES, "state"),
        origin=_parse_origin(raw.get("origin")),
        source_locations=_relative_paths(
            raw.get("source_locations"), "source_locations"
        ),
        source_changes=_parse_source_changes(raw.get("source_changes")),
        expected_relation=_choice(
            raw.get("expected_relation"), EXPECTED_RELATIONS, "expected_relation"
        ),
        proof_contract=_parse_proof_contract(raw.get("proof_contract")),
        artifact_hashes=_parse_artifact_hashes(raw.get("artifact_hashes")),
    )
    if manifest.state != "declared" and not manifest.artifact_hashes:
        raise CorpusRegistryError(
            "prepared or terminal variants require artifact hashes",
            code="incomplete_variant_state",
        )
    if (
        manifest.origin.kind == "negative_control"
        and manifest.expected_relation != "inequivalent_control"
    ):
        raise CorpusRegistryError(
            "negative-control variants must expect inequivalence"
        )
    return manifest


def _read_manifest_payload(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusRegistryError(f"invalid manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CorpusRegistryError(f"manifest must be a JSON object: {manifest_path}")
    if "semantic_hash" in raw:
        try:
            return read_hashed_json(manifest_path)
        except MVPSchemaError as exc:
            raise CorpusRegistryError(str(exc), code=exc.code) from exc
    return raw


def load_corpus_manifest(path: str | Path) -> ManifestV1:
    raw = _read_manifest_payload(path)
    document_type = raw.get("document_type")
    if document_type == REFERENCE_DOCUMENT_TYPE:
        return parse_reference_manifest(raw)
    if document_type == VARIANT_DOCUMENT_TYPE:
        return parse_variant_manifest(raw)
    raise CorpusRegistryError(
        f"unsupported corpus manifest document type: {document_type!r}",
        code="unsupported_document_type",
    )


def _manifest_sort_key(manifest: ManifestV1) -> tuple[str, str]:
    if isinstance(manifest, ReferenceManifestV1):
        return ("reference", manifest.reference_id)
    return ("variant", manifest.variant_id)


def _manifest_kind(manifest: ManifestV1) -> str:
    return "reference" if isinstance(manifest, ReferenceManifestV1) else "variant"


def _manifest_id(manifest: ManifestV1) -> str:
    if isinstance(manifest, ReferenceManifestV1):
        return manifest.reference_id
    return manifest.variant_id


def _stored_semantic_hash(path: Path) -> str:
    try:
        payload = read_hashed_json(path)
    except MVPSchemaError as exc:
        raise CorpusRegistryError(str(exc), code=exc.code) from exc
    digest = payload.get("semantic_hash")
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise CorpusRegistryError(
            f"stored corpus record has an invalid semantic hash: {path}",
            code="artifact_hash_mismatch",
        )
    return digest


class CorpusRegistryV1:
    """Content-addressed, append-only registry for hierarchical RTL evidence."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.references_dir = self.root / "references"
        self.variants_dir = self.root / "variants"

    def _record_path(self, manifest: ManifestV1) -> Path:
        directory = self.references_dir if isinstance(manifest, ReferenceManifestV1) else self.variants_dir
        return directory / _manifest_id(manifest) / "000001.json"

    def _history_paths(self, kind: str, record_id: str) -> tuple[Path, ...]:
        if kind not in {"reference", "variant"}:
            raise CorpusRegistryError(f"unsupported corpus record kind: {kind!r}")
        directory = self.references_dir if kind == "reference" else self.variants_dir
        nested = directory / record_id
        paths: list[Path] = []
        if nested.is_dir():
            paths.extend(sorted(nested.glob("*.json")))
        legacy = directory / f"{record_id}.json"
        if legacy.is_file():
            paths.insert(0, legacy)
        return tuple(paths)

    def _histories(self, directory: Path) -> dict[str, tuple[Path, ...]]:
        if not directory.exists():
            return {}
        if not directory.is_dir():
            raise CorpusRegistryError(f"registry path is not a directory: {directory}")
        collected: dict[str, list[Path]] = {}
        for path in sorted(directory.iterdir()):
            if path.is_file():
                if path.suffix != ".json":
                    raise CorpusRegistryError(
                        f"registry contains unsupported record: {path}",
                        code="unexpected_registry_entry",
                    )
                collected.setdefault(path.stem, []).insert(0, path)
                continue
            if not path.is_dir() or not _ID_PATTERN.fullmatch(path.name):
                raise CorpusRegistryError(
                    f"registry contains unsupported record: {path}",
                    code="unexpected_registry_entry",
                )
            record_paths = tuple(sorted(path.glob("*.json")))
            unexpected = sorted(
                child
                for child in path.iterdir()
                if not child.is_file() or child.suffix != ".json"
            )
            if unexpected or not record_paths:
                raise CorpusRegistryError(
                    f"registry contains invalid history: {path}",
                    code="unexpected_registry_entry",
                )
            collected.setdefault(path.name, []).extend(record_paths)
        return {
            record_id: tuple(paths)
            for record_id, paths in sorted(collected.items())
        }

    def _load_directory(self, directory: Path) -> list[ManifestV1]:
        manifests: list[ManifestV1] = []
        for paths in self._histories(directory).values():
            manifests.append(load_corpus_manifest(paths[-1]))
        return manifests

    def _validate_history(self, paths: tuple[Path, ...]) -> tuple[ManifestV1, ...]:
        manifests = tuple(load_corpus_manifest(path) for path in paths)
        if not manifests:
            raise CorpusRegistryError("corpus history may not be empty")
        first_kind = _manifest_kind(manifests[0])
        first_id = _manifest_id(manifests[0])
        for index, manifest in enumerate(manifests):
            if _manifest_kind(manifest) != first_kind or _manifest_id(manifest) != first_id:
                raise CorpusRegistryError(
                    f"corpus history mixes record identities at {paths[index]}",
                    code="invalid_history",
                )
            if index == 0:
                continue
            expected_prefix = f"{index + 1:06d}-after-{_stored_semantic_hash(paths[index - 1])[:16]}"
            if paths[index].stem != expected_prefix:
                raise CorpusRegistryError(
                    f"corpus history link is invalid: {paths[index]}",
                    code="invalid_history_link",
                )
            self._validate_transition(manifests[index - 1], manifest)
        return manifests

    def histories(self) -> dict[tuple[str, str], tuple[ManifestV1, ...]]:
        result: dict[tuple[str, str], tuple[ManifestV1, ...]] = {}
        for kind, directory in (
            ("reference", self.references_dir),
            ("variant", self.variants_dir),
        ):
            for record_id, paths in self._histories(directory).items():
                result[(kind, record_id)] = self._validate_history(paths)
        return result

    def records(self) -> tuple[ManifestV1, ...]:
        records = [history[-1] for history in self.histories().values()]
        return tuple(sorted(records, key=_manifest_sort_key))

    def _validate_transition(self, previous: ManifestV1, current: ManifestV1) -> None:
        if type(previous) is not type(current) or _manifest_id(previous) != _manifest_id(current):
            raise CorpusRegistryError(
                "corpus progression cannot change record kind or identity",
                code="invalid_history",
            )
        if isinstance(previous, ReferenceManifestV1):
            immutable = (
                "reference_id",
                "tier",
                "categories",
                "lineage",
                "proof_contract",
            )
            for field in immutable:
                if getattr(previous, field) != getattr(current, field):
                    raise CorpusRegistryError(
                        f"reference progression cannot change {field}",
                        code="invalid_qualification_transition",
                    )
            if previous.display_name != current.display_name:
                raise CorpusRegistryError(
                    "reference progression cannot change display_name",
                    code="invalid_qualification_transition",
                )
            previous_provenance = previous.provenance
            current_provenance = current.provenance
            if (
                previous_provenance.project != current_provenance.project
                or previous_provenance.canonical_url != current_provenance.canonical_url
                or previous_provenance.license.expression
                != current_provenance.license.expression
                or previous_provenance.license.url != current_provenance.license.url
            ):
                raise CorpusRegistryError(
                    "reference progression cannot change source or license identity",
                    code="invalid_qualification_transition",
                )
            old_state = previous.qualification.state
            new_state = current.qualification.state
            old_index = QUALIFICATION_STATES.index(old_state)
            new_index = QUALIFICATION_STATES.index(new_state)
            terminal_at_current_stage = (
                new_index == old_index
                and previous.qualification.status == "active"
                and current.qualification.status in {"blocked", "rejected"}
            )
            resume_at_current_stage = (
                new_index == old_index
                and previous.qualification.status == "blocked"
                and current.qualification.status == "active"
            )
            if new_index < old_index or (
                new_index == old_index
                and not terminal_at_current_stage
                and not resume_at_current_stage
            ):
                raise CorpusRegistryError(
                    f"reference qualification must advance beyond {old_state!r}",
                    code="invalid_qualification_transition",
                )
            if previous.qualification.status == "rejected":
                raise CorpusRegistryError(
                    "rejected references cannot advance",
                    code="terminal_qualification_state",
                )
            if previous_provenance.revision is not None and (
                previous_provenance.revision != current_provenance.revision
                or previous_provenance.revision_kind != current_provenance.revision_kind
            ):
                raise CorpusRegistryError(
                    "pinned source revision cannot change",
                    code="invalid_qualification_transition",
                )
            if terminal_at_current_stage or resume_at_current_stage:
                previous_payload = previous.to_dict()
                current_payload = current.to_dict()
                previous_payload["qualification"] = current_payload["qualification"]
                if previous_payload != current_payload:
                    raise CorpusRegistryError(
                        "terminal qualification may only change status and reason",
                        code="invalid_qualification_transition",
                    )
                return
            if previous.split != "unassigned" and previous.split != current.split:
                raise CorpusRegistryError(
                    "assigned data split cannot change",
                    code="split_lineage_leakage",
                )
            old_context = previous.compile_context
            new_context = current.compile_context
            frozen_context = (
                "top",
                "sources",
                "filelist",
                "include_dirs",
                "defines",
                "parameters",
                "generated_inputs",
                "clocks",
                "resets",
                "memories",
                "black_boxes",
            )
            if _state_at_least(old_state, "source_pinned") and (
                any(
                    getattr(old_context, field) != getattr(new_context, field)
                    for field in frozen_context
                )
                or previous.compile_context_hashes != current.compile_context_hashes
                or previous.source_hashes != current.source_hashes
            ):
                raise CorpusRegistryError(
                    "source-pinned compile inputs cannot change",
                    code="invalid_qualification_transition",
                )
            return

        immutable = (
            "variant_id",
            "parent_reference_id",
            "variant_lineage_id",
            "display_name",
            "origin",
            "source_locations",
            "expected_relation",
            "proof_contract",
        )
        for field in immutable:
            if getattr(previous, field) != getattr(current, field):
                raise CorpusRegistryError(
                    f"variant progression cannot change {field}",
                    code="invalid_variant_transition",
                )
        if VARIANT_STATES.index(current.state) <= VARIANT_STATES.index(previous.state):
            raise CorpusRegistryError(
                f"variant state must advance beyond {previous.state!r}",
                code="invalid_variant_transition",
            )
        if previous.source_changes and previous.source_changes != current.source_changes:
            raise CorpusRegistryError(
                "prepared variant source changes cannot change",
                code="invalid_variant_transition",
            )
        previous_hashes = dict(previous.artifact_hashes)
        current_hashes = dict(current.artifact_hashes)
        if any(current_hashes.get(key) != value for key, value in previous_hashes.items()):
            raise CorpusRegistryError(
                "variant artifact hashes are append-only",
                code="invalid_variant_transition",
            )

    def references(self) -> tuple[ReferenceManifestV1, ...]:
        return tuple(
            item for item in self.records() if isinstance(item, ReferenceManifestV1)
        )

    def variants(self) -> tuple[VariantManifestV1, ...]:
        return tuple(
            item for item in self.records() if isinstance(item, VariantManifestV1)
        )

    def _validate_cross_records(self, records: Iterable[ManifestV1]) -> None:
        manifests = tuple(records)
        references = tuple(
            item for item in manifests if isinstance(item, ReferenceManifestV1)
        )
        variants = tuple(
            item for item in manifests if isinstance(item, VariantManifestV1)
        )
        reference_ids: set[str] = set()
        design_lineages: dict[str, str] = {}
        repository_splits: dict[str, str] = {}
        containing_splits: dict[str, str] = {}
        for reference in references:
            if reference.reference_id in reference_ids:
                raise CorpusRegistryError(
                    f"duplicate reference ID: {reference.reference_id}",
                    code="duplicate_reference",
                )
            reference_ids.add(reference.reference_id)
            design_lineage = reference.lineage.design_lineage_id
            if design_lineage in design_lineages:
                raise CorpusRegistryError(
                    "multiple references claim design lineage "
                    f"{design_lineage!r}: {design_lineages[design_lineage]!r} and "
                    f"{reference.reference_id!r}",
                    code="duplicate_design_lineage",
                )
            design_lineages[design_lineage] = reference.reference_id
            if reference.split != "unassigned":
                repository = reference.lineage.repository_lineage_id
                previous_split = repository_splits.get(repository)
                if previous_split is not None and previous_split != reference.split:
                    raise CorpusRegistryError(
                        f"repository lineage {repository!r} crosses data splits",
                        code="split_lineage_leakage",
                    )
                repository_splits[repository] = reference.split
                for containing in reference.lineage.containing_design_ids:
                    previous_containing = containing_splits.get(containing)
                    if previous_containing is not None and previous_containing != reference.split:
                        raise CorpusRegistryError(
                            f"containing design {containing!r} crosses data splits",
                            code="split_hierarchy_leakage",
                        )
                    containing_splits[containing] = reference.split

        variant_ids: set[str] = set()
        variant_lineages: set[str] = set()
        for variant in variants:
            if variant.variant_id in variant_ids:
                raise CorpusRegistryError(
                    f"duplicate variant ID: {variant.variant_id}",
                    code="duplicate_variant",
                )
            variant_ids.add(variant.variant_id)
            if variant.variant_lineage_id in variant_lineages:
                raise CorpusRegistryError(
                    f"duplicate variant lineage: {variant.variant_lineage_id}",
                    code="duplicate_variant_lineage",
                )
            variant_lineages.add(variant.variant_lineage_id)
            if variant.parent_reference_id not in reference_ids:
                raise CorpusRegistryError(
                    f"variant {variant.variant_id!r} has missing parent "
                    f"{variant.parent_reference_id!r}",
                    code="missing_parent_reference",
                )

    def validate_manifest(self, path: str | Path) -> ManifestV1:
        manifest = load_corpus_manifest(path)
        self._validate_cross_records((*self.records(), manifest))
        return manifest

    def advance(self, path: str | Path) -> dict[str, Any]:
        manifest = load_corpus_manifest(path)
        return self.advance_manifest(manifest)

    def advance_manifest(self, manifest: ManifestV1) -> dict[str, Any]:
        kind = _manifest_kind(manifest)
        record_id = _manifest_id(manifest)
        history_paths = self._history_paths(kind, record_id)
        if not history_paths:
            raise CorpusRegistryError(
                f"cannot advance missing {kind} {record_id!r}",
                code="missing_corpus_record",
            )
        history = self._validate_history(history_paths)
        previous = history[-1]
        self._validate_transition(previous, manifest)
        current_records = [
            item
            for item in self.records()
            if not (_manifest_kind(item) == kind and _manifest_id(item) == record_id)
        ]
        self._validate_cross_records((*current_records, manifest))
        predecessor_hash = _stored_semantic_hash(history_paths[-1])
        sequence = len(history_paths) + 1
        directory = self.references_dir if kind == "reference" else self.variants_dir
        output_path = (
            directory
            / record_id
            / f"{sequence:06d}-after-{predecessor_hash[:16]}.json"
        )
        try:
            stored = write_hashed_json(output_path, manifest.to_dict(), exclusive=True)
        except MVPSchemaError as exc:
            raise CorpusRegistryError(str(exc), code=exc.code) from exc
        return {
            "status": "advanced",
            "registry_schema": REGISTRY_SCHEMA_ID,
            "kind": kind,
            "record_id": record_id,
            "sequence": sequence,
            "predecessor_hash": predecessor_hash,
            "record_path": str(output_path),
            "semantic_hash": stored["semantic_hash"],
        }

    def add(self, path: str | Path) -> dict[str, Any]:
        manifest = self.validate_manifest(path)
        return self.add_manifest(manifest, validated=True)

    def add_manifest(
        self,
        manifest: ManifestV1,
        *,
        validated: bool = False,
    ) -> dict[str, Any]:
        if not validated:
            self._validate_cross_records((*self.records(), manifest))
        output_path = self._record_path(manifest)
        try:
            stored = write_hashed_json(output_path, manifest.to_dict(), exclusive=True)
        except MVPSchemaError as exc:
            raise CorpusRegistryError(str(exc), code=exc.code) from exc
        return {
            "status": "added",
            "registry_schema": REGISTRY_SCHEMA_ID,
            "kind": "reference" if isinstance(manifest, ReferenceManifestV1) else "variant",
            "record_id": (
                manifest.reference_id
                if isinstance(manifest, ReferenceManifestV1)
                else manifest.variant_id
            ),
            "record_path": str(output_path),
            "semantic_hash": stored["semantic_hash"],
        }

    def add_many(self, manifests: Iterable[ManifestV1]) -> tuple[dict[str, Any], ...]:
        pending = tuple(manifests)
        if not pending:
            raise CorpusRegistryError("cannot add an empty corpus tranche")
        self._validate_cross_records((*self.records(), *pending))
        output_paths = tuple(self._record_path(manifest) for manifest in pending)
        conflicts = [path for path in output_paths if path.exists()]
        if conflicts:
            raise CorpusRegistryError(
                f"append-only artifact already exists: {conflicts[0]}",
                code="append_only_conflict",
            )
        return tuple(
            self.add_manifest(manifest, validated=True) for manifest in pending
        )

    def validate(self) -> dict[str, Any]:
        histories = self.histories()
        records = self.records()
        self._validate_cross_records(records)
        return {
            "status": "passed",
            "registry_schema": REGISTRY_SCHEMA_ID,
            "registry_root": str(self.root),
            "reference_count": sum(
                isinstance(item, ReferenceManifestV1) for item in records
            ),
            "variant_count": sum(
                isinstance(item, VariantManifestV1) for item in records
            ),
            "record_count": len(records),
            "history_record_count": sum(len(history) for history in histories.values()),
        }

    def listing(self, kind: str = "all") -> dict[str, Any]:
        if kind not in {"all", "references", "variants"}:
            raise CorpusRegistryError(f"unsupported registry listing kind: {kind!r}")
        records = self.records()
        self._validate_cross_records(records)
        items: list[dict[str, Any]] = []
        for manifest in records:
            if isinstance(manifest, ReferenceManifestV1):
                if kind == "variants":
                    continue
                items.append(
                    {
                        "kind": "reference",
                        "record_id": manifest.reference_id,
                        "display_name": manifest.display_name,
                        "tier": manifest.tier,
                        "categories": list(manifest.categories),
                        "qualification_state": manifest.qualification.state,
                        "qualification_status": manifest.qualification.status,
                        "lineage_id": manifest.lineage.design_lineage_id,
                        "upstream_project_id": manifest.lineage.upstream_project_id,
                        "license_expression": manifest.provenance.license.expression,
                        "license_disposition": manifest.provenance.license.disposition,
                        "proof_level": manifest.proof_contract.level,
                        "split": manifest.split,
                    }
                )
            else:
                if kind == "references":
                    continue
                items.append(
                    {
                        "kind": "variant",
                        "record_id": manifest.variant_id,
                        "display_name": manifest.display_name,
                        "parent_reference_id": manifest.parent_reference_id,
                        "variant_lineage_id": manifest.variant_lineage_id,
                        "state": manifest.state,
                        "expected_relation": manifest.expected_relation,
                    }
                )
        return {
            "registry_schema": REGISTRY_SCHEMA_ID,
            "registry_root": str(self.root),
            "kind": kind,
            "count": len(items),
            "records": items,
        }

    def summary(self) -> dict[str, Any]:
        records = self.records()
        self._validate_cross_records(records)
        references = tuple(
            item for item in records if isinstance(item, ReferenceManifestV1)
        )
        variants = tuple(
            item for item in records if isinstance(item, VariantManifestV1)
        )
        tier_counts = {tier: 0 for tier in TIERS}
        qualified_tier_counts = {tier: 0 for tier in TIERS}
        category_counts = {category: 0 for category in CATEGORIES}
        state_counts = {state: 0 for state in QUALIFICATION_STATES}
        status_counts = {status: 0 for status in QUALIFICATION_STATUSES}
        proof_level_counts = {level: 0 for level in PROOF_LEVELS}
        split_counts = {split: 0 for split in SPLIT_ROLES}
        upstream_project_counts: dict[str, int] = {}
        license_expression_counts: dict[str, int] = {}
        license_disposition_counts = {
            disposition: 0 for disposition in LICENSE_DISPOSITIONS
        }
        for reference in references:
            tier_counts[reference.tier] += 1
            state_counts[reference.qualification.state] += 1
            status_counts[reference.qualification.status] += 1
            proof_level_counts[reference.proof_contract.level] += 1
            split_counts[reference.split] += 1
            upstream = reference.lineage.upstream_project_id
            upstream_project_counts[upstream] = upstream_project_counts.get(upstream, 0) + 1
            license_expression = reference.provenance.license.expression
            license_expression_counts[license_expression] = (
                license_expression_counts.get(license_expression, 0) + 1
            )
            license_disposition_counts[
                reference.provenance.license.disposition
            ] += 1
            if (
                _state_at_least(reference.qualification.state, "reference_qualified")
                and reference.qualification.status == "active"
            ):
                qualified_tier_counts[reference.tier] += 1
            for category in reference.categories:
                category_counts[category] += 1
        return {
            "registry_schema": REGISTRY_SCHEMA_ID,
            "registry_root": str(self.root),
            "reference_count": len(references),
            "variant_count": len(variants),
            "tier_reference_counts": tier_counts,
            "tier_qualified_counts": qualified_tier_counts,
            "category_reference_counts": category_counts,
            "qualification_state_counts": state_counts,
            "qualification_status_counts": status_counts,
            "proof_level_reference_counts": proof_level_counts,
            "split_reference_counts": split_counts,
            "upstream_project_reference_counts": dict(
                sorted(upstream_project_counts.items())
            ),
            "license_expression_reference_counts": dict(
                sorted(license_expression_counts.items())
            ),
            "license_disposition_reference_counts": license_disposition_counts,
            "repository_lineage_count": len(
                {item.lineage.repository_lineage_id for item in references}
            ),
            "design_lineage_count": len(
                {item.lineage.design_lineage_id for item in references}
            ),
        }
