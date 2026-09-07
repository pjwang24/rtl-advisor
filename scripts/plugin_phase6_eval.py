#!/usr/bin/env python3
"""Evaluate the Phase 6 plugin release gates without mutating frozen A/B/C data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT = ROOT / "experiments/plugin-abc-v1"
SCHEMA_VERSION = 1
DOCUMENT_TYPE = "rtl-advisor.plugin-phase6-evaluation"


class EvaluationError(RuntimeError):
    """Raised when existing evaluation evidence is malformed."""


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError(f"{path} must contain a JSON object")
    return payload


def _hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _quality(
    scorecard: Mapping[str, Any], repeat: Mapping[str, Any]
) -> dict[str, Any]:
    if scorecard.get("schema") != "rtl-advisor-plugin-abc-scorecard-v1":
        raise EvaluationError("unexpected A/B/C scorecard schema")
    if repeat.get("schema") != "rtl-advisor-plugin-abc-repeat-comparison-v1":
        raise EvaluationError("unexpected A/B/C repeat-comparison schema")
    plugin_rows = [
        row for row in scorecard.get("arms", []) if row.get("arm") == "B"
    ]
    repeat_rows = [
        row for row in repeat.get("comparisons", []) if row.get("arm") == "B"
    ]
    if len(plugin_rows) < 2 or len(repeat_rows) != 1:
        raise EvaluationError(
            "A/B/C evidence requires two plugin runs and one repeat comparison"
        )
    accuracy = statistics.fmean(
        float(row["validated_decision_accuracy_percent"]) for row in plugin_rows
    )
    completion = statistics.fmean(
        float(row["evidence_completion_percent"]) for row in plugin_rows
    )
    harmful = sum(int(row["harmful_recommendations"]) for row in plugin_rows)
    unproven = sum(int(row["unproven_recommendations"]) for row in plugin_rows)
    reproducibility = float(repeat_rows[0]["decision_reproducibility_percent"])
    passed = (
        accuracy >= 95.0
        and completion == 100.0
        and reproducibility == 100.0
        and harmful == 0
        and unproven == 0
    )
    return {
        "status": "passed" if passed else "failed",
        "plugin_run_count": len(plugin_rows),
        "validated_decision_accuracy_percent": round(accuracy, 2),
        "evidence_completion_percent": round(completion, 2),
        "decision_reproducibility_percent": round(reproducibility, 2),
        "harmful_recommendations": harmful,
        "unproven_recommendations": unproven,
    }


def _decision_map(path: Path) -> dict[str, str]:
    payload = _load(path)
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise EvaluationError(f"instrumented result has no cases: {path}")
    result: dict[str, str] = {}
    for item in cases:
        if not isinstance(item, Mapping):
            raise EvaluationError(
                f"instrumented result has an invalid case: {path}"
            )
        case_id = item.get("case_id")
        decision = item.get("decision")
        if not isinstance(case_id, str) or not isinstance(decision, str):
            raise EvaluationError(
                f"instrumented result has an invalid decision: {path}"
            )
        if case_id in result:
            raise EvaluationError(f"instrumented result repeats {case_id}: {path}")
        result[case_id] = decision
    return result


def _plugin_decision_guardrails(
    rows: Sequence[Mapping[str, Any]], *, experiment: Path
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: int(item["repetition"]))
    measured: list[dict[str, str]] = []
    reference_agreements: list[float] = []
    for row in ordered:
        raw_path = row.get("result_path")
        if not isinstance(raw_path, str) or not Path(raw_path).is_file():
            raise EvaluationError("completed plugin telemetry requires a result_path")
        current = _decision_map(Path(raw_path))
        repetition = int(row["repetition"])
        reference = _decision_map(
            experiment / "runs" / "arm-b" / f"r{repetition}" / "result.json"
        )
        if set(current) != set(reference):
            raise EvaluationError("instrumented and frozen plugin case sets differ")
        reference_agreements.append(
            100.0
            * sum(current[case_id] == reference[case_id] for case_id in current)
            / len(current)
        )
        measured.append(current)
    if len(measured) != 2 or set(measured[0]) != set(measured[1]):
        raise EvaluationError("plugin decision guardrail requires two matching case sets")
    repeat_agreement = (
        100.0
        * sum(
            measured[0][case_id] == measured[1][case_id]
            for case_id in measured[0]
        )
        / len(measured[0])
    )
    return {
        "frozen_result_agreement_percent": round(
            statistics.fmean(reference_agreements), 2
        ),
        "measured_repeat_agreement_percent": round(repeat_agreement, 2),
    }


def _event_diagnostics(path: Path) -> dict[str, Any]:
    event_count = 0
    command_count = 0
    command_wave_count = 0
    command_output_bytes = 0
    agent_message_bytes = 0
    active_commands: set[str] = set()
    command_categories: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationError(
            f"cannot read telemetry event stream {path}: {exc}"
        ) from exc
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvaluationError(
                f"invalid telemetry event at {path}:{line_number}: {exc}"
            ) from exc
        event_count += 1
        if not isinstance(event, Mapping) or not isinstance(event.get("item"), Mapping):
            continue
        item = event["item"]
        item_id = str(item.get("id", ""))
        if event.get("type") == "item.started" and item.get("type") == "command_execution":
            if not active_commands:
                command_wave_count += 1
            active_commands.add(item_id)
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            command_count += 1
            active_commands.discard(item_id)
            output = item.get("aggregated_output", "")
            if isinstance(output, str):
                command_output_bytes += len(output.encode("utf-8"))
            command = str(item.get("command", "")).lower()
            if "workflow batch" in command:
                category = "workflow_batch"
            elif "workflow" in command or "run_rtl_advisor.py" in command:
                category = "rtl_advisor"
            elif "jq " in command or "json.tool" in command:
                category = "artifact_read"
            elif "shasum" in command or "sha256sum" in command:
                category = "hash"
            elif any(tool in command for tool in ("yosys", "verilator", "sby ")):
                category = "eda"
            else:
                category = "other"
            command_categories[category] = command_categories.get(category, 0) + 1
        elif event.get("type") == "item.completed" and item.get("type") == "agent_message":
            message = item.get("text", "")
            if isinstance(message, str):
                agent_message_bytes += len(message.encode("utf-8"))
    if command_count and not command_wave_count:
        command_wave_count = command_count
    return {
        "event_count": event_count,
        "command_count": command_count,
        "command_wave_count": command_wave_count,
        "command_categories": command_categories,
        "command_output_bytes": command_output_bytes,
        "agent_message_bytes": agent_message_bytes,
        "output_bytes": command_output_bytes + agent_message_bytes,
    }


def _telemetry(
    path: Path,
    *,
    experiment: Path,
    minimum_reduction_percent: float,
    minimum_latency_reduction_percent: float,
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "blocked",
            "reason": (
                "token and latency telemetry was not captured by the frozen "
                "A/B/C runs"
            ),
            "telemetry_path": str(path),
            "required_fields": [
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "wall_time_seconds",
                "task_completed",
            ],
        }
    payload = _load(path)
    if payload.get("schema") != "rtl-advisor-plugin-abc-telemetry-v1":
        raise EvaluationError("unexpected A/B/C telemetry schema")
    rows = payload.get("runs")
    if not isinstance(rows, list):
        raise EvaluationError("telemetry runs must be an array")
    by_arm: dict[str, list[Mapping[str, Any]]] = {"A": [], "B": [], "C": []}
    excluded_runs: list[dict[str, Any]] = []
    required = {
        "arm",
        "repetition",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "wall_time_seconds",
        "task_completed",
    }
    for row in rows:
        if not isinstance(row, Mapping) or not required <= set(row):
            raise EvaluationError(
                "every telemetry run requires token, latency, and completion fields"
            )
        arm = row.get("arm")
        if arm not in by_arm:
            raise EvaluationError(f"unsupported telemetry arm: {arm!r}")
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            if (
                not isinstance(row[name], int)
                or isinstance(row[name], bool)
                or row[name] < 0
            ):
                raise EvaluationError(
                    f"telemetry {name} must be a non-negative integer"
                )
        if row["total_tokens"] != row["input_tokens"] + row["output_tokens"]:
            raise EvaluationError(
                "telemetry total_tokens must equal input_tokens plus output_tokens"
            )
        cached = row.get("cached_input_tokens", 0)
        if (
            not isinstance(cached, int)
            or isinstance(cached, bool)
            or cached < 0
            or cached > row["input_tokens"]
        ):
            raise EvaluationError(
                "telemetry cached_input_tokens must be between zero and input_tokens"
            )
        if (
            not isinstance(row["wall_time_seconds"], (int, float))
            or row["wall_time_seconds"] < 0
        ):
            raise EvaluationError("telemetry wall_time_seconds must be non-negative")
        if not isinstance(row["task_completed"], bool):
            raise EvaluationError("telemetry task_completed must be boolean")
        admitted = row.get("admitted_to_evaluation", True)
        if not isinstance(admitted, bool):
            raise EvaluationError("telemetry admitted_to_evaluation must be boolean")
        if not admitted:
            reason = row.get("exclusion_reason")
            if not isinstance(reason, str) or not reason:
                raise EvaluationError("excluded telemetry requires exclusion_reason")
            excluded_runs.append(
                {
                    "arm": arm,
                    "repetition": row["repetition"],
                    "attempt": row.get("attempt"),
                    "reason": reason,
                    "events_path": row.get("events_path"),
                }
            )
            continue
        by_arm[str(arm)].append(row)
    if len(by_arm["A"]) < 2 or len(by_arm["B"]) < 2:
        raise EvaluationError(
            "telemetry requires at least two baseline and two plugin runs"
        )

    summaries: dict[str, Any] = {}
    for arm, arm_rows in by_arm.items():
        if not arm_rows:
            continue
        summaries[arm] = {
            "run_count": len(arm_rows),
            "mean_total_tokens": round(
                statistics.fmean(float(row["total_tokens"]) for row in arm_rows), 2
            ),
            "mean_uncached_token_volume": round(
                statistics.fmean(
                    float(
                        row["input_tokens"]
                        - row.get("cached_input_tokens", 0)
                        + row["output_tokens"]
                    )
                    for row in arm_rows
                ),
                2,
            ),
            "mean_wall_time_seconds": round(
                statistics.fmean(
                    float(row["wall_time_seconds"]) for row in arm_rows
                ),
                3,
            ),
            "task_completion_percent": round(
                100.0
                * sum(bool(row["task_completed"]) for row in arm_rows)
                / len(arm_rows),
                2,
            ),
        }
        event_paths = [row.get("events_path") for row in arm_rows]
        if all(
            isinstance(item, str) and Path(item).is_file()
            for item in event_paths
        ):
            diagnostics = [
                _event_diagnostics(Path(str(item))) for item in event_paths
            ]
            summaries[arm]["mean_event_count"] = round(
                statistics.fmean(item["event_count"] for item in diagnostics), 2
            )
            summaries[arm]["mean_command_count"] = round(
                statistics.fmean(item["command_count"] for item in diagnostics), 2
            )
            summaries[arm]["mean_command_wave_count"] = round(
                statistics.fmean(item["command_wave_count"] for item in diagnostics), 2
            )
            summaries[arm]["mean_command_output_bytes"] = round(
                statistics.fmean(
                    item["command_output_bytes"] for item in diagnostics
                ),
                2,
            )
            summaries[arm]["mean_agent_message_bytes"] = round(
                statistics.fmean(item["agent_message_bytes"] for item in diagnostics),
                2,
            )
            summaries[arm]["mean_output_bytes"] = round(
                statistics.fmean(item["output_bytes"] for item in diagnostics), 2
            )
            categories: dict[str, float] = {}
            names = sorted(
                {
                    name
                    for diagnostic in diagnostics
                    for name in diagnostic["command_categories"]
                }
            )
            for name in names:
                categories[name] = round(
                    statistics.fmean(
                        diagnostic["command_categories"].get(name, 0)
                        for diagnostic in diagnostics
                    ),
                    2,
                )
            summaries[arm]["mean_command_categories"] = categories
    baseline_tokens = summaries["A"]["mean_total_tokens"]
    plugin_tokens = summaries["B"]["mean_total_tokens"]
    reduction = (
        100.0 * (baseline_tokens - plugin_tokens) / baseline_tokens
        if baseline_tokens
        else 0.0
    )
    baseline_uncached = summaries["A"]["mean_uncached_token_volume"]
    plugin_uncached = summaries["B"]["mean_uncached_token_volume"]
    uncached_reduction = (
        100.0 * (baseline_uncached - plugin_uncached) / baseline_uncached
        if baseline_uncached
        else 0.0
    )
    baseline_latency = summaries["A"]["mean_wall_time_seconds"]
    plugin_latency = summaries["B"]["mean_wall_time_seconds"]
    latency_reduction = (
        100.0 * (baseline_latency - plugin_latency) / baseline_latency
        if baseline_latency
        else 0.0
    )
    agreement = _plugin_decision_guardrails(by_arm["B"], experiment=experiment)
    passed = (
        reduction >= minimum_reduction_percent
        and latency_reduction >= minimum_latency_reduction_percent
        and summaries["B"]["task_completion_percent"] == 100.0
        and agreement["frozen_result_agreement_percent"] == 100.0
        and agreement["measured_repeat_agreement_percent"] == 100.0
    )
    return {
        "status": "passed" if passed else "failed",
        "telemetry_path": str(path),
        "minimum_token_reduction_percent": minimum_reduction_percent,
        "minimum_latency_reduction_percent": minimum_latency_reduction_percent,
        "observed_token_reduction_percent": round(reduction, 2),
        "observed_uncached_token_reduction_percent": round(
            uncached_reduction, 2
        ),
        "observed_latency_reduction_percent": round(latency_reduction, 2),
        "decision_guardrails": agreement,
        "arms": summaries,
        "excluded_runs": excluded_runs,
    }


def evaluate(
    experiment: Path = DEFAULT_EXPERIMENT,
    *,
    telemetry_path: Path | None = None,
    minimum_reduction_percent: float = 25.0,
    minimum_latency_reduction_percent: float = 25.0,
) -> dict[str, Any]:
    evaluations = experiment / "evaluations"
    scorecard_path = evaluations / "scorecard.json"
    repeat_path = evaluations / "repeat-comparison.json"
    telemetry_path = telemetry_path or evaluations / "telemetry.json"
    quality = _quality(_load(scorecard_path), _load(repeat_path))
    efficiency = _telemetry(
        telemetry_path,
        experiment=experiment,
        minimum_reduction_percent=minimum_reduction_percent,
        minimum_latency_reduction_percent=minimum_latency_reduction_percent,
    )
    statuses = {quality["status"], efficiency["status"]}
    status = (
        "failed"
        if "failed" in statuses
        else ("blocked" if "blocked" in statuses else "passed")
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "schema": "rtl-advisor-plugin-phase6-evaluation-v1",
        "document_type": DOCUMENT_TYPE,
        "experiment_id": "plugin-abc-v1",
        "status": status,
        "quality_gate": quality,
        "efficiency_gate": efficiency,
        "release_ready": status == "passed",
        "sources": {
            "scorecard": str(scorecard_path),
            "repeat_comparison": str(repeat_path),
        },
    }
    payload["semantic_hash"] = _hash(payload)
    return payload


def render_markdown(payload: Mapping[str, Any]) -> str:
    quality = payload["quality_gate"]
    efficiency = payload["efficiency_gate"]
    lines = [
        "# RTL Advisor Phase 6 Evaluation",
        "",
        f"Overall status: **{payload['status']}**",
        "",
        "## Quality and agreement",
        "",
        f"- Plugin decision accuracy: {quality['validated_decision_accuracy_percent']}%",
        f"- Evidence completion: {quality['evidence_completion_percent']}%",
        f"- Decision reproducibility: {quality['decision_reproducibility_percent']}%",
        f"- Harmful recommendations: {quality['harmful_recommendations']}",
        "",
        "## Token and latency observability",
        "",
        f"Efficiency gate: **{efficiency['status']}**",
    ]
    if efficiency["status"] == "blocked":
        lines.extend(("", str(efficiency["reason"])))
    else:
        lines.extend(
            (
                "",
                f"Observed plugin token reduction: {efficiency['observed_token_reduction_percent']}%",
                f"Observed uncached token reduction: {efficiency['observed_uncached_token_reduction_percent']}%",
                f"Observed latency reduction: {efficiency['observed_latency_reduction_percent']}%",
                f"Agreement with frozen plugin decisions: {efficiency['decision_guardrails']['frozen_result_agreement_percent']}%",
            )
        )
    release_ready = "yes" if payload["release_ready"] else "no"
    lines.extend(("", f"Release ready: **{release_ready}**", ""))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--minimum-token-reduction-percent", type=float, default=25.0)
    parser.add_argument("--minimum-latency-reduction-percent", type=float, default=25.0)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = evaluate(
            args.experiment.resolve(),
            telemetry_path=args.telemetry.resolve() if args.telemetry else None,
            minimum_reduction_percent=args.minimum_token_reduction_percent,
            minimum_latency_reduction_percent=args.minimum_latency_reduction_percent,
        )
    except EvaluationError as exc:
        print(f"Phase 6 evaluation failed: {exc}", file=sys.stderr)
        return 2
    output_json = args.output_json or args.experiment / "evaluations/phase6-evaluation.json"
    output_markdown = args.output_markdown or args.experiment / "evaluations/phase6-evaluation.md"
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_markdown.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Phase 6 evaluation: {payload['status']}")
    print(f"JSON: {output_json.resolve()}")
    print(f"Markdown: {output_markdown.resolve()}")
    return 0 if payload["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
