from __future__ import annotations

import hashlib
import json
from pathlib import Path

import rtl_advisor.mvp_agent as mvp_agent
import pytest
from rtl_advisor.config import LibertyConfig, ProjectConfig, SynthesisConfig, ToolConfig


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path) -> ProjectConfig:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(test) {}\n", encoding="utf-8")
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(verilator=str(tool), yosys=str(tool), codex=str(tool), timeout_seconds=5),
        synthesis=SynthesisConfig(driving_cell="BUF_X1", output_load_ff=10.0),
        liberty=LibertyConfig(
            name="test", path=liberty, url="https://example.invalid/lib", sha256=_sha256(liberty),
            license_path=tmp_path / "LICENSE", license_url="https://example.invalid/license", source_commit="test",
        ),
    )


def test_v2_review_is_rules_only_and_candidate_available(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top(input [7:0] a,b,c, output [7:0] y); assign y=a+b+c; endmodule\n")
    monkeypatch.setattr(
        "rtl_advisor.mvp_rewriter.scan_addition_sites",
        lambda design: [{"finding_id": "add-1", "transformation_id": "adder_reduction_association"}],
    )

    result = mvp_agent.agent_v2_review(
        _config(tmp_path), str(source), top="top", objective="timing"
    )

    assert result["schema_version"] == 2
    assert result["decision"] == "candidate_available"
    assert result["evidence"]["model_used"] is False
    assert result["input"]["source_integrity"]["ok"] is True


def test_v2_report_does_not_modify_prior_records(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top(input [7:0] a,b,c, output [7:0] y); assign y=a+b+c; endmodule\n")
    monkeypatch.setattr(
        "rtl_advisor.mvp_rewriter.scan_addition_analysis",
        lambda design: {"findings": [], "exclusions": []},
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(config, str(source), top="top", objective="balanced")
    review_path = Path(review["artifacts"]["review"])
    before = review_path.read_bytes()

    report = mvp_agent.agent_v2_report(config, review["run_id"])

    assert report["decision"] == "unsupported"
    assert Path(report["artifacts"]["html"]).is_file()
    latest = json.loads(
        (Path(report["artifacts"]["snapshots"]) / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["html_sha256"] == _sha256(Path(latest["html_snapshot"]))
    assert report["artifacts"]["html_sha256"] == latest["html_sha256"]
    assert review_path.read_bytes() == before


def test_v2_candidate_stage_is_append_only(tmp_path: Path) -> None:
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c+d; endmodule\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config,
        str(source),
        top="top",
        objective="balanced",
        normalized_command=("review",),
    )
    finding_id = review["findings"][0]["finding_id"]
    first = mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=finding_id,
        normalized_command=("candidate",),
    )

    assert mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=finding_id,
        normalized_command=("candidate",),
    ) == first
    with pytest.raises(mvp_agent.MVPAgentError) as error:
        mvp_agent.agent_v2_candidate(
            config,
            review["run_id"],
            finding_id=finding_id,
            normalized_command=("different-client-command",),
        )
    assert error.value.code == "append_only_conflict"


def test_v2_report_rejects_changed_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "rtl_advisor.mvp_rewriter.scan_addition_analysis",
        lambda design: {"findings": [], "exclusions": []},
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    source.write_text("module top; endmodule\n", encoding="utf-8")

    with pytest.raises(mvp_agent.MVPAgentError) as error:
        mvp_agent.agent_v2_report(config, review["run_id"])

    assert error.value.code == "stale_source_hashes"


def test_v2_capabilities_match_actual_stage_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        mvp_agent,
        "_pyslang_status",
        lambda: {"status": "missing", "path": None},
    )
    monkeypatch.setattr(
        mvp_agent,
        "_command_status",
        lambda command: {
            "status": "missing",
            "configured_command": command,
            "path": None,
        },
    )
    monkeypatch.setattr(
        mvp_agent,
        "_yosys_status",
        lambda config_arg: {
            "status": "available",
            "version": "Yosys 0.63",
            "version_status": "matched",
            "abc_status": "available",
            "path": "/tool/yosys",
        },
    )

    result = mvp_agent.agent_v2_capabilities(config)

    assert result["operations"]["review"]["available"] is True
    assert result["operations"]["candidate"]["available"] is True
    assert result["operations"]["verify"]["available"] is False
    assert result["operations"]["measure"]["available"] is True


def test_v2_verify_never_promotes_low_level_unsafe_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c+d; endmodule\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    candidate = mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][0]["finding_id"],
    )
    monkeypatch.setattr(
        "rtl_advisor.mvp_rewriter.verify_addition_candidate",
        lambda *args, **kwargs: {
            "status": "formal_passed",
            "safe": False,
            "formal": {"status": "passed", "backend": "test"},
        },
    )

    result = mvp_agent.agent_v2_verify(
        config, review["run_id"], candidate_id=candidate["candidate_id"]
    )

    assert result["status"] == "formal_inconclusive"
    assert result["safe"] is False


def test_v2_verify_rejects_rehashed_candidate_with_stale_parent(
    tmp_path: Path,
) -> None:
    from rtl_advisor.mvp_schema import write_hashed_json

    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c+d; endmodule\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    candidate = mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][0]["finding_id"],
    )
    candidate_path = Path(candidate["artifacts"]["candidate_record"])
    tampered = json.loads(candidate_path.read_text(encoding="utf-8"))
    tampered["parents"]["review_semantic_hash"] = "0" * 64
    write_hashed_json(candidate_path, tampered)

    with pytest.raises(mvp_agent.MVPAgentError) as error:
        mvp_agent.agent_v2_verify(
            config, review["run_id"], candidate_id=candidate["candidate_id"]
        )

    assert error.value.code == "artifact_parent_mismatch"


def test_v2_measure_rejects_rehashed_verification_with_stale_parent(
    tmp_path: Path,
) -> None:
    from rtl_advisor.mvp_schema import write_hashed_json

    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c+d; endmodule\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    candidate = mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][0]["finding_id"],
    )
    candidate_root = Path(candidate["artifacts"]["root"])
    write_hashed_json(
        candidate_root / "verification.json",
        mvp_agent._agent_record(
            document_type="rtl-advisor.agent.v2.verification",
            status="formal_passed",
            command=(),
            run_id=review["run_id"],
            decision="formal_passed",
            candidate_id=candidate["candidate_id"],
            safe=True,
            formal={"status": "passed", "backend": "test"},
            parents={"candidate_semantic_hash": "0" * 64},
        ),
    )

    with pytest.raises(mvp_agent.MVPAgentError) as error:
        mvp_agent.agent_v2_measure(
            config, review["run_id"], candidate_id=candidate["candidate_id"]
        )

    assert error.value.code == "artifact_parent_mismatch"


def test_v2_aggregate_never_hides_regression_or_partial_mixed_evidence() -> None:
    review = {
        "decision": "candidate_available",
        "findings": [
            {"finding_id": "site_a"},
            {"finding_id": "site_b"},
        ],
    }
    records = [
        {
            "candidate_id": "candidate_a",
            "candidate": {"finding": {"finding_id": "site_a"}},
            "verification": {"status": "formal_passed"},
            "measurement": {"decision": "measured_improvement"},
        },
        {
            "candidate_id": "candidate_b",
            "candidate": {"finding": {"finding_id": "site_b"}},
            "verification": {"status": "formal_passed"},
            "measurement": {"decision": "regression"},
        },
    ]
    completion = mvp_agent._evidence_summary(records, review)

    assert completion["complete"] is True
    assert completion["mixed_outcomes"] is True
    assert completion["measurement_decision_counts"] == {
        "measured_improvement": 1,
        "regression": 1,
    }
    assert mvp_agent._overall_decision(records, review, completion) == "regression"

    records.pop()
    incomplete = mvp_agent._evidence_summary(records, review)
    assert incomplete["complete"] is False
    assert incomplete["missing_candidate_finding_ids"] == ["site_b"]
    assert mvp_agent._overall_decision(records, review, incomplete) == "incomplete"


def test_v2_report_publishes_missing_eligible_site_and_immutable_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y,z);\n"
        " assign y=a+b+c+d;\n"
        " assign z=d+c+b+a;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    assert len(review["findings"]) == 2
    mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][0]["finding_id"],
    )

    report = mvp_agent.agent_v2_report(config, review["run_id"])

    assert report["status"] == "incomplete"
    assert report["decision"] == "candidate_prepared"
    assert report["completion"]["complete"] is False
    assert report["completion"]["eligible_site_count"] == 2
    assert len(report["completion"]["missing_candidate_finding_ids"]) == 1
    snapshot = (
        Path(report["artifacts"]["snapshots"])
        / f"{report['semantic_hash']}.json"
    )
    assert snapshot.is_file()
    assert json.loads(snapshot.read_text(encoding="utf-8")) == report


def test_v2_report_rejects_partial_candidate_directory(tmp_path: Path) -> None:
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    partial = (
        config.artifacts_dir
        / "agent-v2/runs"
        / review["run_id"]
        / "candidates/addcand_partial"
    )
    partial.mkdir(parents=True)

    with pytest.raises(mvp_agent.MVPAgentError) as error:
        mvp_agent.agent_v2_report(config, review["run_id"])

    assert error.value.code == "missing_candidate_evidence"


def test_v2_run_invalidates_changed_filelist_or_header_context(tmp_path: Path) -> None:
    include = tmp_path / "include"
    include.mkdir()
    header = include / "context.svh"
    header.write_text("`define UNUSED 1\n", encoding="utf-8")
    source = tmp_path / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c+d; endmodule\n",
        encoding="utf-8",
    )
    filelist = tmp_path / "sources.f"
    filelist.write_text("-I include\ntop.sv\n", encoding="utf-8")
    config = _config(tmp_path)
    review = mvp_agent.agent_v2_review(
        config, str(filelist), top="top", objective="balanced"
    )
    header.write_text("`define UNUSED 2\n", encoding="utf-8")

    with pytest.raises(mvp_agent.MVPAgentError) as error:
        mvp_agent.agent_v2_candidate(
            config,
            review["run_id"],
            finding_id=review["findings"][0]["finding_id"],
        )

    assert error.value.code == "stale_compile_context"
