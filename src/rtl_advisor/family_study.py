from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_schema import (
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.transformation_executor import (
    DEFAULT_EXECUTOR_REGISTRY,
    TransformationExecutorError,
)
from rtl_advisor.transformation_registry import DEFAULT_TRANSFORMATION_REGISTRY


FAMILY_STUDY_SCHEMA_VERSION = 1
FAMILY_STUDY_DOCUMENT_TYPE = "rtl-advisor-family-study-v1"
FAMILY_EVIDENCE_DOCUMENT_TYPE = "rtl-advisor-family-evidence-v1"
FAMILY_REPORT_DOCUMENT_TYPE = "rtl-advisor-family-report-v1"
REPEAT_IDS = ("repeat-1", "repeat-2")
MEASUREMENT_LEVELS = ("M0", "M1", "M2")
MEASUREMENT_OUTCOMES = (
    "improved",
    "neutral",
    "regressed",
    "disagreed",
    "failed",
    "inconclusive",
    "blocked",
)
_FORBIDDEN_PREFREEZE_KEYS = {
    "ppa",
    "measurement",
    "measurements",
    "synthesis_result",
    "synthesis_results",
    "classification",
    "recommendation",
    "recommended",
}


class FamilyStudyError(RuntimeError):
    """Raised when family evidence cannot support its stated claim."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_family_study",
    ) -> None:
        super().__init__(message)
        self.code = code


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FamilyStudyError(f"{name} must be an object")
    return value


def _sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise FamilyStudyError(f"{name} must be an array")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise FamilyStudyError(f"{name} must be a non-empty string")
    return value


def _sha256(value: Any, name: str) -> str:
    result = _string(value, name)
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise FamilyStudyError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _ordered(items: Iterable[Mapping[str, Any]], name: str) -> None:
    values = [item.get("order") for item in items]
    if values != list(range(1, len(values) + 1)):
        raise FamilyStudyError(
            f"{name} must use contiguous one-based order",
            code="invalid_cohort_order",
        )


def _assert_no_prefreeze_results(value: Any, path: str = "study") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in _FORBIDDEN_PREFREEZE_KEYS:
                raise FamilyStudyError(
                    f"{path}.{key} is forbidden before the cohort is frozen",
                    code="ppa_visible_before_freeze",
                )
            _assert_no_prefreeze_results(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_prefreeze_results(nested, f"{path}[{index}]")


def seal_family_study(value: Mapping[str, Any]) -> dict[str, Any]:
    core = {
        key: nested
        for key, nested in value.items()
        if key not in {"manifest_hash", "semantic_hash"}
    }
    with_manifest = {**core, "manifest_hash": stable_hash(core)}
    return {**with_manifest, "semantic_hash": stable_hash(with_manifest)}


def validate_family_study(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    if payload.get("schema_version") != FAMILY_STUDY_SCHEMA_VERSION:
        raise FamilyStudyError("unsupported family-study schema version")
    if payload.get("document_type") != FAMILY_STUDY_DOCUMENT_TYPE:
        raise FamilyStudyError("invalid family-study document type")
    _string(payload.get("study_id"), "study_id")
    _string(payload.get("family_id"), "family_id")
    if payload.get("transformation_id") != "same_cycle_arbiter_topology":
        raise FamilyStudyError("this release supports only the arbiter family")

    semantic_hash = _sha256(payload.get("semantic_hash"), "semantic_hash")
    semantic_core = {
        key: nested for key, nested in payload.items() if key != "semantic_hash"
    }
    if semantic_hash != stable_hash(semantic_core):
        raise FamilyStudyError(
            "family-study semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    manifest_hash = _sha256(payload.get("manifest_hash"), "manifest_hash")
    manifest_core = {
        key: nested
        for key, nested in payload.items()
        if key not in {"manifest_hash", "semantic_hash"}
    }
    if manifest_hash != stable_hash(manifest_core):
        raise FamilyStudyError(
            "family-study manifest hash mismatch",
            code="artifact_hash_mismatch",
        )
    _assert_no_prefreeze_results(manifest_core)

    primary = [
        _mapping(item, f"primary_cohort[{index}]")
        for index, item in enumerate(
            _sequence(payload.get("primary_cohort"), "primary_cohort")
        )
    ]
    reserves = [
        _mapping(item, f"reserve_cohort[{index}]")
        for index, item in enumerate(
            _sequence(payload.get("reserve_cohort"), "reserve_cohort")
        )
    ]
    if len(primary) != 10:
        raise FamilyStudyError(
            "the frozen primary cohort must contain exactly ten pairs",
            code="cohort_shortfall",
        )
    if len(reserves) != 3:
        raise FamilyStudyError("the frozen reserve cohort must contain three pairs")
    _ordered(primary, "primary_cohort")
    _ordered(reserves, "reserve_cohort")

    pair_ids: list[str] = []
    reference_ids: list[str] = []
    lineages: set[str] = set()
    configurations: dict[str, set[str]] = {}
    for index, pair in enumerate(primary):
        label = f"primary_cohort[{index}]"
        pair_id = _string(pair.get("pair_id"), f"{label}.pair_id")
        reference_id = _string(pair.get("reference_id"), f"{label}.reference_id")
        pair_ids.append(pair_id)
        reference_ids.append(reference_id)
        lineages.add(_string(pair.get("upstream_project_id"), f"{label}.upstream_project_id"))
        _sha256(pair.get("source_hash"), f"{label}.source_hash")
        _sha256(pair.get("license_hash"), f"{label}.license_hash")
        _string(pair.get("revision"), f"{label}.revision")
        _string(pair.get("candidate_origin"), f"{label}.candidate_origin")
        _string(pair.get("candidate_version"), f"{label}.candidate_version")
        _string(pair.get("executor_id"), f"{label}.executor_id")
        _string(pair.get("executor_version"), f"{label}.executor_version")
        if pair.get("proof_level") not in {"P1", "P2"}:
            raise FamilyStudyError(
                f"{label}.proof_level must be P1 or P2",
                code="unsupported_proof_level",
            )
        matrix = [
            _mapping(item, f"{label}.parameter_matrix[{matrix_index}]")
            for matrix_index, item in enumerate(
                _sequence(pair.get("parameter_matrix"), f"{label}.parameter_matrix")
            )
        ]
        if len(matrix) != 4:
            raise FamilyStudyError(
                f"{label}.parameter_matrix must contain four frozen configurations"
            )
        configuration_ids = [
            _string(item.get("configuration_id"), "configuration_id")
            for item in matrix
        ]
        if len(set(configuration_ids)) != len(configuration_ids):
            raise FamilyStudyError(
                f"{label} contains duplicate configuration IDs",
                code="duplicate_configuration",
            )
        configurations[pair_id] = set(configuration_ids)
    if len(set(pair_ids)) != len(pair_ids):
        raise FamilyStudyError("duplicate pair ID", code="duplicate_pair")
    if len(set(reference_ids)) != len(reference_ids):
        raise FamilyStudyError(
            "a parameter configuration cannot count as a separate reference pair",
            code="duplicate_reference_pair",
        )
    if len(lineages) < 4:
        raise FamilyStudyError(
            "the frozen cohort must span at least four upstream lineages",
            code="lineage_shortfall",
        )

    for index, reserve in enumerate(reserves):
        label = f"reserve_cohort[{index}]"
        _string(reserve.get("reference_id"), f"{label}.reference_id")
        _string(reserve.get("upstream_project_id"), f"{label}.upstream_project_id")
        _string(reserve.get("replacement_condition"), f"{label}.replacement_condition")

    m2 = _mapping(payload.get("m2_selection"), "m2_selection")
    selected = [
        _mapping(item, f"m2_selection.primary[{index}]")
        for index, item in enumerate(
            _sequence(m2.get("primary"), "m2_selection.primary")
        )
    ]
    fallback = [
        _mapping(item, f"m2_selection.fallback[{index}]")
        for index, item in enumerate(
            _sequence(m2.get("fallback"), "m2_selection.fallback")
        )
    ]
    if len(selected) != 5:
        raise FamilyStudyError("exactly five M2 samples must be frozen")
    _ordered(selected, "m2_selection.primary")
    _ordered(fallback, "m2_selection.fallback")
    for group, name in ((selected, "primary"), (fallback, "fallback")):
        for item in group:
            pair_id = _string(item.get("pair_id"), f"M2 {name} pair_id")
            configuration_id = _string(
                item.get("configuration_id"),
                f"M2 {name} configuration_id",
            )
            if configuration_id not in configurations.get(pair_id, set()):
                raise FamilyStudyError(
                    f"M2 {name} sample references an unknown pair/configuration",
                    code="unknown_m2_sample",
                )

    hashes = _mapping(payload.get("frozen_hashes"), "frozen_hashes")
    for name in (
        "tool_hash",
        "liberty_hash",
        "constraint_hash",
        "transformation_registry_hash",
        "executor_registry_hash",
    ):
        _sha256(hashes.get(name), f"frozen_hashes.{name}")
    if (
        hashes["transformation_registry_hash"]
        != DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
    ):
        raise FamilyStudyError(
            "family study uses a stale transformation registry",
            code="stale_transformation_registry",
        )
    if hashes["executor_registry_hash"] != DEFAULT_EXECUTOR_REGISTRY.registry_hash:
        raise FamilyStudyError(
            "family study uses a stale executor registry",
            code="stale_executor_registry",
        )

    gates = _mapping(payload.get("gates"), "gates")
    if gates.get("minimum_pairs") != 10 or gates.get("minimum_lineages") != 3:
        raise FamilyStudyError("family credibility gate was not frozen correctly")
    if gates.get("minimum_reproducibility") != 0.95:
        raise FamilyStudyError("family reproducibility gate must be 0.95")
    promotion = _mapping(gates.get("promotion"), "gates.promotion")
    if (
        promotion.get("minimum_improved_pairs") != 2
        or promotion.get("minimum_improved_lineages") != 2
        or promotion.get("minimum_m2_confirmations") != 2
        or promotion.get("minimum_m2_lineages") != 2
    ):
        raise FamilyStudyError("product-preview promotion gate is incomplete")
    return payload


def read_family_study(path: str | Path) -> dict[str, Any]:
    study_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(study_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FamilyStudyError(f"cannot read family study {study_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise FamilyStudyError("family study must be an object")
    return validate_family_study(payload)


def _pair_configuration_key(pair_id: str, configuration_id: str) -> str:
    return f"{pair_id}/{configuration_id}"


def _result_map(evidence: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for pair in evidence.get("pairs", []):
        if not isinstance(pair, Mapping):
            continue
        pair_id = str(pair.get("pair_id", ""))
        for configuration in pair.get("configurations", []):
            if not isinstance(configuration, Mapping):
                continue
            key = _pair_configuration_key(
                pair_id,
                str(configuration.get("configuration_id", "")),
            )
            result[key] = configuration
    return result


def compare_family_repeats(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    first_results = _result_map(first)
    second_results = _result_map(second)
    keys = sorted(set(first_results) | set(second_results))
    checks: list[dict[str, Any]] = []
    reproduced = 0
    total = 0
    for key in keys:
        left = first_results.get(key)
        right = second_results.get(key)
        if left is None or right is None:
            checks.append({"key": key, "status": "missing_repeat"})
            total += 1
            continue
        levels: dict[str, Any] = {}
        for level in MEASUREMENT_LEVELS:
            left_value = (left.get("measurements") or {}).get(level)
            right_value = (right.get("measurements") or {}).get(level)
            if left_value is None and right_value is None:
                continue
            total += 1
            if not isinstance(left_value, Mapping) or not isinstance(
                right_value, Mapping
            ):
                levels[level] = {"status": "missing_repeat"}
                continue
            if level in {"M0", "M1"}:
                ok = left_value.get("normalized") == right_value.get("normalized")
                levels[level] = {
                    "status": "reproduced" if ok else "mismatch",
                    "exact": ok,
                }
            else:
                direction_ok = left_value.get("outcome") == right_value.get("outcome")
                drifts: dict[str, float] = {}
                metrics_ok = True
                for metric in ("area", "delay"):
                    left_metric = (left_value.get("metrics") or {}).get(metric)
                    right_metric = (right_value.get("metrics") or {}).get(metric)
                    if left_metric is None or right_metric is None:
                        continue
                    denominator = max(abs(float(left_metric)), 1e-12)
                    drift = abs(float(right_metric) - float(left_metric)) / denominator
                    drifts[metric] = drift
                    metrics_ok = metrics_ok and drift <= 0.02
                ok = direction_ok and metrics_ok
                levels[level] = {
                    "status": "reproduced" if ok else "mismatch",
                    "direction_agrees": direction_ok,
                    "relative_drift": drifts,
                }
            if ok:
                reproduced += 1
        checks.append({"key": key, "levels": levels})
    rate = reproduced / total if total else 0.0
    return {
        "status": "passed" if rate >= 0.95 else "failed",
        "reproduced": reproduced,
        "total": total,
        "rate": rate,
        "checks": checks,
    }


def evaluate_family_gates(
    study: Mapping[str, Any],
    evidence: Mapping[str, Any],
    reproducibility: Mapping[str, Any],
) -> dict[str, Any]:
    pairs = [item for item in evidence.get("pairs", []) if isinstance(item, Mapping)]
    proven = [
        item
        for item in pairs
        if item.get("formal_status") == "formal_passed"
    ]
    lineages = {str(item.get("upstream_project_id")) for item in proven}
    recommended = [
        configuration
        for pair in pairs
        for configuration in pair.get("configurations", [])
        if isinstance(configuration, Mapping)
        and configuration.get("recommended") is True
    ]
    invalid_recommendations = [
        item
        for item in recommended
        if item.get("formal_status") != "formal_passed"
        or any(
            ((item.get("measurements") or {}).get(level) or {}).get("outcome")
            == "regressed"
            for level in ("M0", "M1", "M2")
        )
    ]
    selected_m2 = {
        _pair_configuration_key(
            str(item["pair_id"]),
            str(item["configuration_id"]),
        )
        for item in study["m2_selection"]["primary"]
    }
    results = _result_map(evidence)
    completed_m2 = sum(
        1
        for key in selected_m2
        if ((results.get(key, {}).get("measurements") or {}).get("M2") or {}).get(
            "outcome"
        )
        in MEASUREMENT_OUTCOMES
    )
    confirmed = [
        (pair, configuration)
        for pair in pairs
        for configuration in pair.get("configurations", [])
        if isinstance(configuration, Mapping)
        and all(
            ((configuration.get("measurements") or {}).get(level) or {}).get(
                "outcome"
            )
            == "improved"
            for level in ("M0", "M1", "M2")
        )
    ]
    research_checks = {
        "ten_formally_proven_pairs": len(proven) == 10,
        "three_lineages": len(lineages) >= 3,
        "five_m2_samples_recorded": completed_m2 == 5,
        "reproducibility_at_least_95_percent": (
            float(reproducibility.get("rate", 0.0)) >= 0.95
        ),
        "no_invalid_recommendation": not invalid_recommendations,
        "at_least_one_m0_m1_m2_improvement": bool(confirmed),
    }
    improved_pairs = {str(pair.get("pair_id")) for pair, _ in confirmed}
    improved_lineages = {
        str(pair.get("upstream_project_id")) for pair, _ in confirmed
    }
    recommended_regressions = [
        item
        for item in recommended
        if any(
            ((item.get("measurements") or {}).get(level) or {}).get("outcome")
            == "regressed"
            for level in ("M0", "M1")
        )
    ]
    preview_checks = {
        "research_gate_passed": all(research_checks.values()),
        "two_improved_pairs": len(improved_pairs) >= 2,
        "two_improved_lineages": len(improved_lineages) >= 2,
        "two_m2_confirmations": len(confirmed) >= 2,
        "two_m2_lineages": len(improved_lineages) >= 2,
        "no_recommended_m0_m1_regression": not recommended_regressions,
        "current_formal_for_every_recommendation": all(
            item.get("formal_status") == "formal_passed"
            and item.get("formal_current") is True
            for item in recommended
        ),
    }
    return {
        "family_credibility": {
            "status": "passed" if all(research_checks.values()) else "failed",
            "checks": research_checks,
        },
        "product_preview": {
            "status": (
                "supported_preview"
                if all(preview_checks.values())
                else "study_only"
            ),
            "checks": preview_checks,
        },
    }


def _family_root(config: ProjectConfig, study_id: str) -> Path:
    return config.artifacts_dir / "family-studies" / study_id


def run_family_study(
    config: ProjectConfig,
    manifest_path: str | Path,
    *,
    repeat_id: str,
) -> dict[str, Any]:
    if repeat_id not in REPEAT_IDS:
        raise FamilyStudyError(
            f"repeat must be one of {', '.join(REPEAT_IDS)}",
            code="invalid_repeat",
        )
    study = read_family_study(manifest_path)
    family_root = _family_root(config, study["study_id"])
    frozen_path = family_root / "family-study.json"
    if not frozen_path.is_file():
        stored = write_hashed_json(
            frozen_path,
            {
                key: value
                for key, value in study.items()
                if key != "semantic_hash"
            },
            exclusive=True,
        )
        if stored != study:
            raise FamilyStudyError(
                "stored family study differs from the validated manifest",
                code="artifact_hash_mismatch",
            )
    else:
        current = read_hashed_json(
            frozen_path,
            document_type=FAMILY_STUDY_DOCUMENT_TYPE,
            schema_version=FAMILY_STUDY_SCHEMA_VERSION,
        )
        if current != study:
            raise FamilyStudyError(
                "append-only family study conflicts with the requested manifest",
                code="append_only_conflict",
            )
    root = family_root / "repeats" / repeat_id
    path = root / "evidence.json"
    if path.is_file():
        return read_hashed_json(
            path,
            document_type=FAMILY_EVIDENCE_DOCUMENT_TYPE,
            schema_version=FAMILY_STUDY_SCHEMA_VERSION,
        )

    pair_results: list[dict[str, Any]] = []
    qualified_count = 0
    candidate_ready_count = 0
    for pair in study["primary_cohort"]:
        try:
            executor = DEFAULT_EXECUTOR_REGISTRY.get(
                str(pair["executor_id"]),
                str(pair["executor_version"]),
            )
            if str(pair["reference_id"]) not in executor.spec.reference_ids:
                raise TransformationExecutorError(
                    "executor does not own the frozen reference",
                    code="missing_executor",
                )
            source_root = (config.corpus_dir / executor.spec.source_root).resolve()
            source_path = (source_root / str(pair["source_path"])).resolve()
            try:
                source_path.relative_to(source_root)
            except ValueError as exc:
                raise TransformationExecutorError(
                    "frozen source path escapes its upstream root",
                    code="unsafe_path",
                ) from exc
            if (
                not source_path.is_file()
                or file_sha256(source_path) != pair["source_hash"]
            ):
                raise TransformationExecutorError(
                    "frozen source is unavailable or has changed",
                    code="stale_source_hashes",
                )
            license_name = (
                "COPYING"
                if pair["upstream_project_id"]
                == "alexforencich-verilog-axis"
                else "LICENSE"
            )
            license_path = source_root / license_name
            if (
                not license_path.is_file()
                or file_sha256(license_path) != pair["license_hash"]
            ):
                raise TransformationExecutorError(
                    "frozen license is unavailable or has changed",
                    code="stale_license_hash",
                )
            qualification_status = "source_frozen"
            qualified_count += 1
            if pair["reference_id"] == "opentitan-prim-arbiter-ppc":
                candidate_status = "implemented"
            else:
                from rtl_advisor.arbiter_family_execution import (
                    candidate_source_status,
                )

                candidate_status = candidate_source_status(
                    str(pair["reference_id"])
                )
            if candidate_status == "implemented":
                candidate_ready_count += 1
        except TransformationExecutorError as exc:
            qualification_status = f"blocked:{exc.code}"
            candidate_status = "blocked"
        pair_results.append(
            {
                "pair_id": pair["pair_id"],
                "reference_id": pair["reference_id"],
                "upstream_project_id": pair["upstream_project_id"],
                "executor_id": pair["executor_id"],
                "executor_version": pair["executor_version"],
                "qualification_status": qualification_status,
                "candidate_status": candidate_status,
                "formal_status": "not_run",
                "configurations": [
                    {
                        "configuration_id": item["configuration_id"],
                        "formal_status": "not_run",
                        "formal_current": False,
                        "recommended": False,
                        "measurements": {},
                    }
                    for item in pair["parameter_matrix"]
                ],
                "controls": [],
                "failures": [],
            }
        )

    payload = {
        "schema_version": FAMILY_STUDY_SCHEMA_VERSION,
        "document_type": FAMILY_EVIDENCE_DOCUMENT_TYPE,
        "study_id": study["study_id"],
        "family_id": study["family_id"],
        "repeat_id": repeat_id,
        "study_manifest_hash": study["manifest_hash"],
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_registry_hash": DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        "status": (
            "source_qualification_blocked"
            if qualified_count < len(study["primary_cohort"])
            else "candidate_qualification_pending"
            if candidate_ready_count < len(study["primary_cohort"])
            else "ready_to_execute"
        ),
        "pairs": pair_results,
        "exclusions": [],
        "failures": [],
        "summary": {
            "frozen_pair_count": len(study["primary_cohort"]),
            "executor_available_pair_count": qualified_count,
            "candidate_implemented_pair_count": candidate_ready_count,
            "shortfall": len(study["primary_cohort"]) - qualified_count,
            "ppa_inspected": False,
        },
    }
    return write_hashed_json(path, payload, exclusive=True)


def report_family_study(
    config: ProjectConfig,
    study_id: str,
    *,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    root = _family_root(config, study_id)
    manifest = (
        read_family_study(manifest_path)
        if manifest_path is not None
        else read_family_study(root / "family-study.json")
    )
    repeats = []
    for repeat_id in REPEAT_IDS:
        path = root / "repeats" / repeat_id / "evidence.json"
        if path.is_file():
            repeats.append(
                read_hashed_json(
                    path,
                    document_type=FAMILY_EVIDENCE_DOCUMENT_TYPE,
                    schema_version=FAMILY_STUDY_SCHEMA_VERSION,
                )
            )
    if len(repeats) == 2:
        reproducibility = compare_family_repeats(repeats[0], repeats[1])
    else:
        reproducibility = {
            "status": "incomplete",
            "reproduced": 0,
            "total": 0,
            "rate": 0.0,
            "checks": [],
        }
    evidence = repeats[0] if repeats else {"pairs": []}
    gates = evaluate_family_gates(manifest, evidence, reproducibility)
    payload = {
        "schema_version": FAMILY_STUDY_SCHEMA_VERSION,
        "document_type": FAMILY_REPORT_DOCUMENT_TYPE,
        "study_id": study_id,
        "family_id": manifest["family_id"],
        "study_manifest_hash": manifest["manifest_hash"],
        "status": (
            "completed"
            if len(repeats) == 2
            and all(item.get("status") == "completed" for item in repeats)
            else "incomplete"
        ),
        "repeat_semantic_hashes": [
            item.get("semantic_hash") for item in repeats
        ],
        "reproducibility": reproducibility,
        "gates": gates,
        "claim_scope": (
            "Pinned Yosys/ABC and OpenROAD evidence only; no Genus, "
            "production-PPA, or general unseen-RTL claim."
        ),
    }
    report_path = root / "report.json"
    if report_path.is_file():
        current = read_hashed_json(
            report_path,
            document_type=FAMILY_REPORT_DOCUMENT_TYPE,
            schema_version=FAMILY_STUDY_SCHEMA_VERSION,
        )
        if current.get("repeat_semantic_hashes") == payload["repeat_semantic_hashes"]:
            return current
        raise FamilyStudyError(
            "append-only family report is stale; preserve it and use a new study ID",
            code="append_only_conflict",
        )
    return write_hashed_json(report_path, payload, exclusive=True)
