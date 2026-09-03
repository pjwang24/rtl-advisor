from __future__ import annotations

import json
from pathlib import Path

from scripts.plugin_phase7_eval import evaluate


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments/plugin-abc-v1"


def test_phase7_uses_separate_telemetry_and_blocks_until_measured(tmp_path: Path) -> None:
    result = evaluate(EXPERIMENT, telemetry_path=tmp_path / "phase7-telemetry.json")

    assert result["schema"] == "rtl-advisor-plugin-phase7-evaluation-v1"
    assert result["quality_gate"]["status"] == "passed"
    assert result["efficiency_gate"]["status"] == "blocked"
    assert result["release_ready"] is False
    assert "phase7-telemetry.json" in result["sources"]["telemetry"]


def test_phase7_evaluation_schema_is_versioned() -> None:
    schema = json.loads(
        (
            ROOT / "schemas/rtl-advisor-plugin-phase7-evaluation-v1.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["properties"]["schema"]["const"] == (
        "rtl-advisor-plugin-phase7-evaluation-v1"
    )
