from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.frontend_api import FrontendAPIError, FrontendDataStore
import rtl_advisor.frontend_server as frontend_server
import rtl_advisor.mvp_agent as mvp_agent
from rtl_advisor.mvp_schema import compile_context_snapshot, write_hashed_json
from rtl_advisor.rtl_input import DesignInputV2, SourceFileV2


RUN_ID = "mvp-0123456789abcdefabcd"
CANDIDATE_ID = "addcand_0123456789abcdef"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path) -> ProjectConfig:
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(test) {}\n", encoding="utf-8")
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys="yosys",
            codex="codex",
            timeout_seconds=10,
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


def _agent_record(kind: str, *, status: str, **values) -> dict:
    return {
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": f"rtl-advisor.agent.v2.{kind}",
        "flow_version": "rtl-advisor-agent-v2",
        "status": status,
        "run_id": RUN_ID,
        "command": ["rtl-advisor", "agent", kind, "--schema-version", "2", "--json"],
        **values,
    }


def _input_record_payload(source: Path, *, design_hash: str) -> dict:
    design = DesignInputV2(
        schema_version=2,
        top="top",
        files=(SourceFileV2(path=str(source), sha256=_sha256(source)),),
        include_dirs=(),
        defines=(),
        filelists=(),
        design_hash=design_hash,
    )
    return {
        "schema_version": 1,
        "document_type": "rtl-advisor.run.design-input",
        "design_input_schema_version": 2,
        "top": "top",
        "files": [{"path": str(source), "sha256": _sha256(source)}],
        "include_dirs": [],
        "defines": [],
        "filelists": [],
        "design_hash": design_hash,
        "compile_context": compile_context_snapshot(design),
    }


def _make_completed_run(config: ProjectConfig) -> Path:
    root = config.artifacts_dir / "agent-v2/runs" / RUN_ID
    source = config.root / "top.sv"
    source.write_text(
        "module top(input [7:0] a,b,c, output [7:0] y); assign y=(a+b)+c; endmodule\n",
        encoding="utf-8",
    )
    input_record = write_hashed_json(
        root / "input.json",
        _input_record_payload(source, design_hash="a" * 64),
    )
    finding = {
        "finding_id": "addsite_0123456789abcdef",
        "transformation_id": "adder_reduction_association",
        "reason": "unsigned equal-width addition chain can be balanced",
        "source": {"file": str(source), "line": 1, "column": 55},
        "original_expression": "(a+b)+c",
        "replacement_expression": "a+(b+c)",
    }
    review = write_hashed_json(
        root / "review.json",
        _agent_record(
            "review",
            status="completed",
            decision="candidate_available",
            objective="timing",
            input={
                "top": "top",
                "files": [{"path": str(source), "sha256": _sha256(source)}],
                "source_integrity": {"ok": True, "mismatches": []},
            },
            findings=[finding],
            evidence={"input_semantic_hash": input_record["semantic_hash"]},
            limitations=[],
        ),
    )
    candidate_root = root / "candidates" / CANDIDATE_ID
    diff = candidate_root / "candidate.diff"
    diff.parent.mkdir(parents=True, exist_ok=True)
    diff.write_text("-assign y=(a+b)+c;\n+assign y=a+(b+c);\n", encoding="utf-8")
    candidate_source = candidate_root / "design" / "top.sv"
    candidate_source.parent.mkdir(parents=True, exist_ok=True)
    candidate_source.write_text(
        "module top(input [7:0] a,b,c, output [7:0] y); assign y=a+(b+c); endmodule\n",
        encoding="utf-8",
    )
    prepared = write_hashed_json(
        candidate_root / "candidate-core.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.candidate",
            "run_schema": "rtl-advisor-run-v1",
            "candidate_id": CANDIDATE_ID,
            "finding_id": finding["finding_id"],
            "candidate_design": {
                "schema_version": 2,
                "top": "top",
                "files": [
                    {"path": str(candidate_source), "sha256": _sha256(candidate_source)}
                ],
                "include_dirs": [],
                "defines": [],
                "filelists": [],
                "design_hash": "c" * 64,
            },
            "diff_path": str(diff),
            "diff_sha256": _sha256(diff),
        },
    )
    candidate = write_hashed_json(
        candidate_root / "candidate.json",
        _agent_record(
            "candidate",
            status="candidate_prepared",
            decision="candidate_prepared",
            candidate_id=CANDIDATE_ID,
            finding=finding,
            candidate=prepared,
            parents={"review_semantic_hash": review["semantic_hash"]},
            artifacts={"diff": str(diff)},
        ),
    )
    verification = write_hashed_json(
        candidate_root / "verification.json",
        _agent_record(
            "verification",
            status="formal_passed",
            decision="formal_passed",
            candidate_id=CANDIDATE_ID,
            safe=True,
            formal={"status": "passed", "backend": "yosys-equivalence"},
            parents={"candidate_semantic_hash": candidate["semantic_hash"]},
        ),
    )
    profiles = {
        name: {
            "classification": "neutral",
            "recipe": {"recipe_hash": name * 8},
            "baseline": {
                "metrics": {
                    "critical_delay_ps": 100.0,
                    "area_total": 50.0,
                    "cell_count": 20,
                }
            },
            "candidate": {
                "metrics": {
                    "critical_delay_ps": 99.0,
                    "area_total": 50.0,
                    "cell_count": 20,
                }
            },
            "comparison": {
                "critical_delay_ps": {"baseline": 100.0, "candidate": 99.0, "improvement_percent": 1.0},
                "area_total": {"baseline": 50.0, "candidate": 50.0, "improvement_percent": 0.0},
                "cell_count": {"baseline": 20, "candidate": 20, "improvement_percent": 0.0},
            },
        }
        for name in ("standard", "stronger")
    }
    measurement = write_hashed_json(
        candidate_root / "measurement.json",
        _agent_record(
            "measurement",
            status="completed",
            decision="synthesis_handles",
            objective="timing",
            candidate_id=CANDIDATE_ID,
            formal={"status": "passed", "semantic_hash": verification["semantic_hash"]},
            measurements=profiles,
            parents={"verification_semantic_hash": verification["semantic_hash"]},
            limitations=["Yosys/ABC evidence only."],
        ),
    )
    completion = {
        "complete": True,
        "eligible_site_count": 1,
        "candidate_count": 1,
        "formal_count": 1,
        "measurement_count": 1,
        "terminal_candidate_count": 1,
        "missing_candidate_finding_ids": [],
        "missing_formal_candidate_ids": [],
        "missing_measurement_candidate_ids": [],
        "formal_status_counts": {"formal_passed": 1},
        "measurement_decision_counts": {"synthesis_handles": 1},
        "mixed_outcomes": False,
    }
    report_payload = _agent_record(
        "report",
        status="completed",
        decision="synthesis_handles",
        objective="timing",
        review=review,
        candidates=[
            {
                "candidate_id": CANDIDATE_ID,
                "candidate": candidate,
                "verification": verification,
                "measurement": measurement,
            }
        ],
        completion=completion,
        result_counts={
            "formal": {"formal_passed": 1},
            "synthesis": {"synthesis_handles": 1},
        },
        parents={
            "review_semantic_hash": review["semantic_hash"],
            "candidates": {
                CANDIDATE_ID: {
                    "candidate_semantic_hash": candidate["semantic_hash"],
                    "verification_semantic_hash": verification["semantic_hash"],
                    "measurement_semantic_hash": measurement["semantic_hash"],
                }
            },
        },
        limitations=["Yosys/ABC evidence only."],
    )
    report = write_hashed_json(root / "report.json", report_payload)
    snapshot_dir = root / "reports"
    snapshot = write_hashed_json(
        snapshot_dir / f"{report['semantic_hash']}.json", report
    )
    html_snapshot = snapshot_dir / f"{report['semantic_hash']}.html"
    html_snapshot.write_text(
        mvp_agent._render_report_html(report),
        encoding="utf-8",
    )
    write_hashed_json(
        snapshot_dir / "latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.run.report-latest",
            "run_id": RUN_ID,
            "report_semantic_hash": snapshot["semantic_hash"],
            "report_snapshot": str(snapshot_dir / f"{report['semantic_hash']}.json"),
            "html_snapshot": str(html_snapshot),
        },
    )
    return root


def _replace_measurement_with_failure(root: Path) -> dict:
    candidate_root = root / "candidates" / CANDIDATE_ID
    (candidate_root / "measurement.json").unlink()
    verification = json.loads(
        (candidate_root / "verification.json").read_text(encoding="utf-8")
    )
    failure_path = (
        candidate_root
        / "measurement-failures"
        / "0123456789abcdef.json"
    )
    failure = write_hashed_json(
        failure_path,
        _agent_record(
            "measurement-failure",
            status="failed",
            decision="measurement_failed",
            objective="timing",
            candidate_id=CANDIDATE_ID,
            error={
                "code": "missing_yosys",
                "message": "Pinned Yosys 0.63 is not available in the integration environment.",
            },
            parents={"verification_semantic_hash": verification["semantic_hash"]},
            artifacts={"failure": str(failure_path)},
        ),
    )

    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("semantic_hash")
    report["status"] = "incomplete"
    report["decision"] = "formal_passed"
    report["candidates"][0].pop("measurement")
    report["candidates"][0]["measurement_failures"] = [failure]
    report["completion"] = {
        "complete": False,
        "eligible_site_count": 1,
        "candidate_count": 1,
        "formal_count": 1,
        "measurement_count": 0,
        "terminal_candidate_count": 0,
        "missing_candidate_finding_ids": [],
        "missing_formal_candidate_ids": [],
        "missing_measurement_candidate_ids": [CANDIDATE_ID],
        "formal_status_counts": {"formal_passed": 1},
        "measurement_decision_counts": {},
        "mixed_outcomes": False,
    }
    report["result_counts"] = {
        "formal": {"formal_passed": 1},
        "synthesis": {},
    }
    report["parents"]["candidates"][CANDIDATE_ID][
        "measurement_semantic_hash"
    ] = None
    report = write_hashed_json(report_path, report)
    snapshot_dir = root / "reports"
    snapshot = write_hashed_json(
        snapshot_dir / f"{report['semantic_hash']}.json", report
    )
    html_snapshot = snapshot_dir / f"{report['semantic_hash']}.html"
    html_snapshot.write_text(
        mvp_agent._render_report_html(report),
        encoding="utf-8",
    )
    write_hashed_json(
        snapshot_dir / "latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.run.report-latest",
            "run_id": RUN_ID,
            "report_semantic_hash": snapshot["semantic_hash"],
            "report_snapshot": str(snapshot_dir / f"{report['semantic_hash']}.json"),
            "html_snapshot": str(html_snapshot),
        },
    )
    return failure


def _make_unsupported_run(config: ProjectConfig) -> Path:
    root = config.artifacts_dir / "agent-v2/runs" / RUN_ID
    source = config.root / "unsupported.sv"
    source.write_text("module top(input a, output y); assign y=a; endmodule\n")
    input_record = write_hashed_json(
        root / "input.json",
        _input_record_payload(source, design_hash="b" * 64),
    )
    write_hashed_json(
        root / "review.json",
        _agent_record(
            "review",
            status="completed",
            decision="unsupported",
            objective="balanced",
            input={
                "top": "top",
                "files": [{"path": str(source), "sha256": _sha256(source)}],
                "source_integrity": {"ok": True, "mismatches": []},
            },
            findings=[],
            evidence={"input_semantic_hash": input_record["semantic_hash"]},
            limitations=[],
        ),
    )
    return root


def test_runs_api_reads_complete_hash_linked_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _make_completed_run(config)
    store = FrontendDataStore(config)

    listing = store.runs()
    detail = store.run_detail(RUN_ID)
    diff = store.run_diff(RUN_ID)
    artifacts = store.run_artifacts(RUN_ID)

    assert listing["run_schema"] == "rtl-advisor-run-v1"
    assert listing["count"] == 1
    assert listing["invalid"] == []
    assert detail["run"]["decision"] == "synthesis_handles"
    assert detail["run"]["state"] == "completed"
    assert [stage["status"] for stage in detail["run"]["stages"]] == [
        "complete", "complete", "complete", "complete", "complete"
    ]
    assert diff["items"][0]["content"].startswith("-assign")
    assert any(item["path"] == "report.json" for item in artifacts["items"])
    assert {item["stage"] for item in artifacts["commands"]} >= {
        "review", "candidate", "verify", "measure", "report"
    }


def test_runs_api_presents_hash_linked_synthesis_failure_reason(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    failure = _replace_measurement_with_failure(root)

    detail = FrontendDataStore(config).run_detail(RUN_ID)["run"]
    candidate = detail["candidates"][0]

    assert detail["state"] == "incomplete"
    assert candidate["status"] == "measurement_failed"
    assert candidate["measurement"] is None
    assert candidate["measurement_failures"][0]["semantic_hash"] == failure[
        "semantic_hash"
    ]
    assert candidate["measurement_failures"][0]["error"] == {
        "code": "missing_yosys",
        "message": "Pinned Yosys 0.63 is not available in the integration environment.",
    }
    assert detail["stages"][3]["status"] == "failed"


def test_runs_api_covers_empty_unsupported_and_in_progress_states(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = FrontendDataStore(config)
    assert store.runs()["count"] == 0

    _make_unsupported_run(config)
    unsupported = store.run_detail(RUN_ID)["run"]
    assert unsupported["state"] == "unsupported"
    assert unsupported["decision"] == "unsupported"
    assert [stage["status"] for stage in unsupported["stages"]] == [
        "complete", "unavailable", "unavailable", "unavailable", "complete"
    ]

    # Replace the fixture with a candidate that has not reached formal yet.
    import shutil

    shutil.rmtree(config.artifacts_dir / "agent-v2/runs" / RUN_ID)
    root = _make_completed_run(config)
    (root / "report.json").unlink()
    candidate_root = root / "candidates" / CANDIDATE_ID
    (candidate_root / "measurement.json").unlink()
    (candidate_root / "verification.json").unlink()
    active = store.run_detail(RUN_ID)["run"]
    assert active["state"] == "in_progress"
    assert active["decision"] == "candidate_prepared"
    assert [stage["status"] for stage in active["stages"]] == [
        "complete", "complete", "active", "pending", "pending"
    ]


def test_runs_api_covers_failed_formal_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    (root / "report.json").unlink()
    candidate_root = root / "candidates" / CANDIDATE_ID
    (candidate_root / "measurement.json").unlink()
    candidate = json.loads((candidate_root / "candidate.json").read_text())
    write_hashed_json(
        candidate_root / "verification.json",
        _agent_record(
            "verification",
            status="formal_failed",
            decision="formal_failed",
            candidate_id=CANDIDATE_ID,
            safe=False,
            formal={"status": "failed", "backend": "yosys-equivalence"},
            parents={"candidate_semantic_hash": candidate["semantic_hash"]},
        ),
    )

    failed = FrontendDataStore(config).run_detail(RUN_ID)["run"]
    assert failed["state"] == "failed"
    assert failed["decision"] == "formal_failed"
    assert [stage["status"] for stage in failed["stages"]] == [
        "complete", "complete", "failed", "blocked", "failed"
    ]


def test_runs_api_hides_corrupt_run_and_rejects_detail(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    review_path = root / "review.json"
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    payload["decision"] = "unsupported"
    review_path.write_text(json.dumps(payload), encoding="utf-8")
    store = FrontendDataStore(config)

    listing = store.runs()

    assert listing["count"] == 0
    assert listing["invalid"][0]["run_id"] == RUN_ID
    with pytest.raises(FrontendAPIError, match="semantic hash mismatch"):
        store.run_detail(RUN_ID)


def test_runs_api_rejects_report_that_disagrees_with_stages(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("semantic_hash")
    report["decision"] = "measured_improvement"
    write_hashed_json(report_path, report)

    with pytest.raises(FrontendAPIError, match="does not match"):
        FrontendDataStore(config).run_detail(RUN_ID)


def test_runs_server_starts_without_research_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _make_completed_run(config)
    assert not (config.artifacts_dir / "models/v22").exists()

    class FakeServer:
        def __init__(self, address, config_arg):
            self.server_address = address
            self.data_store = FrontendDataStore(config_arg)

        def server_close(self):
            return None

    monkeypatch.setattr(frontend_server, "FrontendHTTPServer", FakeServer)
    server = frontend_server.create_frontend_server(config, port=0)

    assert server.data_store.runs()["count"] == 1


def test_runs_api_marks_missing_eligible_site_as_incomplete(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "multi.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y,z);\n"
        " assign y=a+b+c+d;\n"
        " assign z=d+c+b+a;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    review = mvp_agent.agent_v2_review(
        config, str(source), top="top", objective="balanced"
    )
    assert len(review["findings"]) == 2
    mvp_agent.agent_v2_candidate(
        config,
        review["run_id"],
        finding_id=review["findings"][0]["finding_id"],
    )
    mvp_agent.agent_v2_report(config, review["run_id"])

    run = FrontendDataStore(config).run_detail(review["run_id"])["run"]

    assert run["state"] == "incomplete"
    assert run["completion"]["complete"] is False
    assert run["completion"]["eligible_site_count"] == 2
    assert run["completion"]["candidate_count"] == 1
    assert len(run["completion"]["missing_candidate_finding_ids"]) == 1
    assert run["stages"][-1]["status"] == "pending"


def test_runs_api_mixed_result_logic_prioritizes_regression() -> None:
    records = {
        "review": {
            "decision": "candidate_available",
            "findings": [
                {"finding_id": "site_a"},
                {"finding_id": "site_b"},
            ],
        },
        "report": None,
        "candidates": [
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
        ],
    }

    summary = FrontendDataStore._evidence_summary(records)

    assert summary["complete"] is True
    assert summary["mixed_outcomes"] is True
    assert FrontendDataStore._decision(records) == "regression"


def test_runs_api_rejects_rehashed_candidate_parent_tampering(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    candidate_path = root / "candidates" / CANDIDATE_ID / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["parents"]["review_semantic_hash"] = "0" * 64
    write_hashed_json(candidate_path, candidate)

    with pytest.raises(FrontendAPIError, match="review parent hash mismatch"):
        FrontendDataStore(config).run_detail(RUN_ID)


def test_runs_api_rejects_tampered_candidate_diff(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    diff = root / "candidates" / CANDIDATE_ID / "candidate.diff"
    diff.write_text("tampered\n", encoding="utf-8")
    store = FrontendDataStore(config)

    listing = store.runs()

    assert listing["count"] == 0
    assert "diff hash mismatch" in listing["invalid"][0]["error"]
    with pytest.raises(FrontendAPIError, match="diff hash mismatch"):
        store.run_detail(RUN_ID)


def test_runs_api_rejects_tampered_report_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    snapshot = root / "reports" / f"{report['semantic_hash']}.json"
    snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
    snapshot_payload["decision"] = "measured_improvement"
    snapshot.write_text(json.dumps(snapshot_payload), encoding="utf-8")

    with pytest.raises(FrontendAPIError, match="report snapshot"):
        FrontendDataStore(config).run_detail(RUN_ID)


def test_runs_api_invalidates_completed_run_when_baseline_source_changes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    (tmp_path / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    store = FrontendDataStore(config)

    listing = store.runs()

    assert listing["count"] == 0
    assert listing["invalid"][0]["run_id"] == RUN_ID
    assert "baseline source hashes changed" in listing["invalid"][0]["error"]
    with pytest.raises(FrontendAPIError, match="baseline source hashes changed"):
        store.run_detail(RUN_ID)
    assert root.is_dir()


def test_runs_api_invalidates_run_when_include_tree_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    include_dir = tmp_path / "include"
    include_dir.mkdir()
    header = include_dir / "width.svh"
    header.write_text("`define WIDTH 8\n", encoding="utf-8")
    source = tmp_path / "with_include.sv"
    source.write_text(
        "`include \"width.svh\"\n"
        "module top(input logic [`WIDTH-1:0] a,b,c, output logic [`WIDTH-1:0] y);\n"
        " assign y = a + b + c;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    review = mvp_agent.agent_v2_review(
        config,
        str(source),
        top="top",
        objective="timing",
        include_dirs=(str(include_dir),),
    )
    header.write_text("`define WIDTH 16\n", encoding="utf-8")

    with pytest.raises(FrontendAPIError, match="compile context changed"):
        FrontendDataStore(config).run_detail(review["run_id"])


def test_runs_api_rederives_recipe_classification_from_metrics(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    (root / "report.json").unlink()
    measurement_path = root / "candidates" / CANDIDATE_ID / "measurement.json"
    measurement = json.loads(measurement_path.read_text(encoding="utf-8"))
    measurement.pop("semantic_hash")
    measurement["decision"] = "measured_improvement"
    measurement["measurements"]["standard"]["classification"] = "improved"
    measurement["measurements"]["stronger"]["classification"] = "improved"
    write_hashed_json(measurement_path, measurement)

    with pytest.raises(FrontendAPIError, match="classification does not match"):
        FrontendDataStore(config).run_detail(RUN_ID)


def test_runs_api_rejects_measurement_objective_mismatch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    (root / "report.json").unlink()
    measurement_path = root / "candidates" / CANDIDATE_ID / "measurement.json"
    measurement = json.loads(measurement_path.read_text(encoding="utf-8"))
    measurement.pop("semantic_hash")
    measurement["objective"] = "area"
    write_hashed_json(measurement_path, measurement)

    with pytest.raises(FrontendAPIError, match="objective does not match"):
        FrontendDataStore(config).run_detail(RUN_ID)


def test_runs_api_rejects_tampered_static_html_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = _make_completed_run(config)
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    html_snapshot = root / "reports" / f"{report['semantic_hash']}.html"
    html_snapshot.write_text("<!doctype html><title>tampered</title>\n", encoding="utf-8")

    with pytest.raises(FrontendAPIError, match="HTML content mismatch"):
        FrontendDataStore(config).run_detail(RUN_ID)


def test_research_case_source_rejects_symlink_escape(tmp_path: Path) -> None:
    config = _config(tmp_path)
    case_root = config.corpus_dir / "calibration-v2" / "v2_probe"
    case_root.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("private data\n", encoding="utf-8")
    (case_root / "baseline.sv").symlink_to(secret)
    (case_root / "manifest.json").write_text(
        json.dumps(
            {
                "baseline_id": "v0",
                "variants": [
                    {
                        "id": "v0",
                        "file": "baseline.sv",
                        "kernel_top": "top",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FrontendAPIError, match="unsafe RTL path"):
        FrontendDataStore(config)._case_rtl("v2_probe")
