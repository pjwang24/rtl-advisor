from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.sequential_equivalence import (
    SequentialEquivalenceError,
    _classify_sby_result,
    load_p2_proof_plan,
)


ROOT = Path(__file__).resolve().parents[1]
POSITIVE_PLAN = (
    ROOT / "examples/formal/p2/opentitan_arbiter_ppc_tree_positive.plan.json"
)


def _write_rehashed(path: Path, payload: dict) -> Path:
    result = deepcopy(payload)
    result.pop("semantic_hash", None)
    result["semantic_hash"] = stable_hash(result)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.mark.local_evidence
def test_p2_plan_freezes_same_cycle_contract_and_every_input_hash() -> None:
    plan = load_p2_proof_plan(POSITIVE_PLAN, project_root=ROOT)

    assert plan["backend"] == "sby_miter"
    assert plan["task"] == "positive"
    assert plan["expected_relation"] == "equivalent"
    assert plan["contract"]["level"] == "P2"
    assert plan["contract"]["latency_relation"] == "same_cycle"
    assert len(plan["inputs"]) == 10


def test_p2_plan_rejects_changed_input_hash(tmp_path: Path) -> None:
    plan = json.loads(POSITIVE_PLAN.read_text(encoding="utf-8"))
    plan["inputs"][0]["sha256"] = "0" * 64
    path = _write_rehashed(tmp_path / "stale-plan.json", plan)

    with pytest.raises(SequentialEquivalenceError) as error:
        load_p2_proof_plan(path, project_root=ROOT)
    assert error.value.code == "stale_proof_input"


def test_p2_plan_rejects_latency_changing_contract(tmp_path: Path) -> None:
    plan = json.loads(POSITIVE_PLAN.read_text(encoding="utf-8"))
    plan["contract"]["latency_relation"] = "fixed_offset"
    path = _write_rehashed(tmp_path / "wrong-level.json", plan)

    with pytest.raises(SequentialEquivalenceError) as error:
        load_p2_proof_plan(path, project_root=ROOT)
    assert error.value.code == "invalid_proof_contract"


def test_sby_result_classifier_requires_durable_markers(tmp_path: Path) -> None:
    (tmp_path / "PASS").write_text("passed\n", encoding="utf-8")
    assert _classify_sby_result(0, tmp_path, "DONE (PASS, rc=0)") == "equivalent"

    (tmp_path / "PASS").unlink()
    (tmp_path / "FAIL").write_text("failed\n", encoding="utf-8")
    assert (
        _classify_sby_result(
            2,
            tmp_path,
            "Status returned by engine: FAIL\nDONE (FAIL, rc=2)",
        )
        == "inequivalent"
    )
    assert _classify_sby_result(2, tmp_path, "DONE (FAIL, rc=2)") == "inconclusive"


def test_prove_p2_cli_uses_explicit_formal_runner() -> None:
    args = build_parser().parse_args(
        (
            "corpus",
            "prove-p2",
            "proof.plan.json",
            "--formal-command",
            "pinned-sby",
            "--record-registry",
            "--json",
        )
    )

    assert args.corpus_command == "prove-p2"
    assert args.formal_command == "pinned-sby"
    assert args.record_registry is True
    assert args.json_output is True
