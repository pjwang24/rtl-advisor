from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_advisor.workflow_contract import (
    WorkflowContractError,
    apply_authorization,
    authorization_allows,
    build_workflow_authorization,
    build_workflow_request,
    build_workflow_state,
    next_authorized_stage,
    record_stage_completion,
    validate_authorization_progression,
    validate_stage_start,
    validate_workflow_authorization,
    validate_workflow_request,
)


def _request(tmp_path: Path) -> dict:
    return build_workflow_request(
        input_path=tmp_path / "top.sv",
        input_kind="generated_rtl",
        source_sha256="a" * 64,
        compile_context_hash="b" * 64,
        objective="timing",
        top="top",
    )


def _authorization(request: dict, stage: str, marker: str = "c") -> dict:
    return build_workflow_authorization(
        request,
        authorized_through=stage,
        basis={"kind": "direct_prompt", "prompt_sha256": marker * 64},
        issued_at="2026-08-31T17:00:00-07:00",
        candidate_selection=(
            {"mode": "first_eligible"} if stage != "review" else None
        ),
    )


def _result(tmp_path: Path, stage: str, status: str, **facts: object) -> dict:
    return {
        "status": status,
        "document_type": f"rtl-advisor.agent.v2.{stage}",
        "semantic_hash": (stage[0] if stage[0] in "abcdef" else "d") * 64,
        "artifact_path": str((tmp_path / f"{stage}.json").resolve()),
        "facts": facts,
    }


def _complete_through_review(
    tmp_path: Path, request: dict, authorization: dict, *, eligible: bool
) -> dict:
    state = build_workflow_state(request, authorization)
    state = record_stage_completion(
        state,
        authorization,
        "capabilities",
        _result(tmp_path, "capabilities", "ok"),
    )
    return record_stage_completion(
        state,
        authorization,
        "review",
        _result(
            tmp_path,
            "review",
            "candidate_available" if eligible else "no_change",
            candidate_generation_allowed=eligible,
        ),
    )


def test_request_is_content_addressed_and_rejects_tampering(tmp_path: Path) -> None:
    request = _request(tmp_path)
    repeated = _request(tmp_path)

    assert request == repeated
    assert request["workflow_id"].startswith("workflow-")

    tampered = {**request, "objective": "area"}
    with pytest.raises(WorkflowContractError) as error:
        validate_workflow_request(tampered)

    assert error.value.code in {"workflow_id_mismatch", "semantic_hash_mismatch"}


def test_authorization_maps_to_an_explicit_stage_ceiling(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "verify")

    assert authorization_allows(authorization, "capabilities") is True
    assert authorization_allows(authorization, "review") is True
    assert authorization_allows(authorization, "candidate") is True
    assert authorization_allows(authorization, "verify") is True
    assert authorization_allows(authorization, "measure") is False
    assert authorization_allows(authorization, "report") is True


def test_candidate_authorization_requires_a_frozen_selection_policy(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    with pytest.raises(WorkflowContractError) as error:
        build_workflow_authorization(
            request,
            authorized_through="candidate",
            basis={"kind": "direct_prompt", "prompt_sha256": "c" * 64},
            issued_at="2026-08-31T17:00:00-07:00",
        )

    assert error.value.code == "candidate_selection_required"


def test_confirmed_proposal_binds_confirmation_without_free_text(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    authorization = build_workflow_authorization(
        request,
        authorized_through="measure",
        basis={
            "kind": "confirmed_proposal",
            "proposal_id": "proposal-" + "1" * 20,
            "proposal_semantic_hash": "2" * 64,
            "confirmation_prompt_sha256": "3" * 64,
        },
        issued_at="2026-08-31T17:01:00-07:00",
        candidate_selection={"mode": "first_eligible"},
    )

    validate_workflow_authorization(authorization, request=request)
    assert authorization["basis"]["kind"] == "confirmed_proposal"


def test_candidate_cannot_start_outside_authorization(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "review")
    state = _complete_through_review(
        tmp_path, request, authorization, eligible=True
    )

    with pytest.raises(WorkflowContractError) as error:
        validate_stage_start(
            state,
            authorization,
            "candidate",
            source_sha256="a" * 64,
            compile_context_hash="b" * 64,
        )

    assert error.value.code == "stage_not_authorized"


def test_candidate_requires_an_eligible_review(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "candidate")
    state = _complete_through_review(
        tmp_path, request, authorization, eligible=False
    )

    with pytest.raises(WorkflowContractError) as error:
        validate_stage_start(
            state,
            authorization,
            "candidate",
            source_sha256="a" * 64,
            compile_context_hash="b" * 64,
        )

    assert error.value.code == "candidate_not_eligible"


def test_changed_source_or_compile_context_invalidates_downstream_stage(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "candidate")
    state = _complete_through_review(
        tmp_path, request, authorization, eligible=True
    )

    with pytest.raises(WorkflowContractError) as error:
        validate_stage_start(
            state,
            authorization,
            "candidate",
            source_sha256="f" * 64,
            compile_context_hash="b" * 64,
        )

    assert error.value.code == "stale_input"


def test_measurement_requires_current_safe_formal_pass(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "measure")
    state = _complete_through_review(
        tmp_path, request, authorization, eligible=True
    )
    state = record_stage_completion(
        state,
        authorization,
        "candidate",
        _result(tmp_path, "candidate", "candidate_prepared"),
        source_sha256="a" * 64,
        compile_context_hash="b" * 64,
    )
    state = record_stage_completion(
        state,
        authorization,
        "verify",
        _result(tmp_path, "verify", "formal_failed", safe=False),
        source_sha256="a" * 64,
        compile_context_hash="b" * 64,
    )

    with pytest.raises(WorkflowContractError) as error:
        validate_stage_start(
            state,
            authorization,
            "measure",
            source_sha256="a" * 64,
            compile_context_hash="b" * 64,
        )

    assert error.value.code == "formal_pass_required"
    assert next_authorized_stage(state, authorization) is None


def test_full_authorized_flow_reaches_measurement_once(tmp_path: Path) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "measure")
    state = _complete_through_review(
        tmp_path, request, authorization, eligible=True
    )
    state = record_stage_completion(
        state,
        authorization,
        "candidate",
        _result(tmp_path, "candidate", "candidate_prepared"),
        source_sha256="a" * 64,
        compile_context_hash="b" * 64,
    )
    state = record_stage_completion(
        state,
        authorization,
        "verify",
        _result(tmp_path, "verify", "formal_passed", safe=True),
        source_sha256="a" * 64,
        compile_context_hash="b" * 64,
    )

    assert next_authorized_stage(state, authorization) == "measure"
    state = record_stage_completion(
        state,
        authorization,
        "measure",
        _result(tmp_path, "measure", "completed", decision="synthesis_handles"),
        source_sha256="a" * 64,
        compile_context_hash="b" * 64,
    )

    assert state["status"] == "completed"
    assert next_authorized_stage(state, authorization) is None
    with pytest.raises(WorkflowContractError) as error:
        validate_stage_start(
            state,
            authorization,
            "measure",
            source_sha256="a" * 64,
            compile_context_hash="b" * 64,
        )

    assert error.value.code == "stage_already_completed"


def test_resume_skips_completed_stages_without_rewriting_them(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    authorization = _authorization(request, "measure")
    state = _complete_through_review(
        tmp_path, request, authorization, eligible=True
    )

    assert next_authorized_stage(state, authorization) == "candidate"
    with pytest.raises(WorkflowContractError) as error:
        validate_stage_start(state, authorization, "review")

    assert error.value.code == "stage_already_completed"


def test_new_explicit_authorization_can_extend_but_not_narrow_workflow(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    review_authorization = _authorization(request, "review")
    verify_authorization = _authorization(request, "verify", marker="d")
    state = _complete_through_review(
        tmp_path, request, review_authorization, eligible=True
    )

    extended = apply_authorization(
        state,
        review_authorization,
        verify_authorization,
        request=request,
    )

    assert extended["authorization_semantic_hash"] == verify_authorization[
        "semantic_hash"
    ]
    assert next_authorized_stage(extended, verify_authorization) == "candidate"
    with pytest.raises(WorkflowContractError) as error:
        validate_authorization_progression(
            verify_authorization,
            review_authorization,
            request=request,
        )

    assert error.value.code == "authorization_regression"


def test_authorization_cannot_switch_findings_after_candidate_selection(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    first = _authorization(request, "candidate")
    changed = build_workflow_authorization(
        request,
        authorized_through="verify",
        basis={"kind": "direct_prompt", "prompt_sha256": "e" * 64},
        issued_at="2026-08-31T17:05:00-07:00",
        candidate_selection={"mode": "finding_id", "finding_id": "different"},
    )

    with pytest.raises(WorkflowContractError) as error:
        validate_authorization_progression(first, changed, request=request)

    assert error.value.code == "candidate_selection_mismatch"


def test_workflow_schema_documents_are_present_and_well_formed() -> None:
    root = Path(__file__).resolve().parents[1]
    names = (
        "rtl-advisor-workflow-request-v1.schema.json",
        "rtl-advisor-workflow-authorization-v1.schema.json",
        "rtl-advisor-workflow-state-v1.schema.json",
        "rtl-advisor-workflow-summary-v1.schema.json",
        "rtl-advisor-workflow-preparation-v1.schema.json",
    )

    for name in names:
        schema = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert "semantic_hash" in schema["required"]

    authorization_schema = json.loads(
        (root / "schemas" / names[1]).read_text(encoding="utf-8")
    )
    assert authorization_schema["properties"]["authorized_through"]["enum"] == [
        "review",
        "candidate",
        "verify",
        "measure",
    ]
