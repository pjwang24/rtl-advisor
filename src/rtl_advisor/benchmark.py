from __future__ import annotations

import json
from pathlib import Path
import random
import time
from typing import Any

from rtl_advisor.codex_analysis import CodexAnalysisError, analyze_with_codex
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import load_manifest
from rtl_advisor.graph import build_graph
from rtl_advisor.patch_validation import validate_candidate_patch
from rtl_advisor.rules import write_rule_analysis
from rtl_advisor.suite import load_suite_manifest


BENCHMARK_SCHEMA_VERSION = 1
BENCHMARK_FLOW_VERSION = "rtl-advisor-benchmark-v1"
BENCHMARK_SUITES = ("smoke", "pilot")
ARM_SPECS = {
    "rules": {"mode": "rules", "effort": None},
    "codex-xhigh": {"mode": "codex", "effort": "xhigh"},
    "codex-ultra": {"mode": "codex", "effort": "ultra"},
    "hybrid-xhigh": {"mode": "hybrid", "effort": "xhigh"},
    "hybrid-ultra": {"mode": "hybrid", "effort": "ultra"},
}
MODEL_ARMS = tuple(arm for arm in ARM_SPECS if arm != "rules")
FAMILY_TRANSFORMATIONS = {
    "arithmetic_resource_sharing": "share_arithmetic_by_muxing_inputs",
    "adder_reduction_association": "reassociate_arithmetic_tree",
    "mux_placement": "move_mux_across_operation",
    "priority_selection": "balance_priority_selection",
    "decode_factoring": "factor_repeated_decode",
    "comparator_selection": "factor_comparator_selection",
    "variable_shift": "bound_variable_shift",
    "width_signedness": "narrow_intermediate_width",
    "popcount_saturation": "restructure_popcount_or_saturation",
}
SMOKE_CASE_INDICES = (0, 16, 24, 28)
REPEAT_CASE_INDICES = (0, 4, 8, 12, 16, 20, 24, 28, 32, 1, 5, 9)


class BenchmarkError(RuntimeError):
    """Raised when a benchmark plan or stored artifact is invalid."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def planned_runs(
    suite: str,
    cases: list[dict[str, Any]],
    *,
    arm: str = "all",
) -> tuple[dict[str, Any], ...]:
    if suite not in BENCHMARK_SUITES:
        raise BenchmarkError(f"unsupported benchmark suite: {suite}")
    if arm != "all" and arm not in ARM_SPECS:
        raise BenchmarkError(f"unsupported benchmark arm: {arm}")
    selected_arms = tuple(ARM_SPECS) if arm == "all" else (arm,)
    selected_cases = (
        [cases[index] for index in SMOKE_CASE_INDICES]
        if suite == "smoke"
        else cases
    )
    runs = []
    for case in selected_cases:
        for selected_arm in selected_arms:
            runs.append(
                {
                    "case": case,
                    "arm": selected_arm,
                    "repeat_index": 0,
                }
            )
    if suite == "pilot":
        repeat_case_ids = {cases[index]["case_id"] for index in REPEAT_CASE_INDICES}
        for case in cases:
            if case["case_id"] not in repeat_case_ids:
                continue
            for repeat_index in (1, 2):
                for selected_arm in selected_arms:
                    if selected_arm == "rules":
                        continue
                    runs.append(
                        {
                            "case": case,
                            "arm": selected_arm,
                            "repeat_index": repeat_index,
                        }
                    )
    return tuple(runs)


def _direction(improvement_percent: float, threshold: float = 0.5) -> str:
    if improvement_percent > threshold:
        return "improve"
    if improvement_percent < -threshold:
        return "degrade"
    return "neutral"


def _candidate_classification(comparison: dict[str, Any]) -> str:
    delay = comparison["critical_delay_ps"]["improvement_percent"]
    area = comparison["area_total"]["improvement_percent"]
    beneficial = (delay >= 3.0 and area >= -10.0) or (
        area >= 5.0 and delay >= -2.0
    )
    if beneficial:
        return "beneficial"
    if delay >= -2.0 and area >= -10.0:
        return "neutral"
    return "harmful"


def _candidate_utility(comparison: dict[str, Any]) -> float:
    delay = comparison["critical_delay_ps"]["improvement_percent"]
    area = comparison["area_total"]["improvement_percent"]
    cells = comparison["cell_count"]["improvement_percent"]
    return round(delay + area + 0.1 * cells, 6)


def _top_finding(
    analysis: dict[str, Any],
    expected_transformation: str,
) -> dict[str, Any] | None:
    findings = analysis.get("findings") or []
    matching = [
        finding
        for finding in findings
        if finding.get("transformation_id") == expected_transformation
    ]
    if matching:
        return matching[0]
    return findings[0] if findings else None


def _recommends_action(finding: dict[str, Any] | None) -> bool:
    if finding is None:
        return False
    predicted = finding.get("predicted_effect") or {}
    return "improve" in predicted.values()


def score_analysis(
    family: str,
    analysis: dict[str, Any],
    synthesis_summary: dict[str, Any],
) -> dict[str, Any]:
    try:
        expected_transformation = FAMILY_TRANSFORMATIONS[family]
    except KeyError as exc:
        raise BenchmarkError(f"no benchmark transformation for family {family}") from exc
    comparisons = synthesis_summary.get("comparisons") or []
    if len(comparisons) != 3:
        raise BenchmarkError("ground truth must contain exactly three candidates")
    by_candidate = {
        comparison["candidate_id"]: comparison for comparison in comparisons
    }
    canonical = by_candidate.get("v1")
    if canonical is None:
        raise BenchmarkError("ground truth is missing canonical candidate v1")
    classifications = {
        candidate_id: _candidate_classification(comparison)
        for candidate_id, comparison in by_candidate.items()
    }
    utilities = {
        candidate_id: _candidate_utility(comparison)
        for candidate_id, comparison in by_candidate.items()
    }
    beneficial_available = "beneficial" in classifications.values()
    finding = _top_finding(analysis, expected_transformation)
    recommended_transformation = (
        finding.get("transformation_id") if finding is not None else None
    )
    transformation_match = recommended_transformation == expected_transformation
    recommends_action = _recommends_action(finding)
    actionable_correct = (
        recommends_action == beneficial_available
        and (not recommends_action or transformation_match)
    )
    observed_directions = {
        "delay": _direction(
            canonical["critical_delay_ps"]["improvement_percent"]
        ),
        "area": _direction(canonical["area_total"]["improvement_percent"]),
        "cell_count": _direction(
            canonical["cell_count"]["improvement_percent"]
        ),
    }
    predicted_directions = (
        finding.get("predicted_effect") if finding is not None else None
    )
    direction_correct = None
    direction_total = 0
    direction_matches = 0
    if transformation_match and isinstance(predicted_directions, dict):
        direction_total = 3
        direction_matches = sum(
            predicted_directions.get(metric) == direction
            for metric, direction in observed_directions.items()
        )
        direction_correct = direction_matches / direction_total
    selected_utility = (
        utilities["v1"] if recommends_action and transformation_match else 0.0
    )
    best_candidate_id, best_candidate_utility = max(
        utilities.items(),
        key=lambda item: (item[1], item[0]),
    )
    best_available_utility = max(0.0, best_candidate_utility)
    return {
        "expected_transformation_id": expected_transformation,
        "recommended_transformation_id": recommended_transformation,
        "transformation_match": transformation_match,
        "recommended_action": recommends_action,
        "beneficial_candidate_available": beneficial_available,
        "actionable_correct": actionable_correct,
        "candidate_classifications": classifications,
        "canonical_candidate_id": "v1",
        "canonical_observed_directions": observed_directions,
        "predicted_directions": predicted_directions,
        "direction_matches": direction_matches,
        "direction_total": direction_total,
        "direction_accuracy": direction_correct,
        "candidate_utilities": utilities,
        "selected_utility": selected_utility,
        "best_candidate_id": best_candidate_id,
        "best_available_utility": best_available_utility,
        "ranking_regret": round(
            max(0.0, best_available_utility - selected_utility),
            6,
        ),
    }


def _synthesis_summary(config: ProjectConfig, case_id: str) -> dict[str, Any]:
    path = config.artifacts_dir / "cases" / case_id / "synthesis/summary.json"
    if not path.is_file():
        raise BenchmarkError(f"synthesis ground truth missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"invalid synthesis ground truth {path}: {exc}") from exc


def _rules_analysis(
    config: ProjectConfig,
    manifest_path: Path,
    *,
    force: bool,
) -> tuple[dict[str, Any], Path, float]:
    started = time.monotonic()
    graph_build = build_graph(config, manifest_path, "v0", force=force)
    graph = graph_build.graph
    output_path = (
        config.artifacts_dir
        / "cases"
        / graph["case_id"]
        / "analysis/rules/v0.json"
    )
    result = write_rule_analysis(graph, output_path)
    return result, output_path, time.monotonic() - started


def run_benchmark(
    config: ProjectConfig,
    suite: str,
    *,
    arm: str = "all",
    force: bool = False,
) -> dict[str, Any]:
    heldout_path = config.corpus_dir / "heldout/suite.json"
    heldout = load_suite_manifest(heldout_path)
    runs = planned_runs(suite, heldout["cases"], arm=arm)
    benchmark_root = config.artifacts_dir / "benchmarks" / suite
    run_root = benchmark_root / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    plan_payload = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "flow_version": BENCHMARK_FLOW_VERSION,
        "suite": suite,
        "arm_filter": arm,
        "planned_run_count": len(runs),
        "planned_model_run_count": sum(run["arm"] != "rules" for run in runs),
        "runs": [
            {
                "case_id": run["case"]["case_id"],
                "case_index": run["case"]["index"],
                "arm": run["arm"],
                "repeat_index": run["repeat_index"],
            }
            for run in runs
        ],
    }
    _write_json(benchmark_root / "plan.json", plan_payload)

    completed = 0
    failed = 0
    cached = 0
    patch_cache: dict[str, dict[str, Any]] = {}
    for run in runs:
        case = run["case"]
        selected_arm = run["arm"]
        repeat_index = run["repeat_index"]
        run_key = f"{case['case_id']}__{selected_arm}__r{repeat_index}"
        record_path = run_root / f"{run_key}.json"
        if record_path.is_file() and not force:
            try:
                cached_record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_record = None
            if cached_record is not None:
                cached += 1
                if cached_record.get("status") == "passed":
                    completed += 1
                else:
                    failed += 1
                continue
        manifest_path = (
            heldout_path.parent / case["manifest"]
        ).resolve()
        manifest = load_manifest(manifest_path)
        record: dict[str, Any] = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "flow_version": BENCHMARK_FLOW_VERSION,
            "suite": suite,
            "run_key": run_key,
            "case_id": case["case_id"],
            "case_index": case["index"],
            "family": case["family"],
            "arm": selected_arm,
            "repeat_index": repeat_index,
            "status": "failed",
        }
        try:
            rules_result, rules_path, rules_latency = _rules_analysis(
                config,
                manifest_path,
                force=False,
            )
            if selected_arm == "rules":
                analysis = rules_result
                analysis_path = rules_path
                latency_seconds = rules_latency
                usage: dict[str, int] = {}
                analysis_cached = False
                attempts = 1
            else:
                spec = ARM_SPECS[selected_arm]
                last_error = None
                build = None
                attempts = 0
                for attempt in range(2):
                    attempts += 1
                    try:
                        build = analyze_with_codex(
                            config,
                            manifest,
                            "v0",
                            mode=spec["mode"],
                            effort=spec["effort"],
                            rules_analysis=(
                                rules_result if spec["mode"] == "hybrid" else None
                            ),
                            force=force,
                            run_id=f"benchmark_{selected_arm}_r{repeat_index}_a{attempt}",
                        )
                        break
                    except CodexAnalysisError as exc:
                        last_error = str(exc)
                if build is None:
                    raise BenchmarkError(
                        f"model arm failed after {attempts} attempt(s): {last_error}"
                    )
                analysis = build.result
                analysis_path = build.output_path
                provenance = analysis.get("provenance") or {}
                latency_seconds = float(provenance.get("latency_seconds", 0.0))
                usage = provenance.get("model_usage") or {}
                analysis_cached = build.cached
            ground_truth = _synthesis_summary(config, case["case_id"])
            score = score_analysis(case["family"], analysis, ground_truth)
            patch_result = None
            if score["recommended_action"] and score["transformation_match"]:
                patch_result = patch_cache.get(case["case_id"])
                if patch_result is None:
                    patch_result = validate_candidate_patch(
                        config,
                        manifest,
                        "v1",
                    )
                    patch_cache[case["case_id"]] = patch_result
            record.update(
                {
                    "status": "passed",
                    "analysis_path": str(analysis_path),
                    "analysis_hash": analysis.get("analysis_hash"),
                    "analysis_cached": analysis_cached,
                    "attempts": attempts,
                    "latency_seconds": round(latency_seconds, 6),
                    "model_usage": usage,
                    "findings": analysis.get("findings") or [],
                    "score": score,
                    "patch_validation": (
                        {
                            "status": patch_result["status"],
                            "stages": patch_result["stages"],
                            "result_path": patch_result["result_path"],
                        }
                        if patch_result is not None
                        else None
                    ),
                }
            )
            completed += 1
        except (BenchmarkError, RuntimeError, OSError) as exc:
            record["error"] = str(exc)
            failed += 1
        _write_json(record_path, record)

    result = {
        **plan_payload,
        "completed_run_count": completed,
        "failed_run_count": failed,
        "cached_run_count": cached,
        "status": "passed" if failed == 0 and completed == len(runs) else "failed",
        "runs_path": str(run_root),
    }
    result_path = benchmark_root / "run-summary.json"
    result["result_path"] = str(result_path)
    _write_json(result_path, result)
    return result


def _bootstrap_interval(values: list[float], *, seed: int) -> list[float] | None:
    if not values:
        return None
    generator = random.Random(seed)
    estimates = []
    for _ in range(2000):
        sample = [generator.choice(values) for _ in values]
        estimates.append(sum(sample) / len(sample))
    estimates.sort()
    return [
        round(estimates[int(0.025 * (len(estimates) - 1))], 6),
        round(estimates[int(0.975 * (len(estimates) - 1))], 6),
    ]


def _arm_summary(arm: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    passed = [record for record in records if record.get("status") == "passed"]
    evaluated = [
        record for record in passed if record.get("repeat_index", 0) == 0
    ]
    actionable = [
        1.0 if record["score"]["actionable_correct"] else 0.0
        for record in evaluated
    ]
    direction_matches = sum(
        record["score"]["direction_matches"] for record in evaluated
    )
    direction_total = sum(
        record["score"]["direction_total"] for record in evaluated
    )
    direction_case_count = sum(
        record["score"]["direction_total"] > 0 for record in evaluated
    )
    regrets = [record["score"]["ranking_regret"] for record in evaluated]
    patch_records = [
        record["patch_validation"]
        for record in evaluated
        if record.get("patch_validation") is not None
    ]
    usage: dict[str, int] = {}
    for record in passed:
        for key, value in (record.get("model_usage") or {}).items():
            usage[key] = usage.get(key, 0) + int(value)
    repeat_groups: dict[str, list[tuple[Any, Any]]] = {}
    for record in passed:
        repeat_groups.setdefault(record["case_id"], []).append(
            (
                record["score"]["recommended_action"],
                record["score"]["recommended_transformation_id"],
            )
        )
    repeated = [values for values in repeat_groups.values() if len(values) >= 2]
    agreement = (
        sum(len(set(values)) == 1 for values in repeated) / len(repeated)
        if repeated
        else None
    )
    return {
        "arm": arm,
        "run_count": len(records),
        "passed_count": len(passed),
        "failure_count": len(records) - len(passed),
        "evaluation_case_count": len(evaluated),
        "actionable_accuracy": (
            round(sum(actionable) / len(actionable), 6) if actionable else None
        ),
        "actionable_accuracy_ci95": _bootstrap_interval(
            actionable,
            seed=5601 + tuple(ARM_SPECS).index(arm),
        ),
        "direction_accuracy": (
            round(direction_matches / direction_total, 6)
            if direction_total
            else None
        ),
        "direction_matches": direction_matches,
        "direction_total": direction_total,
        "direction_case_count": direction_case_count,
        "direction_coverage": (
            round(direction_case_count / len(evaluated), 6)
            if evaluated
            else None
        ),
        "mean_ranking_regret": (
            round(sum(regrets) / len(regrets), 6) if regrets else None
        ),
        "patch_attempt_count": len(patch_records),
        "patch_lint_success_rate": (
            round(
                sum(item["stages"]["lint"]["ok"] for item in patch_records)
                / len(patch_records),
                6,
            )
            if patch_records
            else None
        ),
        "patch_equivalence_success_rate": (
            round(
                sum(
                    item["stages"]["equivalence"]["ok"]
                    for item in patch_records
                )
                / len(patch_records),
                6,
            )
            if patch_records
            else None
        ),
        "mean_latency_seconds": (
            round(
                sum(record.get("latency_seconds", 0.0) for record in passed)
                / len(passed),
                6,
            )
            if passed
            else None
        ),
        "model_usage": usage,
        "run_to_run_agreement": (
            round(agreement, 6) if agreement is not None else None
        ),
        "repeated_case_count": len(repeated),
    }


def _family_summaries(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    initial = [
        record
        for record in records
        if record.get("status") == "passed"
        and record.get("repeat_index", 0) == 0
    ]
    summaries: dict[str, dict[str, dict[str, Any]]] = {}
    for family in sorted({record["family"] for record in initial}):
        summaries[family] = {}
        for arm in ARM_SPECS:
            selected = [
                record
                for record in initial
                if record["family"] == family and record["arm"] == arm
            ]
            if not selected:
                continue
            correct = sum(
                bool(record["score"]["actionable_correct"])
                for record in selected
            )
            direction_matches = sum(
                record["score"]["direction_matches"] for record in selected
            )
            direction_total = sum(
                record["score"]["direction_total"] for record in selected
            )
            direction_cases = sum(
                record["score"]["direction_total"] > 0 for record in selected
            )
            regrets = [record["score"]["ranking_regret"] for record in selected]
            summaries[family][arm] = {
                "evaluation_case_count": len(selected),
                "actionable_correct_count": correct,
                "actionable_accuracy": round(correct / len(selected), 6),
                "direction_accuracy": (
                    round(direction_matches / direction_total, 6)
                    if direction_total
                    else None
                ),
                "direction_case_count": direction_cases,
                "direction_coverage": round(
                    direction_cases / len(selected),
                    6,
                ),
                "mean_ranking_regret": round(
                    sum(regrets) / len(regrets),
                    6,
                ),
            }
    return summaries


def _paired_action_difference(
    records: list[dict[str, Any]],
    first_arm: str,
    second_arm: str,
) -> dict[str, Any] | None:
    initial = {
        (record["case_id"], record["arm"]): record
        for record in records
        if record.get("status") == "passed" and record.get("repeat_index") == 0
    }
    differences = []
    for case_id, arm in list(initial):
        if arm != first_arm:
            continue
        first = initial[(case_id, first_arm)]
        second = initial.get((case_id, second_arm))
        if second is None:
            continue
        differences.append(
            float(first["score"]["actionable_correct"])
            - float(second["score"]["actionable_correct"])
        )
    if not differences:
        return None
    mean = sum(differences) / len(differences)
    return {
        "first_arm": first_arm,
        "second_arm": second_arm,
        "paired_case_count": len(differences),
        "accuracy_difference": round(mean, 6),
        "ci95": _bootstrap_interval(
            differences,
            seed=105601 + len(first_arm) + len(second_arm),
        ),
    }


def generate_benchmark_report(
    config: ProjectConfig,
    suite: str,
) -> dict[str, Any]:
    if suite not in BENCHMARK_SUITES:
        raise BenchmarkError(f"unsupported benchmark suite: {suite}")
    benchmark_root = config.artifacts_dir / "benchmarks" / suite
    record_paths = sorted((benchmark_root / "runs").glob("*.json"))
    if not record_paths:
        raise BenchmarkError(f"no stored benchmark runs found for {suite}")
    records = []
    for path in record_paths:
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkError(f"invalid benchmark record {path}: {exc}") from exc
    arm_summaries = {
        arm: _arm_summary(
            arm,
            [record for record in records if record.get("arm") == arm],
        )
        for arm in ARM_SPECS
        if any(record.get("arm") == arm for record in records)
    }
    family_summaries = _family_summaries(records)
    hybrid_vs_rules = _paired_action_difference(
        records,
        "hybrid-xhigh",
        "rules",
    )
    hybrid_vs_codex = _paired_action_difference(
        records,
        "hybrid-xhigh",
        "codex-xhigh",
    )
    materially_better = False
    if hybrid_vs_rules and hybrid_vs_codex:
        materially_better = (
            hybrid_vs_rules["accuracy_difference"] >= 0.08
            and hybrid_vs_codex["accuracy_difference"] >= 0.05
            and hybrid_vs_rules["ci95"][0] > 0
            and hybrid_vs_codex["ci95"][0] > 0
        )
    ultra_comparisons = {
        "codex": _paired_action_difference(
            records,
            "codex-ultra",
            "codex-xhigh",
        ),
        "hybrid": _paired_action_difference(
            records,
            "hybrid-ultra",
            "hybrid-xhigh",
        ),
    }
    ultra_worthwhile = any(
        comparison is not None and comparison["accuracy_difference"] >= 0.03
        for comparison in ultra_comparisons.values()
    )
    report = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "flow_version": BENCHMARK_FLOW_VERSION,
        "suite": suite,
        "record_count": len(records),
        "arm_summaries": arm_summaries,
        "family_summaries": family_summaries,
        "paired_comparisons": {
            "hybrid_xhigh_vs_rules": hybrid_vs_rules,
            "hybrid_xhigh_vs_codex_xhigh": hybrid_vs_codex,
        },
        "hybrid_xhigh_materially_better": materially_better,
        "ultra_comparisons": ultra_comparisons,
        "ultra_worthwhile": ultra_worthwhile,
        "decision_thresholds": {
            "beneficial": (
                "delay >= 3% with area regression <= 10%, or area >= 5% "
                "with delay regression <= 2%"
            ),
            "hybrid_vs_rules_minimum": 0.08,
            "hybrid_vs_codex_minimum": 0.05,
            "ultra_minimum": 0.03,
            "confidence": "95% deterministic bootstrap interval, 2000 resamples",
            "ranking_utility": "delay improvement + area improvement + 0.1 * cell improvement",
        },
        "source": "rebuilt solely from stored benchmark run records",
    }
    json_path = benchmark_root / "report.json"
    markdown_path = benchmark_root / "report.md"
    report["json_path"] = str(json_path)
    report["markdown_path"] = str(markdown_path)
    _write_json(json_path, report)
    lines = [
        f"# RTL Advisor {suite.title()} Benchmark",
        "",
        "## Primary evaluation",
        "",
        "Primary accuracy, direction, and regret metrics use one first-pass record per held-out case. Repeat records are reserved for stability and operational metrics.",
        "",
        "| Arm | Cases | Actionable accuracy (95% CI) | Direction accuracy | Direction coverage | Mean regret |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm, summary in arm_summaries.items():
        action = summary["actionable_accuracy"]
        action_ci = summary["actionable_accuracy_ci95"]
        direction = summary["direction_accuracy"]
        coverage = summary["direction_coverage"]
        regret = summary["mean_ranking_regret"]
        action_text = (
            f"{action} ({action_ci[0]}, {action_ci[1]})"
            if action is not None and action_ci is not None
            else "n/a"
        )
        lines.append(
            f"| {arm} | {summary['evaluation_case_count']} | {action_text} | "
            f"{direction if direction is not None else 'n/a'} | "
            f"{coverage if coverage is not None else 'n/a'} | "
            f"{regret if regret is not None else 'n/a'} |"
        )
    lines.extend(
        [
            "",
            "## Paired decisions",
            "",
            "| Comparison | Accuracy difference | 95% CI | Cases |",
            "|---|---:|---:|---:|",
        ]
    )
    comparisons_for_markdown = {
        "hybrid-xhigh vs rules": hybrid_vs_rules,
        "hybrid-xhigh vs codex-xhigh": hybrid_vs_codex,
        "codex-ultra vs codex-xhigh": ultra_comparisons["codex"],
        "hybrid-ultra vs hybrid-xhigh": ultra_comparisons["hybrid"],
    }
    for label, comparison in comparisons_for_markdown.items():
        if comparison is None:
            lines.append(f"| {label} | n/a | n/a | 0 |")
            continue
        ci = comparison["ci95"]
        lines.append(
            f"| {label} | {comparison['accuracy_difference']} | "
            f"({ci[0]}, {ci[1]}) | {comparison['paired_case_count']} |"
        )
    lines.extend(
        [
            "",
            f"Hybrid/xhigh materially better under the preregistered rule: **{materially_better}**",
            "",
            f"Ultra worthwhile under the preregistered rule: **{ultra_worthwhile}**",
            "",
            "## Operational and safety metrics",
            "",
            "| Arm | Passed runs | Patch attempts | Lint success | Equivalence success | Mean latency (s) | Agreement | Input tokens | Output tokens | Reasoning tokens |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for arm, summary in arm_summaries.items():
        usage = summary["model_usage"]
        lint = summary["patch_lint_success_rate"]
        equivalence = summary["patch_equivalence_success_rate"]
        agreement = summary["run_to_run_agreement"]
        lines.append(
            f"| {arm} | {summary['passed_count']}/{summary['run_count']} | "
            f"{summary['patch_attempt_count']} | "
            f"{lint if lint is not None else 'n/a'} | "
            f"{equivalence if equivalence is not None else 'n/a'} | "
            f"{summary['mean_latency_seconds']} | "
            f"{agreement if agreement is not None else 'n/a'} | "
            f"{usage.get('input_tokens', 0)} | {usage.get('output_tokens', 0)} | "
            f"{usage.get('reasoning_output_tokens', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Family breakdown",
            "",
            "| Family | Arm | Correct/cases | Actionable accuracy | Direction accuracy | Direction coverage | Mean regret |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for family, arms in family_summaries.items():
        for arm, summary in arms.items():
            direction = summary["direction_accuracy"]
            lines.append(
                f"| {family} | {arm} | "
                f"{summary['actionable_correct_count']}/{summary['evaluation_case_count']} | "
                f"{summary['actionable_accuracy']} | "
                f"{direction if direction is not None else 'n/a'} | "
                f"{summary['direction_coverage']} | "
                f"{summary['mean_ranking_regret']} |"
            )
    lines.extend(
        [
            "",
            "## Metric notes",
            "",
            "- Actionable accuracy scores whether the arm recommends the registered transformation exactly when at least one equivalent candidate passes the benefit guardrail.",
            "- Direction accuracy is conditional on identifying the registered transformation; direction coverage reports how often that condition holds.",
            "- Ranking regret maps a recommended transformation to its canonical v1 implementation and compares its utility with the best generated candidate or retaining the baseline.",
            "- Patch rates cover first-pass actionable patches and measure lint/formal safety, not whether synthesis PPA improved.",
            "",
            "This report was rebuilt solely from stored benchmark run records.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report
