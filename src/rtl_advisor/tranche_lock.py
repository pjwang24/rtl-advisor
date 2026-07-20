from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from rtl_advisor.corpus_registry import (
    CATEGORIES,
    ReferenceManifestV1,
    parse_reference_manifest,
)
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.mvp_schema import file_sha256


TRANCHE_SCHEMA_ID = "rtl-advisor-tranche-lock-v1"
TRANCHE_DOCUMENT_TYPE = "rtl-advisor.tranche-lock"
TRANCHE_SCHEMA_VERSION = 1

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PPA_KEYS = {
    "area",
    "cell_count",
    "delay",
    "depth",
    "fmax",
    "logic_depth",
    "power",
    "slack",
    "synthesis_outcome",
}


class TrancheLockError(ValueError):
    """Raised when a frozen corpus tranche is malformed or has been changed."""

    def __init__(self, message: str, *, code: str = "invalid_tranche_lock") -> None:
        super().__init__(message)
        self.code = code


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TrancheLockError(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise TrancheLockError(f"{context} must be an array")
    return value


def _nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrancheLockError(f"{context} must be a non-empty string")
    return value


def _sha256(value: Any, context: str) -> str:
    digest = _nonempty(value, context)
    if not _SHA256_PATTERN.fullmatch(digest):
        raise TrancheLockError(f"{context} must be a lowercase SHA-256 digest")
    return digest


def _identifier(value: Any, context: str) -> str:
    identifier = _nonempty(value, context)
    if not _ID_PATTERN.fullmatch(identifier):
        raise TrancheLockError(f"{context} is not a stable identifier")
    return identifier


def _reject_measurements(value: Any, context: str = "lock") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if context.startswith("lock.references") and str(key).lower() in _PPA_KEYS:
                raise TrancheLockError(
                    f"{context} contains pre-freeze PPA field {key!r}",
                    code="ppa_visible_before_freeze",
                )
            _reject_measurements(item, f"{context}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_measurements(item, f"{context}[{index}]")


def validate_tranche_lock(raw: Mapping[str, Any]) -> dict[str, Any]:
    expected_hash = _sha256(raw.get("semantic_hash"), "semantic_hash")
    core = {key: value for key, value in raw.items() if key != "semantic_hash"}
    if stable_hash(core) != expected_hash:
        raise TrancheLockError(
            "tranche lock semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    if (
        raw.get("schema_version") != TRANCHE_SCHEMA_VERSION
        or raw.get("schema") != TRANCHE_SCHEMA_ID
        or raw.get("document_type") != TRANCHE_DOCUMENT_TYPE
    ):
        raise TrancheLockError("unsupported tranche lock schema", code="unsupported_schema")
    _identifier(raw.get("tranche_id"), "tranche_id")
    if raw.get("tier") not in {"A", "B", "C", "D"}:
        raise TrancheLockError("tier must be A, B, C, or D")

    policy = _object(raw.get("selection_policy"), "selection_policy")
    if policy.get("candidate_ppa_inspected_before_freeze") is not False:
        raise TrancheLockError(
            "tranche must be frozen before candidate PPA is inspected",
            code="ppa_visible_before_freeze",
        )
    candidate_count = policy.get("candidate_count")
    minimum_upstreams = policy.get("minimum_upstream_projects")
    minimum_categories = policy.get("minimum_categories")
    for value, context in (
        (candidate_count, "selection_policy.candidate_count"),
        (minimum_upstreams, "selection_policy.minimum_upstream_projects"),
        (minimum_categories, "selection_policy.minimum_categories"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TrancheLockError(f"{context} must be a positive integer")

    upstreams = _array(raw.get("upstreams"), "upstreams")
    upstream_ids: set[str] = set()
    for index, item in enumerate(upstreams):
        upstream = _object(item, f"upstreams[{index}]")
        upstream_id = _identifier(
            upstream.get("upstream_project_id"),
            f"upstreams[{index}].upstream_project_id",
        )
        if upstream_id in upstream_ids:
            raise TrancheLockError(f"duplicate upstream project: {upstream_id}")
        upstream_ids.add(upstream_id)
        revision = _nonempty(upstream.get("revision"), f"upstreams[{index}].revision")
        if upstream.get("revision_kind") != "commit" or not _COMMIT_PATTERN.fullmatch(revision):
            raise TrancheLockError(
                f"upstreams[{index}] must pin an exact 40-character commit"
            )
        _sha256(upstream.get("archive_sha256"), f"upstreams[{index}].archive_sha256")
        _sha256(upstream.get("license_sha256"), f"upstreams[{index}].license_sha256")
        if upstream.get("license_disposition") == "pending":
            raise TrancheLockError(
                f"upstreams[{index}] requires a recorded license disposition"
            )

    references = _array(raw.get("references"), "references")
    if len(references) != candidate_count:
        raise TrancheLockError(
            f"selection policy requires {candidate_count} references, found {len(references)}"
        )
    reference_ids: set[str] = set()
    orders: set[int] = set()
    represented_upstreams: set[str] = set()
    represented_categories: set[str] = set()
    for index, item in enumerate(references):
        reference = _object(item, f"references[{index}]")
        reference_id = _identifier(
            reference.get("reference_id"), f"references[{index}].reference_id"
        )
        if reference_id in reference_ids:
            raise TrancheLockError(f"duplicate reference ID: {reference_id}")
        reference_ids.add(reference_id)
        order = reference.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise TrancheLockError(f"references[{index}].order must be positive")
        if order in orders:
            raise TrancheLockError(f"duplicate reference order: {order}")
        orders.add(order)
        upstream_id = _identifier(
            reference.get("upstream_project_id"),
            f"references[{index}].upstream_project_id",
        )
        if upstream_id not in upstream_ids:
            raise TrancheLockError(
                f"reference {reference_id!r} names an unknown upstream"
            )
        represented_upstreams.add(upstream_id)
        _nonempty(reference.get("top"), f"references[{index}].top")
        _nonempty(reference.get("primary_source"), f"references[{index}].primary_source")
        _sha256(
            reference.get("primary_source_sha256"),
            f"references[{index}].primary_source_sha256",
        )
        categories = _array(reference.get("categories"), f"references[{index}].categories")
        if not categories or any(category not in CATEGORIES for category in categories):
            raise TrancheLockError(f"reference {reference_id!r} has invalid categories")
        represented_categories.update(str(category) for category in categories)
        if reference.get("proof_level") not in {"P1", "P2", "P3", "P4", "P5", "P6"}:
            raise TrancheLockError(f"reference {reference_id!r} lacks a proof contract")

    if orders != set(range(1, candidate_count + 1)):
        raise TrancheLockError("reference ordering must be contiguous from one")
    if len(represented_upstreams) < minimum_upstreams:
        raise TrancheLockError("tranche does not meet its upstream-project minimum")
    if len(represented_categories) < minimum_categories:
        raise TrancheLockError("tranche does not meet its category minimum")
    _reject_measurements(raw)
    return dict(raw)


def load_tranche_lock(path: str | Path) -> dict[str, Any]:
    lock_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrancheLockError(f"invalid tranche lock {lock_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TrancheLockError(f"tranche lock must be a JSON object: {lock_path}")
    return validate_tranche_lock(raw)


def tranche_summary(lock: Mapping[str, Any], *, path: str | Path | None = None) -> dict[str, Any]:
    references = _array(lock.get("references"), "references")
    categories = sorted(
        {
            category
            for reference in references
            for category in _array(_object(reference, "reference").get("categories"), "categories")
        }
    )
    upstreams = sorted(
        {
            _object(reference, "reference")["upstream_project_id"]
            for reference in references
        }
    )
    return {
        "status": "passed",
        "schema": TRANCHE_SCHEMA_ID,
        "tranche_id": lock["tranche_id"],
        "tier": lock["tier"],
        "reference_count": len(references),
        "stateful_reference_count": sum(
            _object(reference, "reference").get("stateful") is True
            for reference in references
        ),
        "upstream_project_count": len(upstreams),
        "upstream_project_ids": upstreams,
        "category_count": len(categories),
        "categories": categories,
        "candidate_ppa_inspected_before_freeze": False,
        "semantic_hash": lock["semantic_hash"],
        "lock_path": str(Path(path).expanduser().resolve()) if path is not None else None,
    }


def materialize_tranche_references(
    lock: Mapping[str, Any],
) -> tuple[ReferenceManifestV1, ...]:
    """Create deterministic license-reviewed registry records from a tranche lock."""

    validated = validate_tranche_lock(lock)
    upstreams = {
        item["upstream_project_id"]: item
        for item in _array(validated["upstreams"], "upstreams")
    }
    result: list[ReferenceManifestV1] = []
    for item in _array(validated["references"], "references"):
        reference = _object(item, "reference")
        upstream = _object(upstreams[reference["upstream_project_id"]], "upstream")
        stateful = reference.get("stateful") is True
        upstream_id = str(reference["upstream_project_id"])
        primary_source = str(reference["primary_source"])
        behavioral_path = str(reference["behavioral_basis"])
        basis_kind = (
            "documented_pair"
            if reference["reference_id"] == "opentitan-prim-arbiter-ppc"
            else "upstream_tests"
            if any(token in behavioral_path for token in ("test", "formal", "fpv"))
            else "specification"
        )
        if stateful:
            if upstream_id == "alexforencich-verilog-axis":
                clocks = [{"name": "clk", "edge": "rising", "period_ns": None}]
                resets = [
                    {"name": "rst", "polarity": "active_high", "synchronous": True}
                ]
            else:
                clocks = [{"name": "clk_i", "edge": "rising", "period_ns": None}]
                resets = [
                    {
                        "name": "rst_ni",
                        "polarity": "active_low",
                        "synchronous": False,
                    }
                ]
        else:
            clocks = []
            resets = []
        payload = {
            "schema_version": 1,
            "schema": "rtl-advisor-reference-v1",
            "document_type": "rtl-advisor.reference-manifest",
            "reference_id": reference["reference_id"],
            "display_name": reference["display_name"],
            "tier": validated["tier"],
            "categories": reference["categories"],
            "qualification": {
                "state": "license_reviewed",
                "status": "active",
                "reason": None,
            },
            "provenance": {
                "project": upstream_id,
                "canonical_url": upstream["canonical_url"],
                "revision": upstream["revision"],
                "revision_kind": upstream["revision_kind"],
                "license": {
                    "expression": upstream["license_expression"],
                    "url": f"{upstream['canonical_url']}/blob/{upstream['revision']}/{upstream['license_path']}",
                    "sha256": upstream["license_sha256"],
                    "disposition": upstream["license_disposition"],
                },
            },
            "lineage": {
                "upstream_project_id": upstream_id,
                "repository_lineage_id": upstream_id,
                "design_lineage_id": reference["reference_id"],
                "containing_design_ids": [upstream_id],
            },
            "compile_context": {
                "top": reference["top"],
                "sources": [primary_source],
                "filelist": None,
                "include_dirs": [],
                "defines": [],
                "parameters": {},
                "generated_inputs": [],
                "clocks": clocks,
                "resets": resets,
                "memories": [],
                "black_boxes": [],
                "frontend": None,
                "frontend_version": None,
                "build_commands": [],
                "lint_commands": [],
                "test_commands": [],
            },
            "compile_context_hashes": {
                "filelist_sha256": None,
                "include_tree_hashes": [],
                "generated_input_hashes": [],
                "compile_context_hash": None,
            },
            "behavioral_basis": [
                {
                    "kind": basis_kind,
                    "location": behavioral_path,
                    "description": "Pre-registered upstream evidence location; reproduction is pending.",
                }
            ],
            "proof_contract": {
                "schema": "rtl-advisor-proof-v1",
                "level": reference["proof_level"],
                "kind": (
                    "cycle_aligned_sequential_equivalence"
                    if stateful
                    else "combinational_equivalence"
                ),
                "latency_relation": reference["latency_relation"],
                "assumptions": (
                    ["Reset and protocol assumptions will be frozen before proof."]
                    if stateful
                    else []
                ),
                "observables": ["all top-level outputs"],
            },
            "source_hashes": [
                {
                    "path": primary_source,
                    "sha256": reference["primary_source_sha256"],
                }
            ],
            "synthesis_profiles": ["standard", "stronger"],
            "split": "development",
        }
        result.append(parse_reference_manifest(payload))
    return tuple(result)


def verify_tranche_sources(
    lock: Mapping[str, Any],
    source_roots: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Verify an acquired tranche without invoking parsers, formal, or synthesis."""

    validated = validate_tranche_lock(lock)
    configured = {
        upstream_id: Path(root).expanduser().resolve()
        for upstream_id, root in source_roots.items()
    }
    results: list[dict[str, Any]] = []

    def verify_file(
        *,
        upstream_id: str,
        root: Path,
        relative_path: str,
        expected: str,
        role: str,
        reference_id: str | None = None,
    ) -> None:
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise TrancheLockError(
                f"source path escapes upstream root: {relative_path}",
                code="unsafe_path",
            ) from exc
        actual = file_sha256(path) if path.is_file() else None
        results.append(
            {
                "upstream_project_id": upstream_id,
                "reference_id": reference_id,
                "role": role,
                "relative_path": relative_path,
                "path": str(path),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "status": "passed" if actual == expected else "failed",
            }
        )

    for item in _array(validated["upstreams"], "upstreams"):
        upstream = _object(item, "upstream")
        upstream_id = str(upstream["upstream_project_id"])
        root = configured.get(upstream_id)
        if root is None:
            raise TrancheLockError(
                f"missing source root for {upstream_id!r}",
                code="missing_source_root",
            )
        verify_file(
            upstream_id=upstream_id,
            root=root,
            relative_path="source.tar.gz",
            expected=str(upstream["archive_sha256"]),
            role="archive",
        )
        verify_file(
            upstream_id=upstream_id,
            root=root,
            relative_path=str(upstream["license_path"]),
            expected=str(upstream["license_sha256"]),
            role="license",
        )

    for item in _array(validated["references"], "references"):
        reference = _object(item, "reference")
        upstream_id = str(reference["upstream_project_id"])
        root = configured[upstream_id]
        reference_id = str(reference["reference_id"])
        verify_file(
            upstream_id=upstream_id,
            root=root,
            relative_path=str(reference["primary_source"]),
            expected=str(reference["primary_source_sha256"]),
            role="primary_source",
            reference_id=reference_id,
        )
        intended = _object(reference["intended_variant"], "intended_variant")
        if "source" in intended and "source_sha256" in intended:
            verify_file(
                upstream_id=upstream_id,
                root=root,
                relative_path=str(intended["source"]),
                expected=str(intended["source_sha256"]),
                role="upstream_variant_source",
                reference_id=reference_id,
            )

    failed = [result for result in results if result["status"] != "passed"]
    return {
        "status": "passed" if not failed else "failed",
        "schema": TRANCHE_SCHEMA_ID,
        "tranche_id": validated["tranche_id"],
        "semantic_hash": validated["semantic_hash"],
        "verified_file_count": len(results),
        "failed_file_count": len(failed),
        "results": results,
    }
