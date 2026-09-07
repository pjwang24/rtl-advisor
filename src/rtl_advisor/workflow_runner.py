from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_agent import (
    MVPAgentError,
    agent_v2_candidate,
    agent_v2_capabilities,
    agent_v2_error_payload,
    agent_v2_measure,
    agent_v2_report,
    agent_v2_review,
    agent_v2_verify,
    _resolve_design,
)
from rtl_advisor.mvp_schema import (
    MVPSchemaError,
    compile_context_snapshot,
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.workflow_contract import (
    AUTHORIZED_THROUGH,
    OBJECTIVES,
    SOURCE_KINDS,
    SUMMARY_DOCUMENT_TYPE,
    SUMMARY_SCHEMA,
    WORKFLOW_CONTRACT_SCHEMA_VERSION,
    WorkflowContractError,
    apply_authorization,
    build_workflow_authorization,
    build_workflow_preparation,
    build_workflow_request,
    build_workflow_state,
    next_authorized_stage,
    record_stage_completion,
    record_workflow_failure,
    validate_authorization_progression,
    validate_workflow_authorization,
    validate_workflow_request,
    validate_workflow_state,
)


_WORKFLOW_ID = re.compile(r"^workflow-[0-9a-f]{20}$")
_AGENT_TYPES = {
    "capabilities": "rtl-advisor.agent.v2.capabilities",
    "review": "rtl-advisor.agent.v2.review",
    "candidate": "rtl-advisor.agent.v2.candidate",
    "verify": "rtl-advisor.agent.v2.verification",
    "measure": "rtl-advisor.agent.v2.measurement",
    "report": "rtl-advisor.agent.v2.report",
}
_UNTRUSTED_CODES = {
    "artifact_hash_mismatch",
    "artifact_parent_mismatch",
    "semantic_hash_mismatch",
    "unsupported_schema",
    "invalid_artifact",
    "invalid_stage_history",
}


class WorkflowRunnerError(RuntimeError):
    """Raised when a workflow cannot safely start, resume, or report."""

    def __init__(self, message: str, *, code: str = "workflow_runner_error") -> None:
        super().__init__(message)
        self.code = code


def _workflow_root(config: ProjectConfig, workflow_id: str) -> Path:
    if not _WORKFLOW_ID.fullmatch(workflow_id):
        raise WorkflowRunnerError(
            f"invalid workflow ID: {workflow_id!r}", code="invalid_workflow_id"
        )
    return config.artifacts_dir / "workflows-v1" / workflow_id


def _load_mapping(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowRunnerError(
            f"invalid workflow document {resolved}: {exc}",
            code="invalid_workflow_document",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowRunnerError(
            f"workflow document must be an object: {resolved}",
            code="invalid_workflow_document",
        )
    return payload


def _persist_immutable(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = dict(payload)
    if path.is_file():
        existing = read_hashed_json(path)
        if existing != expected:
            raise WorkflowRunnerError(
                f"append-only workflow artifact conflicts: {path}",
                code="append_only_conflict",
            )
        return existing
    try:
        return write_hashed_json(path, expected, exclusive=True)
    except MVPSchemaError as exc:
        if exc.code == "append_only_conflict" and path.is_file():
            existing = read_hashed_json(path)
            if existing == expected:
                return existing
        raise WorkflowRunnerError(str(exc), code=exc.code) from exc


def _write_pointer(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    return write_hashed_json(path, payload)


def _persist_request(root: Path, request: Mapping[str, Any]) -> Path:
    validate_workflow_request(request)
    path = root / "request.json"
    _persist_immutable(path, request)
    return path


def _persist_authorization(root: Path, authorization: Mapping[str, Any]) -> Path:
    path = root / "authorizations" / f"{authorization['authorization_id']}.json"
    _persist_immutable(path, authorization)
    _write_pointer(
        root / "authorization-latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.workflow.authorization-latest",
            "workflow_id": authorization["workflow_id"],
            "authorization_semantic_hash": authorization["semantic_hash"],
            "authorization_path": str(path),
        },
    )
    return path


def _load_request(root: Path) -> dict[str, Any]:
    request = read_hashed_json(root / "request.json")
    validate_workflow_request(request)
    return request


def _load_latest_authorization(
    root: Path, *, request: Mapping[str, Any]
) -> dict[str, Any]:
    pointer = read_hashed_json(
        root / "authorization-latest.json",
        document_type="rtl-advisor.workflow.authorization-latest",
        schema_version=1,
    )
    path = Path(str(pointer.get("authorization_path", ""))).expanduser().resolve()
    authorization = read_hashed_json(path)
    validate_workflow_authorization(authorization, request=request)
    if pointer.get("authorization_semantic_hash") != authorization.get(
        "semantic_hash"
    ):
        raise WorkflowRunnerError(
            "authorization pointer hash mismatch",
            code="artifact_parent_mismatch",
        )
    return authorization


def _persist_state(root: Path, state: Mapping[str, Any]) -> Path:
    path = root / "states" / f"{int(state['revision']):06d}.json"
    _persist_immutable(path, state)
    _write_pointer(
        root / "state-latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.workflow.state-latest",
            "workflow_id": state["workflow_id"],
            "state_revision": state["revision"],
            "state_semantic_hash": state["semantic_hash"],
            "state_path": str(path),
        },
    )
    return path


def _load_latest_state(
    root: Path,
    *,
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    pointer = read_hashed_json(
        root / "state-latest.json",
        document_type="rtl-advisor.workflow.state-latest",
        schema_version=1,
    )
    path = Path(str(pointer.get("state_path", ""))).expanduser().resolve()
    state = read_hashed_json(path)
    validate_workflow_state(state, request=request, authorization=authorization)
    if pointer.get("state_semantic_hash") != state.get("semantic_hash") or pointer.get(
        "state_revision"
    ) != state.get("revision"):
        raise WorkflowRunnerError(
            "state pointer does not match its immutable snapshot",
            code="artifact_parent_mismatch",
        )
    return state


def _validate_agent_payload(payload: Mapping[str, Any], operation: str) -> None:
    if payload.get("schema_version") != 2:
        raise WorkflowRunnerError(
            "agent payload has an unsupported schema", code="unsupported_schema"
        )
    if payload.get("run_schema") != "rtl-advisor-run-v1" or payload.get(
        "flow_version"
    ) != "rtl-advisor-agent-v2":
        raise WorkflowRunnerError(
            "agent payload has an unsupported flow contract",
            code="unsupported_schema",
        )
    if payload.get("document_type") != _AGENT_TYPES[operation]:
        raise WorkflowRunnerError(
            f"unexpected Agent V2 document for {operation}",
            code="invalid_artifact",
        )
    expected = payload.get("semantic_hash")
    core = {key: value for key, value in payload.items() if key != "semantic_hash"}
    if expected != stable_hash(core):
        raise WorkflowRunnerError(
            "Agent V2 semantic hash mismatch", code="semantic_hash_mismatch"
        )
    if not isinstance(payload.get("command"), list):
        raise WorkflowRunnerError(
            "Agent V2 payload has no normalized command", code="invalid_artifact"
        )


def _snapshot_agent_payload(
    root: Path, operation: str, payload: Mapping[str, Any]
) -> tuple[Path, dict[str, Any]]:
    _validate_agent_payload(payload, operation)
    path = root / "stages" / f"{operation}.json"
    written = _persist_immutable(path, payload)
    facts: dict[str, Any] = {}
    decision = payload.get("decision")
    if isinstance(decision, str):
        facts["decision"] = decision
    if operation == "review":
        facts["candidate_generation_allowed"] = (
            payload.get("candidate_generation_allowed") is True
        )
    if operation == "verify":
        facts["safe"] = payload.get("safe") is True
    return path, {
        "status": str(payload.get("status", "")),
        "document_type": str(payload.get("document_type", "")),
        "semantic_hash": str(payload.get("semantic_hash", "")),
        "artifact_path": str(path),
        "facts": facts,
    }


def _load_stage(root: Path, stage: str) -> dict[str, Any]:
    payload = read_hashed_json(root / "stages" / f"{stage}.json")
    _validate_agent_payload(payload, stage)
    return payload


def _agent_command(
    config: ProjectConfig, operation: str, arguments: Sequence[str] = ()
) -> tuple[str, ...]:
    return (
        "rtl-advisor",
        "--config",
        str(config.config_path),
        "agent",
        operation,
        *arguments,
        "--schema-version",
        "2",
        "--json",
    )


def _current_input_hashes(request: Mapping[str, Any]) -> tuple[str, str]:
    input_record = request["input"]
    path = Path(str(input_record["path"])).expanduser().resolve()
    if not path.is_file():
        raise WorkflowRunnerError(
            f"workflow input is unavailable: {path}", code="stale_input"
        )
    current = file_sha256(path)
    if current != input_record["sha256"]:
        raise WorkflowRunnerError(
            "workflow input changed after request creation", code="stale_input"
        )
    return current, str(input_record["compile_context_hash"])


def _validate_review_binding(
    review: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    requested = request["input"]
    agent_input = review.get("input")
    if not isinstance(agent_input, Mapping):
        raise WorkflowRunnerError(
            "review has no normalized input record", code="invalid_artifact"
        )
    if review.get("objective") != request.get("objective"):
        raise WorkflowRunnerError(
            "review objective differs from the workflow request",
            code="artifact_parent_mismatch",
        )
    if requested["kind"] == "qualified_corpus_reference":
        observed = agent_input.get("reference_manifest_hash")
        if agent_input.get("kind") != "qualified_reference":
            raise WorkflowRunnerError(
                "review did not load a qualified corpus reference",
                code="invalid_input_kind",
            )
    else:
        observed = agent_input.get("compile_context_hash")
    if observed != requested["compile_context_hash"]:
        raise WorkflowRunnerError(
            "review compile context differs from the workflow request",
            code="stale_input",
        )
    integrity = agent_input.get("source_integrity")
    if not isinstance(integrity, Mapping) or integrity.get("ok") is not True:
        raise WorkflowRunnerError(
            "review source-integrity check did not pass", code="stale_input"
        )


def _select_finding(
    review: Mapping[str, Any], authorization: Mapping[str, Any]
) -> str:
    findings = review.get("findings")
    if not isinstance(findings, list) or not findings:
        raise WorkflowRunnerError(
            "review produced no eligible finding", code="candidate_not_eligible"
        )
    selection = authorization.get("candidate_selection")
    if not isinstance(selection, Mapping):
        raise WorkflowRunnerError(
            "candidate stage has no authorized selection",
            code="candidate_selection_required",
        )
    if selection.get("mode") == "finding_id":
        requested = str(selection.get("finding_id", ""))
        if any(
            isinstance(item, Mapping) and item.get("finding_id") == requested
            for item in findings
        ):
            return requested
        raise WorkflowRunnerError(
            f"authorized finding is not present: {requested}",
            code="finding_not_found",
        )
    ordered = sorted(
        (item for item in findings if isinstance(item, Mapping)),
        key=lambda item: (
            item.get("rank") if isinstance(item.get("rank"), int) else 2**31,
            str(item.get("finding_id", "")),
        ),
    )
    finding_id = str(ordered[0].get("finding_id", "")) if ordered else ""
    if not finding_id:
        raise WorkflowRunnerError(
            "eligible finding has no stable ID", code="invalid_artifact"
        )
    return finding_id


def _capability_blocker(
    capabilities: Mapping[str, Any], authorization: Mapping[str, Any]
) -> str | None:
    return _capability_blocker_for_ceiling(
        capabilities, str(authorization["authorized_through"])
    )


def _capability_blocker_for_ceiling(
    capabilities: Mapping[str, Any], authorized_through: str
) -> str | None:
    operations = capabilities.get("operations")
    if not isinstance(operations, Mapping):
        return "capabilities payload has no operation availability"
    order = ("review", "candidate", "verify", "measure")
    for operation in order[: order.index(authorized_through) + 1]:
        status = operations.get(operation)
        if not isinstance(status, Mapping) or status.get("available") is not True:
            return f"requested operation is unavailable: {operation}"
    return None


def _run_stage(
    config: ProjectConfig,
    root: Path,
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
    stage: str,
    capabilities_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    input_record = request["input"]
    if stage == "capabilities":
        if capabilities_override is not None:
            _validate_agent_payload(capabilities_override, "capabilities")
            return dict(capabilities_override)
        return agent_v2_capabilities(
            config, normalized_command=_agent_command(config, "capabilities")
        )
    if stage == "review":
        command_arguments: list[str] = [
            str(input_record["path"]),
            "--objective",
            str(request["objective"]),
        ]
        top = input_record.get("top")
        if isinstance(top, str):
            command_arguments.extend(("--top", top))
        for include_dir in input_record["include_dirs"]:
            command_arguments.extend(("-I", str(include_dir)))
        for definition in input_record["defines"]:
            command_arguments.extend(("-D", str(definition)))
        payload = agent_v2_review(
            config,
            str(input_record["path"]),
            objective=str(request["objective"]),
            top=top if isinstance(top, str) else None,
            include_dirs=tuple(str(item) for item in input_record["include_dirs"]),
            defines=tuple(str(item) for item in input_record["defines"]),
            normalized_command=_agent_command(config, "review", command_arguments),
        )
        _validate_review_binding(payload, request)
        return payload
    review = _load_stage(root, "review")
    if stage == "candidate":
        finding_id = _select_finding(review, authorization)
        return agent_v2_candidate(
            config,
            str(review["run_id"]),
            finding_id=finding_id,
            normalized_command=_agent_command(
                config, "candidate", (str(review["run_id"]), "--finding", finding_id)
            ),
        )
    candidate = _load_stage(root, "candidate")
    candidate_id = str(candidate["candidate_id"])
    if stage == "verify":
        return agent_v2_verify(
            config,
            str(review["run_id"]),
            candidate_id=candidate_id,
            normalized_command=_agent_command(
                config,
                "verify",
                (str(review["run_id"]), "--candidate", candidate_id),
            ),
        )
    return agent_v2_measure(
        config,
        str(review["run_id"]),
        candidate_id=candidate_id,
        normalized_command=_agent_command(
            config,
            "measure",
            (str(review["run_id"]), "--candidate", candidate_id),
        ),
    )


def _failure_payload(
    workflow_id: str,
    stage: str,
    error: Exception,
    *,
    command: Sequence[str] = (),
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.workflow.failure",
        "workflow_id": workflow_id,
        "stage": stage,
        "status": "failed",
        "error": {
            "code": str(getattr(error, "code", "workflow_runner_error")),
            "message": str(error),
        },
        "command": list(command),
    }
    payload["semantic_hash"] = stable_hash(payload)
    return payload


def _record_failure(
    root: Path,
    state: Mapping[str, Any],
    authorization: Mapping[str, Any],
    *,
    stage: str,
    error: Exception,
    status: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    failure = dict(payload or _failure_payload(str(state["workflow_id"]), stage, error))
    failure_path = root / "failures" / f"{int(state['revision']) + 1:06d}-{stage}.json"
    _persist_immutable(failure_path, failure)
    code = str(getattr(error, "code", "workflow_runner_error"))
    failure_status = status or ("untrusted" if code in _UNTRUSTED_CODES else "failed")
    updated = record_workflow_failure(
        state,
        authorization,
        stage=stage,
        code=code,
        message=str(error),
        status=failure_status,
        artifact_path=failure_path,
    )
    _persist_state(root, updated)
    return updated


def _report_for_state(
    config: ProjectConfig,
    root: Path,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    if "review" not in state["completed_stages"]:
        return None
    pointer_path = root / "agent-report-latest.json"
    if pointer_path.is_file():
        pointer = read_hashed_json(pointer_path)
        if pointer.get("state_semantic_hash") == state.get("semantic_hash"):
            payload = read_hashed_json(Path(str(pointer["report_path"])))
            _validate_agent_payload(payload, "report")
            return payload
    review = _load_stage(root, "review")
    payload = agent_v2_report(
        config,
        str(review["run_id"]),
        normalized_command=_agent_command(config, "report", (str(review["run_id"]),)),
    )
    _validate_agent_payload(payload, "report")
    snapshot = root / "reports" / f"{state['semantic_hash']}.json"
    written = _persist_immutable(snapshot, payload)
    _write_pointer(
        pointer_path,
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.workflow.agent-report-latest",
            "workflow_id": state["workflow_id"],
            "state_semantic_hash": state["semantic_hash"],
            "report_semantic_hash": written["semantic_hash"],
            "report_path": str(snapshot),
        },
    )
    return written


def _existing_report(root: Path, state: Mapping[str, Any]) -> dict[str, Any] | None:
    pointer_path = root / "agent-report-latest.json"
    if not pointer_path.is_file():
        return None
    pointer = read_hashed_json(pointer_path)
    if pointer.get("state_semantic_hash") != state.get("semantic_hash"):
        return None
    payload = read_hashed_json(Path(str(pointer["report_path"])))
    _validate_agent_payload(payload, "report")
    return payload


def _summary_next_action(
    state: Mapping[str, Any], authorization: Mapping[str, Any]
) -> str:
    if state.get("status") in {"blocked", "failed", "untrusted"}:
        return "inspect_failure"
    completed = set(state["completed_stages"])
    if "capabilities" not in completed:
        return "run_capabilities"
    if "review" not in completed:
        return "run_review"
    review = state["stage_results"]["review"]
    if review["facts"].get("candidate_generation_allowed") is not True:
        return "none"
    if "candidate" not in completed:
        return (
            "prepare_candidate"
            if authorization["authorized_through"] != "review"
            else "request_candidate_authorization"
        )
    if "verify" not in completed:
        return (
            "run_verification"
            if authorization["authorized_through"] in {"verify", "measure"}
            else "request_verify_authorization"
        )
    verification = state["stage_results"]["verify"]
    if verification.get("status") != "formal_passed" or verification[
        "facts"
    ].get("safe") is not True:
        return "none"
    if "measure" not in completed:
        return (
            "run_measurement"
            if authorization["authorized_through"] == "measure"
            else "request_measure_authorization"
        )
    return "none"


def _build_summary(
    root: Path,
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
    state: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    stage_payloads = [
        _load_stage(root, stage) for stage in state["completed_stages"]
    ]
    last = stage_payloads[-1] if stage_payloads else {}
    decision = str(last.get("decision", "pending"))
    if decision not in {
        "pending",
        "candidate_available",
        "no_change",
        "unsupported",
        "candidate_prepared",
        "formal_passed",
        "formal_failed",
        "formal_inconclusive",
        "measured_improvement",
        "synthesis_handles",
        "flow_dependent",
        "regression",
        "evidence_incomplete",
    }:
        decision = "evidence_incomplete"
    verification = (
        _load_stage(root, "verify") if "verify" in state["completed_stages"] else None
    )
    safe = bool(
        isinstance(verification, Mapping)
        and verification.get("status") == "formal_passed"
        and verification.get("safe") is True
    )
    limitations: list[str] = []
    for payload in stage_payloads:
        for item in payload.get("limitations") or []:
            if isinstance(item, str) and item not in limitations:
                limitations.append(item)
    failure = state.get("failure")
    if isinstance(failure, Mapping) and str(failure.get("message")) not in limitations:
        limitations.append(str(failure.get("message")))
    artifacts: dict[str, str] = {
        "workflow_root": str(root),
        "request": str(root / "request.json"),
        "authorization": str(
            root
            / "authorizations"
            / f"{authorization['authorization_id']}.json"
        ),
        "state": str(root / "states" / f"{int(state['revision']):06d}.json"),
    }
    for stage in state["completed_stages"]:
        artifacts[stage] = str(root / "stages" / f"{stage}.json")
    if isinstance(report, Mapping):
        artifacts["agent_report"] = str(
            root / "reports" / f"{state['semantic_hash']}.json"
        )
        agent_artifacts = report.get("artifacts")
        if isinstance(agent_artifacts, Mapping):
            html_path = agent_artifacts.get("html")
            if isinstance(html_path, str) and html_path:
                artifacts["html_report"] = html_path
    summary: dict[str, Any] = {
        "schema_version": WORKFLOW_CONTRACT_SCHEMA_VERSION,
        "schema": SUMMARY_SCHEMA,
        "document_type": SUMMARY_DOCUMENT_TYPE,
        "workflow_id": request["workflow_id"],
        "request_semantic_hash": request["semantic_hash"],
        "authorization_semantic_hash": authorization["semantic_hash"],
        "state_semantic_hash": state["semantic_hash"],
        "status": state["status"],
        "authorized_through": authorization["authorized_through"],
        "completed_stages": list(state["completed_stages"]),
        "decision": decision,
        "safe": safe,
        "limitations": limitations,
        "next_action": _summary_next_action(state, authorization),
        "artifacts": artifacts,
        "normalized_commands": [
            list(payload["command"])
            for payload in stage_payloads
            if isinstance(payload.get("command"), list)
        ],
    }
    if "measure" in state["completed_stages"]:
        measurement = _load_stage(root, "measure")
        profiles: dict[str, Any] = {}
        for name, result in (measurement.get("measurements") or {}).items():
            if isinstance(result, Mapping) and isinstance(
                result.get("classification"), str
            ):
                profiles[str(name)] = {"classification": result["classification"]}
        if profiles:
            summary["profile_results"] = profiles
    summary["semantic_hash"] = stable_hash(summary)
    return summary


def _persist_summary(root: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    path = root / "summaries" / f"{summary['state_semantic_hash']}.json"
    written = _persist_immutable(path, summary)
    _write_pointer(
        root / "summary-latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.workflow.summary-latest",
            "workflow_id": summary["workflow_id"],
            "summary_semantic_hash": summary["semantic_hash"],
            "summary_path": str(path),
        },
    )
    return written


_DIGEST_ACTIONS = {
    "measured_improvement": "recommend_change",
    "no_change": "no_change",
    "synthesis_handles": "no_change",
    "regression": "no_change",
    "unsupported": "unsupported",
}


def _digest_action(decision: str) -> str:
    return _DIGEST_ACTIONS.get(decision, "inconclusive")


def _selected_finding(
    review: Mapping[str, Any] | None,
    authorization: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    if not isinstance(review, Mapping):
        return None
    findings = review.get("findings")
    if not isinstance(findings, list):
        return None
    selection = authorization.get("candidate_selection")
    if isinstance(selection, Mapping) and selection.get("mode") == "finding_id":
        finding_id = selection.get("finding_id")
        for item in findings:
            if isinstance(item, Mapping) and item.get("finding_id") == finding_id:
                return item
    ordered = sorted(
        (item for item in findings if isinstance(item, Mapping)),
        key=lambda item: (
            item.get("rank") if isinstance(item.get("rank"), int) else 2**31,
            str(item.get("finding_id", "")),
        ),
    )
    return ordered[0] if ordered else None


def _digest_rationale(
    decision: str, finding: Mapping[str, Any] | None
) -> str:
    reason = finding.get("reason") if isinstance(finding, Mapping) else None
    if isinstance(reason, str) and reason:
        return reason
    return {
        "no_change": "No eligible registered transformation was found.",
        "unsupported": "The requested input is outside the supported deterministic rules.",
        "formal_failed": "The candidate did not pass its registered formal contract.",
        "formal_inconclusive": "Formal evidence was inconclusive.",
        "measured_improvement": "Both pinned synthesis profiles measured an improvement.",
        "synthesis_handles": "The pinned synthesis profiles neutralized the source-level change.",
        "flow_dependent": "The pinned synthesis profiles produced different classifications.",
        "regression": "At least one pinned synthesis profile measured a regression.",
        "evidence_incomplete": "Required evidence is incomplete.",
    }.get(decision, "The workflow has not reached a terminal evidence state.")


def _build_digest(
    config: ProjectConfig,
    root: Path,
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
    state: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    completed = set(state["completed_stages"])
    review = _load_stage(root, "review") if "review" in completed else None
    candidate = _load_stage(root, "candidate") if "candidate" in completed else None
    verification = _load_stage(root, "verify") if "verify" in completed else None
    measurement = _load_stage(root, "measure") if "measure" in completed else None
    finding = _selected_finding(review, authorization)
    source_locations: list[dict[str, Any]] = []
    if isinstance(finding, Mapping) and isinstance(finding.get("source"), Mapping):
        source = finding["source"]
        location = {
            key: source[key]
            for key in ("file", "line", "column", "end_line", "end_column")
            if key in source
        }
        if location:
            source_locations.append(location)
    excluded = review.get("excluded_sites") if isinstance(review, Mapping) else None
    decision = str(summary["decision"])
    evidence_complete = bool(
        summary.get("status") == "completed"
        and (
            decision in {"no_change", "unsupported"}
            or (
                decision
                in {
                    "measured_improvement",
                    "synthesis_handles",
                    "flow_dependent",
                    "regression",
                }
                and summary.get("safe") is True
                and measurement is not None
            )
        )
    )
    profile_results = {
        str(name): str(result.get("classification"))
        for name, result in ((measurement or {}).get("measurements") or {}).items()
        if isinstance(result, Mapping) and isinstance(result.get("classification"), str)
    }
    finding_record = None
    if isinstance(finding, Mapping):
        finding_record = {
            key: finding[key]
            for key in ("finding_id", "transformation_id")
            if isinstance(finding.get(key), str)
        }
    artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), Mapping) else {}
    digest: dict[str, Any] = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-digest-v1",
        "document_type": "rtl-advisor.workflow.digest",
        "workflow_id": request["workflow_id"],
        "status": summary["status"],
        "decision": decision,
        "action": _digest_action(decision),
        "scope": (
            "finding_only"
            if finding_record
            else "whole_input"
            if decision in {"no_change", "unsupported"} and not excluded
            else "partial_or_pending"
        ),
        "source": {
            "kind": request["input"]["kind"],
            "path": request["input"]["path"],
            "sha256": request["input"]["sha256"],
        },
        "source_locations": source_locations,
        "rationale": _digest_rationale(decision, finding),
        "finding": finding_record,
        "candidate_id": (
            str(candidate["candidate_id"])
            if isinstance(candidate, Mapping) and isinstance(candidate.get("candidate_id"), str)
            else None
        ),
        "formal_status": (
            str(verification.get("status"))
            if isinstance(verification, Mapping)
            else "not_run"
        ),
        "measurement_status": (
            str(measurement.get("status"))
            if isinstance(measurement, Mapping)
            else "not_run"
        ),
        "profile_results": profile_results,
        "safe": summary.get("safe") is True,
        "evidence_complete": evidence_complete,
        "next_action": summary["next_action"],
        "parent": {
            "summary_semantic_hash": summary["semantic_hash"],
            "state_semantic_hash": summary["state_semantic_hash"],
        },
        "artifacts": {
            key: artifacts[key]
            for key in ("agent_report", "html_report")
            if key in artifacts
        },
        "reproduce": [
            "rtl-advisor",
            "--config",
            str(config.config_path),
            "agent",
            "workflow",
            "status",
            str(request["workflow_id"]),
            "--compact",
            "--schema-version",
            "1",
            "--json",
        ],
    }
    digest["artifacts"]["summary"] = str(
        root / "summaries" / f"{summary['state_semantic_hash']}.json"
    )
    digest["semantic_hash"] = stable_hash(digest)
    return digest


def _persist_digest(root: Path, digest: Mapping[str, Any]) -> dict[str, Any]:
    path = root / "digests" / f"{digest['parent']['summary_semantic_hash']}.json"
    written = _persist_immutable(path, digest)
    _write_pointer(
        root / "digest-latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.workflow.digest-latest",
            "workflow_id": digest["workflow_id"],
            "digest_semantic_hash": digest["semantic_hash"],
            "digest_path": str(path),
        },
    )
    return written


def _summary_or_digest(
    config: ProjectConfig,
    root: Path,
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
    state: Mapping[str, Any],
    summary: Mapping[str, Any],
    *,
    compact: bool,
) -> dict[str, Any]:
    if not compact:
        return dict(summary)
    return _persist_digest(
        root,
        _build_digest(config, root, request, authorization, state, summary),
    )


def _advance(
    config: ProjectConfig,
    root: Path,
    request: Mapping[str, Any],
    authorization: Mapping[str, Any],
    state: Mapping[str, Any],
    capabilities_override: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    current = dict(state)
    while True:
        stage = next_authorized_stage(current, authorization)
        if stage is None:
            break
        try:
            if stage == "capabilities":
                source_hash = str(request["input"]["sha256"])
                context_hash = str(request["input"]["compile_context_hash"])
            else:
                source_hash, context_hash = _current_input_hashes(request)
            payload = _run_stage(
                config,
                root,
                request,
                authorization,
                stage,
                capabilities_override=capabilities_override,
            )
            _, stage_result = _snapshot_agent_payload(root, stage, payload)
            current = record_stage_completion(
                current,
                authorization,
                stage,
                stage_result,
                source_sha256=(source_hash if stage in {"candidate", "verify", "measure"} else None),
                compile_context_hash=(
                    context_hash if stage in {"candidate", "verify", "measure"} else None
                ),
            )
            _persist_state(root, current)
            if stage == "capabilities":
                blocker = _capability_blocker(payload, authorization)
                if blocker is not None:
                    error = WorkflowRunnerError(blocker, code="capability_unavailable")
                    current = _record_failure(
                        root,
                        current,
                        authorization,
                        stage="capabilities",
                        error=error,
                        status="blocked",
                    )
                    break
        except (MVPAgentError, MVPSchemaError) as exc:
            agent_error = agent_v2_error_payload(
                stage,
                exc,
                normalized_command=_agent_command(config, stage),
            )
            current = _record_failure(
                root,
                current,
                authorization,
                stage=stage,
                error=exc,
                payload=agent_error,
            )
            break
        except (WorkflowContractError, WorkflowRunnerError) as exc:
            current = _record_failure(
                root, current, authorization, stage=stage, error=exc
            )
            break
    report: dict[str, Any] | None = None
    if "review" in current["completed_stages"] and current.get("status") != "untrusted":
        try:
            report = _report_for_state(config, root, current)
        except (MVPAgentError, MVPSchemaError, WorkflowRunnerError) as exc:
            current = _record_failure(
                root, current, authorization, stage="report", error=exc
            )
    return current, report


def _resolve_preparation_path(config: ProjectConfig, raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config.root / path
    return path.resolve()


def _read_authorization_evidence(path: Path, *, name: str) -> str:
    if not path.is_file():
        raise WorkflowRunnerError(
            f"{name} is unavailable: {path}", code="authorization_evidence_missing"
        )
    try:
        return file_sha256(path)
    except OSError as exc:
        raise WorkflowRunnerError(
            f"cannot hash {name} {path}: {exc}",
            code="authorization_evidence_missing",
        ) from exc


def _proposal_snapshot(path: Path) -> dict[str, Any]:
    proposal = _load_mapping(path)
    identity = {
        "schema_version": 1,
        "document_type": "rtl-advisor.workflow.proposal",
        "proposal": proposal,
    }
    snapshot = {
        **identity,
        "proposal_id": f"proposal-{stable_hash(identity)[:20]}",
    }
    snapshot["semantic_hash"] = stable_hash(snapshot)
    return snapshot


def _reusable_authorization(
    config: ProjectConfig,
    preparation_root: Path,
    request: Mapping[str, Any],
    *,
    authorized_through: str,
    basis: Mapping[str, Any],
    candidate_selection: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    directories = [preparation_root / "authorizations"]
    workflow_authorizations = (
        _workflow_root(config, str(request["workflow_id"])) / "authorizations"
    )
    if workflow_authorizations != directories[0]:
        directories.append(workflow_authorizations)
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("authorization-*.json")):
            authorization = read_hashed_json(path)
            validate_workflow_authorization(authorization, request=request)
            if (
                authorization.get("authorized_through") == authorized_through
                and authorization.get("basis") == dict(basis)
                and authorization.get("candidate_selection")
                == (
                    dict(candidate_selection)
                    if candidate_selection is not None
                    else None
                )
            ):
                return authorization
    return None


def workflow_prepare(
    config: ProjectConfig,
    input_path: str | Path,
    *,
    input_kind: str,
    objective: str,
    authorized_through: str,
    prompt_file: str | Path | None = None,
    proposal_file: str | Path | None = None,
    confirmation_file: str | Path | None = None,
    top: str | None = None,
    include_dirs: Sequence[str | Path] = (),
    defines: Sequence[str] = (),
    candidate_selection: Mapping[str, Any] | None = None,
    output_dir: str | Path | None = None,
    start: bool = False,
    compact: bool = False,
    issued_at: str | None = None,
    normalized_command: Sequence[str] = (),
    _capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize bounded intent into immutable request and authorization records."""

    capabilities = (
        dict(_capabilities)
        if _capabilities is not None
        else agent_v2_capabilities(
            config, normalized_command=_agent_command(config, "capabilities")
        )
    )
    _validate_agent_payload(capabilities, "capabilities")
    if input_kind not in SOURCE_KINDS:
        raise WorkflowRunnerError(
            f"unsupported workflow input kind: {input_kind!r}",
            code="unauthorized_source",
        )
    if objective not in OBJECTIVES:
        raise WorkflowRunnerError(
            f"unsupported workflow objective: {objective!r}",
            code="unsupported_objective",
        )
    if authorized_through not in AUTHORIZED_THROUGH:
        raise WorkflowRunnerError(
            f"unsupported authorization ceiling: {authorized_through!r}",
            code="invalid_workflow_document",
        )
    blocker = _capability_blocker_for_ceiling(capabilities, authorized_through)
    if blocker is not None:
        raise WorkflowRunnerError(blocker, code="capability_unavailable")

    resolved_input = _resolve_preparation_path(config, input_path)
    if not resolved_input.is_file():
        raise WorkflowRunnerError(
            f"input not found: {resolved_input}", code="input_not_found"
        )
    resolved_include_dirs = tuple(
        str(_resolve_preparation_path(config, item)) for item in include_dirs
    )
    try:
        if input_kind == "qualified_corpus_reference":
            if top is not None or resolved_include_dirs or defines:
                raise WorkflowRunnerError(
                    "qualified references own their top, include directories, and defines",
                    code="reference_context_override",
                )
            if objective != "timing":
                raise WorkflowRunnerError(
                    "the qualified reference workflow requires objective=timing",
                    code="objective_mismatch",
                )
            from rtl_advisor.realistic_evidence import (
                RealisticEvidenceError,
                load_supported_reference,
            )

            try:
                reference = load_supported_reference(config, resolved_input)
            except RealisticEvidenceError as exc:
                raise WorkflowRunnerError(str(exc), code=exc.code) from exc
            compile_context_hash = str(reference["manifest_semantic_hash"])
        else:
            from rtl_advisor.realistic_evidence import is_qualified_reference_manifest

            if is_qualified_reference_manifest(resolved_input):
                raise WorkflowRunnerError(
                    "qualified reference manifest requires "
                    "input-kind=qualified_corpus_reference",
                    code="unauthorized_source",
                )
            design, _, manifest_objective = _resolve_design(
                config,
                str(resolved_input),
                top=top,
                include_dirs=resolved_include_dirs,
                defines=tuple(defines),
            )
            if manifest_objective is not None and manifest_objective != objective:
                raise WorkflowRunnerError(
                    f"requested objective {objective!r} does not match frozen "
                    f"manifest objective {manifest_objective!r}",
                    code="objective_mismatch",
                )
            compile_context_hash = str(
                compile_context_snapshot(design)["compile_context_hash"]
            )
    except MVPAgentError as exc:
        raise WorkflowRunnerError(str(exc), code=exc.code) from exc
    except MVPSchemaError as exc:
        raise WorkflowRunnerError(str(exc), code=exc.code) from exc

    request = build_workflow_request(
        input_path=resolved_input,
        input_kind=input_kind,
        source_sha256=file_sha256(resolved_input),
        compile_context_hash=compile_context_hash,
        objective=objective,
        top=top,
        include_dirs=resolved_include_dirs,
        defines=defines,
    )

    direct_basis = prompt_file is not None
    confirmed_basis = proposal_file is not None or confirmation_file is not None
    if direct_basis == confirmed_basis:
        raise WorkflowRunnerError(
            "provide either --prompt-file or both --proposal-file and "
            "--confirmation-file",
            code="authorization_evidence_invalid",
        )
    proposal: dict[str, Any] | None = None
    if direct_basis:
        prompt_path = _resolve_preparation_path(config, prompt_file)
        basis = {
            "kind": "direct_prompt",
            "prompt_sha256": _read_authorization_evidence(
                prompt_path, name="prompt file"
            ),
        }
    else:
        if proposal_file is None or confirmation_file is None:
            raise WorkflowRunnerError(
                "confirmed proposal authorization requires both evidence files",
                code="authorization_evidence_invalid",
            )
        proposal_path = _resolve_preparation_path(config, proposal_file)
        confirmation_path = _resolve_preparation_path(config, confirmation_file)
        proposal = _proposal_snapshot(proposal_path)
        basis = {
            "kind": "confirmed_proposal",
            "proposal_id": proposal["proposal_id"],
            "proposal_semantic_hash": proposal["semantic_hash"],
            "confirmation_prompt_sha256": _read_authorization_evidence(
                confirmation_path, name="confirmation file"
            ),
        }

    if output_dir is None:
        root = (
            config.artifacts_dir
            / "workflow-preparations-v1"
            / str(request["workflow_id"])
        )
    else:
        root = _resolve_preparation_path(config, output_dir)
    authorization = (
        _reusable_authorization(
            config,
            root,
            request,
            authorized_through=authorized_through,
            basis=basis,
            candidate_selection=candidate_selection,
        )
        if issued_at is None
        else None
    )
    if authorization is None:
        authorization = build_workflow_authorization(
            request,
            authorized_through=authorized_through,
            basis=basis,
            issued_at=(
                issued_at
                if issued_at is not None
                else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
            candidate_selection=candidate_selection,
        )
    request_path = root / "request.json"
    authorization_path = (
        root / "authorizations" / f"{authorization['authorization_id']}.json"
    )
    capabilities_path = (
        root / "capabilities" / f"{capabilities['semantic_hash']}.json"
    )
    _persist_immutable(request_path, request)
    _persist_immutable(authorization_path, authorization)
    _persist_immutable(capabilities_path, capabilities)
    artifacts: dict[str, str | Path] = {
        "request": request_path,
        "authorization": authorization_path,
        "capabilities": capabilities_path,
    }
    if proposal is not None:
        proposal_path = root / "proposals" / f"{proposal['proposal_id']}.json"
        _persist_immutable(proposal_path, proposal)
        artifacts["proposal"] = proposal_path
    preparation = build_workflow_preparation(
        request,
        authorization,
        capabilities_semantic_hash=str(capabilities["semantic_hash"]),
        artifacts=artifacts,
        command=normalized_command,
    )
    preparation_path = (
        root / "preparations" / f"{authorization['authorization_id']}.json"
    )
    _persist_immutable(preparation_path, preparation)
    if start:
        return workflow_start(
            config,
            request_path,
            authorization_path,
            compact=compact,
            _capabilities=capabilities,
        )
    return preparation


def workflow_start(
    config: ProjectConfig,
    request_path: str | Path,
    authorization_path: str | Path,
    *,
    compact: bool = False,
    _capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = _load_mapping(request_path)
    authorization = _load_mapping(authorization_path)
    validate_workflow_request(request)
    validate_workflow_authorization(authorization, request=request)
    root = _workflow_root(config, str(request["workflow_id"]))
    _persist_request(root, request)
    authorization_pointer = root / "authorization-latest.json"
    if authorization_pointer.is_file():
        previous = _load_latest_authorization(root, request=request)
        if previous.get("semantic_hash") != authorization.get("semantic_hash"):
            raise WorkflowRunnerError(
                "workflow already exists with different authorization; use resume",
                code="authorization_resume_required",
            )
    else:
        _persist_authorization(root, authorization)
    if (root / "state-latest.json").is_file():
        state = _load_latest_state(
            root, request=request, authorization=authorization
        )
    else:
        state = build_workflow_state(request, authorization)
        _persist_state(root, state)
    state, report = _advance(
        config,
        root,
        request,
        authorization,
        state,
        capabilities_override=_capabilities,
    )
    summary = _build_summary(root, request, authorization, state, report)
    written = _persist_summary(root, summary)
    return _summary_or_digest(
        config, root, request, authorization, state, written, compact=compact
    )


def workflow_resume(
    config: ProjectConfig,
    workflow_id: str,
    authorization_path: str | Path,
    *,
    compact: bool = False,
    _capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _workflow_root(config, workflow_id)
    request = _load_request(root)
    previous = _load_latest_authorization(root, request=request)
    state = _load_latest_state(root, request=request, authorization=previous)
    if state.get("status") in {"blocked", "failed", "untrusted"}:
        raise WorkflowRunnerError(
            "failed or untrusted workflows require a new request",
            code="workflow_not_resumable",
        )
    current = _load_mapping(authorization_path)
    validate_workflow_authorization(current, request=request)
    validate_authorization_progression(previous, current, request=request)
    _persist_authorization(root, current)
    if current["semantic_hash"] != previous["semantic_hash"]:
        state = apply_authorization(
            state, previous, current, request=request
        )
        _persist_state(root, state)
    state, report = _advance(
        config,
        root,
        request,
        current,
        state,
        capabilities_override=_capabilities,
    )
    summary = _build_summary(root, request, current, state, report)
    written = _persist_summary(root, summary)
    return _summary_or_digest(
        config, root, request, current, state, written, compact=compact
    )


def workflow_status(
    config: ProjectConfig, workflow_id: str, *, compact: bool = False
) -> dict[str, Any]:
    root = _workflow_root(config, workflow_id)
    request = _load_request(root)
    authorization = _load_latest_authorization(root, request=request)
    state = _load_latest_state(root, request=request, authorization=authorization)
    summary = _build_summary(
        root, request, authorization, state, _existing_report(root, state)
    )
    written = _persist_summary(root, summary)
    return _summary_or_digest(
        config, root, request, authorization, state, written, compact=compact
    )


def workflow_report(
    config: ProjectConfig, workflow_id: str, *, compact: bool = False
) -> dict[str, Any]:
    root = _workflow_root(config, workflow_id)
    request = _load_request(root)
    authorization = _load_latest_authorization(root, request=request)
    state = _load_latest_state(root, request=request, authorization=authorization)
    try:
        report = _report_for_state(config, root, state)
    except (MVPAgentError, MVPSchemaError, WorkflowRunnerError) as exc:
        state = _record_failure(
            root, state, authorization, stage="report", error=exc
        )
        report = None
    summary = _build_summary(root, request, authorization, state, report)
    written = _persist_summary(root, summary)
    return _summary_or_digest(
        config, root, request, authorization, state, written, compact=compact
    )


def _load_batch_manifest(
    config: ProjectConfig, manifest_path: str | Path
) -> tuple[Path, list[dict[str, Any]], str]:
    path = _resolve_preparation_path(config, manifest_path)
    payload = _load_mapping(path)
    required = {"schema_version", "schema", "document_type", "items"}
    if set(payload) != required:
        raise WorkflowRunnerError(
            "batch manifest has missing or unknown fields",
            code="invalid_batch_manifest",
        )
    if (
        payload.get("schema_version") != 1
        or payload.get("schema") != "rtl-advisor-workflow-batch-manifest-v1"
        or payload.get("document_type") != "rtl-advisor.workflow.batch-manifest"
    ):
        raise WorkflowRunnerError(
            "unsupported workflow batch manifest", code="invalid_batch_manifest"
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 64:
        raise WorkflowRunnerError(
            "batch manifest must contain between 1 and 64 items",
            code="invalid_batch_manifest",
        )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping) or set(raw) - {
            "item_id",
            "input",
            "objective",
            "expected_source_sha256",
        }:
            raise WorkflowRunnerError(
                "batch item has an invalid shape", code="invalid_batch_manifest"
            )
        item_id = raw.get("item_id")
        if (
            not isinstance(item_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item_id)
            or item_id in seen
        ):
            raise WorkflowRunnerError(
                "batch item IDs must be unique safe identifiers",
                code="invalid_batch_manifest",
            )
        seen.add(item_id)
        input_record = raw.get("input")
        if not isinstance(input_record, Mapping) or set(input_record) - {
            "kind",
            "path",
            "top",
            "include_dirs",
            "defines",
        }:
            raise WorkflowRunnerError(
                f"batch item {item_id} has an invalid input",
                code="invalid_batch_manifest",
            )
        kind = input_record.get("kind")
        objective = raw.get("objective")
        raw_path = input_record.get("path")
        if kind not in SOURCE_KINDS or objective not in OBJECTIVES or not isinstance(raw_path, str):
            raise WorkflowRunnerError(
                f"batch item {item_id} has an unsupported kind, objective, or path",
                code="invalid_batch_manifest",
            )
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            source_path = path.parent / source_path
        include_dirs: list[str] = []
        raw_include_dirs = input_record.get("include_dirs", [])
        raw_defines = input_record.get("defines", [])
        if not isinstance(raw_include_dirs, list) or not isinstance(raw_defines, list):
            raise WorkflowRunnerError(
                f"batch item {item_id} compile options must be arrays",
                code="invalid_batch_manifest",
            )
        for raw_dir in raw_include_dirs:
            if not isinstance(raw_dir, str):
                raise WorkflowRunnerError(
                    f"batch item {item_id} include directory must be a string",
                    code="invalid_batch_manifest",
                )
            include_path = Path(raw_dir).expanduser()
            if not include_path.is_absolute():
                include_path = path.parent / include_path
            include_dirs.append(str(include_path.resolve()))
        if any(not isinstance(value, str) or not value for value in raw_defines):
            raise WorkflowRunnerError(
                f"batch item {item_id} define must be a non-empty string",
                code="invalid_batch_manifest",
            )
        expected_hash = raw.get("expected_source_sha256")
        if expected_hash is not None and (
            not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        ):
            raise WorkflowRunnerError(
                f"batch item {item_id} has an invalid expected source hash",
                code="invalid_batch_manifest",
            )
        top = input_record.get("top")
        if top is not None and (not isinstance(top, str) or not top):
            raise WorkflowRunnerError(
                f"batch item {item_id} has an invalid top",
                code="invalid_batch_manifest",
            )
        items.append(
            {
                "item_id": item_id,
                "input": {
                    "kind": kind,
                    "path": str(source_path.resolve()),
                    "include_dirs": include_dirs,
                    "defines": list(raw_defines),
                    **({"top": top} if top is not None else {}),
                },
                "objective": objective,
                **(
                    {"expected_source_sha256": expected_hash}
                    if expected_hash is not None
                    else {}
                ),
            }
        )
    normalized = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-batch-manifest-v1",
        "document_type": "rtl-advisor.workflow.batch-manifest",
        "items": items,
    }
    return path, items, stable_hash(normalized)


def _batch_authorization_basis(
    config: ProjectConfig,
    *,
    prompt_file: str | Path | None,
    proposal_file: str | Path | None,
    confirmation_file: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    direct = prompt_file is not None
    confirmed = proposal_file is not None or confirmation_file is not None
    if direct == confirmed:
        raise WorkflowRunnerError(
            "provide either --prompt-file or both --proposal-file and --confirmation-file",
            code="authorization_evidence_invalid",
        )
    if direct:
        prompt_path = _resolve_preparation_path(config, prompt_file)
        return {
            "kind": "direct_prompt",
            "prompt_sha256": _read_authorization_evidence(prompt_path, name="prompt file"),
        }, None
    if proposal_file is None or confirmation_file is None:
        raise WorkflowRunnerError(
            "confirmed proposal authorization requires both evidence files",
            code="authorization_evidence_invalid",
        )
    proposal = _proposal_snapshot(_resolve_preparation_path(config, proposal_file))
    return {
        "kind": "confirmed_proposal",
        "proposal_id": proposal["proposal_id"],
        "proposal_semantic_hash": proposal["semantic_hash"],
        "confirmation_prompt_sha256": _read_authorization_evidence(
            _resolve_preparation_path(config, confirmation_file),
            name="confirmation file",
        ),
    }, proposal


def _batch_digest_item(item_id: str, digest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "workflow_id": digest["workflow_id"],
        "status": digest["status"],
        "decision": digest["decision"],
        "action": digest["action"],
        "scope": digest["scope"],
        "source_locations": digest["source_locations"],
        "rationale": digest["rationale"],
        "finding": digest["finding"],
        "candidate_id": digest["candidate_id"],
        "formal_status": digest["formal_status"],
        "measurement_status": digest["measurement_status"],
        "profile_results": digest["profile_results"],
        "safe": digest["safe"],
        "evidence_complete": digest["evidence_complete"],
        "next_action": digest["next_action"],
        "digest_semantic_hash": digest["semantic_hash"],
        "digest_path": str(
            _workflow_root_placeholder(digest["artifacts"]["summary"]).parent.parent
            / "digests"
            / f"{digest['parent']['summary_semantic_hash']}.json"
        ),
    }


def _workflow_root_placeholder(summary_path: str) -> Path:
    return Path(summary_path).expanduser().resolve()


def workflow_batch(
    config: ProjectConfig,
    manifest_path: str | Path,
    *,
    authorized_through: str,
    prompt_file: str | Path | None = None,
    proposal_file: str | Path | None = None,
    confirmation_file: str | Path | None = None,
    first_eligible: bool = False,
    output_dir: str | Path | None = None,
    jobs: int = 1,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Execute an ordered set of workflows with one capability discovery call."""

    if authorized_through not in AUTHORIZED_THROUGH or jobs not in {1, 2, 3, 4}:
        raise WorkflowRunnerError(
            "invalid batch authorization ceiling or jobs value",
            code="invalid_batch_request",
        )
    if authorized_through != "review" and not first_eligible:
        raise WorkflowRunnerError(
            "candidate-capable batches require --first-eligible",
            code="candidate_selection_required",
        )
    manifest, items, manifest_semantic_hash = _load_batch_manifest(config, manifest_path)
    basis, proposal = _batch_authorization_basis(
        config,
        prompt_file=prompt_file,
        proposal_file=proposal_file,
        confirmation_file=confirmation_file,
    )
    selection = {"mode": "first_eligible"} if first_eligible else None
    batch_identity = {
        "manifest_semantic_hash": manifest_semantic_hash,
        "authorized_through": authorized_through,
        "basis": basis,
        "candidate_selection": selection,
    }
    batch_id = f"batch-{stable_hash(batch_identity)[:20]}"
    root = (
        _resolve_preparation_path(config, output_dir)
        if output_dir is not None
        else config.artifacts_dir / "workflow-batches-v1" / batch_id
    )
    capabilities = agent_v2_capabilities(
        config, normalized_command=_agent_command(config, "capabilities")
    )
    _validate_agent_payload(capabilities, "capabilities")
    blocker = _capability_blocker_for_ceiling(capabilities, authorized_through)
    if blocker is not None:
        raise WorkflowRunnerError(blocker, code="capability_unavailable")
    capabilities_path = root / "capabilities" / f"{capabilities['semantic_hash']}.json"
    _persist_immutable(capabilities_path, capabilities)

    prepared: list[dict[str, Any]] = []
    request_items: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item["item_id"])
        input_record = item["input"]
        try:
            expected_hash = item.get("expected_source_sha256")
            if expected_hash is not None and file_sha256(input_record["path"]) != expected_hash:
                raise WorkflowRunnerError(
                    "source hash does not match the batch manifest",
                    code="source_hash_mismatch",
                )
            preparation = workflow_prepare(
                config,
                input_record["path"],
                input_kind=input_record["kind"],
                objective=item["objective"],
                authorized_through=authorized_through,
                prompt_file=prompt_file,
                proposal_file=proposal_file,
                confirmation_file=confirmation_file,
                top=input_record.get("top"),
                include_dirs=tuple(input_record["include_dirs"]),
                defines=tuple(input_record["defines"]),
                candidate_selection=selection,
                output_dir=root / "items" / item_id,
                normalized_command=normalized_command,
                _capabilities=capabilities,
            )
            request_payload = _load_mapping(preparation["artifacts"]["request"])
            request_items.append(
                {
                    "item_id": item_id,
                    "status": "ready",
                    "workflow_id": preparation["workflow_id"],
                    "request_semantic_hash": preparation["request_semantic_hash"],
                    "input": item["input"],
                    "objective": item["objective"],
                }
            )
            prepared.append(
                {
                    "item_id": item_id,
                    "workflow_id": preparation["workflow_id"],
                    "request": preparation["artifacts"]["request"],
                    "authorization": preparation["artifacts"]["authorization"],
                }
            )
            validate_workflow_request(request_payload)
        except (OSError, MVPAgentError, MVPSchemaError, WorkflowContractError, WorkflowRunnerError) as exc:
            failure = {
                "item_id": item_id,
                "status": "failed",
                "input": item["input"],
                "objective": item["objective"],
                "error": {
                    "code": str(getattr(exc, "code", "workflow_runner_error")),
                    "message": str(exc),
                },
            }
            request_items.append(failure)
            prepared.append(failure)

    batch_request = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-batch-request-v1",
        "document_type": "rtl-advisor.workflow.batch-request",
        "batch_id": batch_id,
        "manifest_semantic_hash": manifest_semantic_hash,
        "items": request_items,
    }
    batch_request["semantic_hash"] = stable_hash(batch_request)
    request_path = root / "request.json"
    _persist_immutable(request_path, batch_request)
    issued_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    authorization_core = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-batch-authorization-v1",
        "document_type": "rtl-advisor.workflow.batch-authorization",
        "batch_id": batch_id,
        "request_semantic_hash": batch_request["semantic_hash"],
        "authorized_through": authorized_through,
        "basis": basis,
        "candidate_selection": selection,
        "issued_at": issued_at,
    }
    authorization_path = root / "authorization.json"
    if authorization_path.is_file():
        batch_authorization = read_hashed_json(authorization_path)
        comparable = {
            key: value
            for key, value in batch_authorization.items()
            if key not in {"issued_at", "semantic_hash"}
        }
        expected = {
            key: value for key, value in authorization_core.items() if key != "issued_at"
        }
        if comparable != expected:
            raise WorkflowRunnerError(
                "batch authorization conflicts with existing immutable evidence",
                code="append_only_conflict",
            )
    else:
        batch_authorization = dict(authorization_core)
        batch_authorization["semantic_hash"] = stable_hash(batch_authorization)
        _persist_immutable(authorization_path, batch_authorization)
    if proposal is not None:
        _persist_immutable(root / "proposal.json", proposal)

    unique: dict[str, dict[str, Any]] = {}
    for item in prepared:
        workflow_id = item.get("workflow_id")
        if isinstance(workflow_id, str) and workflow_id not in unique:
            unique[workflow_id] = item

    def execute(item: Mapping[str, Any]) -> dict[str, Any]:
        workflow_id = str(item["workflow_id"])
        if (_workflow_root(config, workflow_id) / "authorization-latest.json").is_file():
            return workflow_resume(
                config,
                workflow_id,
                item["authorization"],
                compact=True,
                _capabilities=capabilities,
            )
        return workflow_start(
            config,
            item["request"],
            item["authorization"],
            compact=True,
            _capabilities=capabilities,
        )

    digest_by_workflow: dict[str, dict[str, Any]] = {}
    execution_errors: dict[str, Exception] = {}
    if jobs == 1:
        for workflow_id, item in unique.items():
            try:
                digest_by_workflow[workflow_id] = execute(item)
            except Exception as exc:  # converted to a bounded per-item record below
                execution_errors[workflow_id] = exc
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(execute, item): workflow_id for workflow_id, item in unique.items()}
            for future in as_completed(futures):
                workflow_id = futures[future]
                try:
                    digest_by_workflow[workflow_id] = future.result()
                except Exception as exc:  # converted to a bounded per-item record below
                    execution_errors[workflow_id] = exc

    results: list[dict[str, Any]] = []
    seen_workflows: dict[str, str] = {}
    for item in prepared:
        item_id = str(item["item_id"])
        workflow_id = item.get("workflow_id")
        if not isinstance(workflow_id, str):
            results.append(
                {
                    "item_id": item_id,
                    "status": "failed",
                    "error": item["error"],
                }
            )
            continue
        error = execution_errors.get(workflow_id)
        if error is not None:
            results.append(
                {
                    "item_id": item_id,
                    "workflow_id": workflow_id,
                    "status": "failed",
                    "error": {
                        "code": str(getattr(error, "code", "workflow_runner_error")),
                        "message": str(error),
                    },
                }
            )
            continue
        result = _batch_digest_item(item_id, digest_by_workflow[workflow_id])
        if workflow_id in seen_workflows:
            result["deduplicated_from"] = seen_workflows[workflow_id]
        else:
            seen_workflows[workflow_id] = item_id
        results.append(result)

    failed_count = sum(item.get("status") == "failed" for item in results)
    summary_path = root / "summary.json"
    summary = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-batch-summary-v1",
        "document_type": "rtl-advisor.workflow.batch-summary",
        "batch_id": batch_id,
        "status": "partial" if failed_count else "completed",
        "request_semantic_hash": batch_request["semantic_hash"],
        "authorization_semantic_hash": batch_authorization["semantic_hash"],
        "capabilities_semantic_hash": capabilities["semantic_hash"],
        "jobs": jobs,
        "counts": {
            "items": len(results),
            "completed": len(results) - failed_count,
            "failed": failed_count,
            "unique_workflows": len(unique),
        },
        "items": results,
        "artifacts": {
            "manifest": str(manifest),
            "request": str(request_path),
            "authorization": str(authorization_path),
            "capabilities": str(capabilities_path),
            "root": str(root),
            "summary": str(summary_path),
        },
        "command": list(normalized_command),
    }
    summary["semantic_hash"] = stable_hash(summary)
    _persist_immutable(summary_path, summary)
    return summary


def workflow_error_payload(
    operation: str,
    error: Exception,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-error-v1",
        "document_type": "rtl-advisor.workflow.error",
        "status": "failed",
        "operation": operation,
        "error": {
            "code": str(getattr(error, "code", "workflow_runner_error")),
            "message": str(error),
        },
        "command": list(normalized_command),
    }
    payload["semantic_hash"] = stable_hash(payload)
    return payload


def workflow_exit_code(payload: Mapping[str, Any]) -> int:
    if payload.get("document_type") == "rtl-advisor.workflow.error":
        return 2
    if payload.get("status") in {"failed", "untrusted"}:
        return 2
    if payload.get("status") in {"blocked", "partial"} or payload.get("decision") in {
        "formal_failed",
        "formal_inconclusive",
        "evidence_incomplete",
    }:
        return 4
    return 0
