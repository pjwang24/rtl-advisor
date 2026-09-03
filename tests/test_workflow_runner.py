from __future__ import annotations

import json
from pathlib import Path

import pytest

import rtl_advisor.workflow_runner as workflow_runner
from rtl_advisor.cli import _normalized_agent_command, build_parser
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.mvp_schema import file_sha256, stable_hash
from rtl_advisor.workflow_contract import (
    WorkflowContractError,
    build_workflow_authorization,
    build_workflow_request,
    validate_workflow_preparation,
)


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


def _agent_payload(document_type: str, status: str, **values: object) -> dict:
    payload = {
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "flow_version": "rtl-advisor-agent-v2",
        "document_type": document_type,
        "status": status,
        **values,
        "command": ["rtl-advisor", "agent", document_type.rsplit(".", 1)[-1]],
    }
    payload["semantic_hash"] = stable_hash(payload)
    return payload


def _documents(
    tmp_path: Path, *, authorized_through: str
) -> tuple[Path, Path, dict, dict, Path]:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    request = build_workflow_request(
        input_path=source,
        input_kind="generated_rtl",
        source_sha256=file_sha256(source),
        compile_context_hash="b" * 64,
        objective="timing",
        top="top",
    )
    authorization = build_workflow_authorization(
        request,
        authorized_through=authorized_through,
        basis={"kind": "direct_prompt", "prompt_sha256": "c" * 64},
        issued_at="2026-08-31T17:00:00-07:00",
        candidate_selection=(
            {"mode": "first_eligible"}
            if authorized_through != "review"
            else None
        ),
    )
    request_path = tmp_path / "request.json"
    authorization_path = tmp_path / "authorization.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    authorization_path.write_text(json.dumps(authorization), encoding="utf-8")
    return request_path, authorization_path, request, authorization, source


def _install_agent_fakes(
    monkeypatch: pytest.MonkeyPatch,
    request: dict,
    calls: list[str],
    *,
    formal_status: str = "formal_passed",
    capabilities_available: bool = True,
    review_decision: str = "candidate_available",
    measurement_decision: str = "synthesis_handles",
    corrupt_review_hash: bool = False,
) -> None:
    def capabilities(*args, **kwargs):
        calls.append("capabilities")
        return _agent_payload(
            "rtl-advisor.agent.v2.capabilities",
            "ok",
            operations={
                stage: {"available": capabilities_available}
                for stage in ("review", "candidate", "verify", "measure")
            },
        )

    def review(*args, **kwargs):
        calls.append("review")
        payload = _agent_payload(
            "rtl-advisor.agent.v2.review",
            "completed",
            run_id="mvp-" + "1" * 20,
            decision=review_decision,
            objective="timing",
            input={
                "kind": "rtl_file",
                "compile_context_hash": request["input"]["compile_context_hash"],
                "source_integrity": {"ok": True, "mismatches": []},
            },
            findings=(
                [
                    {"finding_id": "finding-b", "rank": 2},
                    {"finding_id": "finding-a", "rank": 1},
                ]
                if review_decision == "candidate_available"
                else []
            ),
            candidate_generation_allowed=(review_decision == "candidate_available"),
            limitations=["review limitation"],
        )
        if corrupt_review_hash:
            payload["semantic_hash"] = "0" * 64
        return payload

    def candidate(*args, finding_id: str, **kwargs):
        calls.append(f"candidate:{finding_id}")
        return _agent_payload(
            "rtl-advisor.agent.v2.candidate",
            "candidate_prepared",
            run_id="mvp-" + "1" * 20,
            candidate_id="candidate-1",
            decision="candidate_prepared",
            limitations=["candidate is unproven"],
        )

    def verify(*args, **kwargs):
        calls.append("verify")
        safe = formal_status == "formal_passed"
        return _agent_payload(
            "rtl-advisor.agent.v2.verification",
            formal_status,
            run_id="mvp-" + "1" * 20,
            candidate_id="candidate-1",
            decision=formal_status,
            safe=safe,
            limitations=["formal scope limitation"],
        )

    def measure(*args, **kwargs):
        calls.append("measure")
        return _agent_payload(
            "rtl-advisor.agent.v2.measurement",
            "completed",
            run_id="mvp-" + "1" * 20,
            candidate_id="candidate-1",
            decision=measurement_decision,
            measurements={
                "standard": {"classification": "neutral"},
                "stronger": {"classification": "neutral"},
            },
            limitations=["pinned recipes only"],
        )

    def report(*args, **kwargs):
        calls.append("report")
        return _agent_payload(
            "rtl-advisor.agent.v2.report",
            "completed",
            run_id="mvp-" + "1" * 20,
            decision=(
                "synthesis_handles" if "measure" in calls else "incomplete"
            ),
            artifacts={"html": str((Path(request["input"]["path"]).parent / "report.html").resolve())},
        )

    monkeypatch.setattr(workflow_runner, "agent_v2_capabilities", capabilities)
    monkeypatch.setattr(workflow_runner, "agent_v2_review", review)
    monkeypatch.setattr(workflow_runner, "agent_v2_candidate", candidate)
    monkeypatch.setattr(workflow_runner, "agent_v2_verify", verify)
    monkeypatch.setattr(workflow_runner, "agent_v2_measure", measure)
    monkeypatch.setattr(workflow_runner, "agent_v2_report", report)


def test_full_workflow_executes_deterministic_stages_and_persists_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "completed"
    assert summary["decision"] == "synthesis_handles"
    assert summary["safe"] is True
    assert summary["completed_stages"] == [
        "capabilities",
        "review",
        "candidate",
        "verify",
        "measure",
    ]
    assert calls == [
        "capabilities",
        "review",
        "candidate:finding-a",
        "verify",
        "measure",
        "report",
    ]
    assert Path(summary["artifacts"]["state"]).is_file()
    assert Path(summary["artifacts"]["agent_report"]).is_file()

    status = workflow_runner.workflow_status(_config(tmp_path), summary["workflow_id"])
    assert status["semantic_hash"] == summary["semantic_hash"]
    assert calls[-1] == "report"


def test_review_only_stops_before_candidate_and_requests_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="review"
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["completed_stages"] == ["capabilities", "review"]
    assert summary["decision"] == "candidate_available"
    assert summary["next_action"] == "request_candidate_authorization"
    assert "candidate:finding-a" not in calls


def test_resume_with_new_authorization_runs_only_remaining_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="review"
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)
    config = _config(tmp_path)
    first = workflow_runner.workflow_start(config, request_path, authorization_path)
    extended = build_workflow_authorization(
        request,
        authorized_through="measure",
        basis={"kind": "direct_prompt", "prompt_sha256": "d" * 64},
        issued_at="2026-08-31T17:02:00-07:00",
        candidate_selection={"mode": "first_eligible"},
    )
    extended_path = tmp_path / "extended.json"
    extended_path.write_text(json.dumps(extended), encoding="utf-8")

    resumed = workflow_runner.workflow_resume(
        config, first["workflow_id"], extended_path
    )

    assert resumed["decision"] == "synthesis_handles"
    assert calls.count("capabilities") == 1
    assert calls.count("review") == 1
    assert calls.count("candidate:finding-a") == 1
    assert calls.count("verify") == 1
    assert calls.count("measure") == 1


def test_capability_failure_stops_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch, request, calls, capabilities_available=False
    )

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "blocked"
    assert summary["completed_stages"] == ["capabilities"]
    assert summary["next_action"] == "inspect_failure"
    assert calls == ["capabilities"]


def test_formal_failure_is_terminal_and_never_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch, request, calls, formal_status="formal_failed"
    )

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["decision"] == "formal_failed"
    assert summary["safe"] is False
    assert "measure" not in calls
    assert workflow_runner.workflow_exit_code(summary) == 4


@pytest.mark.parametrize("decision", ["no_change", "unsupported"])
def test_review_terminal_decisions_stop_without_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch, request, calls, review_decision=decision
    )

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "completed"
    assert summary["decision"] == decision
    assert summary["completed_stages"] == ["capabilities", "review"]
    assert summary["next_action"] == "none"
    assert not any(item.startswith("candidate:") for item in calls)
    assert workflow_runner.workflow_exit_code(summary) == 0


@pytest.mark.parametrize("formal_status", ["formal_failed", "formal_inconclusive"])
def test_formal_nonpass_states_are_terminal_and_block_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    formal_status: str,
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch, request, calls, formal_status=formal_status
    )

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "completed"
    assert summary["decision"] == formal_status
    assert summary["safe"] is False
    assert summary["next_action"] == "none"
    assert "measure" not in calls
    assert workflow_runner.workflow_exit_code(summary) == 4


@pytest.mark.parametrize(
    "decision,expected_exit",
    [
        ("measured_improvement", 0),
        ("synthesis_handles", 0),
        ("flow_dependent", 0),
        ("regression", 0),
        ("evidence_incomplete", 4),
    ],
)
def test_measurement_terminal_decision_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    expected_exit: int,
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch, request, calls, measurement_decision=decision
    )

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "completed"
    assert summary["decision"] == decision
    assert summary["safe"] is True
    assert summary["completed_stages"][-1] == "measure"
    assert summary["next_action"] == "none"
    assert workflow_runner.workflow_exit_code(summary) == expected_exit


@pytest.mark.parametrize(
    "authorized_through,decision,next_action,completed",
    [
        (
            "review",
            "candidate_available",
            "request_candidate_authorization",
            ["capabilities", "review"],
        ),
        (
            "candidate",
            "candidate_prepared",
            "request_verify_authorization",
            ["capabilities", "review", "candidate"],
        ),
        (
            "verify",
            "formal_passed",
            "request_measure_authorization",
            ["capabilities", "review", "candidate", "verify"],
        ),
    ],
)
def test_authorization_ceiling_states_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorized_through: str,
    decision: str,
    next_action: str,
    completed: list[str],
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through=authorized_through
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "completed"
    assert summary["decision"] == decision
    assert summary["next_action"] == next_action
    assert summary["completed_stages"] == completed


def test_invalid_agent_semantic_hash_marks_workflow_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="review"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch, request, calls, corrupt_review_hash=True
    )

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert summary["status"] == "untrusted"
    assert summary["decision"] == "pending"
    assert summary["next_action"] == "inspect_failure"
    assert workflow_runner.workflow_exit_code(summary) == 2


def test_resume_rejects_changed_input_before_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, source = _documents(
        tmp_path, authorized_through="review"
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)
    config = _config(tmp_path)
    first = workflow_runner.workflow_start(config, request_path, authorization_path)
    source.write_text("module top; wire changed; endmodule\n", encoding="utf-8")
    extended = build_workflow_authorization(
        request,
        authorized_through="candidate",
        basis={"kind": "direct_prompt", "prompt_sha256": "e" * 64},
        issued_at="2026-08-31T17:03:00-07:00",
        candidate_selection={"mode": "first_eligible"},
    )
    extended_path = tmp_path / "candidate-authorization.json"
    extended_path.write_text(json.dumps(extended), encoding="utf-8")

    summary = workflow_runner.workflow_resume(
        config, first["workflow_id"], extended_path
    )

    assert summary["status"] == "failed"
    assert summary["next_action"] == "inspect_failure"
    assert "candidate:finding-a" not in calls


def test_cli_normalizes_workflow_start_paths(tmp_path: Path) -> None:
    request_path, authorization_path, _, _, _ = _documents(
        tmp_path, authorized_through="review"
    )
    config = _config(tmp_path)
    args = build_parser().parse_args(
        [
            "--config",
            str(config.config_path),
            "agent",
            "workflow",
            "start",
            str(request_path),
            "--authorization",
            str(authorization_path),
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
        "workflow",
        "start",
        str(request_path.resolve()),
        "--authorization",
        str(authorization_path.resolve()),
        "--schema-version",
        "1",
        "--json",
    )


def test_prepare_builds_hash_linked_documents_without_exposing_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(
        "Review this generated RTL through candidate preparation.",
        encoding="utf-8",
    )
    calls: list[str] = []

    def capabilities(*args, **kwargs):
        calls.append("capabilities")
        return _agent_payload(
            "rtl-advisor.agent.v2.capabilities",
            "ok",
            operations={
                stage: {"available": True}
                for stage in ("review", "candidate", "verify", "measure")
            },
        )

    monkeypatch.setattr(workflow_runner, "agent_v2_capabilities", capabilities)
    result = workflow_runner.workflow_prepare(
        config,
        source,
        input_kind="generated_rtl",
        objective="balanced",
        authorized_through="candidate",
        prompt_file=prompt,
        top="top",
        candidate_selection={"mode": "first_eligible"},
        normalized_command=("rtl-advisor", "agent", "workflow", "prepare"),
    )
    repeated = workflow_runner.workflow_prepare(
        config,
        source,
        input_kind="generated_rtl",
        objective="balanced",
        authorized_through="candidate",
        prompt_file=prompt,
        top="top",
        candidate_selection={"mode": "first_eligible"},
        normalized_command=("rtl-advisor", "agent", "workflow", "prepare"),
    )

    validate_workflow_preparation(result)
    assert repeated == result
    assert calls == ["capabilities", "capabilities"]
    assert result["status"] == "prepared"
    request = json.loads(Path(result["artifacts"]["request"]).read_text())
    authorization = json.loads(
        Path(result["artifacts"]["authorization"]).read_text()
    )
    assert request["workflow_id"] == result["workflow_id"]
    assert request["input"]["sha256"] == file_sha256(source)
    assert request["input"]["compile_context_hash"] != "0" * 64
    assert authorization["basis"] == {
        "kind": "direct_prompt",
        "prompt_sha256": file_sha256(prompt),
    }
    assert authorization["candidate_selection"] == {"mode": "first_eligible"}
    assert "Review this generated RTL" not in json.dumps(result)
    assert "Review this generated RTL" not in json.dumps(authorization)


def test_prepare_checks_capabilities_before_reading_missing_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def capabilities(*args, **kwargs):
        calls.append("capabilities")
        return _agent_payload(
            "rtl-advisor.agent.v2.capabilities",
            "ok",
            operations={
                stage: {"available": stage != "verify"}
                for stage in ("review", "candidate", "verify", "measure")
            },
        )

    monkeypatch.setattr(workflow_runner, "agent_v2_capabilities", capabilities)
    with pytest.raises(workflow_runner.WorkflowRunnerError) as error:
        workflow_runner.workflow_prepare(
            _config(tmp_path),
            tmp_path / "missing.sv",
            input_kind="generated_rtl",
            objective="balanced",
            authorized_through="verify",
            prompt_file=tmp_path / "also-missing.txt",
            top="top",
            candidate_selection={"mode": "first_eligible"},
        )

    assert error.value.code == "capability_unavailable"
    assert calls == ["capabilities"]


def test_prepare_confirmed_proposal_is_canonicalized_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps(
            {
                "input": str(source),
                "objective": "timing",
                "authorized_through": "review",
            }
        ),
        encoding="utf-8",
    )
    confirmation = tmp_path / "confirmation.txt"
    confirmation.write_text("proceed\n", encoding="utf-8")

    monkeypatch.setattr(
        workflow_runner,
        "agent_v2_capabilities",
        lambda *args, **kwargs: _agent_payload(
            "rtl-advisor.agent.v2.capabilities",
            "ok",
            operations={
                stage: {"available": True}
                for stage in ("review", "candidate", "verify", "measure")
            },
        ),
    )
    result = workflow_runner.workflow_prepare(
        _config(tmp_path),
        source,
        input_kind="explicitly_approved_open_rtl",
        objective="timing",
        authorized_through="review",
        proposal_file=proposal,
        confirmation_file=confirmation,
        top="top",
        issued_at="2026-08-31T17:11:00-07:00",
    )

    proposal_snapshot = json.loads(Path(result["artifacts"]["proposal"]).read_text())
    authorization = json.loads(
        Path(result["artifacts"]["authorization"]).read_text()
    )
    assert authorization["basis"] == {
        "kind": "confirmed_proposal",
        "proposal_id": proposal_snapshot["proposal_id"],
        "proposal_semantic_hash": proposal_snapshot["semantic_hash"],
        "confirmation_prompt_sha256": file_sha256(confirmation),
    }


def test_prepare_requires_candidate_selection_for_candidate_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("prepare a candidate\n", encoding="utf-8")
    monkeypatch.setattr(
        workflow_runner,
        "agent_v2_capabilities",
        lambda *args, **kwargs: _agent_payload(
            "rtl-advisor.agent.v2.capabilities",
            "ok",
            operations={
                stage: {"available": True}
                for stage in ("review", "candidate", "verify", "measure")
            },
        ),
    )

    with pytest.raises(WorkflowContractError) as error:
        workflow_runner.workflow_prepare(
            _config(tmp_path),
            source,
            input_kind="generated_rtl",
            objective="balanced",
            authorized_through="candidate",
            prompt_file=prompt,
            top="top",
            issued_at="2026-08-31T17:12:00-07:00",
        )

    assert error.value.code == "candidate_selection_required"


def test_cli_normalizes_workflow_prepare_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this\n", encoding="utf-8")
    output = tmp_path / "prepared"
    args = build_parser().parse_args(
        [
            "--config",
            str(config.config_path),
            "agent",
            "workflow",
            "prepare",
            str(source),
            "--input-kind",
            "generated_rtl",
            "--objective",
            "balanced",
            "--authorized-through",
            "review",
            "--prompt-file",
            str(prompt),
            "--top",
            "top",
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
        "workflow",
        "prepare",
        str(source.resolve()),
        "--input-kind",
        "generated_rtl",
        "--objective",
        "balanced",
        "--authorized-through",
        "review",
        "--top",
        "top",
        "--prompt-file",
        str(prompt.resolve()),
        "--output-dir",
        str(output.resolve()),
        "--schema-version",
        "1",
        "--json",
    )


def test_start_requires_resume_for_a_different_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="review"
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)
    config = _config(tmp_path)
    first = workflow_runner.workflow_start(config, request_path, authorization_path)
    extended = build_workflow_authorization(
        request,
        authorized_through="candidate",
        basis={"kind": "direct_prompt", "prompt_sha256": "f" * 64},
        issued_at="2026-08-31T17:04:00-07:00",
        candidate_selection={"mode": "first_eligible"},
    )
    extended_path = tmp_path / "wrong-start-authorization.json"
    extended_path.write_text(json.dumps(extended), encoding="utf-8")

    with pytest.raises(workflow_runner.WorkflowRunnerError) as error:
        workflow_runner.workflow_start(config, request_path, extended_path)

    assert error.value.code == "authorization_resume_required"
    current = workflow_runner.workflow_status(config, first["workflow_id"])
    assert current["authorized_through"] == "review"


def test_capabilities_run_before_stale_input_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, source = _documents(
        tmp_path, authorized_through="review"
    )
    calls: list[str] = []
    _install_agent_fakes(monkeypatch, request, calls)
    source.unlink()

    summary = workflow_runner.workflow_start(
        _config(tmp_path), request_path, authorization_path
    )

    assert calls == ["capabilities"]
    assert summary["completed_stages"] == ["capabilities"]
    assert summary["status"] == "failed"


def test_compact_digest_matches_full_summary_and_stays_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, authorization_path, request, _, _ = _documents(
        tmp_path, authorized_through="measure"
    )
    calls: list[str] = []
    _install_agent_fakes(
        monkeypatch,
        request,
        calls,
        measurement_decision="measured_improvement",
    )
    config = _config(tmp_path)

    full = workflow_runner.workflow_start(config, request_path, authorization_path)
    digest = workflow_runner.workflow_status(
        config, full["workflow_id"], compact=True
    )

    assert digest["document_type"] == "rtl-advisor.workflow.digest"
    assert digest["decision"] == full["decision"]
    assert digest["action"] == "recommend_change"
    assert digest["evidence_complete"] is True
    assert digest["parent"] == {
        "summary_semantic_hash": full["semantic_hash"],
        "state_semantic_hash": full["state_semantic_hash"],
    }
    assert digest["profile_results"] == {
        "standard": "neutral",
        "stronger": "neutral",
    }
    assert len(json.dumps(digest, separators=(",", ":")).encode()) < 4096


@pytest.mark.parametrize(
    "decision,action",
    [
        ("no_change", "no_change"),
        ("synthesis_handles", "no_change"),
        ("regression", "no_change"),
        ("unsupported", "unsupported"),
        ("flow_dependent", "inconclusive"),
        ("evidence_incomplete", "inconclusive"),
    ],
)
def test_digest_action_mapping_is_deterministic(
    decision: str, action: str
) -> None:
    assert workflow_runner._digest_action(decision) == action


def test_batch_preserves_order_deduplicates_and_discovers_capabilities_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review these generated designs\n", encoding="utf-8")
    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "schema": "rtl-advisor-workflow-batch-manifest-v1",
                "document_type": "rtl-advisor.workflow.batch-manifest",
                "items": [
                    {
                        "item_id": "first",
                        "input": {
                            "kind": "generated_rtl",
                            "path": "top.sv",
                            "top": "top",
                        },
                        "objective": "balanced",
                    },
                    {
                        "item_id": "duplicate",
                        "input": {
                            "kind": "generated_rtl",
                            "path": "top.sv",
                            "top": "top",
                        },
                        "objective": "balanced",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    request = build_workflow_request(
        input_path=source,
        input_kind="generated_rtl",
        source_sha256=file_sha256(source),
        compile_context_hash="b" * 64,
        objective="balanced",
        top="top",
    )
    _install_agent_fakes(
        monkeypatch, request, calls, review_decision="no_change"
    )
    monkeypatch.setattr(workflow_runner, "_validate_review_binding", lambda *args: None)

    result = workflow_runner.workflow_batch(
        config,
        manifest,
        authorized_through="review",
        prompt_file=prompt,
    )

    assert result["status"] == "completed"
    assert [item["item_id"] for item in result["items"]] == [
        "first",
        "duplicate",
    ]
    assert result["counts"]["unique_workflows"] == 1
    assert result["items"][1]["deduplicated_from"] == "first"
    assert calls.count("capabilities") == 1
    assert calls.count("review") == 1


def test_batch_replay_resumes_existing_workflow_with_new_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    first_prompt = tmp_path / "first-prompt.txt"
    second_prompt = tmp_path / "second-prompt.txt"
    first_prompt.write_text("review generated designs, repetition one\n", encoding="utf-8")
    second_prompt.write_text("review generated designs, repetition two\n", encoding="utf-8")
    manifest = tmp_path / "batch.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "schema": "rtl-advisor-workflow-batch-manifest-v1",
                "document_type": "rtl-advisor.workflow.batch-manifest",
                "items": [
                    {
                        "item_id": "only",
                        "input": {
                            "kind": "generated_rtl",
                            "path": "top.sv",
                            "top": "top",
                        },
                        "objective": "balanced",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    request = build_workflow_request(
        input_path=source,
        input_kind="generated_rtl",
        source_sha256=file_sha256(source),
        compile_context_hash="b" * 64,
        objective="balanced",
        top="top",
    )
    _install_agent_fakes(
        monkeypatch, request, calls, review_decision="no_change"
    )
    monkeypatch.setattr(workflow_runner, "_validate_review_binding", lambda *args: None)

    first = workflow_runner.workflow_batch(
        config,
        manifest,
        authorized_through="review",
        prompt_file=first_prompt,
        output_dir=tmp_path / "first-batch",
    )
    second = workflow_runner.workflow_batch(
        config,
        manifest,
        authorized_through="review",
        prompt_file=second_prompt,
        output_dir=tmp_path / "second-batch",
        jobs=4,
    )

    assert first["status"] == second["status"] == "completed"
    assert first["items"][0]["decision"] == second["items"][0]["decision"]
    assert calls.count("capabilities") == 2
    assert calls.count("review") == 1


def test_batch_continues_after_item_hash_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this generated design\n", encoding="utf-8")
    manifest = tmp_path / "batch.json"
    items = []
    for item_id, expected in (("stale", "0" * 64), ("current", file_sha256(source))):
        items.append(
            {
                "item_id": item_id,
                "input": {
                    "kind": "generated_rtl",
                    "path": "top.sv",
                    "top": "top",
                },
                "objective": "balanced",
                "expected_source_sha256": expected,
            }
        )
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "schema": "rtl-advisor-workflow-batch-manifest-v1",
                "document_type": "rtl-advisor.workflow.batch-manifest",
                "items": items,
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []
    request = build_workflow_request(
        input_path=source,
        input_kind="generated_rtl",
        source_sha256=file_sha256(source),
        compile_context_hash="b" * 64,
        objective="balanced",
        top="top",
    )
    _install_agent_fakes(
        monkeypatch, request, calls, review_decision="no_change"
    )
    monkeypatch.setattr(workflow_runner, "_validate_review_binding", lambda *args: None)

    result = workflow_runner.workflow_batch(
        config,
        manifest,
        authorized_through="review",
        prompt_file=prompt,
    )

    assert result["status"] == "partial"
    assert result["counts"] == {
        "items": 2,
        "completed": 1,
        "failed": 1,
        "unique_workflows": 1,
    }
    assert result["items"][0]["error"]["code"] == "source_hash_mismatch"
    assert result["items"][1]["decision"] == "no_change"
    assert workflow_runner.workflow_exit_code(result) == 4
