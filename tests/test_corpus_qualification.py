from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rtl_advisor.corpus_qualification import (
    CorpusQualificationError,
    load_qualification_plan,
)
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.tranche_lock import load_tranche_lock


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "examples/corpus/wave2_tier_a/tranche.lock.json"
PLAN_PATH = ROOT / "examples/corpus/wave2_tier_a/qualification.plan.json"


def _write_rehashed(path: Path, payload: dict) -> Path:
    result = deepcopy(payload)
    result.pop("semantic_hash", None)
    result["semantic_hash"] = stable_hash(result)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def test_wave2_qualification_plan_preserves_frozen_order_and_disables_ppa() -> None:
    lock = load_tranche_lock(LOCK_PATH)
    plan = load_qualification_plan(PLAN_PATH, tranche_lock=lock)

    assert len(plan["references"]) == 12
    assert [item["reference_id"] for item in plan["references"]] == [
        item["reference_id"] for item in lock["references"]
    ]
    assert plan["tool_contract"]["candidate_synthesis_enabled"] is False
    assert plan["tool_contract"]["yosys_slang_plugin"].endswith("/slang.so")


def test_qualification_plan_rejects_reordered_reference(tmp_path: Path) -> None:
    lock = load_tranche_lock(LOCK_PATH)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    plan["references"][0], plan["references"][1] = (
        plan["references"][1],
        plan["references"][0],
    )
    path = _write_rehashed(tmp_path / "reordered.json", plan)

    with pytest.raises(CorpusQualificationError) as error:
        load_qualification_plan(path, tranche_lock=lock)
    assert error.value.code == "tranche_membership_changed"


def test_qualification_plan_rejects_candidate_synthesis(tmp_path: Path) -> None:
    lock = load_tranche_lock(LOCK_PATH)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    plan["tool_contract"]["candidate_synthesis_enabled"] = True
    path = _write_rehashed(tmp_path / "ppa-enabled.json", plan)

    with pytest.raises(CorpusQualificationError) as error:
        load_qualification_plan(path, tranche_lock=lock)
    assert error.value.code == "ppa_visible_before_freeze"
