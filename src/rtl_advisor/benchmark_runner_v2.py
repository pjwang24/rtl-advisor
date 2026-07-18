from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
from typing import Any

from rtl_advisor.advisor_explanation_v2 import (
    AdvisorExplanationError,
    explain_gate_decision,
)
from rtl_advisor.advisor_v2 import FEATURE_ORDER, PROFILES, analyze_live_rtl
from rtl_advisor.benchmark_v2 import (
    MODEL_ARMS,
    STABILITY_ARMS,
    V2_ARMS,
    aggregate_scores,
    record_blind_unseal,
    score_v2_analysis,
    stability_cases,
    stratified_paired_interval,
    verify_benchmark_lock,
)
from rtl_advisor.candidate_v2 import emit_selected_candidate
from rtl_advisor.codex_analysis import CodexAnalysisError, analyze_with_codex
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, load_manifest
from rtl_advisor.graph import build_graph
from rtl_advisor.rules import write_rule_analysis
from rtl_advisor.synthesis import synthesize_case


RUNNER_FLOW_VERSION = "rtl-advisor-benchmark-runner-v2"
DIRECTION_ESTIMATES = {"improve": 2.0, "neutral": 0.0, "degrade": -2.0}


class BenchmarkRunnerV2Error(RuntimeError):
    """Raised when the preregistered v2 blind run cannot be completed safely."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkRunnerV2Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkRunnerV2Error(f"invalid {description} {path}")
    return value


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive_existing_campaign(root: Path) -> Path | None:
    run_root = root / "runs"
    if not run_root.is_dir() or not any(run_root.glob("*.json")):
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root / "archives" / timestamp
    archive.mkdir(parents=True, exist_ok=False)
    shutil.copytree(run_root, archive / "runs")
    for name in (
        "run-plan.json",
        "run-progress.json",
        "run-summary.json",
        "report.json",
    ):
        source = root / name
        if source.is_file():
            shutil.copy2(source, archive / name)
    blind_synthesis = root / "blind-synthesis"
    if blind_synthesis.is_dir():
        shutil.copytree(blind_synthesis, archive / "blind-synthesis")
    return archive


def benchmark_run_plan(blind_suite: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cases = sorted(
        blind_suite.get("cases") or [],
        key=lambda case: (case["family"], case["index"]),
    )
    if len(cases) != 72:
        raise BenchmarkRunnerV2Error("v2 benchmark requires exactly 72 blind cases")
    runs = [
        {"case": case, "arm": arm, "repeat_index": 0}
        for case in cases
        for arm in V2_ARMS
    ]
    for arm in STABILITY_ARMS:
        for case in stability_cases(blind_suite):
            for repeat_index in (1, 2):
                runs.append(
                    {"case": case, "arm": arm, "repeat_index": repeat_index}
                )
    model_runs = sum(run["arm"] in MODEL_ARMS for run in runs)
    if len(runs) != 480 or model_runs != 264:
        raise BenchmarkRunnerV2Error(
            f"invalid v2 run plan: runs={len(runs)}, model_calls={model_runs}"
        )
    return tuple(runs)


def _blind_suite(lock: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = Path(lock["blind_suite"]["path"])
    suite = _load_json(path, "blind suite")
    if suite.get("suite_hash") != lock["blind_suite"]["suite_hash"]:
        raise BenchmarkRunnerV2Error("blind suite hash does not match the lock")
    return path, suite


def synthesize_locked_blind_suite(
    config: ProjectConfig,
    lock_path: str | Path,
    *,
    workers: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    if workers < 1 or workers > 8:
        raise BenchmarkRunnerV2Error("blind synthesis workers must be between 1 and 8")
    lock = verify_benchmark_lock(lock_path, config)
    suite_path, suite = _blind_suite(lock)
    validation_path = config.artifacts_dir / "validation/v2/heldout-v2/summary.json"
    validation = _load_json(validation_path, "held-out validation summary")
    if not (
        validation.get("status") == "passed"
        and validation.get("passed_count") == 72
        and validation.get("suite_hash") == suite["suite_hash"]
        and validation.get("synthesis_requested") is False
    ):
        raise BenchmarkRunnerV2Error(
            "held-out lint/formal validation must pass under the locked suite hash"
        )
    unseal = record_blind_unseal(config, lock_path)
    root = config.artifacts_dir / "benchmarks/v2/blind-synthesis"
    case_root = root / "cases"
    progress_path = root / "progress.json"
    completed: list[dict[str, Any]] = []

    def worker(case: dict[str, Any]) -> dict[str, Any]:
        record_path = case_root / f"{case['case_id']}.json"
        if record_path.is_file() and not force:
            cached = _load_json(record_path, "blind synthesis record")
            if (
                cached.get("lock_hash") == lock["lock_hash"]
                and cached.get("status") == "passed"
            ):
                return {**cached, "cached": True}
        manifest = load_manifest(suite_path.parent / case["manifest"])
        try:
            _, summary = synthesize_case(config, manifest, variant_id="all", force=force)
            result = {
                "flow_version": RUNNER_FLOW_VERSION,
                "lock_hash": lock["lock_hash"],
                "case_id": case["case_id"],
                "family": case["family"],
                "status": "passed",
                "cached": False,
                "summary_path": str(
                    config.artifacts_dir
                    / "cases"
                    / case["case_id"]
                    / "synthesis/summary.json"
                ),
                "comparison_count": len(summary.get("comparisons") or []),
            }
        except Exception as exc:
            result = {
                "flow_version": RUNNER_FLOW_VERSION,
                "lock_hash": lock["lock_hash"],
                "case_id": case["case_id"],
                "family": case["family"],
                "status": "failed",
                "cached": False,
                "error": str(exc),
            }
        _write_json(record_path, result)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = [executor.submit(worker, case) for case in suite["cases"]]
        for future in as_completed(pending):
            completed.append(future.result())
            _write_json(
                progress_path,
                {
                    "flow_version": RUNNER_FLOW_VERSION,
                    "lock_hash": lock["lock_hash"],
                    "case_count": 72,
                    "completed_count": len(completed),
                    "passed_count": sum(
                        result["status"] == "passed" for result in completed
                    ),
                    "failed_count": sum(
                        result["status"] != "passed" for result in completed
                    ),
                },
            )
    failures = [result for result in completed if result["status"] != "passed"]
    result = {
        "flow_version": RUNNER_FLOW_VERSION,
        "lock_hash": lock["lock_hash"],
        "unseal": unseal,
        "status": "passed" if not failures else "failed",
        "case_count": 72,
        "passed_count": 72 - len(failures),
        "failed_count": len(failures),
        "failures": sorted(failures, key=lambda item: item["case_id"]),
    }
    summary_path = root / "summary.json"
    result["summary_path"] = str(summary_path)
    _write_json(summary_path, result)
    return result


def _expected_transformation(family: str) -> str:
    from rtl_advisor.advisor_v2 import TRANSFORMATION_FAMILIES

    matches = [
        transformation
        for transformation, registered_family in TRANSFORMATION_FAMILIES.items()
        if registered_family == family
    ]
    if len(matches) != 1:
        raise BenchmarkRunnerV2Error(f"no unique transformation for {family}")
    return matches[0]


def adapt_v1_analysis(raw: dict[str, Any], family: str) -> dict[str, Any]:
    transformation = _expected_transformation(family)
    finding = next(
        (
            item
            for item in raw.get("findings") or []
            if item.get("transformation_id") == transformation
        ),
        None,
    )
    if finding is None:
        return {
            "decision": "abstain",
            "selected_candidate_id": None,
            "candidates": [],
            "raw_finding_count": len(raw.get("findings") or []),
        }
    predicted = finding.get("predicted_effect") or {}
    predictions = {
        metric: {"estimate": DIRECTION_ESTIMATES[direction]}
        for metric, direction in predicted.items()
        if direction in DIRECTION_ESTIMATES
    }
    recommend = any(direction == "improve" for direction in predicted.values())
    candidate = {
        "candidate_id": "v1-canonical-candidate",
        "template_id": "v1",
        "transformation_id": transformation,
        "predicted_improvement_percent": predictions,
    }
    return {
        "decision": "recommend" if recommend else "abstain",
        "selected_candidate_id": candidate["candidate_id"] if recommend else None,
        "candidates": [candidate],
        "raw_finding_count": len(raw.get("findings") or []),
    }


def _rules_analysis(
    config: ProjectConfig,
    manifest: CaseManifest,
) -> tuple[dict[str, Any], Path]:
    graph = build_graph(config, manifest, manifest.baseline_id, force=False).graph
    path = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "analysis/rules"
        / f"{manifest.baseline_id}.json"
    )
    return write_rule_analysis(graph, path), path


def _load_challenger(lock: dict[str, Any]) -> dict[str, Any]:
    try:
        import joblib
    except ImportError as exc:
        raise BenchmarkRunnerV2Error("joblib is required for the challenger") from exc
    artifact = lock["model_artifacts"]["challenger"]
    payload = joblib.load(artifact["path"])
    expected_order = [*FEATURE_ORDER, "family_code"]
    if payload.get("feature_order") != expected_order:
        raise BenchmarkRunnerV2Error("challenger feature ordering changed")
    if payload.get("training_suite_hash") != lock["calibration_suite"]["suite_hash"]:
        raise BenchmarkRunnerV2Error("challenger training suite changed")
    return payload


def challenger_analysis(
    calibrated_analysis: dict[str, Any],
    family: str,
    challenger: dict[str, Any],
    *,
    profile_id: str = "balanced",
) -> dict[str, Any]:
    profile = PROFILES[profile_id]
    family_code = challenger["family_codes"].get(family)
    if family_code is None:
        raise BenchmarkRunnerV2Error(f"challenger has no family code for {family}")
    candidates = json.loads(json.dumps(calibrated_analysis.get("candidates") or []))
    for candidate in candidates:
        features = candidate.get("features") or {}
        vector = [float(features[name]) for name in FEATURE_ORDER]
        prediction = challenger["estimator"].predict([[*vector, family_code]])[0]
        metrics = {
            metric: {
                "estimate": round(float(value), 6),
                "lower": round(float(value), 6),
                "upper": round(float(value), 6),
            }
            for metric, value in zip(
                ("delay", "area", "cell_count"), prediction, strict=True
            )
        }
        candidate["predicted_improvement_percent"] = metrics
        candidate["eligible"] = profile.eligible(
            metrics["delay"]["estimate"], metrics["area"]["estimate"]
        )
        candidate["challenger_utility"] = round(
            profile.utility(
                metrics["delay"]["estimate"],
                metrics["area"]["estimate"],
                metrics["cell_count"]["estimate"],
            ),
            6,
        )
        candidate["rejection_reasons"] = [] if candidate["eligible"] else [
            f"does not clear {profile_id} on point predictions"
        ]
    candidates.sort(
        key=lambda candidate: (
            not candidate["eligible"],
            -candidate.get("challenger_utility", float("-inf")),
            candidate["candidate_id"],
        )
    )
    for index, candidate in enumerate(candidates, start=1):
        candidate["rank"] = index
    selected = next((candidate for candidate in candidates if candidate["eligible"]), None)
    return {
        "decision": "recommend" if selected else "abstain",
        "selected_candidate_id": selected["candidate_id"] if selected else None,
        "candidates": candidates,
        "profile": profile_id,
        "challenger": True,
    }


def _synthesis_summary(config: ProjectConfig, case_id: str) -> dict[str, Any]:
    path = config.artifacts_dir / "cases" / case_id / "synthesis/summary.json"
    value = _load_json(path, "blind synthesis summary")
    if value.get("status") != "passed":
        raise BenchmarkRunnerV2Error(f"blind synthesis did not pass for {case_id}")
    return value


def _v2_analysis(
    config: ProjectConfig,
    manifest: CaseManifest,
    output_dir: Path,
) -> tuple[dict[str, Any], Path]:
    return analyze_live_rtl(
        config,
        top=manifest.baseline.kernel_top,
        files=(str(manifest.variant_path(manifest.baseline)),),
        profile_id="balanced",
        mode="calibrated",
        output_dir=output_dir,
        force=False,
    )


def _run_one(
    config: ProjectConfig,
    lock: dict[str, Any],
    manifest: CaseManifest,
    family: str,
    arm: str,
    repeat_index: int,
    analysis_root: Path,
    challenger: dict[str, Any],
) -> dict[str, Any]:
    started = time.monotonic()
    usage: dict[str, int] = {}
    explanation: dict[str, Any] | None = None
    candidate_emission: dict[str, Any] | None = None
    analysis_path: Path | None = None
    if arm == "v1_rules":
        raw, analysis_path = _rules_analysis(config, manifest)
        analysis = adapt_v1_analysis(raw, family)
    elif arm in {"v1_codex_xhigh", "v1_hybrid_xhigh"}:
        mode = "codex" if arm == "v1_codex_xhigh" else "hybrid"
        rules, _ = _rules_analysis(config, manifest)
        run_id = (
            f"v2_{lock['lock_hash'][:12]}_{arm}_r{repeat_index}"
        )
        build = analyze_with_codex(
            config,
            manifest,
            manifest.baseline_id,
            mode=mode,
            effort="xhigh",
            rules_analysis=rules if mode == "hybrid" else None,
            force=False,
            run_id=run_id,
        )
        raw = build.result
        analysis_path = build.output_path
        analysis = adapt_v1_analysis(raw, family)
        provenance = raw.get("provenance") or {}
        usage = provenance.get("model_usage") or {}
    elif arm in {
        "v2_calibrated_gate",
        "v2_safe_advisor_xhigh",
        "v2_random_forest_challenger",
    }:
        output_dir = (
            analysis_root / arm / manifest.case_id / f"r{repeat_index}"
        )
        calibrated, analysis_path = _v2_analysis(config, manifest, output_dir)
        if arm == "v2_random_forest_challenger":
            analysis = challenger_analysis(calibrated, family, challenger)
        else:
            analysis = calibrated
        if arm == "v2_safe_advisor_xhigh":
            explanation = explain_gate_decision(
                config,
                calibrated,
                analysis_path,
                allow_model_source=True,
                force=False,
            )
            if explanation.get("status") != "completed":
                raise AdvisorExplanationError(
                    explanation.get("reason", "advisor explanation did not complete")
                )
            usage = explanation.get("usage") or {}
            if repeat_index == 0:
                candidate_emission = emit_selected_candidate(
                    config,
                    calibrated,
                    analysis_path,
                    candidate_source="templates",
                )
    else:
        raise BenchmarkRunnerV2Error(f"unsupported v2 arm: {arm}")
    score = score_v2_analysis(
        analysis,
        _synthesis_summary(config, manifest.case_id),
    )
    return {
        "analysis": analysis,
        "analysis_path": str(analysis_path) if analysis_path else None,
        "score": score,
        "latency_seconds": round(time.monotonic() - started, 6),
        "model_usage": usage,
        "explanation": explanation,
        "candidate_emission": candidate_emission,
    }


def run_locked_v2_benchmark(
    config: ProjectConfig,
    lock_path: str | Path,
    *,
    synthesis_workers: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    lock = verify_benchmark_lock(lock_path, config)
    suite_path, suite = _blind_suite(lock)
    root = config.artifacts_dir / "benchmarks/v2"
    archive_path = _archive_existing_campaign(root) if force else None
    synthesis = synthesize_locked_blind_suite(
        config,
        lock_path,
        workers=synthesis_workers,
        force=force,
    )
    if synthesis["status"] != "passed":
        raise BenchmarkRunnerV2Error("blind synthesis failed; model calls were not started")
    plan = benchmark_run_plan(suite)
    challenger = _load_challenger(lock)
    run_root = root / "runs"
    analysis_root = root / "analyses"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "run-plan.json",
        {
            "flow_version": RUNNER_FLOW_VERSION,
            "lock_hash": lock["lock_hash"],
            "run_count": len(plan),
            "model_call_count": sum(run["arm"] in MODEL_ARMS for run in plan),
            "runs": [
                {
                    "case_id": run["case"]["case_id"],
                    "family": run["case"]["family"],
                    "arm": run["arm"],
                    "repeat_index": run["repeat_index"],
                }
                for run in plan
            ],
        },
    )
    passed = failed = cached = 0
    for index, run in enumerate(plan, start=1):
        case = run["case"]
        arm = run["arm"]
        repeat_index = int(run["repeat_index"])
        run_key = f"{case['case_id']}__{arm}__r{repeat_index}"
        record_path = run_root / f"{run_key}.json"
        if record_path.is_file() and not force:
            record = _load_json(record_path, "v2 benchmark record")
            if record.get("lock_hash") == lock["lock_hash"]:
                cached += 1
                passed += record.get("status") == "passed"
                failed += record.get("status") != "passed"
                continue
        record: dict[str, Any] = {
            "flow_version": RUNNER_FLOW_VERSION,
            "lock_hash": lock["lock_hash"],
            "run_key": run_key,
            "case_id": case["case_id"],
            "family": case["family"],
            "arm": arm,
            "repeat_index": repeat_index,
            "model_call": arm in MODEL_ARMS,
            "status": "failed",
        }
        try:
            manifest = load_manifest(suite_path.parent / case["manifest"])
            result = _run_one(
                config,
                lock,
                manifest,
                case["family"],
                arm,
                repeat_index,
                analysis_root,
                challenger,
            )
            analysis = result.pop("analysis")
            record.update(
                result,
                status="passed",
                decision=analysis.get("decision"),
                selected_candidate_id=analysis.get("selected_candidate_id"),
                candidates=analysis.get("candidates") or [],
                schema_valid=True,
            )
            passed += 1
        except Exception as exc:  # One attempt per preregistered run; preserve evidence.
            record["error"] = str(exc)
            record["schema_valid"] = False
            failed += 1
        _write_json(record_path, record)
        _write_json(
            root / "run-progress.json",
            {
                "flow_version": RUNNER_FLOW_VERSION,
                "lock_hash": lock["lock_hash"],
                "run_count": len(plan),
                "completed_count": index,
                "passed_count": passed,
                "failed_count": failed,
                "cached_count": cached,
            },
        )
    result = {
        "flow_version": RUNNER_FLOW_VERSION,
        "lock_hash": lock["lock_hash"],
        "status": "passed" if failed == 0 else "failed",
        "run_count": len(plan),
        "model_call_count": 264,
        "passed_count": passed,
        "failed_count": failed,
        "cached_count": cached,
        "campaign_status": synthesis["unseal"]["status"],
        "previous_campaign_archive": (
            str(archive_path) if archive_path is not None else None
        ),
        "runs_path": str(run_root),
    }
    summary_path = root / "run-summary.json"
    result["summary_path"] = str(summary_path)
    _write_json(summary_path, result)
    result["report"] = build_v2_benchmark_report(config, lock_path)
    _write_json(summary_path, result)
    return result


def _stability_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("arm") not in STABILITY_ARMS:
            continue
        grouped.setdefault((record["arm"], record["case_id"]), []).append(record)
    complete = 0
    stable = 0
    for group in grouped.values():
        by_repeat = {int(record["repeat_index"]): record for record in group}
        if set(by_repeat) != {0, 1, 2} or any(
            record.get("status") != "passed" for record in by_repeat.values()
        ):
            continue
        complete += 1
        signatures = {
            (
                record.get("decision"),
                (record.get("score") or {}).get("selected_template_id"),
            )
            for record in by_repeat.values()
        }
        stable += len(signatures) == 1
    return {
        "complete_case_arm_groups": complete,
        "stable_case_arm_groups": stable,
        "decision_and_candidate_stability": stable / complete if complete else 0.0,
    }


def build_v2_benchmark_report(
    config: ProjectConfig,
    lock_path: str | Path,
) -> dict[str, Any]:
    lock = verify_benchmark_lock(lock_path)
    run_root = config.artifacts_dir / "benchmarks/v2/runs"
    records = [
        _load_json(path, "v2 benchmark record")
        for path in sorted(run_root.glob("*.json"))
    ]
    records = [record for record in records if record.get("lock_hash") == lock["lock_hash"]]
    arm_summaries: dict[str, Any] = {}
    for arm in V2_ARMS:
        base = [
            record
            for record in records
            if record.get("arm") == arm and record.get("repeat_index") == 0
        ]
        passed = [record for record in base if record.get("status") == "passed"]
        summary: dict[str, Any] = {
            "expected_case_count": 72,
            "record_count": len(base),
            "passed_count": len(passed),
            "failure_count": len(base) - len(passed),
            "reliability": len(passed) / 72,
        }
        if passed:
            summary["micro"] = aggregate_scores(record["score"] for record in passed)
            by_family = {}
            for family in sorted({record["family"] for record in passed}):
                family_records = [record for record in passed if record["family"] == family]
                by_family[family] = aggregate_scores(
                    record["score"] for record in family_records
                )
            summary["per_family"] = by_family
            summary["macro_actionable_accuracy"] = sum(
                value["actionable_accuracy"] for value in by_family.values()
            ) / len(by_family)
        arm_summaries[arm] = summary

    paired: dict[str, list[float]] = {}
    hybrid = {
        record["case_id"]: record
        for record in records
        if record.get("arm") == "v1_hybrid_xhigh"
        and record.get("repeat_index") == 0
        and record.get("status") == "passed"
    }
    advisor = {
        record["case_id"]: record
        for record in records
        if record.get("arm") == "v2_safe_advisor_xhigh"
        and record.get("repeat_index") == 0
        and record.get("status") == "passed"
    }
    for case_id in sorted(set(hybrid) & set(advisor)):
        family = advisor[case_id]["family"]
        difference = 100.0 * (
            float(advisor[case_id]["score"]["actionable_correct"])
            - float(hybrid[case_id]["score"]["actionable_correct"])
        )
        paired.setdefault(family, []).append(difference)
    paired_interval = None
    paired_mean = None
    if len(paired) == 9 and all(paired.values()):
        paired_interval = list(stratified_paired_interval(paired))
        paired_mean = sum(
            sum(values) / len(values) for values in paired.values()
        ) / len(paired)

    model_records = [record for record in records if record.get("model_call")]
    usage: dict[str, int] = {}
    for record in model_records:
        for key, value in (record.get("model_usage") or {}).items():
            usage[key] = usage.get(key, 0) + int(value)
    advisor_base = [
        record
        for record in records
        if record.get("arm") == "v2_safe_advisor_xhigh"
        and record.get("repeat_index") == 0
        and record.get("status") == "passed"
    ]
    emissions = [
        record.get("candidate_emission")
        for record in advisor_base
        if record.get("decision") == "recommend"
    ]
    report = {
        "flow_version": RUNNER_FLOW_VERSION,
        "lock_hash": lock["lock_hash"],
        "source": "rebuilt solely from stored v2 benchmark run records",
        "record_count": len(records),
        "expected_record_count": 480,
        "model_call_record_count": len(model_records),
        "expected_model_call_count": 264,
        "model_usage": usage,
        "arm_summaries": arm_summaries,
        "paired_v2_advisor_minus_v1_hybrid": {
            "mean_percentage_points": paired_mean,
            "confidence_interval_95": paired_interval,
            "bootstrap_samples": lock["bootstrap_samples"],
        },
        "stability": _stability_summary(records),
        "candidate_generation": {
            "recommended_count": len(emissions),
            "accepted_count": sum(
                isinstance(emission, dict) and emission.get("status") == "accepted"
                for emission in emissions
            ),
        },
    }
    report_path = config.artifacts_dir / "benchmarks/v2/report.json"
    report["report_path"] = str(report_path)
    _write_json(report_path, report)
    return report
