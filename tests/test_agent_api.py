from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import rtl_advisor.agent_api as agent_api
from rtl_advisor.agent_api import (
    AgentAPIError,
    _review_decision,
    agent_candidate,
    agent_capabilities,
    agent_review,
    agent_verify,
)
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_config(tmp_path: Path) -> ProjectConfig:
    tool = tmp_path / "fake-tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(test) {}\n", encoding="utf-8")
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator=str(tool),
            yosys=str(tool),
            codex=str(tool),
            timeout_seconds=5,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="test",
            path=liberty,
            url="https://example.invalid/cells.lib",
            sha256=_sha256(liberty),
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="test",
        ),
    )


def _fake_analysis(
    config: ProjectConfig,
    source: Path,
    *,
    decision: str = "recommend",
) -> tuple[dict, Path]:
    root = config.artifacts_dir / "designs" / "design-hash"
    root.mkdir(parents=True, exist_ok=True)
    design = {
        "schema_version": 2,
        "top": "top",
        "files": [{"path": str(source), "sha256": _sha256(source)}],
        "include_dirs": [],
        "defines": [],
        "filelists": [],
        "design_hash": "design-hash",
    }
    (root / "input.json").write_text(
        json.dumps(design, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    analysis = {
        "schema_version": 2,
        "flow_version": "rtl-advisor-calibrated-gate-v2",
        "design_hash": "design-hash",
        "profile": "balanced",
        "mode": "calibrated",
        "decision": decision,
        "selected_candidate_id": "candidate01" if decision == "recommend" else None,
        "candidates": [
            {
                "candidate_id": "candidate01",
                "finding_id": "finding01",
                "rank": 1,
                "transformation_id": "narrow_intermediate_width",
                "family": "width_signedness",
                "source": {"locations": [{"file": str(source), "start_line": 1}]},
                "eligible": decision == "recommend",
                "predicted_improvement_percent": {
                    "delay": {"estimate": 4.0, "lower": 3.0, "upper": 5.0},
                    "area": {"estimate": 1.0, "lower": 0.0, "upper": 2.0},
                    "cell_count": None,
                },
                "rejection_reasons": [],
            }
        ],
        "gate": {
            "status": "calibrated",
            "reason": "test",
            "model_hash": "model-hash",
        },
        "artifacts": {"root": str(root)},
    }
    analysis_path = root / "analysis-v2.json"
    analysis_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return analysis, analysis_path


def test_capabilities_reports_models_as_diagnostic_only(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    result = agent_capabilities(config)

    assert result["status"] == "ok"
    assert result["analysis"]["live_recommendation_ready"] is False
    assert result["operations"]["review"]["available"] is True
    assert result["operations"]["candidate_generation"]["available"] is False
    assert all(model["ready"] is False for model in result["models"])
    assert result["tools"]["yosys"]["status"] == "available"
    assert result["tools"]["liberty"]["status"] == "available"
    assert Path(result["artifacts"]["capabilities"]).is_file()


def test_review_decision_never_promotes_diagnostic_model() -> None:
    analysis = {
        "decision": "recommend",
        "gate": {"status": "calibrated"},
    }

    assert _review_decision(analysis, model_release_status="diagnostic_only") == (
        "blocked",
        "failed",
        "the installed analysis model is diagnostic-only",
    )
    assert _review_decision(analysis, model_release_status="ready")[1] == "recommended"
    assert _review_decision(
        {"decision": "unsupported", "gate": {"status": "blocked"}},
        model_release_status="ready",
    )[1] == "unsupported"


def test_review_is_deterministic_and_preserves_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _make_config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    original_hash = _sha256(source)

    def fake_analyze(*args, **kwargs):
        return _fake_analysis(config, source)

    monkeypatch.setattr(agent_api, "analyze_live_rtl", fake_analyze)

    first = agent_review(
        config,
        str(source),
        objective="balanced",
        top="top",
    )
    second = agent_review(
        config,
        str(source),
        objective="balanced",
        top="top",
    )

    assert first["status"] == "blocked"
    assert first["decision"] == "failed"
    assert first["candidate_generation_allowed"] is False
    assert first["run_id"] == second["run_id"]
    assert first["semantic_hash"] == second["semantic_hash"]
    assert first["input"]["source_integrity"]["ok"] is True
    assert _sha256(source) == original_hash
    with pytest.raises(AgentAPIError, match="not eligible"):
        agent_candidate(config, first["run_id"], finding_id="finding01")


def test_candidate_and_verify_delegate_through_versioned_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _make_config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    def fake_analyze(*args, **kwargs):
        return _fake_analysis(config, source)

    ready_registry = tuple(
        {**item, "release_status": "ready"}
        if item["model_id"] == "v2"
        else item
        for item in agent_api.MODEL_REGISTRY
    )
    monkeypatch.setattr(agent_api, "MODEL_REGISTRY", ready_registry)
    monkeypatch.setattr(agent_api, "analyze_live_rtl", fake_analyze)
    review = agent_review(
        config,
        str(source),
        objective="timing",
        top="top",
    )
    assert review["decision"] == "recommended"

    source.write_text("module top; wire changed = 1'b1; endmodule\n", encoding="utf-8")
    with pytest.raises(AgentAPIError, match="source hashes changed"):
        agent_candidate(
            config,
            review["run_id"],
            finding_id="finding01",
        )
    source.write_text("module top; endmodule\n", encoding="utf-8")

    candidate_root = tmp_path / "candidate-root"
    candidate_root.mkdir()
    diff_path = candidate_root / "candidate.diff"
    diff_path.write_text("test diff\n", encoding="utf-8")

    monkeypatch.setattr(
        agent_api,
        "emit_selected_candidate",
        lambda *args, **kwargs: {
            "status": "prepared",
            "candidate_id": "candidate01",
            "artifact_root": str(candidate_root),
            "diff_path": str(diff_path),
            "source_integrity": {"original": {"ok": True}},
        },
    )
    candidate = agent_candidate(
        config,
        review["run_id"],
        finding_id="finding01",
    )

    assert candidate["status"] == "prepared"
    assert candidate["safe"] is False
    assert Path(candidate["artifacts"]["candidate_record"]).is_file()

    monkeypatch.setattr(
        agent_api,
        "verify_emitted_candidate",
        lambda *args, **kwargs: {
            "safe": True,
            "candidate_design_hash": "candidate-hash",
            "artifact_root": str(candidate_root),
            "diff_path": str(diff_path),
            "source_integrity": {
                "original": {"ok": True},
                "candidate": {"ok": True},
            },
            "formal": {
                "status": "passed",
                "proof_semantic_hash": "proof-hash",
            },
        },
    )
    verification = agent_verify(
        config,
        review["run_id"],
        candidate_id="candidate01",
    )

    assert verification["status"] == "passed"
    assert verification["safe"] is True
    assert verification["formal"]["proof_semantic_hash"] == "proof-hash"
