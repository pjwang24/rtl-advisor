from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from rtl_advisor.advisor_explanation_v2 import (
    ADVISOR_EFFORT,
    ADVISOR_PROMPT_VERSION,
    ADVISOR_RESPONSE_CONTRACT_VERSION,
)
from rtl_advisor.advisor_v2 import PROFILES
from rtl_advisor.calibration_v22 import verify_frozen_inputs_v22
from rtl_advisor.config import ProjectConfig
from rtl_advisor.features_v21 import FEATURE_SCHEMA_HASH_V21
from rtl_advisor.rules_v21 import RULESET_VERSION_V21
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


BENCHMARK_SCHEMA_VERSION_V22 = 22
BENCHMARK_FLOW_VERSION_V22 = "rtl-advisor-benchmark-v22"
BENCHMARK_SEED_V22 = 20260716
V22_ARMS = (
    "v1_rules",
    "v1_codex_xhigh",
    "v1_hybrid_xhigh",
    "v22_random_forest_point",
    "v22_family_risk_gate",
    "v22_safe_advisor_xhigh",
)
V22_MODEL_ARMS = (
    "v1_codex_xhigh",
    "v1_hybrid_xhigh",
    "v22_safe_advisor_xhigh",
)
V22_STABILITY_ARMS = ("v1_hybrid_xhigh", "v22_safe_advisor_xhigh")
WEAK_V1_FAMILIES = {
    "arithmetic_resource_sharing",
    "mux_placement",
    "priority_selection",
}
PROMOTION_TARGETS_V22 = {
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


class BenchmarkV22Error(RuntimeError):
    """Raised when the frozen V2.2 pre-blind contract is violated."""


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkV22Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkV22Error(f"expected object in {description} {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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


def assert_preblind_ready_v22(config: ProjectConfig) -> dict[str, Any]:
    root = config.artifacts_dir / "models/v22"
    required = {
        "summary": root / "summary.json",
        "metadata": root / "metadata.json",
        "policy": root / "policy.json",
        "input_lock": root / "input-lock.json",
        "calibration_report": root / "calibration-report.json",
        "family_bundle": root / "family-model-bundle.joblib",
        "family_oof": root / "family-grouped-oof.json",
        "family_frontier": root / "family-threshold-frontier.json",
    }
    for name, path in required.items():
        if not path.is_file():
            raise BenchmarkV22Error(f"V2.2 pre-blind dependency missing ({name}): {path}")
    summary = _load_json(required["summary"], "V2.2 summary")
    metadata = _load_json(required["metadata"], "V2.2 metadata")
    policy = _load_json(required["policy"], "V2.2 policy")
    report = _load_json(required["calibration_report"], "V2.2 calibration report")
    if not (
        summary.get("status") == "passed"
        and summary.get("risk_policy_feasible") is True
        and summary.get("direction_policy_feasible") is True
        and summary.get("physical_evidence_feasible") is True
        and metadata.get("risk_policy_feasible") is True
        and policy.get("feasible") is True
        and report.get("status") == "passed"
    ):
        raise BenchmarkV22Error(
            "V2.2 calibration policy gates have not passed; blind lock is forbidden"
        )
    if metadata.get("metadata_hash") != _json_hash(
        {key: value for key, value in metadata.items() if key != "metadata_hash"}
    ):
        raise BenchmarkV22Error("V2.2 metadata content hash mismatch")
    if policy.get("policy_hash") != _json_hash(
        {key: value for key, value in policy.items() if key != "policy_hash"}
    ):
        raise BenchmarkV22Error("V2.2 policy content hash mismatch")
    try:
        input_lock = verify_frozen_inputs_v22(config)
    except Exception as exc:
        raise BenchmarkV22Error(str(exc)) from exc
    if input_lock["input_lock_hash"] != summary.get("input_lock_hash"):
        raise BenchmarkV22Error("V2.2 input lock changed after calibration")
    return {
        "summary": summary,
        "metadata": metadata,
        "policy": policy,
        "report": report,
        "input_lock": input_lock,
        "artifacts": required,
    }


def _stability_cases(blind: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in blind.get("cases") or []:
        by_family[str(case["family"])].append(case)
    selected = []
    for family, cases in sorted(by_family.items()):
        ordered = sorted(cases, key=lambda case: case["case_id"])
        if not ordered:
            raise BenchmarkV22Error(f"V2.2 blind suite has no {family} cases")
        selected.append(ordered[0])
        if family in WEAK_V1_FAMILIES:
            if len(ordered) < 2:
                raise BenchmarkV22Error(f"V2.2 stability suite needs two {family} cases")
            selected.append(ordered[1])
    if len(selected) != 12:
        raise BenchmarkV22Error(
            f"V2.2 stability subset must contain 12 cases, got {len(selected)}"
        )
    return tuple(selected)


def model_call_plan_v22(blind: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cases = sorted(
        blind.get("cases") or [], key=lambda case: (case["family"], case["index"])
    )
    if len(cases) != 72:
        raise BenchmarkV22Error(f"V2.2 blind suite must contain 72 cases, got {len(cases)}")
    calls = [
        {
            "case_id": case["case_id"],
            "family": case["family"],
            "arm": arm,
            "repeat_index": 0,
            "model": "gpt-5.6-sol",
            "effort": ADVISOR_EFFORT,
        }
        for arm in V22_MODEL_ARMS
        for case in cases
    ]
    for arm in V22_STABILITY_ARMS:
        for case in _stability_cases(blind):
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
        raise BenchmarkV22Error(f"V2.2 call plan must contain 264 calls, got {len(calls)}")
    return tuple(calls)


def benchmark_run_plan_v22(blind: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    cases = sorted(
        blind.get("cases") or [], key=lambda case: (case["family"], case["index"])
    )
    runs = [
        {"case": case, "arm": arm, "repeat_index": 0}
        for case in cases
        for arm in V22_ARMS
    ]
    for arm in V22_STABILITY_ARMS:
        for case in _stability_cases(blind):
            for repeat_index in (1, 2):
                runs.append({"case": case, "arm": arm, "repeat_index": repeat_index})
    if len(runs) != 480:
        raise BenchmarkV22Error(f"V2.2 run plan must contain 480 records, got {len(runs)}")
    return tuple(runs)


def create_benchmark_lock_v22(config: ProjectConfig) -> Path:
    readiness = assert_preblind_ready_v22(config)
    blind_path = config.corpus_dir / "heldout-v22/suite.json"
    validation_path = (
        config.artifacts_dir / "validation/v22/heldout-v22/summary.json"
    )
    if not blind_path.is_file():
        raise BenchmarkV22Error(
            "V2.2 calibration passed but heldout-v22 has not been generated"
        )
    if not validation_path.is_file():
        raise BenchmarkV22Error(
            "V2.2 calibration passed but heldout-v22 formal prevalidation is missing"
        )
    blind = _load_json(blind_path, "V2.2 blind suite")
    if not (
        blind.get("schema_version") == BENCHMARK_SCHEMA_VERSION_V22
        and blind.get("split") == "heldout-v22"
        and blind.get("case_count") == 72
        and blind.get("all_prior_disjoint") is True
    ):
        raise BenchmarkV22Error("V2.2 blind suite contract is not satisfied")
    validation = _load_json(validation_path, "V2.2 blind validation")
    if not (
        validation.get("status") == "passed"
        and validation.get("passed_count") == 72
        and validation.get("synthesis_requested") is False
    ):
        raise BenchmarkV22Error("V2.2 blind lint/formal prevalidation is incomplete")
    calls = model_call_plan_v22(blind)
    runs = benchmark_run_plan_v22(blind)
    artifacts = {
        **readiness["artifacts"],
        "blind_v22": blind_path,
        "blind_validation": validation_path,
    }
    core = {
        "schema_version": BENCHMARK_SCHEMA_VERSION_V22,
        "flow_version": BENCHMARK_FLOW_VERSION_V22,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": BENCHMARK_SEED_V22,
        "artifacts": {
            name: {"path": str(path.resolve()), "sha256": _file_hash(path)}
            for name, path in sorted(artifacts.items())
        },
        "blind_suite_hash": blind["suite_hash"],
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "ruleset_version": RULESET_VERSION_V21,
        "profile_hash": _profile_hash(),
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "response_contract_version": ADVISOR_RESPONSE_CONTRACT_VERSION,
        "arms": list(V22_ARMS),
        "promotion_targets": PROMOTION_TARGETS_V22,
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
    root = config.artifacts_dir / "benchmarks/v22"
    lock_path = root / "benchmark-lock.json"
    _write_json(lock_path, lock)
    _write_json(
        root / "model-call-plan.json",
        {
            "schema_version": BENCHMARK_SCHEMA_VERSION_V22,
            "lock_hash": lock["lock_hash"],
            "call_count": len(calls),
            "calls": list(calls),
        },
    )
    _write_json(
        root / "run-plan.json",
        {
            "schema_version": BENCHMARK_SCHEMA_VERSION_V22,
            "lock_hash": lock["lock_hash"],
            "run_count": len(runs),
            "runs": list(runs),
        },
    )
    return lock_path
