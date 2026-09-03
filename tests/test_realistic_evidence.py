from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from rtl_advisor.config import load_config
import rtl_advisor.mvp_agent as mvp_agent
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.realistic_evidence import (
    ARBITER_CONFIGURATIONS,
    RealisticEvidenceError,
    arbiter_findings,
    load_supported_reference,
    prepare_arbiter_candidate,
    validate_arbiter_candidate,
    validate_reference_input,
    write_reference_input,
)


ROOT = Path(__file__).resolve().parents[1]


def _reference_manifest() -> Path:
    records = sorted(
        (ROOT / "corpus/registry-v1/references/opentitan-prim-arbiter-ppc").glob(
            "*.json"
        )
    )
    for path in reversed(records):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["qualification"] == {
            "reason": None,
            "state": "reference_qualified",
            "status": "active",
        }:
            return path
    raise AssertionError("qualified OpenTitan reference fixture is unavailable")


def test_reference_findings_freeze_all_four_configurations(tmp_path: Path) -> None:
    config = load_config(ROOT / "rtl-advisor.toml")
    reference = load_supported_reference(config, _reference_manifest())
    findings = arbiter_findings(reference)

    assert [item["configuration"] for item in findings] == [
        dict(item) for item in ARBITER_CONFIGURATIONS
    ]
    assert all(item["proof_level"] == "P2" for item in findings)
    assert findings[0]["measurement_levels"] == ["M0", "M1"]
    assert findings[-1]["measurement_levels"] == ["M0", "M1", "M2"]

    record = write_reference_input(tmp_path / "input.json", reference)
    validate_reference_input(record)

    stale = {
        **record,
        "transformation_registry_hash": "0" * 64,
    }
    stale["semantic_hash"] = stable_hash(
        {key: value for key, value in stale.items() if key != "semantic_hash"}
    )
    with pytest.raises(RealisticEvidenceError) as error:
        validate_reference_input(stale)
    assert error.value.code == "stale_transformation_registry"


def test_arbiter_candidate_is_isolated_and_hash_bound(tmp_path: Path) -> None:
    config = load_config(ROOT / "rtl-advisor.toml")
    reference = load_supported_reference(config, _reference_manifest())
    input_record = write_reference_input(tmp_path / "input.json", reference)
    finding = arbiter_findings(reference)[1]
    upstream_before = {
        path: path.read_bytes()
        for path in (
            ROOT / "corpus/upstream/opentitan/hw/ip/prim/rtl/prim_arbiter_ppc.sv",
            ROOT / "corpus/upstream/opentitan/hw/ip/prim/rtl/prim_arbiter_tree.sv",
        )
    }

    candidate = prepare_arbiter_candidate(
        config,
        input_record,
        finding,
        tmp_path / "candidates",
    )

    validate_arbiter_candidate(candidate)
    assert candidate["reference_id"] == "opentitan-prim-arbiter-ppc"
    assert candidate["configuration_id"] == "n04-dw32"
    assert candidate["proof_contract"]["level"] == "P2"
    assert candidate["frontend"]["kind"] == "yosys-slang"
    diff = Path(candidate["diff_path"]).read_text(encoding="utf-8")
    assert "prim_arbiter_ppc" in diff
    assert "prim_arbiter_tree" in diff
    for path, content in upstream_before.items():
        assert path.read_bytes() == content


def test_agent_v2_exposes_reference_configurations_as_candidates(
    tmp_path: Path,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )

    review = mvp_agent.agent_v2_review(
        config,
        str(_reference_manifest()),
        objective="timing",
        normalized_command=("review",),
    )
    assert review["decision"] == "candidate_available"
    assert len(review["findings"]) == 4
    assert review["input"]["kind"] == "qualified_reference"

    candidate = mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][2]["finding_id"],
        normalized_command=("candidate",),
    )
    assert candidate["configuration_id"] == "n08-dw32"
    assert candidate["candidate_origin"] == "upstream_alternative"
    assert candidate["proof_contract"]["level"] == "P2"
    assert candidate["measurement_levels"] == ["M0", "M1", "M2"]
    assert mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][2]["finding_id"],
        normalized_command=("candidate",),
    ) == candidate

    report = mvp_agent.agent_v2_report(
        config,
        review["run_id"],
        normalized_command=("report",),
    )
    assert report["status"] == "incomplete"
    assert report["source_integrity"]["ok"] is True
