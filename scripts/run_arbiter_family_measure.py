#!/usr/bin/env python3
"""Run proof-gated M0/M1 measurement for the frozen arbiter family."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_advisor.arbiter_family_execution import (  # noqa: E402
    family_designs_from_candidate,
)
from rtl_advisor.config import load_config  # noqa: E402
from rtl_advisor.mvp_measure import (  # noqa: E402
    MVPMeasurementError,
    measure_candidate,
)
from rtl_advisor.mvp_schema import (  # noqa: E402
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.transformation_executor import (  # noqa: E402
    DEFAULT_EXECUTOR_REGISTRY,
)


SLANG_PLUGIN_PATH = "/opt/oss-cad-suite/share/yosys/plugins/slang.so"
PREPPA_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-preppa-evidence"
EVIDENCE_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-m0-m1-evidence"


def _normalized_profile(profile: dict[str, Any]) -> dict[str, Any]:
    baseline = profile["baseline"]
    candidate = profile["candidate"]
    return {
        "classification": profile["classification"],
        "comparison": profile["comparison"],
        "recipe_hash": profile["recipe"]["recipe_hash"],
        "constraints_sha256": baseline["constraints"]["sha256"],
        "baseline": {
            "metrics": baseline["metrics"],
            "netlist_sha256": baseline["netlist"]["sha256"],
            "cell_signature": baseline.get("cell_signature"),
        },
        "candidate": {
            "metrics": candidate["metrics"],
            "netlist_sha256": candidate["netlist"]["sha256"],
            "cell_signature": candidate.get("cell_signature"),
        },
    }


def _measure_one(
    config: Any,
    preppa_root: Path,
    result: dict[str, Any],
    repeat_root: Path,
) -> dict[str, Any]:
    candidate_id = str(result["candidate_id"])
    candidate_root = preppa_root / "candidates" / candidate_id
    candidate = read_hashed_json(
        candidate_root / "candidate-core.json",
        document_type="rtl-advisor.candidate",
        schema_version=1,
    )
    formal = read_hashed_json(
        candidate_root / "formal" / "formal.json",
        document_type="rtl-advisor.formal-result",
        schema_version=1,
    )
    if (
        formal.get("semantic_hash") != result["formal_semantic_hash"]
        or formal.get("status") != "formal_passed"
        or formal.get("safe") is not True
    ):
        raise RuntimeError(
            f"current formal proof is required for {candidate_id}"
        )
    executor = DEFAULT_EXECUTOR_REGISTRY.for_candidate(candidate)
    baseline, alternative = family_designs_from_candidate(
        candidate,
        executor.spec,
    )
    measurement = measure_candidate(
        config,
        baseline,
        alternative,
        formal,
        repeat_root
        / str(result["reference_id"])
        / str(result["configuration_id"]),
        objective="timing",
        frontend={
            "kind": "yosys-slang",
            "plugin_path": SLANG_PLUGIN_PATH,
        },
    )
    profiles = measurement["measurements"]
    return {
        "reference_id": result["reference_id"],
        "configuration_id": result["configuration_id"],
        "candidate_id": candidate_id,
        "formal_semantic_hash": formal["semantic_hash"],
        "measurement_semantic_hash": measurement["semantic_hash"],
        "decision": measurement["decision"],
        "profiles": {
            "M0": _normalized_profile(profiles["standard"]),
            "M1": _normalized_profile(profiles["stronger"]),
        },
        "artifacts": measurement["artifacts"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preppa", type=Path, required=True)
    parser.add_argument("--repeat", choices=("repeat-1", "repeat-2"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", action="append")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    arguments = parser.parse_args()

    config = load_config(ROOT / "rtl-advisor.toml")
    if arguments.timeout_seconds <= 0:
        raise RuntimeError("--timeout-seconds must be positive")
    config = replace(
        config,
        tools=replace(
            config.tools,
            timeout_seconds=arguments.timeout_seconds,
        ),
    )
    preppa = read_hashed_json(
        arguments.preppa,
        document_type=PREPPA_DOCUMENT_TYPE,
        schema_version=1,
    )
    if preppa.get("ppa_inspected") is not False:
        raise RuntimeError("pre-PPA evidence must remain outcome-blind")
    selected = [
        dict(item)
        for item in preppa["results"]
        if not arguments.reference
        or item["reference_id"] in set(arguments.reference)
    ]
    if not selected:
        raise RuntimeError("no frozen configurations selected")
    if any(
        item.get("formal_status") != "formal_passed"
        or item.get("safe") is not True
        for item in selected
    ):
        raise RuntimeError("every selected configuration must formally pass")

    preppa_root = config.artifacts_dir / str(preppa["freeze_version"])
    repeat_root = (
        config.artifacts_dir
        / "family-studies"
        / "arbiter-family-credibility-v1"
        / "measurements"
        / arguments.repeat
    )
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in selected:
        try:
            records.append(
                _measure_one(
                    config,
                    preppa_root,
                    item,
                    repeat_root,
                )
            )
        except (MVPMeasurementError, RuntimeError) as exc:
            failures.append(
                {
                    "reference_id": item["reference_id"],
                    "configuration_id": item["configuration_id"],
                    "candidate_id": item["candidate_id"],
                    "error": {
                        "code": getattr(exc, "code", "measurement_failed"),
                        "detail": str(exc),
                    },
                }
            )

    payload = {
        "schema_version": 1,
        "document_type": EVIDENCE_DOCUMENT_TYPE,
        "study_id": "arbiter-family-credibility-v1",
        "family_id": "same-cycle-arbiter-topology-v1",
        "repeat_id": arguments.repeat,
        "preppa_semantic_hash": preppa["semantic_hash"],
        "tool_scope": "pinned Yosys/ABC and Nangate45 only",
        "status": "completed" if not failures else "completed_with_failures",
        "summary": {
            "selected_configuration_count": len(selected),
            "measured_configuration_count": len(records),
            "failure_count": len(failures),
            "decision_counts": {
                decision: sum(
                    record["decision"] == decision for record in records
                )
                for decision in (
                    "measured_improvement",
                    "synthesis_handles",
                    "flow_dependent",
                    "regression",
                )
            },
        },
        "results": records,
        "failures": failures,
    }
    output = arguments.output.expanduser().resolve()
    if output.is_file():
        stored = read_hashed_json(
            output,
            document_type=EVIDENCE_DOCUMENT_TYPE,
            schema_version=1,
        )
        expected = {**payload, "semantic_hash": stable_hash(payload)}
        if stored != expected:
            raise RuntimeError(f"append-only measurement conflict: {output}")
    else:
        stored = write_hashed_json(output, payload, exclusive=True)
    print(json.dumps(stored, indent=2, sort_keys=True))
    return 0 if not failures else 4


if __name__ == "__main__":
    raise SystemExit(main())
