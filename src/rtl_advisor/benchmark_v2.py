from __future__ import annotations

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
from rtl_advisor.advisor_v2 import FEATURE_SCHEMA_HASH, PROFILES
from rtl_advisor.config import ProjectConfig
from rtl_advisor.rules import RULESET_VERSION
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command
from rtl_advisor.v2_corpus import V2_SUITE_SCHEMA_VERSION


BENCHMARK_V2_SCHEMA_VERSION = 2
BENCHMARK_V2_FLOW_VERSION = "rtl-advisor-benchmark-v2"
BENCHMARK_SEED = 20260714
BOOTSTRAP_SAMPLES = 10_000
V2_ARMS = (
    "v1_rules",
    "v1_codex_xhigh",
    "v1_hybrid_xhigh",
    "v2_calibrated_gate",
    "v2_safe_advisor_xhigh",
    "v2_random_forest_challenger",
)
MODEL_ARMS = (
    "v1_codex_xhigh",
    "v1_hybrid_xhigh",
    "v2_safe_advisor_xhigh",
)
STABILITY_ARMS = ("v1_hybrid_xhigh", "v2_safe_advisor_xhigh")
WEAK_V1_FAMILIES = {
    "arithmetic_resource_sharing",
    "mux_placement",
    "priority_selection",
}
PRIMARY_TARGETS = {
    "actionable_accuracy": 0.65,
    "paired_improvement_percentage_points": 10.0,
    "harmful_recommendation_rate": 0.10,
    "opportunity_coverage": 0.50,
    "direction_accuracy": 0.70,
    "direction_coverage": 0.90,
    "exact_best_accuracy": 0.60,
    "normalized_ranking_regret": 0.25,
}


class BenchmarkV2Error(RuntimeError):
    """Raised when a v2 benchmark violates its frozen protocol."""


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _profile_hash() -> str:
    return _json_hash(
        {
            profile_id: {
                "delay_weight": profile.delay_weight,
                "area_weight": profile.area_weight,
                "cell_weight": profile.cell_weight,
            }
            for profile_id, profile in PROFILES.items()
        }
    )


def _load_suite(path: Path, expected_split: str) -> dict[str, Any]:
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkV2Error(f"invalid suite {path}: {exc}") from exc
    if not isinstance(suite, dict) or suite.get("schema_version") != V2_SUITE_SCHEMA_VERSION:
        raise BenchmarkV2Error(f"unsupported suite schema in {path}")
    if suite.get("split") != expected_split:
        raise BenchmarkV2Error(
            f"expected {expected_split} suite, got {suite.get('split')}"
        )
    return suite


def _tool_version(config: ProjectConfig, command: tuple[str, ...]) -> str:
    try:
        completed = run_command(command, timeout_seconds=config.tools.timeout_seconds)
    except ToolExecutionError as exc:
        return f"unavailable: {exc}"
    if completed.returncode != 0:
        return f"unavailable: {completed.stderr or completed.stdout}"
    return first_output_line(completed) or "unknown"


def stability_cases(blind_suite: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    by_family: dict[str, list[dict[str, Any]]] = {}
    for case in blind_suite.get("cases") or []:
        by_family.setdefault(str(case["family"]), []).append(case)
    selected = []
    for family, cases in sorted(by_family.items()):
        ordered = sorted(cases, key=lambda case: case["case_id"])
        if not ordered:
            raise BenchmarkV2Error(f"blind suite has no cases for {family}")
        selected.append(ordered[0])
        if family in WEAK_V1_FAMILIES:
            if len(ordered) < 2:
                raise BenchmarkV2Error(f"stability suite needs two {family} cases")
            selected.append(ordered[1])
    if len(selected) != 12:
        raise BenchmarkV2Error(f"stability subset must contain 12 cases, got {len(selected)}")
    return tuple(selected)


def model_call_plan(blind_suite: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cases = sorted(
        blind_suite.get("cases") or [],
        key=lambda case: (case["family"], case["index"]),
    )
    if len(cases) != 72:
        raise BenchmarkV2Error(f"blind suite must contain 72 cases, got {len(cases)}")
    calls = [
        {
            "case_id": case["case_id"],
            "family": case["family"],
            "arm": arm,
            "repeat_index": 0,
            "model": "gpt-5.6-sol",
            "effort": ADVISOR_EFFORT,
        }
        for arm in MODEL_ARMS
        for case in cases
    ]
    for arm in STABILITY_ARMS:
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
        raise BenchmarkV2Error(f"model call plan must contain 264 calls, got {len(calls)}")
    return tuple(calls)


def create_benchmark_lock(
    config: ProjectConfig,
    *,
    calibration_suite_path: str | Path | None = None,
    blind_suite_path: str | Path | None = None,
    gate_model_path: str | Path | None = None,
) -> Path:
    calibration_path = Path(
        calibration_suite_path
        or config.corpus_dir / "calibration-v2/suite.json"
    ).expanduser().resolve()
    blind_path = Path(
        blind_suite_path or config.corpus_dir / "heldout-v2/suite.json"
    ).expanduser().resolve()
    gate_path = Path(
        gate_model_path or config.artifacts_dir / "models/v2/gate.json"
    ).expanduser().resolve()
    model_root = gate_path.parent
    model_artifact_paths = {
        "gate": gate_path,
        "challenger": model_root / "challenger.joblib",
        "challenger_metadata": model_root / "challenger.json",
        "calibration_rows": model_root / "calibration-rows.json",
    }
    calibration = _load_suite(calibration_path, "calibration-v2")
    blind = _load_suite(blind_path, "heldout-v2")
    for name, artifact_path in model_artifact_paths.items():
        if not artifact_path.is_file():
            raise BenchmarkV2Error(f"{name} model artifact not found: {artifact_path}")
    calls = model_call_plan(blind)
    core = {
        "schema_version": BENCHMARK_V2_SCHEMA_VERSION,
        "flow_version": BENCHMARK_V2_FLOW_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": BENCHMARK_SEED,
        "calibration_suite": {
            "path": str(calibration_path),
            "file_sha256": _file_hash(calibration_path),
            "suite_hash": calibration["suite_hash"],
        },
        "blind_suite": {
            "path": str(blind_path),
            "file_sha256": _file_hash(blind_path),
            "suite_hash": blind["suite_hash"],
        },
        "gate_model": {
            "path": str(gate_path),
            "file_sha256": _file_hash(gate_path),
        },
        "model_artifacts": {
            name: {
                "path": str(artifact_path),
                "file_sha256": _file_hash(artifact_path),
            }
            for name, artifact_path in model_artifact_paths.items()
        },
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "ruleset_version": RULESET_VERSION,
        "profile_hash": _profile_hash(),
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "response_contract_version": ADVISOR_RESPONSE_CONTRACT_VERSION,
        "arms": list(V2_ARMS),
        "primary_profile": "balanced",
        "primary_targets": PRIMARY_TARGETS,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "call_count": len(calls),
        "call_plan_hash": _json_hash(calls),
        "tool_versions": {
            "yosys": _tool_version(config, (config.tools.yosys, "-V")),
            "verilator": _tool_version(config, (config.tools.verilator, "--version")),
            "codex": _tool_version(config, (config.tools.codex, "--version")),
        },
    }
    lock = {**core, "lock_hash": _json_hash(core)}
    root = config.artifacts_dir / "benchmarks/v2"
    lock_path = root / "benchmark-lock.json"
    call_path = root / "model-call-plan.json"
    _write_json(lock_path, lock)
    _write_json(
        call_path,
        {
            "schema_version": BENCHMARK_V2_SCHEMA_VERSION,
            "lock_hash": lock["lock_hash"],
            "call_count": len(calls),
            "calls": list(calls),
        },
    )
    return lock_path


def verify_benchmark_lock(
    path: str | Path,
    config: ProjectConfig | None = None,
) -> dict[str, Any]:
    lock_path = Path(path).expanduser().resolve()
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkV2Error(f"invalid benchmark lock {lock_path}: {exc}") from exc
    expected = lock.get("lock_hash")
    core = {key: value for key, value in lock.items() if key != "lock_hash"}
    if expected != _json_hash(core):
        raise BenchmarkV2Error("benchmark lock content hash mismatch")
    runtime_contract = {
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "profile_hash": _profile_hash(),
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "response_contract_version": ADVISOR_RESPONSE_CONTRACT_VERSION,
        "ruleset_version": RULESET_VERSION,
    }
    for key, value in runtime_contract.items():
        if lock.get(key) != value:
            raise BenchmarkV2Error(f"benchmark runtime contract changed: {key}")
    for key in ("calibration_suite", "blind_suite", "gate_model"):
        item = lock[key]
        path = Path(item["path"])
        if not path.is_file() or _file_hash(path) != item["file_sha256"]:
            raise BenchmarkV2Error(f"benchmark lock dependency changed: {path}")
    for item in (lock.get("model_artifacts") or {}).values():
        path = Path(item["path"])
        if not path.is_file() or _file_hash(path) != item["file_sha256"]:
            raise BenchmarkV2Error(f"benchmark lock dependency changed: {path}")
    if config is not None:
        current_versions = {
            "yosys": _tool_version(config, (config.tools.yosys, "-V")),
            "verilator": _tool_version(config, (config.tools.verilator, "--version")),
            "codex": _tool_version(config, (config.tools.codex, "--version")),
        }
        if lock.get("tool_versions") != current_versions:
            raise BenchmarkV2Error("benchmark tool versions changed")
    return lock


def record_blind_unseal(config: ProjectConfig, lock_path: str | Path) -> dict[str, Any]:
    lock = verify_benchmark_lock(lock_path, config)
    path = config.artifacts_dir / "benchmarks/v2/unseal.json"
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        return {**previous, "fresh": False, "status": "rerun"}
    result = {
        "schema_version": BENCHMARK_V2_SCHEMA_VERSION,
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


def score_v2_analysis(
    analysis: dict[str, Any],
    synthesis_summary: dict[str, Any],
    *,
    profile_id: str = "balanced",
) -> dict[str, Any]:
    try:
        profile = PROFILES[profile_id]
    except KeyError as exc:
        raise BenchmarkV2Error(f"unknown profile: {profile_id}") from exc
    comparisons = {
        comparison["candidate_id"]: comparison
        for comparison in synthesis_summary.get("comparisons") or []
    }
    if set(comparisons) != {"v1", "v2", "v3"}:
        raise BenchmarkV2Error("ground truth must contain v1, v2, and v3")
    actual = {template: _actual_metrics(value) for template, value in comparisons.items()}
    eligible = {
        template: profile.eligible(metrics["delay"], metrics["area"])
        for template, metrics in actual.items()
    }
    utilities = {
        template: profile.utility(
            metrics["delay"], metrics["area"], metrics["cell_count"]
        )
        for template, metrics in actual.items()
    }
    best_template = max(utilities, key=lambda template: (utilities[template], template))
    opportunities = [template for template, value in eligible.items() if value]
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
    actionable_correct = (
        recommended and selected_eligible
    ) or (not recommended and not opportunities)
    harmful = recommended and not selected_eligible
    best_eligible = max(
        (utilities[template] for template in opportunities),
        default=0.0,
    )
    selected_utility = utilities.get(selected_template, 0.0)
    regret = max(0.0, best_eligible - selected_utility)
    normalized_regret = (
        0.0 if best_eligible <= 0 else min(1.0, regret / abs(best_eligible))
    )
    predicted = (
        selected.get("predicted_improvement_percent") if selected else None
    ) or {}
    direction_pairs = []
    if selected_template in actual:
        for metric in ("delay", "area", "cell_count"):
            prediction = predicted.get(metric)
            if not isinstance(prediction, dict):
                continue
            direction_pairs.append(
                {
                    "metric": metric,
                    "predicted": _direction(float(prediction["estimate"])),
                    "observed": _direction(actual[selected_template][metric]),
                }
            )
    return {
        "profile": profile_id,
        "recommended": recommended,
        "selected_template_id": selected_template,
        "eligible_templates": opportunities,
        "actionable_correct": actionable_correct,
        "harmful_recommendation": harmful,
        "opportunity": bool(opportunities),
        "opportunity_covered": bool(recommended and selected_eligible),
        "best_template_id": best_template,
        "exact_best": bool(recommended and selected_template == best_template),
        "utilities": {key: round(value, 6) for key, value in utilities.items()},
        "normalized_ranking_regret": round(normalized_regret, 6),
        "direction_pairs": direction_pairs,
    }


def aggregate_scores(scores: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(scores)
    if not rows:
        raise BenchmarkV2Error("cannot aggregate an empty score set")
    recommendations = [row for row in rows if row["recommended"]]
    opportunities = [row for row in rows if row["opportunity"]]
    direction_pairs = [pair for row in rows for pair in row["direction_pairs"]]
    return {
        "case_count": len(rows),
        "actionable_accuracy": sum(row["actionable_correct"] for row in rows) / len(rows),
        "harmful_recommendation_rate": (
            sum(row["harmful_recommendation"] for row in recommendations)
            / len(recommendations)
            if recommendations
            else 0.0
        ),
        "opportunity_coverage": (
            sum(row["opportunity_covered"] for row in opportunities)
            / len(opportunities)
            if opportunities
            else 0.0
        ),
        "exact_best_accuracy": (
            sum(row["exact_best"] for row in recommendations) / len(recommendations)
            if recommendations
            else 0.0
        ),
        "normalized_ranking_regret": sum(
            row["normalized_ranking_regret"] for row in rows
        )
        / len(rows),
        "direction_coverage": len(direction_pairs) / (len(rows) * 3),
        "direction_accuracy": (
            sum(pair["predicted"] == pair["observed"] for pair in direction_pairs)
            / len(direction_pairs)
            if direction_pairs
            else 0.0
        ),
    }


def stratified_paired_interval(
    differences_by_family: dict[str, list[float]],
) -> tuple[float, float]:
    if not differences_by_family or any(not values for values in differences_by_family.values()):
        raise BenchmarkV2Error("paired bootstrap requires every family")
    rng = random.Random(BENCHMARK_SEED)
    estimates = []
    families = sorted(differences_by_family)
    for _ in range(BOOTSTRAP_SAMPLES):
        family_means = []
        for family in families:
            values = differences_by_family[family]
            sampled = [values[rng.randrange(len(values))] for _ in values]
            family_means.append(sum(sampled) / len(sampled))
        estimates.append(sum(family_means) / len(family_means))
    estimates.sort()
    lower = estimates[math.floor(0.025 * (len(estimates) - 1))]
    upper = estimates[math.ceil(0.975 * (len(estimates) - 1))]
    return round(lower, 6), round(upper, 6)
