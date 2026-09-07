from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.transaction_equivalence import (
    TransactionEquivalenceError,
    load_p3_proof_plan,
)


ROOT = Path(__file__).resolve().parents[1]
POSITIVE_PLAN = ROOT / "examples/formal/p3/verilog_axis_pipeline_positive.plan.json"


def _write_rehashed(path: Path, payload: dict) -> Path:
    result = deepcopy(payload)
    result.pop("semantic_hash", None)
    result["semantic_hash"] = stable_hash(result)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.mark.local_evidence
def test_p3_plan_freezes_transaction_contract_bounds_and_inputs() -> None:
    plan = load_p3_proof_plan(POSITIVE_PLAN, project_root=ROOT)

    assert plan["task"] == "positive"
    assert plan["contract"]["level"] == "P3"
    assert plan["contract"]["latency_relation"] == "transaction_ordered"
    assert plan["contract"]["bounds"] == {
        "transaction_count": 3,
        "max_stall_cycles": 2,
        "drain_cycles": 15,
    }
    assert len(plan["inputs"]) == 4


def test_p3_plan_rejects_same_cycle_contract(tmp_path: Path) -> None:
    plan = json.loads(POSITIVE_PLAN.read_text(encoding="utf-8"))
    plan["contract"]["latency_relation"] = "same_cycle"
    path = _write_rehashed(tmp_path / "same-cycle.json", plan)

    with pytest.raises(TransactionEquivalenceError) as error:
        load_p3_proof_plan(path, project_root=ROOT)

    assert error.value.code == "invalid_proof_contract"


def test_p3_plan_rejects_unbounded_stalls(tmp_path: Path) -> None:
    plan = json.loads(POSITIVE_PLAN.read_text(encoding="utf-8"))
    plan["contract"]["bounds"]["max_stall_cycles"] = 0
    path = _write_rehashed(tmp_path / "unbounded.json", plan)

    with pytest.raises(TransactionEquivalenceError, match="positive finite bounds"):
        load_p3_proof_plan(path, project_root=ROOT)


def test_prove_p3_cli_exposes_registry_recording() -> None:
    args = build_parser().parse_args(
        (
            "corpus",
            "prove-p3",
            "proof.plan.json",
            "--formal-command",
            "pinned-sby",
            "--record-registry",
            "--json",
        )
    )

    assert args.corpus_command == "prove-p3"
    assert args.formal_command == "pinned-sby"
    assert args.record_registry is True
    assert args.json_output is True
