#!/usr/bin/env python3
"""Evaluate Phase 7 token, latency, quality, and agreement release gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.plugin_abc import DEFAULT_EXPERIMENT
from scripts.plugin_phase6_eval import (
    EvaluationError,
    _hash,
    _load,
    _quality,
    _telemetry,
)


def evaluate(
    experiment: Path = DEFAULT_EXPERIMENT,
    *,
    telemetry_path: Path | None = None,
    minimum_token_reduction_percent: float = 25.0,
    minimum_latency_reduction_percent: float = 25.0,
) -> dict[str, Any]:
    evaluations = experiment / "evaluations"
    telemetry_path = telemetry_path or evaluations / "phase7" / "telemetry.json"
    scorecard_path = evaluations / "scorecard.json"
    repeat_path = evaluations / "repeat-comparison.json"
    quality = _quality(_load(scorecard_path), _load(repeat_path))
    efficiency = _telemetry(
        telemetry_path,
        experiment=experiment,
        minimum_reduction_percent=minimum_token_reduction_percent,
        minimum_latency_reduction_percent=minimum_latency_reduction_percent,
    )
    statuses = {quality["status"], efficiency["status"]}
    status = (
        "failed"
        if "failed" in statuses
        else ("blocked" if "blocked" in statuses else "passed")
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "schema": "rtl-advisor-plugin-phase7-evaluation-v1",
        "document_type": "rtl-advisor.plugin-phase7-evaluation",
        "experiment_id": "plugin-abc-v1",
        "status": status,
        "quality_gate": quality,
        "efficiency_gate": efficiency,
        "release_ready": status == "passed",
        "sources": {
            "scorecard": str(scorecard_path),
            "repeat_comparison": str(repeat_path),
            "telemetry": str(telemetry_path),
        },
    }
    payload["semantic_hash"] = _hash(payload)
    return payload


def render_markdown(payload: Mapping[str, Any]) -> str:
    quality = payload["quality_gate"]
    efficiency = payload["efficiency_gate"]
    lines = [
        "# RTL Advisor Phase 7 Evaluation",
        "",
        f"Overall status: **{payload['status']}**",
        "",
        f"Quality gate: **{quality['status']}**",
        f"Efficiency gate: **{efficiency['status']}**",
    ]
    if efficiency["status"] == "blocked":
        lines.extend(("", str(efficiency["reason"])))
    else:
        lines.extend(
            (
                "",
                f"Token reduction: {efficiency['observed_token_reduction_percent']}% (minimum {efficiency['minimum_token_reduction_percent']}%)",
                f"Latency reduction: {efficiency['observed_latency_reduction_percent']}% (minimum {efficiency['minimum_latency_reduction_percent']}%)",
                f"Task completion: {efficiency['arms']['B']['task_completion_percent']}%",
                f"Frozen-decision agreement: {efficiency['decision_guardrails']['frozen_result_agreement_percent']}%",
                f"Repeat agreement: {efficiency['decision_guardrails']['measured_repeat_agreement_percent']}%",
            )
        )
    lines.extend(("", f"Release ready: **{'yes' if payload['release_ready'] else 'no'}**", ""))
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
    experiment = args.experiment.resolve()
    try:
        payload = evaluate(
            experiment,
            telemetry_path=args.telemetry.resolve() if args.telemetry else None,
            minimum_token_reduction_percent=args.minimum_token_reduction_percent,
            minimum_latency_reduction_percent=args.minimum_latency_reduction_percent,
        )
    except EvaluationError as exc:
        print(f"Phase 7 evaluation failed: {exc}", file=sys.stderr)
        return 2
    output_root = experiment / "evaluations" / "phase7"
    output_json = args.output_json or output_root / "phase7-evaluation.json"
    output_markdown = args.output_markdown or output_root / "phase7-evaluation.md"
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_markdown.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Phase 7 evaluation: {payload['status']}")
    print(f"JSON: {output_json.resolve()}")
    print(f"Markdown: {output_markdown.resolve()}")
    return 0 if payload["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
