from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _framework():
    import importlib.util

    path = ROOT / "scripts" / "plugin_abc.py"
    spec = importlib.util.spec_from_file_location("plugin_abc", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_manifest_has_12_generated_and_12_open_hash_matched_cases() -> None:
    result = _framework().validate_manifest()
    assert result["ok"] is True
    assert result["case_count"] == 24
    assert result["source_kinds"] == {"generated": 12, "open": 12}


def test_protocol_oracle_packets_and_schemas_are_hash_locked() -> None:
    assert _framework().validate_manifest()["ok"] is True


def test_arm_packets_exclude_the_hidden_oracle_and_freeze_access_rules() -> None:
    framework = _framework()
    packets = {arm: framework.make_packet(arm, 1) for arm in "ABC"}
    assert all(len(packet["cases"]) == 24 for packet in packets.values())
    assert all("oracle" not in json.dumps(packet).lower() or "do not read" in json.dumps(packet).lower() for packet in packets.values())
    assert "Do not inspect or invoke RTL Advisor" in packets["A"]["access_rule"]
    assert "installed rtl-advisor:analyze-rtl skill" in packets["B"]["access_rule"]
    assert "Reason broadly" in packets["C"]["access_rule"]


def test_stale_or_incomplete_arm_result_is_rejected(tmp_path: Path) -> None:
    framework = _framework()
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({
            "schema": "rtl-advisor-plugin-abc-arm-result-v1",
            "experiment_id": "plugin-abc-v1",
            "arm": "A",
            "repetition": 1,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "manifest_sha256": "0" * 64,
            "cases": [],
        }),
        encoding="utf-8",
    )
    with pytest.raises(framework.FrameworkError, match="stale manifest hash"):
        framework.validate_arm_result(path)


def test_scorer_marks_unproven_recommendations_incorrect() -> None:
    framework = _framework()
    result = {
        "case_id": "g01",
        "decision": "recommend_change",
        "evidence_complete": False,
        "candidates": [{
            "formal_status": "not_run",
            "measurement_status": "not_run",
            "final_state": "unproven",
        }],
    }
    oracle = {"known_action": "evaluate_candidate"}
    score = framework._case_score(result, oracle)
    assert score["correct"] is False
    assert score["unproven_recommendation"] is True


def test_scorer_accepts_evidence_backed_synthesis_handles_no_change() -> None:
    framework = _framework()
    result = {
        "case_id": "g01",
        "decision": "no_change",
        "evidence_complete": True,
        "candidates": [{
            "formal_status": "formal_passed",
            "measurement_status": "measured",
            "final_state": "synthesis_handles",
        }],
    }
    oracle = {"known_action": "evaluate_candidate"}
    score = framework._case_score(result, oracle)
    assert score["correct"] is True
    assert score["opportunity_validated"] is True
    result["evidence_complete"] = False
    assert framework._case_score(result, oracle)["correct"] is False


def test_candidate_paths_are_confined_to_assigned_arm_or_artifact_store() -> None:
    framework = _framework()
    allowed = framework._candidate_path(
        "experiments/plugin-abc-v1/runs/arm-a/r1/candidates/g01.sv",
        arm="A",
        repetition=1,
    )
    assert allowed.is_relative_to(ROOT)
    with pytest.raises(framework.FrameworkError, match="outside the assigned"):
        framework._candidate_path(
            "experiments/plugin-abc-v1/runs/arm-b/r1/candidates/g01.sv",
            arm="A",
            repetition=1,
        )


def test_score_requires_independent_audit(tmp_path: Path) -> None:
    framework = _framework()
    with pytest.raises(framework.FrameworkError):
        framework.score([tmp_path / "missing-result.json"])


def test_measurement_audit_requires_standard_and_stronger_profiles(tmp_path: Path) -> None:
    framework = _framework()
    single = tmp_path / "single.json"
    single.write_text(json.dumps({"measurements": {"standard": {}}}), encoding="utf-8")
    both = tmp_path / "both.json"
    both.write_text(
        json.dumps({"measurements": {"standard": {}, "stronger": {}}}),
        encoding="utf-8",
    )
    assert framework._has_two_recipe_measurement([{"path": str(single), "exists": True}]) is False
    assert framework._has_two_recipe_measurement([{"path": str(both), "exists": True}]) is True


def test_repeat_compare_requires_two_results() -> None:
    framework = _framework()
    with pytest.raises(framework.FrameworkError, match="at least two"):
        framework.compare_repetitions([])


def test_third_packet_can_be_limited_to_decision_disagreements() -> None:
    framework = _framework()
    packet = framework.make_packet("A", 3, case_ids=["g01", "o01"])
    assert packet["case_selection"] == "decision_disagreements_only"
    assert [item["case_id"] for item in packet["cases"]] == ["g01", "o01"]
    with pytest.raises(framework.FrameworkError, match="reserved"):
        framework.make_packet("A", 2, case_ids=["g01"])


def test_tiebreak_resolution_requires_first_two_repetitions() -> None:
    framework = _framework()
    with pytest.raises(framework.FrameworkError):
        framework.resolve_tiebreaks([])
