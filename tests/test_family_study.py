from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from rtl_advisor.config import load_config
from rtl_advisor.family_study import (
    FamilyStudyError,
    compare_family_repeats,
    evaluate_family_gates,
    read_family_study,
    run_family_study,
    seal_family_study,
    validate_family_study,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples/corpus/arbiter_family_v1/family-study.json"


def _completed_evidence(study: dict, *, improved_pairs: int = 2) -> dict:
    pairs = []
    for pair_index, pair in enumerate(study["primary_cohort"]):
        configurations = []
        for configuration_index, configuration in enumerate(pair["parameter_matrix"]):
            improved = pair_index < improved_pairs and configuration_index == 0
            measurements = {
                "M0": {
                    "outcome": "improved" if improved else "neutral",
                    "normalized": {"area": 100, "delay": 1.0},
                },
                "M1": {
                    "outcome": "improved" if improved else "neutral",
                    "normalized": {"area": 100, "delay": 1.0},
                },
            }
            key = (pair["pair_id"], configuration["configuration_id"])
            selected = {
                (item["pair_id"], item["configuration_id"])
                for item in study["m2_selection"]["primary"]
            }
            if key in selected:
                measurements["M2"] = {
                    "outcome": "improved" if improved else "neutral",
                    "metrics": {"area": 100.0, "delay": 1.0},
                }
            configurations.append(
                {
                    "configuration_id": configuration["configuration_id"],
                    "formal_status": "formal_passed",
                    "formal_current": True,
                    "recommended": improved,
                    "measurements": measurements,
                }
            )
        pairs.append(
            {
                "pair_id": pair["pair_id"],
                "upstream_project_id": pair["upstream_project_id"],
                "formal_status": "formal_passed",
                "configurations": configurations,
            }
        )
    return {"pairs": pairs}


def test_frozen_family_study_is_valid_and_has_ten_distinct_pairs() -> None:
    study = read_family_study(MANIFEST)

    assert len(study["primary_cohort"]) == 10
    assert len({item["reference_id"] for item in study["primary_cohort"]}) == 10
    assert len({item["upstream_project_id"] for item in study["primary_cohort"]}) == 4
    assert len(study["m2_selection"]["primary"]) == 5
    assert study["freeze_policy"]["candidate_ppa_inspected"] is False


def test_family_manifest_rejects_tamper_prefreeze_ppa_and_duplicate_pair() -> None:
    study = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tampered = seal_family_study({**study, "claim_scope": "tampered"})
    tampered["semantic_hash"] = study["semantic_hash"]
    with pytest.raises(FamilyStudyError) as hash_error:
        validate_family_study(tampered)
    assert hash_error.value.code == "artifact_hash_mismatch"

    ppa = dict(study)
    ppa["ppa"] = {"delay": 1.0}
    with pytest.raises(FamilyStudyError) as ppa_error:
        validate_family_study(seal_family_study(ppa))
    assert ppa_error.value.code == "ppa_visible_before_freeze"

    duplicate = json.loads(MANIFEST.read_text(encoding="utf-8"))
    duplicate["primary_cohort"][1]["pair_id"] = duplicate["primary_cohort"][0][
        "pair_id"
    ]
    with pytest.raises(FamilyStudyError) as duplicate_error:
        validate_family_study(seal_family_study(duplicate))
    assert duplicate_error.value.code == "duplicate_pair"


def test_repeatability_requires_exact_m0_m1_and_two_percent_m2() -> None:
    study = read_family_study(MANIFEST)
    first = _completed_evidence(study)
    second = json.loads(json.dumps(first))

    reproduced = compare_family_repeats(first, second)
    assert reproduced["status"] == "passed"
    assert reproduced["rate"] == 1.0

    second["pairs"][0]["configurations"][0]["measurements"]["M0"]["normalized"][
        "area"
    ] = 101
    second["pairs"][0]["configurations"][0]["measurements"]["M2"] = {
        "outcome": "improved",
        "metrics": {"area": 103.0, "delay": 1.0},
    }
    mismatch = compare_family_repeats(first, second)
    assert mismatch["rate"] < 1.0


def test_product_preview_gate_is_stronger_than_family_gate() -> None:
    study = read_family_study(MANIFEST)
    evidence = _completed_evidence(study, improved_pairs=0)
    reproduction = {"rate": 1.0}

    no_win = evaluate_family_gates(study, evidence, reproduction)
    assert no_win["family_credibility"]["status"] == "failed"
    assert no_win["product_preview"]["status"] == "study_only"

    evidence = _completed_evidence(study, improved_pairs=2)
    # Make two pre-registered M2 samples confirm two different lineages.
    for pair_index in (0, 2):
        pair = evidence["pairs"][pair_index]
        selected = next(
            item
            for item in study["m2_selection"]["primary"]
            if item["pair_id"] == pair["pair_id"]
        )
        configuration = next(
            item
            for item in pair["configurations"]
            if item["configuration_id"] == selected["configuration_id"]
        )
        configuration["recommended"] = True
        for level in ("M0", "M1"):
            configuration["measurements"][level]["outcome"] = "improved"
        configuration["measurements"]["M2"]["outcome"] = "improved"
    passed = evaluate_family_gates(study, evidence, reproduction)
    assert passed["family_credibility"]["status"] == "passed"
    assert passed["product_preview"]["status"] == "supported_preview"


def test_study_run_records_all_pairs_without_pretending_measurement(
    tmp_path: Path,
) -> None:
    config = replace(
        load_config(ROOT / "rtl-advisor.toml"),
        artifacts_dir=tmp_path / "artifacts",
    )

    evidence = run_family_study(config, MANIFEST, repeat_id="repeat-1")

    assert len(evidence["pairs"]) == 10
    assert evidence["summary"]["ppa_inspected"] is False
    assert all(
        configuration["measurements"] == {}
        for pair in evidence["pairs"]
        for configuration in pair["configurations"]
    )
