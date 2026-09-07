from __future__ import annotations

import json
from pathlib import Path

from scripts.plugin_phase6_eval import evaluate


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments/plugin-abc-v1"


def test_existing_quality_evidence_passes_but_missing_telemetry_blocks_release(
    tmp_path: Path,
) -> None:
    result = evaluate(EXPERIMENT, telemetry_path=tmp_path / "missing.json")

    assert result["quality_gate"]["status"] == "passed"
    assert result["quality_gate"]["validated_decision_accuracy_percent"] == 95.83
    assert result["quality_gate"]["decision_reproducibility_percent"] == 100.0
    assert result["efficiency_gate"]["status"] == "blocked"
    assert result["status"] == "blocked"
    assert result["release_ready"] is False


def test_complete_telemetry_quantifies_token_reduction_and_latency(
    tmp_path: Path,
) -> None:
    telemetry = tmp_path / "telemetry.json"
    telemetry.write_text(
        json.dumps(
            {
                "schema": "rtl-advisor-plugin-abc-telemetry-v1",
                "experiment_id": "plugin-abc-v1",
                "runs": [
                    {
                        "arm": "A",
                        "repetition": 1,
                        "input_tokens": 800,
                        "output_tokens": 200,
                        "total_tokens": 1000,
                        "wall_time_seconds": 20,
                        "task_completed": True,
                    },
                    {
                        "arm": "A",
                        "repetition": 2,
                        "input_tokens": 880,
                        "output_tokens": 220,
                        "total_tokens": 1100,
                        "wall_time_seconds": 22,
                        "task_completed": True,
                    },
                    {
                        "arm": "B",
                        "repetition": 1,
                        "input_tokens": 400,
                        "output_tokens": 100,
                        "total_tokens": 500,
                        "wall_time_seconds": 12,
                        "task_completed": True,
                        "result_path": str(
                            EXPERIMENT / "runs/arm-b/r1/result.json"
                        ),
                    },
                    {
                        "arm": "B",
                        "repetition": 2,
                        "input_tokens": 480,
                        "output_tokens": 120,
                        "total_tokens": 600,
                        "wall_time_seconds": 14,
                        "task_completed": True,
                        "result_path": str(
                            EXPERIMENT / "runs/arm-b/r2/result.json"
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = evaluate(EXPERIMENT, telemetry_path=telemetry)

    assert result["status"] == "passed"
    assert result["release_ready"] is True
    assert result["efficiency_gate"]["observed_token_reduction_percent"] == 47.62
    assert result["efficiency_gate"]["observed_uncached_token_reduction_percent"] == 47.62
    assert result["efficiency_gate"]["minimum_latency_reduction_percent"] == 25.0
    assert result["efficiency_gate"]["arms"]["B"]["mean_wall_time_seconds"] == 13.0
    assert result["efficiency_gate"]["decision_guardrails"] == {
        "frozen_result_agreement_percent": 100.0,
        "measured_repeat_agreement_percent": 100.0,
    }


def test_latency_gate_blocks_release_even_when_token_gate_passes(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry.json"
    runs = []
    for arm, total, wall in (("A", 1000, 10), ("B", 500, 9)):
        for repetition in (1, 2):
            row = {
                "arm": arm,
                "repetition": repetition,
                "input_tokens": total - 100,
                "output_tokens": 100,
                "total_tokens": total,
                "wall_time_seconds": wall,
                "task_completed": True,
            }
            if arm == "B":
                row["result_path"] = str(
                    EXPERIMENT / f"runs/arm-b/r{repetition}/result.json"
                )
            runs.append(row)
    telemetry.write_text(
        json.dumps(
            {
                "schema": "rtl-advisor-plugin-abc-telemetry-v1",
                "experiment_id": "plugin-abc-v1",
                "runs": runs,
            }
        ),
        encoding="utf-8",
    )

    result = evaluate(EXPERIMENT, telemetry_path=telemetry)

    assert result["efficiency_gate"]["observed_token_reduction_percent"] == 50.0
    assert result["efficiency_gate"]["observed_latency_reduction_percent"] == 10.0
    assert result["efficiency_gate"]["status"] == "failed"
    assert result["release_ready"] is False


def test_evaluator_excludes_audited_superseded_run(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry.json"
    runs = []
    for arm in ("A", "B"):
        for repetition in (1, 2):
            row = {
                "arm": arm,
                "repetition": repetition,
                "input_tokens": 900 if arm == "A" else 400,
                "output_tokens": 100,
                "total_tokens": 1000 if arm == "A" else 500,
                "wall_time_seconds": 20 if arm == "A" else 10,
                "task_completed": True,
            }
            if arm == "B":
                row["result_path"] = str(
                    EXPERIMENT / f"runs/arm-b/r{repetition}/result.json"
                )
            runs.append(row)
    runs.append(
        {
            **runs[-1],
            "attempt": 0,
            "admitted_to_evaluation": False,
            "exclusion_reason": "known orchestration defect",
        }
    )
    telemetry.write_text(
        json.dumps(
            {
                "schema": "rtl-advisor-plugin-abc-telemetry-v1",
                "experiment_id": "plugin-abc-v1",
                "runs": runs,
            }
        ),
        encoding="utf-8",
    )

    result = evaluate(EXPERIMENT, telemetry_path=telemetry)

    assert result["release_ready"] is True
    assert result["efficiency_gate"]["arms"]["B"]["run_count"] == 2
    assert result["efficiency_gate"]["excluded_runs"][0]["reason"] == (
        "known orchestration defect"
    )


def test_phase6_contract_schemas_are_versioned() -> None:
    telemetry = json.loads(
        (ROOT / "schemas/rtl-advisor-plugin-abc-telemetry-v1.schema.json").read_text()
    )
    evaluation = json.loads(
        (
            ROOT
            / "schemas/rtl-advisor-plugin-phase6-evaluation-v1.schema.json"
        ).read_text()
    )

    assert telemetry["properties"]["schema"]["const"] == (
        "rtl-advisor-plugin-abc-telemetry-v1"
    )
    assert evaluation["properties"]["status"]["enum"] == [
        "passed",
        "failed",
        "blocked",
    ]
