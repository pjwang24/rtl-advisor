from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser
from rtl_advisor.corpus_behavior import CorpusBehaviorError, load_behavior_plan
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.tranche_lock import load_tranche_lock


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "examples/corpus/wave2_tier_a/tranche.lock.json"
PLAN_PATH = ROOT / "examples/corpus/wave2_tier_a/behavior.plan.json"


def _write_rehashed(path: Path, payload: dict) -> Path:
    result = deepcopy(payload)
    result.pop("semantic_hash", None)
    result["semantic_hash"] = stable_hash(result)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def test_behavior_plan_preserves_all_refs_and_honest_dispositions() -> None:
    lock = load_tranche_lock(LOCK_PATH)
    plan = load_behavior_plan(PLAN_PATH, project_root=ROOT, tranche_lock=lock)

    assert [item["reference_id"] for item in plan["references"]] == [
        item["reference_id"] for item in lock["references"]
    ]
    assert sum(item["disposition"] == "qualify" for item in plan["references"]) == 8
    assert sum(item["disposition"] == "blocked" for item in plan["references"]) == 4


def test_behavior_plan_rejects_silent_reference_removal(tmp_path: Path) -> None:
    lock = load_tranche_lock(LOCK_PATH)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    plan["references"].pop()
    path = _write_rehashed(tmp_path / "missing.json", plan)

    with pytest.raises(CorpusBehaviorError) as error:
        load_behavior_plan(path, project_root=ROOT, tranche_lock=lock)

    assert error.value.code == "tranche_membership_changed"


def test_behavior_plan_rejects_changed_harness(tmp_path: Path) -> None:
    lock = load_tranche_lock(LOCK_PATH)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    check = next(
        check
        for item in plan["references"]
        for check in item["checks"]
        if check["type"] == "formal_task"
    )
    check["inputs"][0]["sha256"] = "0" * 64
    path = _write_rehashed(tmp_path / "changed.json", plan)

    with pytest.raises(CorpusBehaviorError) as error:
        load_behavior_plan(path, project_root=ROOT, tranche_lock=lock)

    assert error.value.code == "stale_behavior_input"


def test_behavior_cli_can_record_registry_progression() -> None:
    args = build_parser().parse_args(
        (
            "corpus",
            "behavior-tranche",
            "tranche.lock.json",
            "behavior.plan.json",
            "--record-registry",
            "--json",
        )
    )

    assert args.corpus_command == "behavior-tranche"
    assert args.record_registry is True
    assert args.json_output is True
