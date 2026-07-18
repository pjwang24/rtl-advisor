from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from typing import Any

from rtl_advisor.advisor_v2 import PROFILES
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, VariantSpec, load_manifest
from rtl_advisor.synthesis import SynthesisResult, _yosys_version, compare_results
from rtl_advisor.synthesis_redundancy import (
    VARIANT_IDS,
    _cell_signature,
    _is_neutral,
    _is_useful,
    _load_json,
    _load_synthesis_result,
    _manifest_path,
    _relative,
    _run_aggressive_variant,
    _stable_hash,
    _verify_diagnostic,
    _verify_equivalence_proof,
    _verify_standard_result,
    _write_json,
)
from rtl_advisor.tools import sha256_file


SCHEMA_VERSION = 1
FLOW_VERSION = "rtl-advisor-synthesis-robustness-full-calibration-v1"
ARTIFACT_RELATIVE_ROOT = Path("synthesis-robustness/full-calibration-v1")
EXPECTED_CASE_COUNT = 936
EXPECTED_CANDIDATE_COUNT = 2808
EXPECTED_RUN_COUNT = 3744
EXPECTED_FAMILY_COUNT = 9
EXPECTED_CASES_PER_FAMILY = 104
SUPPORT_FLOOR = 10
IMPLEMENTATION_PLAN_SHA256 = (
    "b760529e249e6c193c4ba7b537fdd07d1fd6631653046b05b92bb0c7ff2291df"
)
CLASSIFICATIONS = (
    "robust_useful",
    "flow_conflict",
    "absorbed_by_stronger_synthesis",
    "stronger_recipe_only",
    "synthesis_absorbed",
    "not_useful",
)


class SynthesisRobustnessFullError(RuntimeError):
    """Raised when full-calibration robustness evidence is incomplete or stale."""


def _artifact_root(config: ProjectConfig) -> Path:
    return config.artifacts_dir / ARTIFACT_RELATIVE_ROOT


def _implementation_plan(config: ProjectConfig) -> Path:
    return config.root / "implementation plan/synthesis robustness full calibration v1.md"


def _calibration_rows_path(config: ProjectConfig) -> Path:
    return config.artifacts_dir / "models/v21/calibration-rows.json"


def _load_calibration_rows(config: ProjectConfig) -> tuple[Path, dict[str, Any]]:
    path = _calibration_rows_path(config)
    payload = _load_json(path, "V2.1 calibration rows")
    rows = payload.get("rows")
    if (
        payload.get("row_count") != EXPECTED_CANDIDATE_COUNT
        or not isinstance(rows, list)
        or len(rows) != EXPECTED_CANDIDATE_COUNT
    ):
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_CANDIDATE_COUNT} calibration rows"
        )
    keys: set[tuple[str, str]] = set()
    for row in rows:
        split = row.get("training_split")
        if split not in {"calibration-v2", "calibration-v21"}:
            raise SynthesisRobustnessFullError(
                f"non-calibration row present in training source: {split!r}"
            )
        key = (str(row.get("case_id")), str(row.get("template_id")))
        if key in keys:
            raise SynthesisRobustnessFullError(
                f"duplicate calibration row {key[0]}/{key[1]}"
            )
        keys.add(key)
    return path, payload


def _validate_full_population(cases: list[dict[str, Any]]) -> None:
    if len(cases) != EXPECTED_CASE_COUNT:
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_CASE_COUNT} diagnostic cases, found {len(cases)}"
        )
    family_counts = Counter(str(case.get("family")) for case in cases)
    if len(family_counts) != EXPECTED_FAMILY_COUNT:
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_FAMILY_COUNT} families, found {len(family_counts)}"
        )
    unexpected = {
        family: count
        for family, count in family_counts.items()
        if count != EXPECTED_CASES_PER_FAMILY
    }
    if unexpected:
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_CASES_PER_FAMILY} cases per family: {unexpected}"
        )


def build_full_plan(config: ProjectConfig) -> dict[str, Any]:
    plan_document = _implementation_plan(config)
    if not plan_document.is_file():
        raise SynthesisRobustnessFullError(
            f"implementation plan missing: {plan_document}"
        )
    plan_document_sha256 = sha256_file(plan_document)
    if plan_document_sha256 != IMPLEMENTATION_PLAN_SHA256:
        raise SynthesisRobustnessFullError(
            "full-calibration implementation plan hash changed"
        )
    diagnostic_path, diagnostic = _verify_diagnostic(config)
    cases = diagnostic.get("cases")
    if not isinstance(cases, list):
        raise SynthesisRobustnessFullError("diagnostic cases must be a list")
    _validate_full_population(cases)
    calibration_path, calibration = _load_calibration_rows(config)
    calibration_keys = {
        (str(row["case_id"]), str(row["template_id"]))
        for row in calibration["rows"]
    }
    if not config.liberty.path.is_file():
        raise SynthesisRobustnessFullError("configured Liberty file is missing")
    liberty_sha256 = sha256_file(config.liberty.path)
    if liberty_sha256 != config.liberty.sha256:
        raise SynthesisRobustnessFullError("configured Liberty checksum mismatch")
    yosys_version = _yosys_version(config)
    plan_cases: list[dict[str, Any]] = []
    planned_candidate_keys: set[tuple[str, str]] = set()
    for case in sorted(
        cases, key=lambda item: (str(item["family"]), str(item["case_id"]))
    ):
        manifest_path = _manifest_path(config, str(case["case_id"]))
        manifest = load_manifest(manifest_path)
        if manifest.family != case["family"]:
            raise SynthesisRobustnessFullError(
                f"family mismatch for {manifest.case_id}"
            )
        variant_evidence: dict[str, Any] = {}
        for variant_id in VARIANT_IDS:
            variant = manifest.variant(variant_id)
            entry: dict[str, Any] = {
                "source_sha256": variant.sha256,
                "standard_synthesis": _verify_standard_result(
                    config, manifest, variant
                ),
            }
            if variant_id != manifest.baseline_id:
                entry["equivalence"] = _verify_equivalence_proof(
                    config, manifest, variant
                )
                planned_candidate_keys.add((manifest.case_id, variant_id))
            variant_evidence[variant_id] = entry
        plan_cases.append(
            {
                "case_id": manifest.case_id,
                "family": manifest.family,
                "training_split": (
                    "calibration-v21"
                    if manifest.case_id.startswith("v21_")
                    else "calibration-v2"
                ),
                "topology_signature": case["topology_signature"],
                "diagnostic_category": case["classification"]["category"],
                "manifest_path": _relative(config, manifest_path),
                "manifest_sha256": sha256_file(manifest_path),
                "variant_evidence": variant_evidence,
            }
        )
    if planned_candidate_keys != calibration_keys:
        missing = sorted(calibration_keys - planned_candidate_keys)[:5]
        extra = sorted(planned_candidate_keys - calibration_keys)[:5]
        raise SynthesisRobustnessFullError(
            f"plan/calibration row alignment failed; missing={missing}, extra={extra}"
        )
    core = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "case_count": len(plan_cases),
        "candidate_count": len(planned_candidate_keys),
        "run_count": len(plan_cases) * len(VARIANT_IDS),
        "family_count": len({case["family"] for case in plan_cases}),
        "blind_labels_used": False,
        "source": {
            "implementation_plan_path": _relative(config, plan_document),
            "implementation_plan_sha256": plan_document_sha256,
            "diagnostic_path": _relative(config, diagnostic_path),
            "diagnostic_sha256": sha256_file(diagnostic_path),
            "diagnostic_hash": diagnostic["diagnostic_hash"],
            "calibration_rows_path": _relative(config, calibration_path),
            "calibration_rows_sha256": sha256_file(calibration_path),
            "calibration_feature_schema_hash": calibration[
                "feature_schema_hash"
            ],
            "calibration_row_count": calibration["row_count"],
        },
        "synthesis_environment": {
            "yosys_version": yosys_version,
            "liberty_name": config.liberty.name,
            "liberty_sha256": liberty_sha256,
            "liberty_source_commit": config.liberty.source_commit,
            "driving_cell": config.synthesis.driving_cell,
            "output_load_ff": config.synthesis.output_load_ff,
        },
        "thresholds": {
            "delay_improvement_percent": 3.0,
            "area_guardrail_percent": -10.0,
            "area_improvement_percent": 5.0,
            "delay_guardrail_percent": -2.0,
            "direction_neutral_percent": 1.0,
            "family_support_floor": SUPPORT_FLOOR,
        },
        "classification_order": list(CLASSIFICATIONS),
        "cases": plan_cases,
    }
    if core["run_count"] != EXPECTED_RUN_COUNT:
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_RUN_COUNT} planned runs"
        )
    return {**core, "plan_hash": _stable_hash(core)}


def create_full_plan(config: ProjectConfig) -> Path:
    path = _artifact_root(config) / "plan.json"
    current = build_full_plan(config)
    if path.is_file():
        frozen = _load_json(path, "frozen full-calibration robustness plan")
        if frozen != current or frozen.get("plan_hash") != current["plan_hash"]:
            raise SynthesisRobustnessFullError(
                "frozen full-calibration robustness plan no longer matches its inputs"
            )
        return path
    _write_json(path, current)
    return path


def _load_full_plan(config: ProjectConfig) -> dict[str, Any]:
    path = create_full_plan(config)
    plan = _load_json(path, "full-calibration robustness plan")
    core = {key: value for key, value in plan.items() if key != "plan_hash"}
    if plan.get("plan_hash") != _stable_hash(core):
        raise SynthesisRobustnessFullError(
            "full-calibration robustness plan hash mismatch"
        )
    return plan


def _checkpoint_summary(
    config: ProjectConfig,
    plan: dict[str, Any],
    *,
    completed: list[SynthesisResult],
    failures: list[dict[str, str]],
    status: str,
) -> dict[str, Any]:
    summary = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "plan_hash": plan["plan_hash"],
        "status": status,
        "run_count": plan["run_count"],
        "completed_count": len(completed) + len(failures),
        "passed_count": len(completed),
        "failed_count": len(failures),
        "fresh_count": sum(not result.cached for result in completed),
        "cached_count": sum(result.cached for result in completed),
        "failures": sorted(
            failures, key=lambda item: (item["case_id"], item["variant_id"])
        ),
    }
    path = _artifact_root(config) / "run-summary.json"
    summary["summary_path"] = str(path.resolve())
    _write_json(path, summary)
    return summary


def run_full_sweep(
    config: ProjectConfig,
    *,
    workers: int = 8,
) -> dict[str, Any]:
    if not 1 <= workers <= 16:
        raise SynthesisRobustnessFullError("workers must be between 1 and 16")
    plan = _load_full_plan(config)
    liberty_sha256 = sha256_file(config.liberty.path)
    yosys_version = _yosys_version(config)
    if liberty_sha256 != plan["synthesis_environment"]["liberty_sha256"]:
        raise SynthesisRobustnessFullError("Liberty changed after plan creation")
    if yosys_version != plan["synthesis_environment"]["yosys_version"]:
        raise SynthesisRobustnessFullError("Yosys changed after plan creation")
    jobs: list[tuple[CaseManifest, VariantSpec]] = []
    for case in plan["cases"]:
        manifest = load_manifest(config.root / case["manifest_path"])
        for variant_id in VARIANT_IDS:
            jobs.append((manifest, manifest.variant(variant_id)))
    if len(jobs) != EXPECTED_RUN_COUNT:
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_RUN_COUNT} jobs, found {len(jobs)}"
        )
    results: list[SynthesisResult] = []
    failures: list[dict[str, str]] = []
    runs_root = _artifact_root(config) / "runs"
    _checkpoint_summary(
        config, plan, completed=results, failures=failures, status="running"
    )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _run_aggressive_variant,
                config,
                manifest,
                variant,
                plan_hash=plan["plan_hash"],
                yosys_version=yosys_version,
                liberty_sha256=liberty_sha256,
                output_root=runs_root,
                flow_version=FLOW_VERSION,
            ): (manifest.case_id, variant.variant_id)
            for manifest, variant in jobs
        }
        for future in as_completed(futures):
            case_id, variant_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append(
                    {
                        "case_id": case_id,
                        "variant_id": variant_id,
                        "error": str(exc),
                    }
                )
            if (len(results) + len(failures)) % 100 == 0:
                _checkpoint_summary(
                    config,
                    plan,
                    completed=results,
                    failures=failures,
                    status="running",
                )
    final_status = (
        "passed"
        if not failures and len(results) == EXPECTED_RUN_COUNT
        else "failed"
    )
    summary = _checkpoint_summary(
        config,
        plan,
        completed=results,
        failures=failures,
        status=final_status,
    )
    if final_status != "passed":
        raise SynthesisRobustnessFullError(
            f"full synthesis-robustness sweep failed for {len(failures)} runs; "
            f"see {summary['summary_path']}"
        )
    report = build_full_report(config)
    return {
        **summary,
        "report_path": report["markdown_path"],
        "training_rows_path": report["training_rows_path"],
    }


def direction_label(value: float) -> str:
    if value > 1.0:
        return "improve"
    if value < -1.0:
        return "degrade"
    return "neutral"


def directions_compatible(first: str, second: str) -> bool:
    allowed = {"improve", "neutral", "degrade"}
    if first not in allowed or second not in allowed:
        raise SynthesisRobustnessFullError("unknown direction label")
    return first == second or "neutral" in {first, second}


def classify_robust_candidate(
    standard_comparison: dict[str, Any],
    stronger_comparison: dict[str, Any],
    *,
    stronger_cell_signatures_equal: bool,
) -> dict[str, Any]:
    standard_useful = _is_useful(standard_comparison)
    stronger_useful = _is_useful(stronger_comparison)
    metric_keys = {
        "delay": "critical_delay_ps",
        "area": "area_total",
        "cell_count": "cell_count",
    }
    standard_directions = {
        metric: direction_label(
            float(standard_comparison[key]["improvement_percent"])
        )
        for metric, key in metric_keys.items()
    }
    stronger_directions = {
        metric: direction_label(
            float(stronger_comparison[key]["improvement_percent"])
        )
        for metric, key in metric_keys.items()
    }
    compatibility = {
        metric: directions_compatible(
            standard_directions[metric], stronger_directions[metric]
        )
        for metric in metric_keys
    }
    if standard_useful and stronger_useful:
        classification = (
            "robust_useful"
            if compatibility["delay"] and compatibility["area"]
            else "flow_conflict"
        )
    elif standard_useful:
        classification = "absorbed_by_stronger_synthesis"
    elif stronger_useful:
        classification = "stronger_recipe_only"
    elif _is_neutral(stronger_comparison) and stronger_cell_signatures_equal:
        classification = "synthesis_absorbed"
    else:
        classification = "not_useful"
    return {
        "classification": classification,
        "standard_useful": standard_useful,
        "stronger_useful": stronger_useful,
        "robust_eligible": classification == "robust_useful",
        "standard_directions": standard_directions,
        "stronger_directions": stronger_directions,
        "direction_compatibility": compatibility,
    }


def _comparison_targets(comparison: dict[str, Any]) -> dict[str, float]:
    return {
        "delay": float(
            comparison["critical_delay_ps"]["improvement_percent"]
        ),
        "area": float(comparison["area_total"]["improvement_percent"]),
        "cell_count": float(comparison["cell_count"]["improvement_percent"]),
    }


def _verify_original_targets(
    original: dict[str, Any],
    standard_targets: dict[str, float],
) -> None:
    for metric in ("delay", "area", "cell_count"):
        if abs(float(original["targets"][metric]) - standard_targets[metric]) > 1e-6:
            raise SynthesisRobustnessFullError(
                f"standard target drift for {original['case_id']}/"
                f"{original['template_id']}/{metric}"
            )


def _read_case_results(
    config: ProjectConfig,
    plan: dict[str, Any],
    case: dict[str, Any],
) -> tuple[dict[str, SynthesisResult], dict[str, SynthesisResult]]:
    standard: dict[str, SynthesisResult] = {}
    stronger: dict[str, SynthesisResult] = {}
    for variant_id in VARIANT_IDS:
        standard_path = (
            config.artifacts_dir
            / "cases"
            / case["case_id"]
            / "synthesis"
            / variant_id
            / "result.json"
        )
        stronger_path = (
            _artifact_root(config)
            / "runs"
            / case["case_id"]
            / variant_id
            / "result.json"
        )
        standard[variant_id] = _load_synthesis_result(
            standard_path, "standard synthesis result"
        )
        stronger[variant_id] = _load_synthesis_result(
            stronger_path, "stronger synthesis result"
        )
        if stronger[variant_id].provenance.get("plan_hash") != plan["plan_hash"]:
            raise SynthesisRobustnessFullError(
                f"stronger result belongs to another plan: "
                f"{case['case_id']}/{variant_id}"
            )
        if stronger[variant_id].provenance.get("flow_version") != FLOW_VERSION:
            raise SynthesisRobustnessFullError(
                f"unexpected stronger flow: {case['case_id']}/{variant_id}"
            )
        expected_sha = case["variant_evidence"][variant_id]["source_sha256"]
        if (
            standard[variant_id].source_sha256 != expected_sha
            or stronger[variant_id].source_sha256 != expected_sha
        ):
            raise SynthesisRobustnessFullError(
                f"source hash mismatch: {case['case_id']}/{variant_id}"
            )
    return standard, stronger


def _write_training_table(
    config: ProjectConfig,
    plan: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    root = _artifact_root(config)
    core = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "plan_hash": plan["plan_hash"],
        "blind_labels_used": False,
        "source_feature_schema_hash": plan["source"][
            "calibration_feature_schema_hash"
        ],
        "row_count": len(rows),
        "rows": rows,
    }
    table = {**core, "training_table_hash": _stable_hash(core)}
    json_path = root / "training-rows.json"
    jsonl_path = root / "training-rows.jsonl"
    _write_json(json_path, table)
    jsonl_path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return {
        "payload": table,
        "json_path": json_path,
        "jsonl_path": jsonl_path,
        "json_sha256": sha256_file(json_path),
        "jsonl_sha256": sha256_file(jsonl_path),
    }


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def build_full_report(config: ProjectConfig) -> dict[str, Any]:
    plan = _load_full_plan(config)
    _, calibration = _load_calibration_rows(config)
    original_by_key = {
        (str(row["case_id"]), str(row["template_id"])): row
        for row in calibration["rows"]
    }
    rows: list[dict[str, Any]] = []
    for case in plan["cases"]:
        standard, stronger = _read_case_results(config, plan, case)
        baseline_signature = _cell_signature(stronger["v0"])
        for variant_id in VARIANT_IDS[1:]:
            key = (case["case_id"], variant_id)
            try:
                original = original_by_key[key]
            except KeyError as exc:
                raise SynthesisRobustnessFullError(
                    f"missing original calibration row {key[0]}/{key[1]}"
                ) from exc
            standard_comparison = compare_results(
                standard["v0"], standard[variant_id]
            )
            stronger_comparison = compare_results(
                stronger["v0"], stronger[variant_id]
            )
            standard_targets = _comparison_targets(standard_comparison)
            stronger_targets = _comparison_targets(stronger_comparison)
            _verify_original_targets(original, standard_targets)
            labels = classify_robust_candidate(
                standard_comparison,
                stronger_comparison,
                stronger_cell_signatures_equal=(
                    baseline_signature == _cell_signature(stronger[variant_id])
                ),
            )
            row_core = {
                "case_id": case["case_id"],
                "family": case["family"],
                "topology_signature": case["topology_signature"],
                "training_split": case["training_split"],
                "diagnostic_category": case["diagnostic_category"],
                "template_id": variant_id,
                "transformation_id": original["transformation_id"],
                "kernel_feature_hash": original["kernel_feature_hash"],
                "features": original["features"],
                "standard_targets": standard_targets,
                "standard_eligible": bool(original["eligible"]),
                "stronger_targets": stronger_targets,
                **labels,
                "robust_best": False,
                "stronger_cell_signatures_equal": (
                    baseline_signature == _cell_signature(stronger[variant_id])
                ),
                "plan_hash": plan["plan_hash"],
            }
            rows.append(row_core)
    if len(rows) != EXPECTED_CANDIDATE_COUNT:
        raise SynthesisRobustnessFullError(
            f"expected {EXPECTED_CANDIDATE_COUNT} output rows, found {len(rows)}"
        )
    if len({(row["case_id"], row["template_id"]) for row in rows}) != len(rows):
        raise SynthesisRobustnessFullError("duplicate output training row")
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    for case_rows in by_case.values():
        eligible = [row for row in case_rows if row["robust_eligible"]]
        if eligible:
            best = max(
                eligible,
                key=lambda row: (
                    PROFILES["balanced"].utility(
                        row["stronger_targets"]["delay"],
                        row["stronger_targets"]["area"],
                        row["stronger_targets"]["cell_count"],
                    ),
                    row["template_id"],
                ),
            )
            best["robust_best"] = True
    final_rows = []
    for row in sorted(rows, key=lambda item: (item["case_id"], item["template_id"])):
        final_rows.append({**row, "row_hash": _stable_hash(row)})
    training = _write_training_table(config, plan, final_rows)

    classification_counts = Counter(row["classification"] for row in final_rows)
    standard_useful = [row for row in final_rows if row["standard_useful"]]
    stronger_useful = [row for row in final_rows if row["stronger_useful"]]
    robust_useful = [row for row in final_rows if row["robust_eligible"]]
    standard_cases = {row["case_id"] for row in standard_useful}
    stronger_cases = {row["case_id"] for row in stronger_useful}
    robust_cases = {row["case_id"] for row in robust_useful}
    families: dict[str, dict[str, Any]] = {}
    for family in sorted({row["family"] for row in final_rows}):
        family_rows = [row for row in final_rows if row["family"] == family]
        family_cases = {row["case_id"] for row in family_rows}
        family_standard = [row for row in family_rows if row["standard_useful"]]
        family_stronger = [row for row in family_rows if row["stronger_useful"]]
        family_robust = [row for row in family_rows if row["robust_eligible"]]
        family_robust_cases = {row["case_id"] for row in family_robust}
        family_classes = Counter(row["classification"] for row in family_rows)
        standard_count = len(family_standard)
        robust_case_count = len(family_robust_cases)
        nonopportunity_count = len(family_cases) - robust_case_count
        families[family] = {
            "case_count": len(family_cases),
            "candidate_count": len(family_rows),
            "standard_useful_case_count": len(
                {row["case_id"] for row in family_standard}
            ),
            "stronger_useful_case_count": len(
                {row["case_id"] for row in family_stronger}
            ),
            "robust_opportunity_case_count": robust_case_count,
            "robust_nonopportunity_case_count": nonopportunity_count,
            "standard_useful_candidate_count": standard_count,
            "stronger_useful_candidate_count": len(family_stronger),
            "robust_useful_candidate_count": len(family_robust),
            "standard_candidate_retention_rate": (
                len(family_robust) / standard_count if standard_count else None
            ),
            "direction_compatibility": {
                metric: sum(
                    bool(row["direction_compatibility"][metric])
                    for row in family_rows
                )
                / len(family_rows)
                for metric in ("delay", "area", "cell_count")
            },
            "classification_counts": {
                name: family_classes[name] for name in CLASSIFICATIONS
            },
            "future_training_supported": (
                robust_case_count >= SUPPORT_FLOOR
                and nonopportunity_count >= SUPPORT_FLOOR
            ),
        }
    standard_count = len(standard_useful)
    supported_families = [
        family
        for family, summary in families.items()
        if summary["future_training_supported"]
    ]
    direction_compatibility = {
        metric: sum(
            bool(row["direction_compatibility"][metric]) for row in final_rows
        )
        / len(final_rows)
        for metric in ("delay", "area", "cell_count")
    }
    core = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "plan_hash": plan["plan_hash"],
        "blind_labels_used": False,
        "status": "passed",
        "case_count": EXPECTED_CASE_COUNT,
        "candidate_count": EXPECTED_CANDIDATE_COUNT,
        "equivalence_proof_count": EXPECTED_CANDIDATE_COUNT,
        "stronger_synthesis_run_count": EXPECTED_RUN_COUNT,
        "standard_useful_case_count": len(standard_cases),
        "stronger_useful_case_count": len(stronger_cases),
        "robust_opportunity_case_count": len(robust_cases),
        "robust_nonopportunity_case_count": EXPECTED_CASE_COUNT - len(robust_cases),
        "standard_useful_candidate_count": standard_count,
        "stronger_useful_candidate_count": len(stronger_useful),
        "robust_useful_candidate_count": len(robust_useful),
        "standard_candidate_retention_rate": (
            len(robust_useful) / standard_count if standard_count else None
        ),
        "classification_counts": {
            name: classification_counts[name] for name in CLASSIFICATIONS
        },
        "direction_compatibility": direction_compatibility,
        "future_training_supported_family_count": len(supported_families),
        "future_training_supported_families": supported_families,
        "families": families,
        "training_table": {
            "path": str(training["json_path"].resolve()),
            "sha256": training["json_sha256"],
            "jsonl_path": str(training["jsonl_path"].resolve()),
            "jsonl_sha256": training["jsonl_sha256"],
            "training_table_hash": training["payload"]["training_table_hash"],
            "row_count": training["payload"]["row_count"],
        },
    }
    report = {**core, "report_hash": _stable_hash(core)}
    root = _artifact_root(config)
    json_path = root / "report.json"
    markdown_path = root / "report.md"
    report["json_path"] = str(json_path.resolve())
    report["markdown_path"] = str(markdown_path.resolve())
    report["training_rows_path"] = str(training["json_path"].resolve())
    _write_json(json_path, report)

    retention = report["standard_candidate_retention_rate"]
    lines = [
        "# Full-Calibration Synthesis Robustness V1",
        "",
        "> Generated calibration RTL only. No company RTL, held-out labels, or commercial synthesis results were used.",
        "",
        "## Result",
        "",
        f"- Cases: {EXPECTED_CASE_COUNT}",
        f"- Formally equivalent candidates: {EXPECTED_CANDIDATE_COUNT}/{EXPECTED_CANDIDATE_COUNT}",
        f"- Stronger-synthesis runs: {EXPECTED_RUN_COUNT}/{EXPECTED_RUN_COUNT}",
        f"- Standard-flow useful candidates: {standard_count}",
        f"- Stronger-recipe useful candidates: {len(stronger_useful)}",
        f"- Flow-robust useful candidates: {len(robust_useful)}",
        f"- Standard useful candidates retained as flow-robust: {_format_percent(retention)}",
        f"- Cases containing a flow-robust opportunity: {len(robust_cases)}/{EXPECTED_CASE_COUNT}",
        f"- Families with enough robust opportunity and no-change support: {len(supported_families)}/{EXPECTED_FAMILY_COUNT}",
        "",
        "A flow-robust candidate must meet the balanced usefulness rule in both synthesis recipes without a conflicting delay or area direction.",
        "",
        "## Candidate outcomes",
        "",
        "| Outcome | Candidates | Plain-language meaning |",
        "|---|---:|---|",
        f"| Robust useful | {classification_counts['robust_useful']} | Useful under both recipes with compatible delay and area direction. |",
        f"| Flow conflict | {classification_counts['flow_conflict']} | Useful under both recipes, but an important direction changes sign. |",
        f"| Removed by stronger synthesis | {classification_counts['absorbed_by_stronger_synthesis']} | Useful only under the standard recipe. |",
        f"| Stronger-recipe only | {classification_counts['stronger_recipe_only']} | Useful only after the stronger recipe changes the implementation. |",
        f"| Synthesis absorbed | {classification_counts['synthesis_absorbed']} | Effectively unchanged after stronger synthesis with the same mapped cell mix. |",
        f"| Not useful | {classification_counts['not_useful']} | Did not meet the balanced usefulness rule in either stable form. |",
        "",
        "## Results by RTL pattern",
        "",
        "| RTL pattern | Robust cases | Robust candidates | Standard retention | Training support |",
        "|---|---:|---:|---:|---|",
    ]
    for family, summary in families.items():
        lines.append(
            f"| {family} | {summary['robust_opportunity_case_count']}/"
            f"{summary['case_count']} | {summary['robust_useful_candidate_count']}/"
            f"{summary['candidate_count']} | "
            f"{_format_percent(summary['standard_candidate_retention_rate'])} | "
            f"{'ready' if summary['future_training_supported'] else 'more data needed'} |"
        )
    lines.extend(
        (
            "",
            "## Direction compatibility",
            "",
            f"- Delay: {_format_percent(direction_compatibility['delay'])}",
            f"- Area: {_format_percent(direction_compatibility['area'])}",
            f"- Cell count: {_format_percent(direction_compatibility['cell_count'])}",
            "",
            "## Next model decision",
            "",
            (
                "Families meeting the preregistered support floor: "
                + (", ".join(supported_families) if supported_families else "none")
                + "."
            ),
            "",
            "Only supported families should enter the next flow-robust eligibility model. Recipe-only and conflicting candidates remain target-dependent evidence, not general recommendations.",
            "",
            "This calibration sweep does not promote RTL Advisor. A new sealed blind evaluation, approved commercial-tool replication, and block-scale open-RTL testing remain required.",
            "",
            f"Plan hash: `{plan['plan_hash']}`",
            "",
            f"Training-table hash: `{training['payload']['training_table_hash']}`",
            "",
            f"Report hash: `{report['report_hash']}`",
            "",
        )
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report
