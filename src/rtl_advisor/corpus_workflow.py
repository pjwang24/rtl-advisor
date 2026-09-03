from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_qualification import qualify_tranche
from rtl_advisor.corpus_registry import (
    CATEGORIES,
    PROOF_LEVELS,
    QUALIFICATION_STATES,
    QUALIFICATION_STATUSES,
    SPLIT_ROLES,
    TIERS,
    CorpusRegistryV1,
    ReferenceManifestV1,
    VariantManifestV1,
)
from rtl_advisor.mvp_schema import (
    MVPSchemaError,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.tranche_lock import (
    load_tranche_lock,
    materialize_tranche_references,
    tranche_summary,
)


CORPUS_SCHEMA_VERSION = 1
COVERAGE_SCHEMA = "rtl-advisor-corpus-coverage-v1"
QUALIFICATION_SCHEMA = "rtl-advisor-corpus-qualification-v1"
TRANCHE_VALIDATION_SCHEMA = "rtl-advisor-corpus-tranche-validation-v1"
REGISTRATION_SCHEMA = "rtl-advisor-corpus-registration-v1"
COVERAGE_DOCUMENT_TYPE = "rtl-advisor.corpus.coverage"
QUALIFICATION_DOCUMENT_TYPE = "rtl-advisor.corpus.qualification"
TRANCHE_VALIDATION_DOCUMENT_TYPE = "rtl-advisor.corpus.tranche-validation"
REGISTRATION_DOCUMENT_TYPE = "rtl-advisor.corpus.registration"
ERROR_DOCUMENT_TYPE = "rtl-advisor.corpus.error"


class CorpusWorkflowError(RuntimeError):
    """Raised when an Agent-facing corpus operation cannot be trusted."""

    def __init__(self, message: str, *, code: str = "corpus_workflow_error") -> None:
        super().__init__(message)
        self.code = code


def _count_rows(
    values: Iterable[str],
    *,
    domain: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    counts = Counter(values)
    keys = tuple(domain) if domain is not None else tuple(sorted(counts))
    return [{"category": key, "count": counts.get(key, 0)} for key in keys]


def _normalized_filter(values: Sequence[str], allowed: Sequence[str], name: str) -> tuple[str, ...]:
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise CorpusWorkflowError(
            f"unsupported {name}: {', '.join(unknown)}",
            code="invalid_corpus_filter",
        )
    return tuple(sorted(set(values)))


def _persist_immutable(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = dict(payload)
    expected.pop("semantic_hash", None)
    expected["semantic_hash"] = stable_hash(expected)
    if path.is_file():
        existing = read_hashed_json(path)
        if existing != expected:
            raise CorpusWorkflowError(
                f"immutable corpus result conflicts: {path}",
                code="append_only_conflict",
            )
        return existing
    try:
        return write_hashed_json(path, expected, exclusive=True)
    except MVPSchemaError as exc:
        raise CorpusWorkflowError(str(exc), code=exc.code) from exc


def _registry_snapshot(
    references: Sequence[ReferenceManifestV1],
    variants: Sequence[VariantManifestV1],
) -> str:
    return stable_hash(
        {
            "references": [item.to_dict() for item in references],
            "variants": [item.to_dict() for item in variants],
        }
    )


def corpus_coverage(
    config: ProjectConfig,
    *,
    registry: CorpusRegistryV1,
    tiers: Sequence[str] = (),
    categories: Sequence[str] = (),
    splits: Sequence[str] = (),
    qualification_states: Sequence[str] = (),
    qualification_statuses: Sequence[str] = (),
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a compact, lineage-aware, read-only registry coverage audit."""

    filters = {
        "tiers": list(_normalized_filter(tiers, TIERS, "tier")),
        "categories": list(_normalized_filter(categories, CATEGORIES, "category")),
        "splits": list(_normalized_filter(splits, SPLIT_ROLES, "split")),
        "qualification_states": list(
            _normalized_filter(
                qualification_states, QUALIFICATION_STATES, "qualification state"
            )
        ),
        "qualification_statuses": list(
            _normalized_filter(
                qualification_statuses,
                QUALIFICATION_STATUSES,
                "qualification status",
            )
        ),
    }
    registry.validate()
    all_records = registry.records()
    all_references = tuple(
        item for item in all_records if isinstance(item, ReferenceManifestV1)
    )
    all_variants = tuple(
        item for item in all_records if isinstance(item, VariantManifestV1)
    )
    snapshot_hash = _registry_snapshot(all_references, all_variants)

    def included(reference: ReferenceManifestV1) -> bool:
        return (
            (not filters["tiers"] or reference.tier in filters["tiers"])
            and (
                not filters["categories"]
                or bool(set(reference.categories) & set(filters["categories"]))
            )
            and (not filters["splits"] or reference.split in filters["splits"])
            and (
                not filters["qualification_states"]
                or reference.qualification.state in filters["qualification_states"]
            )
            and (
                not filters["qualification_statuses"]
                or reference.qualification.status
                in filters["qualification_statuses"]
            )
        )

    references = tuple(item for item in all_references if included(item))
    reference_ids = {item.reference_id for item in references}
    variants = tuple(
        item for item in all_variants if item.parent_reference_id in reference_ids
    )
    design_lineages = {item.lineage.design_lineage_id for item in references}
    repository_lineages = {
        item.lineage.repository_lineage_id for item in references
    }
    containing_designs = {
        design_id
        for item in references
        for design_id in item.lineage.containing_design_ids
    }
    qualified = tuple(
        item
        for item in references
        if item.qualification.status == "active"
        and QUALIFICATION_STATES.index(item.qualification.state)
        >= QUALIFICATION_STATES.index("reference_qualified")
    )
    parameter_variants = tuple(
        item for item in variants if item.origin.kind == "upstream_parameter"
    )
    naive_record_count = len(references) + len(variants)
    lineage_count = len(design_lineages)
    ratio = round(naive_record_count / lineage_count, 6) if lineage_count else None

    missing_tiers = sorted(set(TIERS) - {item.tier for item in references})
    missing_categories = sorted(
        set(CATEGORIES)
        - {category for item in references for category in item.categories}
    )
    flags: list[dict[str, Any]] = []
    if references and len(repository_lineages) < 3:
        flags.append(
            {
                "code": "limited_repository_diversity",
                "severity": "medium",
                "count": len(repository_lineages),
                "message": "Fewer than three independent repository lineages are represented.",
            }
        )
    if missing_tiers:
        flags.append(
            {
                "code": "tier_coverage_gap",
                "severity": "medium",
                "count": len(missing_tiers),
                "message": "One or more corpus tiers have no independent design lineage.",
            }
        )
    if missing_categories:
        flags.append(
            {
                "code": "category_coverage_gap",
                "severity": "medium",
                "count": len(missing_categories),
                "message": "One or more RTL categories have no independent design lineage.",
            }
        )
    non_active = sum(item.qualification.status != "active" for item in references)
    if non_active:
        flags.append(
            {
                "code": "inactive_references",
                "severity": "high",
                "count": non_active,
                "message": "Blocked or rejected references are excluded from qualified coverage.",
            }
        )

    identity = {
        "registry_root": str(registry.root),
        "registry_snapshot_semantic_hash": snapshot_hash,
        "filters": filters,
    }
    coverage_id = f"coverage-{stable_hash(identity)[:20]}"
    artifact_path = (
        config.artifacts_dir
        / "corpus-workflow-v1"
        / "coverage"
        / coverage_id
        / "coverage.json"
    ).resolve()
    core = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "schema": COVERAGE_SCHEMA,
        "document_type": COVERAGE_DOCUMENT_TYPE,
        "coverage_id": coverage_id,
        "status": "ready" if references else "empty",
        "read_only": True,
        "counting_unit": "independent_design_lineage",
        "registry": {
            "root": str(registry.root),
            "schema": "rtl-advisor-corpus-v1",
            "snapshot_semantic_hash": snapshot_hash,
        },
        "filters": filters,
        "population": {
            "independent_design_lineage_count": lineage_count,
            "repository_lineage_count": len(repository_lineages),
            "containing_design_count": len(containing_designs),
            "reference_record_count": len(references),
            "qualified_design_lineage_count": len(
                {item.lineage.design_lineage_id for item in qualified}
            ),
            "variant_record_count": len(variants),
            "parameter_variant_count": len(parameter_variants),
            "naive_reference_plus_variant_count": naive_record_count,
            "naive_to_lineage_ratio": ratio,
        },
        "breakdowns": {
            "tiers": _count_rows((item.tier for item in references), domain=TIERS),
            "categories": _count_rows(
                (
                    category
                    for item in references
                    for category in item.categories
                ),
                domain=CATEGORIES,
            ),
            "splits": _count_rows(
                (item.split for item in references), domain=SPLIT_ROLES
            ),
            "qualification_states": _count_rows(
                (item.qualification.state for item in references),
                domain=QUALIFICATION_STATES,
            ),
            "qualification_statuses": _count_rows(
                (item.qualification.status for item in references),
                domain=QUALIFICATION_STATUSES,
            ),
            "proof_levels": _count_rows(
                (item.proof_contract.level for item in references),
                domain=PROOF_LEVELS,
            ),
            "upstream_projects": _count_rows(
                item.lineage.upstream_project_id for item in references
            ),
            "variant_origins": _count_rows(item.origin.kind for item in variants),
        },
        "coverage_gaps": {
            "missing_tiers": missing_tiers,
            "missing_categories": missing_categories,
            "unassigned_split_count": sum(
                item.split == "unassigned" for item in references
            ),
            "not_reference_qualified_count": len(references) - len(qualified),
        },
        "quality_flags": flags,
        "limitations": [
            "Coverage counts registry metadata, not behavioral diversity within a design lineage.",
            "Variants and parameter configurations are reported separately and never increase the primary independent-design count.",
            "A qualified reference is evidence for its frozen compile context, not every configuration of the upstream design.",
        ],
        "artifacts": {"coverage": str(artifact_path)},
        "command": list(normalized_command),
    }
    return _persist_immutable(artifact_path, core)


def qualify_corpus(
    config: ProjectConfig,
    *,
    tranche_lock_path: str | Path,
    qualification_plan_path: str | Path,
    registry: CorpusRegistryV1,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Run the existing frozen qualification engine and wrap its result."""

    result = qualify_tranche(
        config,
        tranche_lock_path=tranche_lock_path,
        qualification_plan_path=qualification_plan_path,
        registry=registry,
    )
    identity = {
        "plan_semantic_hash": result["plan_semantic_hash"],
        "tranche_semantic_hash": result["tranche_semantic_hash"],
        "registry_root": str(registry.root),
        "tranche_lock_path": str(Path(tranche_lock_path).expanduser().resolve()),
        "qualification_plan_path": str(
            Path(qualification_plan_path).expanduser().resolve()
        ),
    }
    qualification_id = f"qualification-{stable_hash(identity)[:20]}"
    source_summary = (
        config.artifacts_dir
        / "corpus-qualification"
        / str(result["plan_id"])
        / str(result["plan_semantic_hash"])
        / "summary.json"
    ).resolve()
    artifact_path = (
        config.artifacts_dir
        / "corpus-workflow-v1"
        / "qualification"
        / qualification_id
        / "qualification.json"
    ).resolve()
    core = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "schema": QUALIFICATION_SCHEMA,
        "document_type": QUALIFICATION_DOCUMENT_TYPE,
        "qualification_id": qualification_id,
        "status": result["status"],
        "read_only": False,
        "registry_mutation": "append_only",
        "plan_id": result["plan_id"],
        "plan_semantic_hash": result["plan_semantic_hash"],
        "tranche_id": result["tranche_id"],
        "tranche_semantic_hash": result["tranche_semantic_hash"],
        "summary": {
            "reference_count": result["reference_count"],
            "build_reproduced_count": result["build_reproduced_count"],
            "blocked_count": result["blocked_count"],
            "verified_source_file_count": result["source_integrity"][
                "verified_file_count"
            ],
        },
        "source_result_semantic_hash": result["semantic_hash"],
        "artifacts": {
            "qualification": str(artifact_path),
            "qualification_engine_summary": str(source_summary),
            "registry": str(registry.root),
        },
        "limitations": [
            "Qualification reproduces the frozen compile contexts only; candidate synthesis remains disabled.",
            "Blocked references remain visible and do not count as qualified coverage.",
        ],
        "command": list(normalized_command),
    }
    return _persist_immutable(artifact_path, core)


def validate_corpus_tranche(
    config: ProjectConfig,
    *,
    tranche_lock_path: str | Path,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate an already-frozen tranche without registering or running tools."""

    lock_path = Path(tranche_lock_path).expanduser().resolve()
    summary = tranche_summary(load_tranche_lock(lock_path), path=lock_path)
    validation_id = f"tranche-validation-{stable_hash({'tranche': summary['semantic_hash'], 'lock_path': str(lock_path)})[:20]}"
    artifact_path = (
        config.artifacts_dir
        / "corpus-workflow-v1"
        / "tranche-validation"
        / validation_id
        / "validation.json"
    ).resolve()
    core = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "schema": TRANCHE_VALIDATION_SCHEMA,
        "document_type": TRANCHE_VALIDATION_DOCUMENT_TYPE,
        "validation_id": validation_id,
        "status": "passed",
        "read_only": True,
        "tranche": {
            "tranche_id": summary["tranche_id"],
            "semantic_hash": summary["semantic_hash"],
            "tier": summary["tier"],
            "reference_count": summary["reference_count"],
            "upstream_project_count": summary["upstream_project_count"],
            "category_count": summary["category_count"],
            "candidate_ppa_inspected_before_freeze": summary[
                "candidate_ppa_inspected_before_freeze"
            ],
        },
        "artifacts": {
            "validation": str(artifact_path),
            "tranche_lock": str(lock_path),
        },
        "command": list(normalized_command),
    }
    return _persist_immutable(artifact_path, core)


def register_corpus_tranche(
    config: ProjectConfig,
    *,
    tranche_lock_path: str | Path,
    registry: CorpusRegistryV1,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Append a validated frozen tranche to Corpus Registry V1."""

    lock_path = Path(tranche_lock_path).expanduser().resolve()
    lock = load_tranche_lock(lock_path)
    summary = tranche_summary(lock, path=lock_path)
    registration_id = f"registration-{stable_hash({'tranche': summary['semantic_hash'], 'lock_path': str(lock_path), 'registry': str(registry.root)})[:20]}"
    artifact_path = (
        config.artifacts_dir
        / "corpus-workflow-v1"
        / "registration"
        / registration_id
        / "registration.json"
    ).resolve()
    manifests = materialize_tranche_references(lock)
    if artifact_path.is_file():
        existing = read_hashed_json(
            artifact_path,
            document_type=REGISTRATION_DOCUMENT_TYPE,
            schema_version=CORPUS_SCHEMA_VERSION,
        )
        registry.validate()
        expected_ids = {item.reference_id for item in manifests}
        current_ids = {item.reference_id for item in registry.references()}
        if not expected_ids.issubset(current_ids):
            raise CorpusWorkflowError(
                "stored registration result no longer matches the registry",
                code="artifact_parent_mismatch",
            )
        return existing
    records = registry.add_many(manifests)
    core = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "schema": REGISTRATION_SCHEMA,
        "document_type": REGISTRATION_DOCUMENT_TYPE,
        "registration_id": registration_id,
        "status": "registered",
        "read_only": False,
        "registry_mutation": "append_only",
        "tranche_id": summary["tranche_id"],
        "tranche_semantic_hash": summary["semantic_hash"],
        "registered_count": len(records),
        "record_semantic_hashes": [str(item["semantic_hash"]) for item in records],
        "artifacts": {
            "registration": str(artifact_path),
            "tranche_lock": str(lock_path),
            "registry": str(registry.root),
        },
        "command": list(normalized_command),
    }
    return _persist_immutable(artifact_path, core)


def corpus_error_payload(
    operation: str,
    error: Exception,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    payload = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "schema": "rtl-advisor-corpus-error-v1",
        "document_type": ERROR_DOCUMENT_TYPE,
        "operation": operation,
        "status": "failed",
        "error": {
            "code": str(getattr(error, "code", "corpus_workflow_error")),
            "message": str(error),
        },
        "command": list(normalized_command),
    }
    payload["semantic_hash"] = stable_hash(payload)
    return payload


def corpus_exit_code(payload: Mapping[str, Any]) -> int:
    if payload.get("document_type") == ERROR_DOCUMENT_TYPE:
        return 2
    if payload.get("document_type") == QUALIFICATION_DOCUMENT_TYPE:
        return 0 if payload.get("status") == "passed" else 4
    return 0
