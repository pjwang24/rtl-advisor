from __future__ import annotations

from collections import defaultdict
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
from rtl_advisor.advisor_v21 import write_case_analysis_v21
from rtl_advisor.benchmark_runner_v2 import adapt_v1_analysis
from rtl_advisor.benchmark_v21 import (
    METRICS,
    PROMOTION_TARGETS_V21,
    V21_ARMS,
    V21_MODEL_ARMS,
    V21_STABILITY_ARMS,
    aggregate_scores_v21,
    benchmark_run_plan,
    record_blind_unseal_v21,
    score_v21_analysis,
    stratified_paired_interval_v21,
    verify_benchmark_lock_v21,
)
from rtl_advisor.candidate_v2 import emit_selected_candidate
from rtl_advisor.codex_analysis import analyze_with_codex
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, load_manifest
from rtl_advisor.graph import build_graph
from rtl_advisor.rules import write_rule_analysis
from rtl_advisor.synthesis import synthesize_case


RUNNER_FLOW_VERSION_V21 = "rtl-advisor-benchmark-runner-v21"


class BenchmarkRunnerV21Error(RuntimeError):
    """Raised when the locked V2.1 blind execution cannot proceed safely."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkRunnerV21Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkRunnerV21Error(f"expected object for {description}: {path}")
    return payload


def _blind_suite(lock: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = Path(lock["blind_suite"]["path"])
    suite = _load_json(path, "V2.1 blind suite")
    if suite.get("suite_hash") != lock["blind_suite"]["suite_hash"]:
        raise BenchmarkRunnerV21Error("V2.1 blind suite does not match lock")
    return path, suite


def _archive_campaign(root: Path) -> Path | None:
    run_root = root / "runs"
    if not run_root.is_dir() or not any(run_root.glob("*.json")):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = root / "archives" / stamp
    archive.mkdir(parents=True, exist_ok=False)
    shutil.copytree(run_root, archive / "runs")
    for name in ("run-progress.json", "run-summary.json", "report.json", "report.md"):
        source = root / name
        if source.is_file():
            shutil.copy2(source, archive / name)
    return archive


def synthesize_locked_blind_suite_v21(
    config: ProjectConfig,
    lock_path: str | Path,
    *,
    workers: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    if workers < 1 or workers > 8:
        raise BenchmarkRunnerV21Error("blind synthesis workers must be 1..8")
    lock = verify_benchmark_lock_v21(lock_path, config)
    suite_path, suite = _blind_suite(lock)
    unseal = record_blind_unseal_v21(config, lock_path)
    root = config.artifacts_dir / "benchmarks/v21/blind-synthesis"
    case_root = root / "cases"
    progress_path = root / "progress.json"
    completed = []

    def worker(case: dict[str, Any]) -> dict[str, Any]:
        path = case_root / f"{case['case_id']}.json"
        if path.is_file() and not force:
            cached = _load_json(path, "V2.1 blind synthesis record")
            if cached.get("lock_hash") == lock["lock_hash"] and cached.get("status") == "passed":
                return {**cached, "cached": True}
        manifest = load_manifest(suite_path.parent / case["manifest"])
        try:
            _, summary = synthesize_case(config, manifest, variant_id="all", force=force)
            result = {
                "flow_version": RUNNER_FLOW_VERSION_V21,
                "lock_hash": lock["lock_hash"],
                "case_id": case["case_id"],
                "family": case["family"],
                "status": "passed",
                "cached": False,
                "comparison_count": len(summary.get("comparisons") or []),
            }
        except Exception as exc:
            result = {
                "flow_version": RUNNER_FLOW_VERSION_V21,
                "lock_hash": lock["lock_hash"],
                "case_id": case["case_id"],
                "family": case["family"],
                "status": "failed",
                "cached": False,
                "error": str(exc),
            }
        _write_json(path, result)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, case) for case in suite["cases"]]
        for future in as_completed(futures):
            completed.append(future.result())
            _write_json(
                progress_path,
                {
                    "flow_version": RUNNER_FLOW_VERSION_V21,
                    "lock_hash": lock["lock_hash"],
                    "case_count": 72,
                    "completed_count": len(completed),
                    "passed_count": sum(item["status"] == "passed" for item in completed),
                    "failed_count": sum(item["status"] != "passed" for item in completed),
                },
            )
    failures = [item for item in completed if item["status"] != "passed"]
    result = {
        "flow_version": RUNNER_FLOW_VERSION_V21,
        "lock_hash": lock["lock_hash"],
        "unseal": unseal,
        "status": "passed" if not failures else "failed",
        "case_count": 72,
        "passed_count": 72 - len(failures),
        "failed_count": len(failures),
        "failures": sorted(failures, key=lambda item: item["case_id"]),
    }
    result["summary_path"] = str(root / "summary.json")
    _write_json(root / "summary.json", result)
    return result


def _rules_analysis(
    config: ProjectConfig, manifest: CaseManifest
) -> tuple[dict[str, Any], Path]:
    graph = build_graph(config, manifest, manifest.baseline_id).graph
    path = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "analysis/rules"
        / f"{manifest.baseline_id}.json"
    )
    return write_rule_analysis(graph, path), path


def _synthesis_summary(config: ProjectConfig, case_id: str) -> dict[str, Any]:
    summary = _load_json(
        config.artifacts_dir / "cases" / case_id / "synthesis/summary.json",
        "V2.1 blind synthesis summary",
    )
    if summary.get("status") != "passed":
        raise BenchmarkRunnerV21Error(f"blind synthesis did not pass for {case_id}")
    return summary


def _run_one(
    config: ProjectConfig,
    lock: dict[str, Any],
    manifest: CaseManifest,
    family: str,
    arm: str,
    repeat_index: int,
    analysis_root: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    usage: dict[str, int] = {}
    explanation: dict[str, Any] | None = None
    emission: dict[str, Any] | None = None
    analysis_path: Path | None = None
    if arm == "v1_rules":
        raw, analysis_path = _rules_analysis(config, manifest)
        analysis = adapt_v1_analysis(raw, family)
    elif arm in {"v1_codex_xhigh", "v1_hybrid_xhigh"}:
        mode = "codex" if arm == "v1_codex_xhigh" else "hybrid"
        rules, _ = _rules_analysis(config, manifest)
        build = analyze_with_codex(
            config,
            manifest,
            manifest.baseline_id,
            mode=mode,
            effort="xhigh",
            rules_analysis=rules if mode == "hybrid" else None,
            force=False,
            run_id=f"v21_{lock['lock_hash'][:12]}_{arm}_r{repeat_index}",
        )
        raw = build.result
        analysis_path = build.output_path
        analysis = adapt_v1_analysis(raw, family)
        usage = (raw.get("provenance") or {}).get("model_usage") or {}
    elif arm in {
        "v21_random_forest_point",
        "v21_risk_gate",
        "v21_safe_advisor_xhigh",
    }:
        mode = {
            "v21_random_forest_point": "point",
            "v21_risk_gate": "risk",
            "v21_safe_advisor_xhigh": "safe",
        }[arm]
        output_dir = analysis_root / arm / manifest.case_id / f"r{repeat_index}"
        analysis, analysis_path = write_case_analysis_v21(
            config, manifest, output_dir, mode=mode
        )
        if arm == "v21_safe_advisor_xhigh":
            explanation_input = {
                **analysis,
                "gate": {
                    "status": "passed" if analysis["decision"] == "recommend" else "abstained",
                    "flow_version": analysis["flow_version"],
                    "ood": analysis.get("ood"),
                },
                "features": (
                    analysis["candidates"][0].get("features", {})
                    if analysis.get("candidates")
                    else {}
                ),
            }
            try:
                explanation = explain_gate_decision(
                    config,
                    explanation_input,
                    analysis_path,
                    allow_model_source=True,
                    force=False,
                )
                usage = explanation.get("usage") or {}
            except AdvisorExplanationError as exc:
                # Explanation is non-authoritative. Preserve the deterministic
                # decision and record failure without converting the run to a
                # recommendation failure.
                explanation = {"status": "failed", "reason": str(exc)}
            if repeat_index == 0:
                emission = emit_selected_candidate(
                    config,
                    analysis,
                    analysis_path,
                    candidate_source="templates",
                )
    else:
        raise BenchmarkRunnerV21Error(f"unsupported V2.1 arm: {arm}")
    score = score_v21_analysis(
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
        "candidate_emission": emission,
    }


def run_locked_v21_benchmark(
    config: ProjectConfig,
    lock_path: str | Path,
    *,
    synthesis_workers: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    lock = verify_benchmark_lock_v21(lock_path, config)
    suite_path, suite = _blind_suite(lock)
    root = config.artifacts_dir / "benchmarks/v21"
    archive = _archive_campaign(root) if force else None
    synthesis = synthesize_locked_blind_suite_v21(
        config, lock_path, workers=synthesis_workers, force=force
    )
    if synthesis["status"] != "passed":
        raise BenchmarkRunnerV21Error("blind synthesis failed before model calls")
    plan = benchmark_run_plan(suite)
    run_root = root / "runs"
    analysis_root = root / "analyses"
    run_root.mkdir(parents=True, exist_ok=True)
    passed = failed = cached = 0
    for index, run in enumerate(plan, 1):
        case, arm = run["case"], run["arm"]
        repeat_index = int(run["repeat_index"])
        run_key = f"{case['case_id']}__{arm}__r{repeat_index}"
        record_path = run_root / f"{run_key}.json"
        if record_path.is_file() and not force:
            record = _load_json(record_path, "V2.1 benchmark record")
            if record.get("lock_hash") == lock["lock_hash"]:
                cached += 1
                passed += int(record.get("status") == "passed")
                failed += int(record.get("status") != "passed")
                continue
        record: dict[str, Any] = {
            "flow_version": RUNNER_FLOW_VERSION_V21,
            "lock_hash": lock["lock_hash"],
            "run_key": run_key,
            "case_id": case["case_id"],
            "family": case["family"],
            "arm": arm,
            "repeat_index": repeat_index,
            "model_call": arm in V21_MODEL_ARMS,
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
        except Exception as exc:  # exactly one recorded attempt; no substitution
            record.update(error=str(exc), schema_valid=False)
            failed += 1
        _write_json(record_path, record)
        _write_json(
            root / "run-progress.json",
            {
                "flow_version": RUNNER_FLOW_VERSION_V21,
                "lock_hash": lock["lock_hash"],
                "run_count": 480,
                "completed_count": index,
                "passed_count": passed,
                "failed_count": failed,
                "cached_count": cached,
            },
        )
    result = {
        "flow_version": RUNNER_FLOW_VERSION_V21,
        "lock_hash": lock["lock_hash"],
        "status": "passed" if failed == 0 else "failed",
        "run_count": 480,
        "model_call_count": 264,
        "passed_count": passed,
        "failed_count": failed,
        "cached_count": cached,
        "campaign_status": synthesis["unseal"]["status"],
        "previous_campaign_archive": str(archive) if archive else None,
    }
    result["summary_path"] = str(root / "run-summary.json")
    _write_json(root / "run-summary.json", result)
    result["report"] = build_v21_benchmark_report(config, lock_path)
    _write_json(root / "run-summary.json", result)
    return result


def _actionable_correct(score: dict[str, Any]) -> bool:
    return bool(score["opportunity_covered"] or score["true_abstention"])


def _stability(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("arm") in V21_STABILITY_ARMS:
            groups[(record["arm"], record["case_id"])].append(record)
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"complete": 0, "deterministic": 0, "semantic": 0}
    )
    for (arm, _), group in groups.items():
        repeats = {int(item["repeat_index"]): item for item in group}
        if set(repeats) != {0, 1, 2} or any(item.get("status") != "passed" for item in repeats.values()):
            continue
        counts[arm]["complete"] += 1
        signatures = {
            (item.get("decision"), (item.get("score") or {}).get("selected_template_id"))
            for item in repeats.values()
        }
        counts[arm]["deterministic"] += int(len(signatures) == 1)
        semantic_signatures = set()
        for item in repeats.values():
            response = ((item.get("explanation") or {}).get("response") or {})
            semantic_signatures.add(
                (
                    item.get("decision"),
                    (item.get("score") or {}).get("selected_template_id"),
                    response.get("decision"),
                    response.get("transformation_id"),
                    json.dumps(response.get("predicted_directions"), sort_keys=True),
                )
            )
        counts[arm]["semantic"] += int(len(semantic_signatures) == 1)
    per_arm = {
        arm: {
            **values,
            "decision_and_candidate_stability": (
                values["deterministic"] / values["complete"]
                if values["complete"]
                else 0.0
            ),
            "semantic_stability": (
                values["semantic"] / values["complete"]
                if values["complete"]
                else 0.0
            ),
        }
        for arm, values in sorted(counts.items())
    }
    safe = per_arm.get(
        "v21_safe_advisor_xhigh",
        {"complete": 0, "deterministic": 0, "semantic": 0,
         "decision_and_candidate_stability": 0.0, "semantic_stability": 0.0},
    )
    return {
        "per_arm": per_arm,
        "complete_safe_advisor_groups": safe["complete"],
        "deterministic_stable_safe_advisor_groups": safe["deterministic"],
        "semantic_stable_safe_advisor_groups": safe["semantic"],
        "decision_and_candidate_stability": safe["decision_and_candidate_stability"],
        "advisor_semantic_stability": safe["semantic_stability"],
    }


def build_v21_benchmark_report(
    config: ProjectConfig, lock_path: str | Path
) -> dict[str, Any]:
    lock = verify_benchmark_lock_v21(lock_path)
    root = config.artifacts_dir / "benchmarks/v21"
    records = [
        _load_json(path, "V2.1 benchmark record")
        for path in sorted((root / "runs").glob("*.json"))
    ]
    records = [item for item in records if item.get("lock_hash") == lock["lock_hash"]]
    arm_summaries = {}
    for arm in V21_ARMS:
        base = [
            item
            for item in records
            if item.get("arm") == arm and item.get("repeat_index") == 0
        ]
        passed = [item for item in base if item.get("status") == "passed"]
        arm_summaries[arm] = {
            "expected_case_count": 72,
            "record_count": len(base),
            "passed_count": len(passed),
            "reliability": len(passed) / 72,
            "metrics": aggregate_scores_v21(passed) if passed else None,
        }
    hybrid = {
        item["case_id"]: item
        for item in records
        if item.get("arm") == "v1_hybrid_xhigh"
        and item.get("repeat_index") == 0
        and item.get("status") == "passed"
    }
    safe = {
        item["case_id"]: item
        for item in records
        if item.get("arm") == "v21_safe_advisor_xhigh"
        and item.get("repeat_index") == 0
        and item.get("status") == "passed"
    }
    differences: dict[str, list[float]] = defaultdict(list)
    for case_id in sorted(set(hybrid) & set(safe)):
        differences[safe[case_id]["family"]].append(
            100.0
            * (
                float(_actionable_correct(safe[case_id]["score"]))
                - float(_actionable_correct(hybrid[case_id]["score"]))
            )
        )
    paired_mean = paired_interval = None
    if len(differences) == 9 and all(differences.values()):
        paired_mean = sum(sum(values) / len(values) for values in differences.values()) / 9
        paired_interval = list(stratified_paired_interval_v21(differences))
    stability = _stability(records)
    safe_base = list(safe.values())
    explanations = [item.get("explanation") or {} for item in safe_base]
    recommended = [item for item in safe_base if item.get("decision") == "recommend"]
    emissions = [item.get("candidate_emission") or {} for item in recommended]
    accepted = sum(item.get("status") == "accepted" for item in emissions)
    safe_metrics = arm_summaries["v21_safe_advisor_xhigh"]["metrics"]
    physical = _load_json(
        Path(lock["artifacts"]["physical_report"]["path"]), "locked physical report"
    )
    checks: dict[str, bool] = {"complete_safe_metrics": safe_metrics is not None}
    if safe_metrics is not None:
        micro = safe_metrics["micro"]
        checks.update(
            {
                "micro_balanced_actionable_accuracy": micro["balanced_actionable_accuracy"] >= 0.70,
                "macro_balanced_actionable_accuracy": safe_metrics["macro_balanced_actionable_accuracy"] >= 0.65,
                "opportunity_coverage": micro["opportunity_coverage"] >= 0.50,
                "abstention_specificity": micro["abstention_specificity"] >= 0.90,
                "harmful_recommendation_rate": micro["harmful_recommendation_rate"] <= 0.10,
                "direction_accuracy": micro["direction"]["accuracy"] >= 0.70,
                "direction_coverage": micro["direction"]["coverage"] >= 0.90,
                "per_metric_direction_accuracy": all(
                    micro["direction"]["per_metric"][metric]["accuracy"] >= 0.60
                    for metric in METRICS
                ),
                "tie_aware_exact_best_accuracy": micro["tie_aware_exact_best_accuracy"] >= 0.60,
                "conditional_normalized_ranking_regret": micro["conditional_normalized_ranking_regret"] <= 0.10,
            }
        )
    checks.update(
        {
            "paired_improvement": bool(
                paired_mean is not None
                and paired_interval is not None
                and paired_mean >= 10.0
                and paired_interval[0] > 0.0
            ),
            "accepted_candidate_lint_formal": accepted == len(emissions),
            "candidate_emission_yield": accepted / len(recommended) >= 0.80 if recommended else True,
            "deterministic_stability": stability["decision_and_candidate_stability"] == 1.0,
            "advisor_schema_reliability": (
                sum(item.get("status") == "completed" for item in explanations)
                / len(explanations)
                >= 0.98
                if explanations
                else False
            ),
            "advisor_semantic_stability": stability["advisor_semantic_stability"] >= 0.85,
            "openroad_physical_evidence": bool(
                (physical.get("physical_evidence_gate") or {}).get("passed")
            ),
        }
    )
    core = {
        "schema_version": 21,
        "flow_version": RUNNER_FLOW_VERSION_V21,
        "lock_hash": lock["lock_hash"],
        "source": "rebuilt solely from stored locked V2.1 records",
        "record_count": len(records),
        "expected_record_count": 480,
        "model_call_record_count": sum(item.get("model_call") for item in records),
        "expected_model_call_count": 264,
        "arm_summaries": arm_summaries,
        "paired_safe_minus_v1_hybrid": {
            "mean_percentage_points": paired_mean,
            "confidence_interval_95": paired_interval,
            "bootstrap_samples": lock["bootstrap_samples"],
        },
        "stability": stability,
        "candidate_generation": {
            "recommended_count": len(recommended),
            "accepted_count": accepted,
            "yield": accepted / len(recommended) if recommended else 1.0,
        },
        "explanation": {
            "case_count": len(explanations),
            "completed_count": sum(item.get("status") == "completed" for item in explanations),
        },
        "promotion": {
            "passed": all(checks.values()),
            "checks": checks,
            "targets": PROMOTION_TARGETS_V21,
        },
    }
    report = {**core, "report_hash": hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()}
    report["json_path"] = str(root / "report.json")
    report["markdown_path"] = str(root / "report.md")
    _write_json(root / "report.json", report)
    lines = [
        "# RTL Advisor V2.1 Blind Benchmark",
        "",
        f"Promotion: **{'PASS' if report['promotion']['passed'] else 'FAIL'}**",
        "",
        f"- Stored records: {len(records)}/480",
        f"- Model-call records: {core['model_call_record_count']}/264",
        f"- Report hash: `{report['report_hash']}`",
        "",
    ]
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
