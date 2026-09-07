from __future__ import annotations

import json
from pathlib import Path

import pytest

import rtl_advisor.evidence_explorer as explorer
from rtl_advisor.cli import _normalized_agent_command, build_parser
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.mvp_schema import read_hashed_json, stable_hash


RUN_ID = "mvp-" + "1" * 20
CANDIDATE_ID = "candidate-1"
WORKFLOW_ID = "workflow-" + "2" * 20


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys="yosys",
            codex="codex",
            timeout_seconds=5,
        ),
        synthesis=SynthesisConfig(driving_cell="BUF_X1", output_load_ff=10.0),
        liberty=LibertyConfig(
            name="test",
            path=tmp_path / "cells.lib",
            url="https://example.invalid/cells.lib",
            sha256="a" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="test",
        ),
    )


def _row(config: ProjectConfig, profile: str, classification: str) -> dict:
    return {
        "row_id": f"{RUN_ID}:{CANDIDATE_ID}:{profile}",
        "run_id": RUN_ID,
        "candidate_id": CANDIDATE_ID,
        "top": "top",
        "objective": "timing",
        "transformation_id": "adder_reduction_association",
        "profile": profile,
        "decision": "regression",
        "classification": classification,
        "classification_reason": "Recorded rule result.",
        "candidate_decision_reason": "At least one profile regressed.",
        "formal_status": "formal_passed",
        "safe": True,
        "delay_improvement_percent": -3.0 if classification == "regressed" else 0.0,
        "area_improvement_percent": 0.0,
        "cell_count_improvement_percent": 0.0,
        "recipe_hash": "3" * 64,
        "measurement_semantic_hash": "4" * 64,
        "artifact_path": str(
            config.artifacts_dir
            / "agent-v2/runs"
            / RUN_ID
            / "candidates"
            / CANDIDATE_ID
            / "measurement.json"
        ),
        "limitations": ["Pinned recipes only."],
        "source_kind": "agent_v2_run",
    }


def _analytics(config: ProjectConfig, *, invalid: list[dict] | None = None) -> dict:
    return {
        "metric_definitions": {
            "delay_improvement_percent": "Percent delay reduction."
        },
        "classification_policies": {"timing": {"label": "Timing"}},
        "measurements": [
            _row(config, "standard", "neutral"),
            _row(config, "stronger", "regressed"),
        ],
        "invalid": invalid or [],
    }


def _install_analytics(
    monkeypatch: pytest.MonkeyPatch,
    config: ProjectConfig,
    *,
    invalid: list[dict] | None = None,
) -> None:
    payload = _analytics(config, invalid=invalid)

    class Store:
        def __init__(self, supplied: ProjectConfig):
            assert supplied == config

        def analytics(self) -> dict:
            return payload

    monkeypatch.setattr(explorer, "FrontendDataStore", Store)
    monkeypatch.setattr(
        explorer,
        "_workflow_bindings",
        lambda supplied: ({RUN_ID: WORKFLOW_ID}, [], {WORKFLOW_ID}),
    )


def test_exploration_keeps_profile_classification_and_candidate_decision_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _install_analytics(monkeypatch, config)

    result = explorer.explore_evidence(
        config,
        workflow_ids=(WORKFLOW_ID,),
        normalized_command=("rtl-advisor", "agent", "evidence", "explore"),
    )

    assert result["status"] == "ready"
    assert result["read_only"] is True
    assert result["summary"] == {
        "evidence_group_count": 1,
        "workflow_count": 1,
        "run_count": 1,
        "measured_candidate_count": 1,
        "profile_observation_count": 2,
        "formal_safe_observation_count": 2,
        "invalid_source_count": 0,
        "decision_counts": {"regression": 1},
        "classification_counts": {"neutral": 1, "regressed": 1},
    }
    dataset = read_hashed_json(Path(result["artifacts"]["dataset"]))
    assert [row["classification"] for row in dataset["measurements"]] == [
        "neutral",
        "regressed",
    ]
    assert {row["decision"] for row in dataset["measurements"]} == {"regression"}
    assert all(row["safe"] is True for row in dataset["measurements"])
    assert result["dataset_semantic_hash"] == dataset["semantic_hash"]
    assert result["semantic_hash"] == stable_hash(
        {key: value for key, value in result.items() if key != "semantic_hash"}
    )


def test_exploration_filters_points_but_counts_candidates_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _install_analytics(monkeypatch, config)

    result = explorer.explore_evidence(
        config,
        profiles=("stronger",),
        classifications=("regressed",),
    )

    assert result["summary"]["profile_observation_count"] == 1
    assert result["summary"]["measured_candidate_count"] == 1
    assert result["summary"]["decision_counts"] == {"regression": 1}
    assert result["summary"]["classification_counts"] == {"regressed": 1}


def test_exploration_is_idempotent_for_identical_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _install_analytics(monkeypatch, config)

    first = explorer.explore_evidence(config)
    second = explorer.explore_evidence(config)

    assert second == first


def test_invalid_sources_are_excluded_and_make_result_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _install_analytics(
        monkeypatch,
        config,
        invalid=[{"run_id": "mvp-" + "9" * 20, "error": "hash mismatch"}],
    )

    result = explorer.explore_evidence(config)

    assert result["status"] == "partial"
    assert result["summary"]["invalid_source_count"] == 1
    assert explorer.evidence_exit_code(result) == 4
    dataset = json.loads(Path(result["artifacts"]["dataset"]).read_text())
    assert dataset["invalid_sources"][0]["message"] == "hash mismatch"


def test_unsafe_measurement_row_is_never_charted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    payload = _analytics(config)
    payload["measurements"][0]["safe"] = False

    class Store:
        def __init__(self, supplied: ProjectConfig):
            pass

        def analytics(self) -> dict:
            return payload

    monkeypatch.setattr(explorer, "FrontendDataStore", Store)
    monkeypatch.setattr(explorer, "_workflow_bindings", lambda supplied: ({}, [], set()))

    with pytest.raises(explorer.EvidenceExplorerError) as error:
        explorer.explore_evidence(config)

    assert error.value.code == "formal_pass_required"


def test_exploration_schema_documents_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in (
        "rtl-advisor-evidence-exploration-v1.schema.json",
        "rtl-advisor-evidence-chart-data-v1.schema.json",
    ):
        schema = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert "semantic_hash" in schema["required"]


def test_cli_normalizes_compact_evidence_filters(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output = tmp_path / "exploration"
    args = build_parser().parse_args(
        [
            "--config",
            str(config.config_path),
            "agent",
            "evidence",
            "explore",
            "--workflow-id",
            WORKFLOW_ID,
            "--profile",
            "stronger",
            "--classification",
            "regressed",
            "--output-dir",
            str(output),
            "--schema-version",
            "1",
            "--json",
        ]
    )

    command = _normalized_agent_command(config, args)

    assert command == (
        "rtl-advisor",
        "--config",
        str(config.config_path),
        "agent",
        "evidence",
        "explore",
        "--workflow-id",
        WORKFLOW_ID,
        "--profile",
        "stronger",
        "--classification",
        "regressed",
        "--output-dir",
        str(output.resolve()),
        "--schema-version",
        "1",
        "--json",
    )
