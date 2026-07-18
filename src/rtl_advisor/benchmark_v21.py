from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable

from rtl_advisor.advisor_explanation_v2 import (
    ADVISOR_EFFORT,
    ADVISOR_PROMPT_VERSION,
    ADVISOR_RESPONSE_CONTRACT_VERSION,
)
from rtl_advisor.advisor_v2 import PROFILES
from rtl_advisor.config import ProjectConfig
from rtl_advisor.features_v21 import FEATURE_SCHEMA_HASH_V21
from rtl_advisor.rules_v21 import RULESET_VERSION_V21
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command
from rtl_advisor.v21_corpus import V21_SUITE_SCHEMA_VERSION


BENCHMARK_SCHEMA_VERSION_V21 = 21
BENCHMARK_FLOW_VERSION_V21 = "rtl-advisor-benchmark-v21"
BENCHMARK_SEED_V21 = 20260715
BOOTSTRAP_SAMPLES_V21 = 10_000
TIE_EPSILON = 1e-6
METRICS = ("delay", "area", "cell_count")
V21_ARMS = (
    "v1_rules",
    "v1_codex_xhigh",
    "v1_hybrid_xhigh",
    "v21_random_forest_point",
    "v21_risk_gate",
    "v21_safe_advisor_xhigh",
)
V21_MODEL_ARMS = (
    "v1_codex_xhigh",
    "v1_hybrid_xhigh",
    "v21_safe_advisor_xhigh",
)
V21_STABILITY_ARMS = ("v1_hybrid_xhigh", "v21_safe_advisor_xhigh")
WEAK_V1_FAMILIES = {
    "arithmetic_resource_sharing",
    "mux_placement",
    "priority_selection",
}
PROMOTION_TARGETS_V21 = {
    "micro_balanced_actionable_accuracy": 0.70,
    "macro_balanced_actionable_accuracy": 0.65,
    "opportunity_coverage": 0.50,
    "abstention_specificity": 0.90,
    "harmful_recommendation_rate": 0.10,
    "paired_improvement_percentage_points": 10.0,
    "direction_accuracy": 0.70,
    "direction_coverage": 0.90,
    "per_metric_direction_accuracy": 0.60,
    "tie_aware_exact_best_accuracy": 0.60,
    "conditional_normalized_ranking_regret": 0.10,
}


class BenchmarkV21Error(RuntimeError):
    """Raised when the frozen V2.1 benchmark contract is violated."""


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        raise BenchmarkV21Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkV21Error(f"expected JSON object for {description}: {path}")
    return payload


def _profile_hash() -> str:
    return _json_hash(
        {
            name: {
                "delay_weight": profile.delay_weight,
                "area_weight": profile.area_weight,
                "cell_weight": profile.cell_weight,
            }
            for name, profile in PROFILES.items()
        }
    )


def _tool_version(config: ProjectConfig, command: tuple[str, ...]) -> str:
    try:
        result = run_command(command, timeout_seconds=config.tools.timeout_seconds)
    except ToolExecutionError as exc:
        return f"unavailable: {exc}"
    if result.returncode != 0:
        return f"unavailable: {result.stderr or result.stdout}"
    return first_output_line(result) or "unknown"


def stability_cases(blind_suite: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in blind_suite.get("cases") or []:
        by_family[str(case["family"])].append(case)
    selected = []
    for family, cases in sorted(by_family.items()):
        ordered = sorted(cases, key=lambda case: case["case_id"])
        if not ordered:
            raise BenchmarkV21Error(f"blind suite has no {family} cases")
        selected.append(ordered[0])
        if family in WEAK_V1_FAMILIES:
            if len(ordered) < 2:
                raise BenchmarkV21Error(f"stability suite needs two {family} cases")
            selected.append(ordered[1])
    if len(selected) != 12:
        raise BenchmarkV21Error(f"stability subset must contain 12 cases, got {len(selected)}")
    return tuple(selected)


def model_call_plan(blind_suite: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cases = sorted(
        blind_suite.get("cases") or [], key=lambda case: (case["family"], case["index"])
    )
    if len(cases) != 72:
        raise BenchmarkV21Error(f"V2.1 blind suite must contain 72 cases, got {len(cases)}")
    calls = [
        {
            "case_id": case["case_id"],
            "family": case["family"],
            "arm": arm,
            "repeat_index": 0,
            "model": "gpt-5.6-sol",
            "effort": ADVISOR_EFFORT,
        }
        for arm in V21_MODEL_ARMS
        for case in cases
    ]
    for arm in V21_STABILITY_ARMS:
        for case in stability_cases(blind_suite):
            for repeat_index in (1, 2):
                calls.append(
                    {
                        "case_id": case["case_id"],
                        "family": case["family"],
                        "arm": arm,
                        "repeat_index": repeat_index,
                        "model": "gpt-5.6-sol",
                        "effort": ADVISOR_EFFORT,
                    }
                )
    if len(calls) != 264:
        raise BenchmarkV21Error(f"model call plan must contain 264 calls, got {len(calls)}")
    return tuple(calls)


def benchmark_run_plan(blind_suite: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cases = sorted(
        blind_suite.get("cases") or [], key=lambda case: (case["family"], case["index"])
    )
    runs = [
        {"case": case, "arm": arm, "repeat_index": 0}
        for case in cases
        for arm in V21_ARMS
    ]
    for arm in V21_STABILITY_ARMS:
        for case in stability_cases(blind_suite):
            for repeat_index in (1, 2):
                runs.append({"case": case, "arm": arm, "repeat_index": repeat_index})
    if len(runs) != 480:
        raise BenchmarkV21Error(f"V2.1 run plan must contain 480 records, got {len(runs)}")
    return tuple(runs)


def create_benchmark_lock_v21(config: ProjectConfig) -> Path:
    root = config.artifacts_dir / "benchmarks/v21"
    calibration_v2 = config.corpus_dir / "calibration-v2/suite.json"
    calibration_v21 = config.corpus_dir / "calibration-v21/suite.json"
    blind_v21 = config.corpus_dir / "heldout-v21/suite.json"
    blind = _load_json(blind_v21, "V2.1 blind suite")
    if (
        blind.get("schema_version") != V21_SUITE_SCHEMA_VERSION
        or blind.get("split") != "heldout-v21"
        or blind.get("case_count") != 72
        or not blind.get("v2_disjoint")
    ):
        raise BenchmarkV21Error("V2.1 blind suite contract is not satisfied")
    model_root = config.artifacts_dir / "models/v21"
    artifacts = {
        "calibration_v2": calibration_v2,
        "calibration_v21": calibration_v21,
        "blind_v21": blind_v21,
        "model_bundle": model_root / "model-bundle.joblib",
        "model_metadata": model_root / "metadata.json",
        "model_summary": model_root / "summary.json",
        "policy": model_root / "policy.json",
        "ood": model_root / "ood.json",
        "calibration_rows": model_root / "calibration-rows.json",
        "physical_report": config.artifacts_dir / "openroad/v2/report.json",
        "calibration_validation": config.artifacts_dir
        / "validation/v21/calibration-v21/summary.json",
        "blind_validation": config.artifacts_dir
        / "validation/v21/heldout-v21/summary.json",
    }
    for name, path in artifacts.items():
        if not path.is_file():
            raise BenchmarkV21Error(f"lock dependency missing ({name}): {path}")
    physical = _load_json(artifacts["physical_report"], "physical report")
    if not (physical.get("physical_evidence_gate") or {}).get("passed"):
        raise BenchmarkV21Error("OpenROAD physical-evidence gate has not passed")
    model_metadata = _load_json(artifacts["model_metadata"], "V2.1 model metadata")
    model_summary = _load_json(artifacts["model_summary"], "V2.1 model summary")
    if not (
        model_summary.get("status") == "passed"
        and model_metadata.get("risk_policy_feasible") is True
        and model_metadata.get("direction_policy_feasible") is True
    ):
        raise BenchmarkV21Error(
            "V2.1 calibration policy gates have not passed; blind lock is forbidden"
        )
    calibration_validation = _load_json(
        artifacts["calibration_validation"], "V2.1 calibration validation"
    )
    blind_validation = _load_json(
        artifacts["blind_validation"], "V2.1 blind validation"
    )
    if not (
        calibration_validation.get("status") == "passed"
        and calibration_validation.get("passed_count") == 576
        and calibration_validation.get("synthesis_requested") is True
    ):
        raise BenchmarkV21Error("V2.1 calibration validation/synthesis is incomplete")
    if not (
        blind_validation.get("status") == "passed"
        and blind_validation.get("passed_count") == 72
        and blind_validation.get("synthesis_requested") is False
    ):
        raise BenchmarkV21Error("V2.1 blind lint/formal prevalidation is incomplete")
    calls = model_call_plan(blind)
    runs = benchmark_run_plan(blind)
    core = {
        "schema_version": BENCHMARK_SCHEMA_VERSION_V21,
        "flow_version": BENCHMARK_FLOW_VERSION_V21,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": BENCHMARK_SEED_V21,
        "artifacts": {
            name: {
                "path": str(path.resolve()),
                "file_sha256": _file_hash(path),
            }
            for name, path in artifacts.items()
        },
        "blind_suite": {
            "path": str(blind_v21.resolve()),
            "file_sha256": _file_hash(blind_v21),
            "suite_hash": blind["suite_hash"],
        },
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "ruleset_version": RULESET_VERSION_V21,
        "profile_hash": _profile_hash(),
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "response_contract_version": ADVISOR_RESPONSE_CONTRACT_VERSION,
        "arms": list(V21_ARMS),
        "promotion_targets": PROMOTION_TARGETS_V21,
        "bootstrap_samples": BOOTSTRAP_SAMPLES_V21,
        "run_count": len(runs),
        "run_plan_hash": _json_hash(runs),
        "call_count": len(calls),
        "call_plan_hash": _json_hash(calls),
        "tool_versions": {
            "yosys": _tool_version(config, (config.tools.yosys, "-V")),
            "verilator": _tool_version(config, (config.tools.verilator, "--version")),
            "codex": _tool_version(config, (config.tools.codex, "--version")),
        },
    }
    lock = {**core, "lock_hash": _json_hash(core)}
    lock_path = root / "benchmark-lock.json"
    _write_json(lock_path, lock)
    _write_json(
        root / "model-call-plan.json",
        {
            "schema_version": BENCHMARK_SCHEMA_VERSION_V21,
            "lock_hash": lock["lock_hash"],
            "call_count": len(calls),
            "calls": list(calls),
        },
    )
    _write_json(
        root / "run-plan.json",
        {
            "schema_version": BENCHMARK_SCHEMA_VERSION_V21,
            "lock_hash": lock["lock_hash"],
            "run_count": len(runs),
            "runs": list(runs),
        },
    )
    return lock_path


def verify_benchmark_lock_v21(
    path: str | Path, config: ProjectConfig | None = None
) -> dict[str, Any]:
    lock_path = Path(path).expanduser().resolve()
    lock = _load_json(lock_path, "V2.1 benchmark lock")
    core = {key: value for key, value in lock.items() if key != "lock_hash"}
    if lock.get("lock_hash") != _json_hash(core):
        raise BenchmarkV21Error("V2.1 benchmark lock content hash mismatch")
    runtime = {
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "ruleset_version": RULESET_VERSION_V21,
        "profile_hash": _profile_hash(),
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "response_contract_version": ADVISOR_RESPONSE_CONTRACT_VERSION,
    }
    for key, expected in runtime.items():
        if lock.get(key) != expected:
            raise BenchmarkV21Error(f"V2.1 runtime contract changed: {key}")
    for item in lock.get("artifacts", {}).values():
        dependency = Path(item["path"])
        if not dependency.is_file() or _file_hash(dependency) != item["file_sha256"]:
            raise BenchmarkV21Error(f"V2.1 lock dependency changed: {dependency}")
    if config is not None:
        versions = {
            "yosys": _tool_version(config, (config.tools.yosys, "-V")),
            "verilator": _tool_version(config, (config.tools.verilator, "--version")),
            "codex": _tool_version(config, (config.tools.codex, "--version")),
        }
        if versions != lock.get("tool_versions"):
            raise BenchmarkV21Error("V2.1 benchmark tool versions changed")
    return lock


def record_blind_unseal_v21(config: ProjectConfig, lock_path: str | Path) -> dict[str, Any]:
    lock = verify_benchmark_lock_v21(lock_path, config)
    path = config.artifacts_dir / "benchmarks/v21/unseal.json"
    if path.is_file():
        previous = _load_json(path, "V2.1 unseal record")
        if previous.get("lock_hash") != lock["lock_hash"]:
            raise BenchmarkV21Error("V2.1 unseal record belongs to another lock")
        return {**previous, "fresh": False, "status": "rerun"}
    result = {
        "schema_version": BENCHMARK_SCHEMA_VERSION_V21,
        "status": "fresh_blind_run",
        "fresh": True,
        "lock_hash": lock["lock_hash"],
        "unsealed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(path, result)
    return result


def _actual_metrics(comparison: dict[str, Any]) -> dict[str, float]:
    return {
        "delay": float(comparison["critical_delay_ps"]["improvement_percent"]),
        "area": float(comparison["area_total"]["improvement_percent"]),
        "cell_count": float(comparison["cell_count"]["improvement_percent"]),
    }


def _direction(value: float) -> str:
    if value > 1.0:
        return "improve"
    if value < -1.0:
        return "degrade"
    return "neutral"


def score_v21_analysis(
    analysis: dict[str, Any],
    synthesis_summary: dict[str, Any],
    *,
    profile_id: str = "balanced",
) -> dict[str, Any]:
    try:
        profile = PROFILES[profile_id]
    except KeyError as exc:
        raise BenchmarkV21Error(f"unknown profile: {profile_id}") from exc
    comparisons = {
        item["candidate_id"]: item for item in synthesis_summary.get("comparisons") or []
    }
    if set(comparisons) != {"v1", "v2", "v3"}:
        raise BenchmarkV21Error("ground truth must contain v1, v2, and v3")
    actual = {template: _actual_metrics(value) for template, value in comparisons.items()}
    eligible = {
        template: profile.eligible(values["delay"], values["area"])
        for template, values in actual.items()
    }
    utilities = {
        template: profile.utility(values["delay"], values["area"], values["cell_count"])
        for template, values in actual.items()
    }
    maximum = max(utilities.values())
    best_candidate_ids = sorted(
        template
        for template, utility in utilities.items()
        if abs(utility - maximum) <= TIE_EPSILON
    )
    eligible_templates = [template for template, value in eligible.items() if value]
    selected_id = analysis.get("selected_candidate_id")
    selected = next(
        (
            candidate
            for candidate in analysis.get("candidates") or []
            if candidate.get("candidate_id") == selected_id
        ),
        None,
    )
    selected_template = selected.get("template_id") if selected else None
    recommended = analysis.get("decision") == "recommend"
    selected_eligible = bool(selected_template and eligible.get(selected_template, False))
    covered = bool(recommended and selected_eligible)
    best_eligible_utility = max(
        (utilities[template] for template in eligible_templates), default=0.0
    )
    selected_utility = utilities.get(selected_template, 0.0)
    conditional_regret = (
        min(
            1.0,
            max(0.0, best_eligible_utility - selected_utility)
            / abs(best_eligible_utility),
        )
        if covered and best_eligible_utility > 0
        else 0.0
    )
    predicted = (selected or {}).get("predicted_improvement_percent") or {}
    direction_pairs = []
    if recommended and selected_template in actual:
        for metric in METRICS:
            prediction = predicted.get(metric)
            if not isinstance(prediction, dict):
                continue
            predicted_direction = prediction.get("direction")
            if predicted_direction is None and prediction.get("estimate") is not None:
                predicted_direction = _direction(float(prediction["estimate"]))
            if predicted_direction in {None, "uncertain"}:
                continue
            direction_pairs.append(
                {
                    "metric": metric,
                    "predicted": predicted_direction,
                    "observed": _direction(actual[selected_template][metric]),
                }
            )
    return {
        "profile": profile_id,
        "recommended": recommended,
        "selected_template_id": selected_template,
        "eligible_templates": eligible_templates,
        "opportunity": bool(eligible_templates),
        "opportunity_covered": covered,
        "true_abstention": bool(not eligible_templates and not recommended),
        "harmful_recommendation": bool(recommended and not selected_eligible),
        "utilities": {key: round(value, 9) for key, value in utilities.items()},
        "best_candidate_ids": best_candidate_ids,
        "tie_aware_exact_best": bool(covered and selected_template in best_candidate_ids),
        "conditional_normalized_ranking_regret": conditional_regret,
        "direction_pairs": direction_pairs,
    }


def _micro_metrics(scores: list[dict[str, Any]]) -> dict[str, Any]:
    opportunities = [score for score in scores if score["opportunity"]]
    negatives = [score for score in scores if not score["opportunity"]]
    recommendations = [score for score in scores if score["recommended"]]
    covered = [score for score in opportunities if score["opportunity_covered"]]
    true_abstentions = [score for score in negatives if score["true_abstention"]]
    opportunity_recall = len(covered) / len(opportunities) if opportunities else 0.0
    specificity = len(true_abstentions) / len(negatives) if negatives else 0.0
    direction_pairs = [pair for score in recommendations for pair in score["direction_pairs"]]
    per_metric = {}
    for metric in METRICS:
        pairs = [pair for pair in direction_pairs if pair["metric"] == metric]
        per_metric[metric] = {
            "pair_count": len(pairs),
            "accuracy": (
                sum(pair["predicted"] == pair["observed"] for pair in pairs) / len(pairs)
                if pairs
                else 0.0
            ),
        }
    return {
        "case_count": len(scores),
        "opportunity_count": len(opportunities),
        "recommendation_count": len(recommendations),
        "covered_opportunity_count": len(covered),
        "opportunity_coverage": opportunity_recall,
        "abstention_specificity": specificity,
        "balanced_actionable_accuracy": (opportunity_recall + specificity) / 2.0,
        "harmful_recommendation_rate": (
            sum(score["harmful_recommendation"] for score in recommendations)
            / len(recommendations)
            if recommendations
            else 0.0
        ),
        "tie_aware_exact_best_accuracy": (
            sum(score["tie_aware_exact_best"] for score in covered) / len(covered)
            if covered
            else 0.0
        ),
        "conditional_normalized_ranking_regret": (
            sum(score["conditional_normalized_ranking_regret"] for score in covered)
            / len(covered)
            if covered
            else 0.0
        ),
        "missed_opportunity_count": len(opportunities) - len(covered),
        "direction": {
            "possible_recommended_metric_slots": len(recommendations) * len(METRICS),
            "covered_metric_slots": len(direction_pairs),
            "coverage": (
                len(direction_pairs) / (len(recommendations) * len(METRICS))
                if recommendations
                else 0.0
            ),
            "accuracy": (
                sum(pair["predicted"] == pair["observed"] for pair in direction_pairs)
                / len(direction_pairs)
                if direction_pairs
                else 0.0
            ),
            "per_metric": per_metric,
        },
    }


def aggregate_scores_v21(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(records)
    if not values:
        raise BenchmarkV21Error("cannot aggregate empty V2.1 scores")
    scores = [record.get("score", record) for record in values]
    micro = _micro_metrics(scores)
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record, score in zip(values, scores, strict=True):
        family = record.get("family") or score.get("family")
        if family is None:
            raise BenchmarkV21Error("macro metrics require a family on every record")
        by_family[str(family)].append(score)
    family_metrics = {
        family: _micro_metrics(items) for family, items in sorted(by_family.items())
    }
    macro_balanced = sum(
        item["balanced_actionable_accuracy"] for item in family_metrics.values()
    ) / len(family_metrics)
    return {
        "micro": micro,
        "macro_balanced_actionable_accuracy": macro_balanced,
        "by_family": family_metrics,
    }


def stratified_paired_interval_v21(
    differences_by_family: dict[str, list[float]],
) -> tuple[float, float]:
    if not differences_by_family or any(not values for values in differences_by_family.values()):
        raise BenchmarkV21Error("paired bootstrap requires every family")
    rng = random.Random(BENCHMARK_SEED_V21)
    estimates = []
    for _ in range(BOOTSTRAP_SAMPLES_V21):
        family_means = []
        for family in sorted(differences_by_family):
            values = differences_by_family[family]
            sampled = [values[rng.randrange(len(values))] for _ in values]
            family_means.append(sum(sampled) / len(sampled))
        estimates.append(sum(family_means) / len(family_means))
    estimates.sort()
    lower = estimates[math.floor(0.025 * (len(estimates) - 1))]
    upper = estimates[math.ceil(0.975 * (len(estimates) - 1))]
    return round(lower, 6), round(upper, 6)
