from __future__ import annotations

from datetime import datetime
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from rtl_advisor.mvp_schema import stable_hash


WORKFLOW_CONTRACT_SCHEMA_VERSION = 1
REQUEST_SCHEMA = "rtl-advisor-workflow-request-v1"
AUTHORIZATION_SCHEMA = "rtl-advisor-workflow-authorization-v1"
STATE_SCHEMA = "rtl-advisor-workflow-state-v1"
SUMMARY_SCHEMA = "rtl-advisor-workflow-summary-v1"
PREPARATION_SCHEMA = "rtl-advisor-workflow-preparation-v1"

REQUEST_DOCUMENT_TYPE = "rtl-advisor.workflow.request"
AUTHORIZATION_DOCUMENT_TYPE = "rtl-advisor.workflow.authorization"
STATE_DOCUMENT_TYPE = "rtl-advisor.workflow.state"
SUMMARY_DOCUMENT_TYPE = "rtl-advisor.workflow.summary"
PREPARATION_DOCUMENT_TYPE = "rtl-advisor.workflow.preparation"

OBJECTIVES = ("timing", "area", "balanced")
SOURCE_KINDS = (
    "generated_rtl",
    "explicitly_approved_open_rtl",
    "qualified_corpus_reference",
)
EXECUTION_STAGES = ("capabilities", "review", "candidate", "verify", "measure")
FAILURE_STAGES = (*EXECUTION_STAGES, "report")
AUTHORIZED_THROUGH = ("review", "candidate", "verify", "measure")

_STAGE_INDEX = {stage: index for index, stage in enumerate(EXECUTION_STAGES)}
_AUTHORIZATION_INDEX = {
    stage: _STAGE_INDEX[stage] for stage in AUTHORIZED_THROUGH
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_ID_RE = re.compile(r"^workflow-[0-9a-f]{20}$")
_AUTHORIZATION_ID_RE = re.compile(r"^authorization-[0-9a-f]{20}$")
_PROPOSAL_ID_RE = re.compile(r"^proposal-[0-9a-f]{20}$")


class WorkflowContractError(ValueError):
    """Raised when a workflow document or transition violates the contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _require_exact_fields(
    payload: Mapping[str, Any],
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    missing = required - payload.keys()
    if missing:
        raise WorkflowContractError(
            f"missing required fields: {sorted(missing)}",
            code="invalid_workflow_document",
        )
    unknown = payload.keys() - required - optional
    if unknown:
        raise WorkflowContractError(
            f"unknown fields: {sorted(unknown)}",
            code="invalid_workflow_document",
        )


def _require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WorkflowContractError(
            f"{name} must be a lowercase SHA-256 digest",
            code="invalid_workflow_document",
        )
    return value


def _validate_semantic_hash(payload: Mapping[str, Any]) -> None:
    expected = _require_sha256(payload.get("semantic_hash"), "semantic_hash")
    core = {key: value for key, value in payload.items() if key != "semantic_hash"}
    if expected != stable_hash(core):
        raise WorkflowContractError(
            "workflow document semantic hash mismatch",
            code="semantic_hash_mismatch",
        )


def _hashed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("semantic_hash", None)
    result["semantic_hash"] = stable_hash(result)
    return result


def _parse_timestamp(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise WorkflowContractError(
            f"{name} must be an RFC 3339 timestamp",
            code="invalid_workflow_document",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowContractError(
            f"{name} must be an RFC 3339 timestamp",
            code="invalid_workflow_document",
        ) from exc
    if parsed.tzinfo is None:
        raise WorkflowContractError(
            f"{name} must include a timezone",
            code="invalid_workflow_document",
        )


def build_workflow_request(
    *,
    input_path: str | Path,
    input_kind: str,
    source_sha256: str,
    compile_context_hash: str,
    objective: str,
    top: str | None = None,
    include_dirs: Sequence[str | Path] = (),
    defines: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a content-addressed request from already-normalized local input."""

    path = Path(input_path).expanduser().resolve()
    input_record: dict[str, Any] = {
        "kind": input_kind,
        "path": str(path),
        "sha256": source_sha256,
        "compile_context_hash": compile_context_hash,
        "include_dirs": [
            str(Path(item).expanduser().resolve()) for item in include_dirs
        ],
        "defines": list(defines),
    }
    if top is not None:
        input_record["top"] = top
    identity = {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "schema": REQUEST_SCHEMA,
        "document_type": REQUEST_DOCUMENT_TYPE,
        "input": input_record,
        "objective": objective,
    }
    request = {
        **identity,
        "workflow_id": f"workflow-{stable_hash(identity)[:20]}",
    }
    result = _hashed(request)
    validate_workflow_request(result)
    return result


def validate_workflow_request(payload: Mapping[str, Any]) -> None:
    _require_exact_fields(
        payload,
        {
            "schema_version",
            "schema",
            "document_type",
            "workflow_id",
            "input",
            "objective",
            "semantic_hash",
        },
    )
    if payload.get("schema_version") != WORKFLOW_CONTRACT_SCHEMA_VERSION:
        raise WorkflowContractError(
            "unsupported workflow request schema version", code="unsupported_schema"
        )
    if payload.get("schema") != REQUEST_SCHEMA or payload.get(
        "document_type"
    ) != REQUEST_DOCUMENT_TYPE:
        raise WorkflowContractError(
            "unexpected workflow request document type",
            code="invalid_workflow_document",
        )
    workflow_id = payload.get("workflow_id")
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise WorkflowContractError(
            "invalid workflow ID", code="invalid_workflow_document"
        )
    if payload.get("objective") not in OBJECTIVES:
        raise WorkflowContractError(
            "invalid workflow objective", code="invalid_workflow_document"
        )
    input_record = payload.get("input")
    if not isinstance(input_record, Mapping):
        raise WorkflowContractError(
            "workflow input must be an object", code="invalid_workflow_document"
        )
    _require_exact_fields(
        input_record,
        {
            "kind",
            "path",
            "sha256",
            "compile_context_hash",
            "include_dirs",
            "defines",
        },
        {"top"},
    )
    if input_record.get("kind") not in SOURCE_KINDS:
        raise WorkflowContractError(
            "unsupported workflow input kind", code="unauthorized_source"
        )
    raw_path = input_record.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise WorkflowContractError(
            "workflow input path must be absolute",
            code="invalid_workflow_document",
        )
    top = input_record.get("top")
    if top is not None and (not isinstance(top, str) or not top.strip()):
        raise WorkflowContractError(
            "workflow top must be a non-empty string",
            code="invalid_workflow_document",
        )
    _require_sha256(input_record.get("sha256"), "input.sha256")
    _require_sha256(
        input_record.get("compile_context_hash"), "input.compile_context_hash"
    )
    include_dirs = input_record.get("include_dirs")
    if not isinstance(include_dirs, list) or any(
        not isinstance(item, str) or not Path(item).is_absolute()
        for item in include_dirs
    ):
        raise WorkflowContractError(
            "workflow include directories must be absolute paths",
            code="invalid_workflow_document",
        )
    defines = input_record.get("defines")
    if not isinstance(defines, list) or any(
        not isinstance(item, str) or not item for item in defines
    ):
        raise WorkflowContractError(
            "workflow defines must be non-empty strings",
            code="invalid_workflow_document",
        )
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"workflow_id", "semantic_hash"}
    }
    if workflow_id != f"workflow-{stable_hash(identity)[:20]}":
        raise WorkflowContractError(
            "workflow ID does not match the normalized request",
            code="workflow_id_mismatch",
        )
    _validate_semantic_hash(payload)


def build_workflow_authorization(
    request: Mapping[str, Any],
    *,
    authorized_through: str,
    basis: Mapping[str, Any],
    issued_at: str,
    candidate_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_workflow_request(request)
    core = {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "schema": AUTHORIZATION_SCHEMA,
        "document_type": AUTHORIZATION_DOCUMENT_TYPE,
        "workflow_id": request["workflow_id"],
        "request_semantic_hash": request["semantic_hash"],
        "authorized_through": authorized_through,
        "basis": dict(basis),
        "issued_at": issued_at,
    }
    if candidate_selection is not None:
        core["candidate_selection"] = dict(candidate_selection)
    authorization = {
        **core,
        "authorization_id": f"authorization-{stable_hash(core)[:20]}",
    }
    result = _hashed(authorization)
    validate_workflow_authorization(result, request=request)
    return result


def validate_workflow_authorization(
    payload: Mapping[str, Any], *, request: Mapping[str, Any] | None = None
) -> None:
    _require_exact_fields(
        payload,
        {
            "schema_version",
            "schema",
            "document_type",
            "authorization_id",
            "workflow_id",
            "request_semantic_hash",
            "authorized_through",
            "basis",
            "issued_at",
            "semantic_hash",
        },
        {"candidate_selection"},
    )
    if payload.get("schema_version") != WORKFLOW_CONTRACT_SCHEMA_VERSION:
        raise WorkflowContractError(
            "unsupported workflow authorization schema version",
            code="unsupported_schema",
        )
    if payload.get("schema") != AUTHORIZATION_SCHEMA or payload.get(
        "document_type"
    ) != AUTHORIZATION_DOCUMENT_TYPE:
        raise WorkflowContractError(
            "unexpected workflow authorization document type",
            code="invalid_workflow_document",
        )
    workflow_id = payload.get("workflow_id")
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise WorkflowContractError(
            "invalid workflow ID", code="invalid_workflow_document"
        )
    authorization_id = payload.get("authorization_id")
    if not isinstance(authorization_id, str) or not _AUTHORIZATION_ID_RE.fullmatch(
        authorization_id
    ):
        raise WorkflowContractError(
            "invalid authorization ID", code="invalid_workflow_document"
        )
    _require_sha256(payload.get("request_semantic_hash"), "request_semantic_hash")
    if payload.get("authorized_through") not in AUTHORIZED_THROUGH:
        raise WorkflowContractError(
            "invalid authorization stage", code="invalid_workflow_document"
        )
    authorized_through = str(payload["authorized_through"])
    selection = payload.get("candidate_selection")
    candidate_is_authorized = (
        _AUTHORIZATION_INDEX[authorized_through]
        >= _AUTHORIZATION_INDEX["candidate"]
    )
    if candidate_is_authorized and not isinstance(selection, Mapping):
        raise WorkflowContractError(
            "candidate authorization requires a selection policy",
            code="candidate_selection_required",
        )
    if not candidate_is_authorized and selection is not None:
        raise WorkflowContractError(
            "review-only authorization cannot select a candidate",
            code="invalid_workflow_document",
        )
    if isinstance(selection, Mapping):
        mode = selection.get("mode")
        if mode == "first_eligible":
            _require_exact_fields(selection, {"mode"})
        elif mode == "finding_id":
            _require_exact_fields(selection, {"mode", "finding_id"})
            finding_id = selection.get("finding_id")
            if not isinstance(finding_id, str) or not finding_id:
                raise WorkflowContractError(
                    "candidate finding ID must be a non-empty string",
                    code="invalid_workflow_document",
                )
        else:
            raise WorkflowContractError(
                "unsupported candidate selection policy",
                code="invalid_workflow_document",
            )
    basis = payload.get("basis")
    if not isinstance(basis, Mapping):
        raise WorkflowContractError(
            "authorization basis must be an object",
            code="invalid_workflow_document",
        )
    kind = basis.get("kind")
    if kind == "direct_prompt":
        _require_exact_fields(basis, {"kind", "prompt_sha256"})
        _require_sha256(basis.get("prompt_sha256"), "basis.prompt_sha256")
    elif kind == "confirmed_proposal":
        _require_exact_fields(
            basis,
            {
                "kind",
                "proposal_id",
                "proposal_semantic_hash",
                "confirmation_prompt_sha256",
            },
        )
        proposal_id = basis.get("proposal_id")
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.fullmatch(
            proposal_id
        ):
            raise WorkflowContractError(
                "invalid proposal ID", code="invalid_workflow_document"
            )
        _require_sha256(
            basis.get("proposal_semantic_hash"), "basis.proposal_semantic_hash"
        )
        _require_sha256(
            basis.get("confirmation_prompt_sha256"),
            "basis.confirmation_prompt_sha256",
        )
    else:
        raise WorkflowContractError(
            "unsupported authorization basis", code="invalid_workflow_document"
        )
    _parse_timestamp(payload.get("issued_at"), "issued_at")
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"authorization_id", "semantic_hash"}
    }
    if authorization_id != f"authorization-{stable_hash(identity)[:20]}":
        raise WorkflowContractError(
            "authorization ID does not match its contents",
            code="authorization_id_mismatch",
        )
    _validate_semantic_hash(payload)
    if request is not None:
        validate_workflow_request(request)
        if workflow_id != request.get("workflow_id") or payload.get(
            "request_semantic_hash"
        ) != request.get("semantic_hash"):
            raise WorkflowContractError(
                "authorization is not bound to this workflow request",
                code="authorization_request_mismatch",
            )


def build_workflow_preparation(
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    capabilities_semantic_hash: str,
    artifacts: Mapping[str, str | Path],
    command: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the compact result of deterministic intent preparation."""

    validate_workflow_request(request)
    validate_workflow_authorization(authorization, request=request)
    preparation = {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "schema": PREPARATION_SCHEMA,
        "document_type": PREPARATION_DOCUMENT_TYPE,
        "status": "prepared",
        "workflow_id": request["workflow_id"],
        "request_semantic_hash": request["semantic_hash"],
        "authorization_semantic_hash": authorization["semantic_hash"],
        "capabilities_semantic_hash": capabilities_semantic_hash,
        "authorized_through": authorization["authorized_through"],
        "artifacts": {
            str(name): str(Path(path).expanduser().resolve())
            for name, path in artifacts.items()
        },
        "command": list(command),
    }
    result = _hashed(preparation)
    validate_workflow_preparation(result)
    return result


def validate_workflow_preparation(payload: Mapping[str, Any]) -> None:
    _require_exact_fields(
        payload,
        {
            "schema_version",
            "schema",
            "document_type",
            "status",
            "workflow_id",
            "request_semantic_hash",
            "authorization_semantic_hash",
            "capabilities_semantic_hash",
            "authorized_through",
            "artifacts",
            "command",
            "semantic_hash",
        },
    )
    if payload.get("schema_version") != WORKFLOW_CONTRACT_SCHEMA_VERSION:
        raise WorkflowContractError(
            "unsupported workflow preparation schema version",
            code="unsupported_schema",
        )
    if payload.get("schema") != PREPARATION_SCHEMA or payload.get(
        "document_type"
    ) != PREPARATION_DOCUMENT_TYPE:
        raise WorkflowContractError(
            "unexpected workflow preparation document type",
            code="invalid_workflow_document",
        )
    if payload.get("status") != "prepared":
        raise WorkflowContractError(
            "workflow preparation status must be prepared",
            code="invalid_workflow_document",
        )
    workflow_id = payload.get("workflow_id")
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise WorkflowContractError(
            "invalid workflow ID", code="invalid_workflow_document"
        )
    for field in (
        "request_semantic_hash",
        "authorization_semantic_hash",
        "capabilities_semantic_hash",
    ):
        _require_sha256(payload.get(field), field)
    if payload.get("authorized_through") not in AUTHORIZED_THROUGH:
        raise WorkflowContractError(
            "invalid authorization stage", code="invalid_workflow_document"
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping) or not {
        "request",
        "authorization",
        "capabilities",
    }.issubset(artifacts):
        raise WorkflowContractError(
            "workflow preparation must identify its core artifacts",
            code="invalid_workflow_document",
        )
    for name, raw_path in artifacts.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(raw_path, str)
            or not Path(raw_path).is_absolute()
        ):
            raise WorkflowContractError(
                "workflow preparation artifact paths must be named and absolute",
                code="invalid_workflow_document",
            )
    command = payload.get("command")
    if not isinstance(command, list) or any(
        not isinstance(item, str) for item in command
    ):
        raise WorkflowContractError(
            "workflow preparation command must be a string array",
            code="invalid_workflow_document",
        )
    _validate_semantic_hash(payload)


def authorization_allows(
    authorization: Mapping[str, Any], operation: str
) -> bool:
    validate_workflow_authorization(authorization)
    if operation == "capabilities" or operation == "report":
        return True
    if operation not in _STAGE_INDEX:
        return False
    ceiling = str(authorization["authorized_through"])
    return _STAGE_INDEX[operation] <= _AUTHORIZATION_INDEX[ceiling]


def validate_authorization_progression(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
) -> None:
    validate_workflow_authorization(previous, request=request)
    validate_workflow_authorization(current, request=request)
    if previous.get("workflow_id") != current.get("workflow_id") or previous.get(
        "request_semantic_hash"
    ) != current.get("request_semantic_hash"):
        raise WorkflowContractError(
            "authorization progression changed workflow identity",
            code="authorization_request_mismatch",
        )
    previous_stage = str(previous["authorized_through"])
    current_stage = str(current["authorized_through"])
    if _AUTHORIZATION_INDEX[current_stage] < _AUTHORIZATION_INDEX[previous_stage]:
        raise WorkflowContractError(
            "authorization cannot narrow an existing workflow",
            code="authorization_regression",
        )
    previous_selection = previous.get("candidate_selection")
    current_selection = current.get("candidate_selection")
    if previous_selection is not None and current_selection != previous_selection:
        raise WorkflowContractError(
            "candidate selection cannot change after authorization",
            code="candidate_selection_mismatch",
        )


def build_workflow_state(
    request: Mapping[str, Any], authorization: Mapping[str, Any]
) -> dict[str, Any]:
    validate_workflow_request(request)
    validate_workflow_authorization(authorization, request=request)
    input_record = request["input"]
    state = {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "schema": STATE_SCHEMA,
        "document_type": STATE_DOCUMENT_TYPE,
        "workflow_id": request["workflow_id"],
        "request_semantic_hash": request["semantic_hash"],
        "authorization_semantic_hash": authorization["semantic_hash"],
        "revision": 0,
        "status": "ready",
        "input_identity": {
            "sha256": input_record["sha256"],
            "compile_context_hash": input_record["compile_context_hash"],
        },
        "completed_stages": [],
        "stage_results": {},
    }
    result = _hashed(state)
    validate_workflow_state(result, request=request, authorization=authorization)
    return result


def validate_workflow_state(
    payload: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
    authorization: Mapping[str, Any] | None = None,
) -> None:
    _require_exact_fields(
        payload,
        {
            "schema_version",
            "schema",
            "document_type",
            "workflow_id",
            "request_semantic_hash",
            "authorization_semantic_hash",
            "revision",
            "status",
            "input_identity",
            "completed_stages",
            "stage_results",
            "semantic_hash",
        },
        {"failure"},
    )
    if payload.get("schema_version") != WORKFLOW_CONTRACT_SCHEMA_VERSION:
        raise WorkflowContractError(
            "unsupported workflow state schema version", code="unsupported_schema"
        )
    if payload.get("schema") != STATE_SCHEMA or payload.get(
        "document_type"
    ) != STATE_DOCUMENT_TYPE:
        raise WorkflowContractError(
            "unexpected workflow state document type",
            code="invalid_workflow_document",
        )
    workflow_id = payload.get("workflow_id")
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise WorkflowContractError(
            "invalid workflow ID", code="invalid_workflow_document"
        )
    if payload.get("status") not in {
        "ready",
        "running",
        "waiting_authorization",
        "blocked",
        "failed",
        "completed",
        "untrusted",
    }:
        raise WorkflowContractError(
            "invalid workflow state status", code="invalid_workflow_document"
        )
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise WorkflowContractError(
            "workflow revision must be a non-negative integer",
            code="invalid_workflow_document",
        )
    identity = payload.get("input_identity")
    if not isinstance(identity, Mapping):
        raise WorkflowContractError(
            "workflow input identity must be an object",
            code="invalid_workflow_document",
        )
    _require_exact_fields(identity, {"sha256", "compile_context_hash"})
    _require_sha256(identity.get("sha256"), "input_identity.sha256")
    _require_sha256(
        identity.get("compile_context_hash"), "input_identity.compile_context_hash"
    )
    completed = payload.get("completed_stages")
    if not isinstance(completed, list) or any(
        not isinstance(stage, str) for stage in completed
    ):
        raise WorkflowContractError(
            "completed stages must be a string array",
            code="invalid_workflow_document",
        )
    expected_prefix = list(EXECUTION_STAGES[: len(completed)])
    if completed != expected_prefix:
        raise WorkflowContractError(
            "completed workflow stages must form an ordered prefix",
            code="invalid_stage_history",
        )
    results = payload.get("stage_results")
    if not isinstance(results, Mapping) or set(results) != set(completed):
        raise WorkflowContractError(
            "stage results must match completed stages",
            code="invalid_stage_history",
        )
    for stage, result in results.items():
        if not isinstance(result, Mapping):
            raise WorkflowContractError(
                f"stage result for {stage} must be an object",
                code="invalid_stage_history",
            )
        _require_exact_fields(
            result,
            {"status", "document_type", "semantic_hash", "artifact_path", "facts"},
        )
        if not isinstance(result.get("status"), str) or not result.get("status"):
            raise WorkflowContractError(
                f"stage result for {stage} has no status",
                code="invalid_stage_history",
            )
        if not isinstance(result.get("document_type"), str) or not result.get(
            "document_type"
        ):
            raise WorkflowContractError(
                f"stage result for {stage} has no document type",
                code="invalid_stage_history",
            )
        _require_sha256(result.get("semantic_hash"), f"stage_results.{stage}.semantic_hash")
        artifact_path = result.get("artifact_path")
        if not isinstance(artifact_path, str) or not Path(artifact_path).is_absolute():
            raise WorkflowContractError(
                f"stage artifact path for {stage} must be absolute",
                code="invalid_stage_history",
            )
        if not isinstance(result.get("facts"), Mapping):
            raise WorkflowContractError(
                f"stage facts for {stage} must be an object",
                code="invalid_stage_history",
            )
    failure = payload.get("failure")
    if failure is not None:
        if not isinstance(failure, Mapping):
            raise WorkflowContractError(
                "workflow failure must be an object",
                code="invalid_workflow_document",
            )
        _require_exact_fields(
            failure,
            {"stage", "code", "message"},
            {"artifact_path"},
        )
        if failure.get("stage") not in FAILURE_STAGES:
            raise WorkflowContractError(
                "workflow failure has an invalid stage",
                code="invalid_workflow_document",
            )
        for name in ("code", "message"):
            if not isinstance(failure.get(name), str) or not failure.get(name):
                raise WorkflowContractError(
                    f"workflow failure {name} must be a non-empty string",
                    code="invalid_workflow_document",
                )
        failure_path = failure.get("artifact_path")
        if failure_path is not None and (
            not isinstance(failure_path, str)
            or not Path(failure_path).is_absolute()
        ):
            raise WorkflowContractError(
                "workflow failure artifact path must be absolute",
                code="invalid_workflow_document",
            )
    _require_sha256(payload.get("request_semantic_hash"), "request_semantic_hash")
    _require_sha256(
        payload.get("authorization_semantic_hash"), "authorization_semantic_hash"
    )
    _validate_semantic_hash(payload)
    if request is not None:
        validate_workflow_request(request)
        if payload.get("workflow_id") != request.get("workflow_id") or payload.get(
            "request_semantic_hash"
        ) != request.get("semantic_hash"):
            raise WorkflowContractError(
                "state is not bound to this workflow request",
                code="state_request_mismatch",
            )
        if identity != {
            "sha256": request["input"]["sha256"],
            "compile_context_hash": request["input"]["compile_context_hash"],
        }:
            raise WorkflowContractError(
                "state input identity differs from the workflow request",
                code="state_request_mismatch",
            )
    if authorization is not None:
        validate_workflow_authorization(authorization, request=request)
        if (
            payload.get("workflow_id") != authorization.get("workflow_id")
            or payload.get("request_semantic_hash")
            != authorization.get("request_semantic_hash")
            or payload.get("authorization_semantic_hash")
            != authorization.get("semantic_hash")
        ):
            raise WorkflowContractError(
                "state does not use the current authorization",
                code="state_authorization_mismatch",
            )
        for stage in completed:
            if not authorization_allows(authorization, stage):
                raise WorkflowContractError(
                    f"completed stage {stage} exceeds authorization",
                    code="stage_not_authorized",
                )


def validate_input_integrity(
    state: Mapping[str, Any],
    *,
    source_sha256: str,
    compile_context_hash: str,
) -> None:
    _require_sha256(source_sha256, "source_sha256")
    _require_sha256(compile_context_hash, "compile_context_hash")
    identity = state.get("input_identity")
    if not isinstance(identity, Mapping) or source_sha256 != identity.get(
        "sha256"
    ) or compile_context_hash != identity.get("compile_context_hash"):
        raise WorkflowContractError(
            "source or compile context changed after workflow creation",
            code="stale_input",
        )


def next_authorized_stage(
    state: Mapping[str, Any], authorization: Mapping[str, Any]
) -> str | None:
    validate_workflow_state(state, authorization=authorization)
    if state.get("status") in {"blocked", "failed", "untrusted"}:
        return None
    completed = list(state["completed_stages"])
    if len(completed) == len(EXECUTION_STAGES):
        return None
    stage = EXECUTION_STAGES[len(completed)]
    if not authorization_allows(authorization, stage):
        return None
    results = state["stage_results"]
    if stage == "candidate" and results["review"]["facts"].get(
        "candidate_generation_allowed"
    ) is not True:
        return None
    if stage == "verify" and results["candidate"].get("status") != "candidate_prepared":
        return None
    if stage == "measure":
        verification = results["verify"]
        if verification.get("status") != "formal_passed" or verification[
            "facts"
        ].get("safe") is not True:
            return None
    return stage


def validate_stage_start(
    state: Mapping[str, Any],
    authorization: Mapping[str, Any],
    stage: str,
    *,
    source_sha256: str | None = None,
    compile_context_hash: str | None = None,
) -> None:
    validate_workflow_state(state, authorization=authorization)
    completed = list(state["completed_stages"])
    if stage in completed:
        raise WorkflowContractError(
            f"stage {stage} is already complete", code="stage_already_completed"
        )
    expected = EXECUTION_STAGES[len(completed)] if len(completed) < len(
        EXECUTION_STAGES
    ) else None
    if stage != expected:
        raise WorkflowContractError(
            f"stage {stage} cannot start; expected {expected}",
            code="invalid_stage_transition",
        )
    if not authorization_allows(authorization, stage):
        raise WorkflowContractError(
            f"stage {stage} exceeds authorization",
            code="stage_not_authorized",
        )
    if stage in {"candidate", "verify", "measure"}:
        if source_sha256 is None or compile_context_hash is None:
            raise WorkflowContractError(
                f"stage {stage} requires current input hashes",
                code="missing_input_integrity",
            )
        validate_input_integrity(
            state,
            source_sha256=source_sha256,
            compile_context_hash=compile_context_hash,
        )
    results = state["stage_results"]
    if stage == "candidate" and results["review"]["facts"].get(
        "candidate_generation_allowed"
    ) is not True:
        raise WorkflowContractError(
            "review did not authorize candidate generation",
            code="candidate_not_eligible",
        )
    if stage == "verify" and results["candidate"].get("status") != "candidate_prepared":
        raise WorkflowContractError(
            "candidate is not prepared", code="candidate_not_prepared"
        )
    if stage == "measure":
        verification = results["verify"]
        if verification.get("status") != "formal_passed" or verification[
            "facts"
        ].get("safe") is not True:
            raise WorkflowContractError(
                "measurement requires a current safe formal pass",
                code="formal_pass_required",
            )


def record_stage_completion(
    state: Mapping[str, Any],
    authorization: Mapping[str, Any],
    stage: str,
    result: Mapping[str, Any],
    *,
    source_sha256: str | None = None,
    compile_context_hash: str | None = None,
) -> dict[str, Any]:
    validate_stage_start(
        state,
        authorization,
        stage,
        source_sha256=source_sha256,
        compile_context_hash=compile_context_hash,
    )
    _require_exact_fields(
        result,
        {"status", "document_type", "semantic_hash", "artifact_path", "facts"},
    )
    new_results = dict(state["stage_results"])
    new_results[stage] = dict(result)
    completed = [*state["completed_stages"], stage]
    updated = {
        key: value
        for key, value in state.items()
        if key not in {"semantic_hash", "revision", "status", "completed_stages", "stage_results"}
    }
    updated.update(
        {
            "revision": int(state["revision"]) + 1,
            "status": "ready",
            "completed_stages": completed,
            "stage_results": new_results,
        }
    )
    provisional = _hashed(updated)
    next_stage = next_authorized_stage(provisional, authorization)
    if next_stage is None or (
        stage == "review"
        and result.get("facts", {}).get("candidate_generation_allowed") is not True
    ):
        updated["status"] = "completed"
    final = _hashed(updated)
    validate_workflow_state(final, authorization=authorization)
    return final


def apply_authorization(
    state: Mapping[str, Any],
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    validate_workflow_state(state, request=request, authorization=previous)
    validate_authorization_progression(previous, current, request=request)
    updated = {
        key: value
        for key, value in state.items()
        if key not in {"semantic_hash", "authorization_semantic_hash", "revision", "status"}
    }
    updated.update(
        {
            "authorization_semantic_hash": current["semantic_hash"],
            "revision": int(state["revision"]) + 1,
            "status": "ready",
        }
    )
    provisional = _hashed(updated)
    if next_authorized_stage(provisional, current) is None:
        updated["status"] = "completed"
    final = _hashed(updated)
    validate_workflow_state(final, request=request, authorization=current)
    return final


def record_workflow_failure(
    state: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    stage: str,
    code: str,
    message: str,
    status: str = "failed",
    artifact_path: str | Path | None = None,
) -> dict[str, Any]:
    validate_workflow_state(state, authorization=authorization)
    if stage not in FAILURE_STAGES:
        raise WorkflowContractError(
            "invalid failed workflow stage", code="invalid_stage_transition"
        )
    if status not in {"blocked", "failed", "untrusted"}:
        raise WorkflowContractError(
            "invalid workflow failure status", code="invalid_workflow_document"
        )
    if not code or not message:
        raise WorkflowContractError(
            "workflow failure requires a code and message",
            code="invalid_workflow_document",
        )
    failure: dict[str, Any] = {
        "stage": stage,
        "code": code,
        "message": message,
    }
    if artifact_path is not None:
        path = Path(artifact_path).expanduser().resolve()
        failure["artifact_path"] = str(path)
    updated = {
        key: value
        for key, value in state.items()
        if key not in {"semantic_hash", "revision", "status", "failure"}
    }
    updated.update(
        {
            "revision": int(state["revision"]) + 1,
            "status": status,
            "failure": failure,
        }
    )
    final = _hashed(updated)
    validate_workflow_state(final, authorization=authorization)
    return final
