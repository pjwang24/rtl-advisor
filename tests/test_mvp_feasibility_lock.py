from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "docs/evidence/mvp-v1-feasibility.json"


def test_frozen_feasibility_lock_is_complete_and_outcome_blind() -> None:
    payload = json.loads(LOCK.read_text(encoding="utf-8"))

    assert payload["protocol"]["selection_order"] == [
        "orfs-riscv32i",
        "orfs-ibex",
        "picorv32",
        "orfs-cva6",
    ]
    assert payload["protocol"]["candidate_synthesis_allowed"] is False
    assert len(payload["sources"]) == 4
    for source in payload["sources"]:
        assert len(source["snapshot_source_sha256"]) == 64
        assert source["eligible_sites"] == []
        assert source["compile_command"] is None
        assert source["compile_status"] == "not_run_no_eligible_site"
        assert source["status"] == "no_qualifying_module"
        assert source.get("license_path") or source.get("license_source_url")

    result = payload["result"]
    assert result == {
        "status": "blocked",
        "qualifying_module_count": 0,
        "required_module_count": 2,
        "reason": (
            "The complete pre-registered corpus contains no module satisfying "
            "every frozen eligibility rule."
        ),
        "candidate_synthesis_run": False,
        "ppa_inspected": False,
    }
    assert {item["project"] for item in payload["near_matches"]} == {
        source["id"] for source in payload["sources"]
    }
