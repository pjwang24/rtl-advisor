from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import shutil
from typing import Any, Sequence

from rtl_advisor import __version__
from rtl_advisor.advisor_v2 import (
    ANALYSIS_SCHEMA_VERSION,
    AdvisorV2Error,
    GATE_FLOW_VERSION,
    analyze_live_rtl,
)
from rtl_advisor.candidate_v2 import (
    CandidateV2Error,
    emit_selected_candidate,
    verify_emitted_candidate,
)
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CorpusError, available_families, load_manifest
from rtl_advisor.rtl_input import RTLInputError


AGENT_SCHEMA_VERSION = 1
AGENT_FLOW_VERSION = "rtl-advisor-agent-v1"
OBJECTIVE_PROFILES = {
    "timing": "timing-first",
    "area": "area-first",
    "balanced": "balanced",
}
AGENT_DECISIONS = (
    "recommended",
    "synthesis_likely_handles",
    "target_flow_confirmation",
    "no_change",
    "unsupported",
    "failed",
)
FLOW_ROBUST_SUPPORTED_FAMILIES = (
    "adder_reduction_association",
    "decode_factoring",
    "mux_placement",
    "popcount_saturation",
    "priority_selection",
    "width_signedness",
)
RESEARCH_ONLY_FAMILIES = (
    "arithmetic_resource_sharing",
    "comparator_selection",
    "variable_shift",
)
MODEL_REGISTRY = (
    {
        "model_id": "v2",
        "artifact": "models/v2/gate.json",
        "release_status": "diagnostic_only",
        "reason": "the original advisor did not pass its release evaluation",
    },
    {
        "model_id": "v21",
        "artifact": "models/v21/policy.json",
        "release_status": "diagnostic_only",
        "reason": "V2.1 was not promoted by its frozen gates",
    },
    {
        "model_id": "v22",
        "artifact": "models/v22/policy.json",
        "release_status": "diagnostic_only",
        "reason": "V2.2 scored below its frozen release threshold",
    },
    {
        "model_id": "flow-robust-next",
        "artifact": None,
        "release_status": "unavailable",
        "reason": "the next flow-robust model has not been trained or sealed",
    },
)
_RUN_ID = re.compile(r"^review-[0-9a-f]{20}$")
_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class AgentAPIError(RuntimeError):
    """Raised when an agent operation cannot satisfy its stable contract."""

    def __init__(self, message: str, *, code: str = "agent_error") -> None:
        super().__init__(message)
        self.code = code


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_document(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    final = dict(payload)
    final["semantic_hash"] = _json_hash(final)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return final


def _read_document(
    path: Path,
    *,
    document_type: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentAPIError(
            f"invalid agent artifact {path}: {exc}", code="invalid_artifact"
        ) from exc
    if not isinstance(payload, dict):
        raise AgentAPIError(
            f"agent artifact must be an object: {path}", code="invalid_artifact"
        )
    if payload.get("schema_version") != AGENT_SCHEMA_VERSION:
        raise AgentAPIError(
            f"unsupported agent schema in {path}", code="unsupported_schema"
        )
    if payload.get("document_type") != document_type:
        raise AgentAPIError(
            f"unexpected agent document type in {path}", code="invalid_artifact"
        )
    expected = payload.get("semantic_hash")
    core = {key: value for key, value in payload.items() if key != "semantic_hash"}
    if expected != _json_hash(core):
        raise AgentAPIError(
            f"agent artifact semantic hash mismatch: {path}",
            code="artifact_hash_mismatch",
        )
    return payload


def _command_status(command: str) -> dict[str, Any]:
    try:
        executable = shlex.split(command)[0]
    except (ValueError, IndexError):
        return {"status": "missing", "configured_command": command, "path": None}
    candidate = Path(executable).expanduser()
    if candidate.is_absolute() or "/" in executable:
        resolved = candidate.resolve() if candidate.exists() else None
    else:
        located = shutil.which(executable)
        resolved = Path(located).resolve() if located else None
    return {
        "status": "available" if resolved and resolved.is_file() else "missing",
        "configured_command": command,
        "path": str(resolved) if resolved else None,
    }


def _pyslang_status() -> dict[str, Any]:
    spec = importlib.util.find_spec("pyslang")
    return {
        "status": "available" if spec is not None else "missing",
        "path": spec.origin if spec is not None else None,
    }


def _liberty_status(config: ProjectConfig) -> dict[str, Any]:
    path = config.liberty.path
    if not path.is_file():
        return {
            "status": "missing",
            "path": str(path),
            "expected_sha256": config.liberty.sha256,
            "actual_sha256": None,
        }
    actual = _file_hash(path)
    return {
        "status": "available" if actual == config.liberty.sha256 else "mismatch",
        "path": str(path),
        "expected_sha256": config.liberty.sha256,
        "actual_sha256": actual,
    }


def _model_capabilities(config: ProjectConfig) -> list[dict[str, Any]]:
    models = []
    for registered in MODEL_REGISTRY:
        artifact = registered["artifact"]
        path = config.artifacts_dir / artifact if artifact else None
        installed = bool(path and path.is_file())
        models.append(
            {
                **registered,
                "artifact": str(path) if path else None,
                "installed": installed,
                "ready": registered["release_status"] == "ready" and installed,
            }
        )
    return models


def agent_capabilities(
    config: ProjectConfig,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    tools = {
        "pyslang": _pyslang_status(),
        "verilator": _command_status(config.tools.verilator),
        "yosys": _command_status(config.tools.yosys),
        "codex": _command_status(config.tools.codex),
        "liberty": _liberty_status(config),
    }
    models = _model_capabilities(config)
    review_tools_available = (
        tools["pyslang"]["status"] == "available"
        and tools["yosys"]["status"] == "available"
    )
    candidate_tools_available = (
        review_tools_available
        and tools["verilator"]["status"] == "available"
    )
    recommendation_ready = any(model["ready"] for model in models)
    payload = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "document_type": "rtl-advisor.agent.capabilities",
        "flow_version": AGENT_FLOW_VERSION,
        "status": "ok",
        "cli_version": __version__,
        "analysis": {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "flow_version": GATE_FLOW_VERSION,
            "live_recommendation_ready": recommendation_ready,
        },
        "input_forms": {
            "generated_case_manifest": True,
            "single_rtl_file_with_top": True,
            "normalized_design_input": True,
            "filelist_with_top": True,
            "candidate_generation_with_external_include_dirs": False,
        },
        "objectives": list(OBJECTIVE_PROFILES),
        "decisions": list(AGENT_DECISIONS),
        "operations": {
            "review": {
                "implemented": True,
                "available": review_tools_available,
                "live_recommendations": recommendation_ready,
            },
            "candidate_generation": {
                "implemented": True,
                "available": candidate_tools_available and recommendation_ready,
                "requires_eligible_review": True,
            },
            "formal_verification": {
                "implemented": True,
                "available": tools["yosys"]["status"] == "available",
                "required_before_safe": True,
            },
            "source_mutation": {"implemented": False, "available": False},
        },
        "tools": tools,
        "models": models,
        "families": {
            "registered": list(available_families()),
            "flow_robust_calibration_support": list(
                FLOW_ROBUST_SUPPORTED_FAMILIES
            ),
            "research_only": list(RESEARCH_ONLY_FAMILIES),
        },
        "limitations": [
            "No installed model is approved for live recommendations.",
            "Candidate generation is isolated and requires an eligible review.",
            "A candidate is safe only after current hash-matched formal proof.",
            "Target-flow PPA confirmation remains external to this interface.",
        ],
        "command": list(normalized_command),
    }
    output_path = config.artifacts_dir / "agent" / "capabilities-v1.json"
    payload["artifacts"] = {"capabilities": str(output_path)}
    return _write_document(output_path, payload)


def _resolve_input(
    config: ProjectConfig,
    input_path: str,
    *,
    top: str | None,
    include_dirs: Sequence[str],
    defines: Sequence[str],
) -> dict[str, Any]:
    path = Path(input_path).expanduser()
    if not path.is_absolute():
        path = config.root / path
    path = path.resolve()

    if path.is_dir() or path.name == "manifest.json":
        try:
            manifest = load_manifest(path)
        except CorpusError as exc:
            raise AgentAPIError(str(exc), code="invalid_manifest") from exc
        baseline = manifest.baseline
        return {
            "kind": "generated_case",
            "requested_path": str(path),
            "top": baseline.wrapper_top,
            "files": (str(manifest.variant_path(baseline)),),
            "filelist": None,
            "include_dirs": tuple(include_dirs),
            "defines": tuple(defines),
            "case_id": manifest.case_id,
            "family": manifest.family,
            "manifest": str(manifest.path),
        }

    if not path.is_file():
        raise AgentAPIError(f"input not found: {path}", code="input_not_found")

    if path.name == "input.json":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            stored_files = tuple(str(item["path"]) for item in raw["files"])
            stored_top = str(raw["top"])
            stored_includes = tuple(str(item) for item in raw["include_dirs"])
            stored_defines = tuple(str(item) for item in raw["defines"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AgentAPIError(
                f"invalid normalized design input {path}: {exc}",
                code="invalid_design_input",
            ) from exc
        return {
            "kind": "normalized_design_input",
            "requested_path": str(path),
            "top": stored_top,
            "files": stored_files,
            "filelist": None,
            "include_dirs": stored_includes,
            "defines": stored_defines,
            "input_artifact": str(path),
        }

    if top is None:
        raise AgentAPIError(
            "--top is required for an RTL source or filelist",
            code="top_required",
        )
    if path.suffix.lower() in {".f", ".flist", ".lst"}:
        return {
            "kind": "filelist",
            "requested_path": str(path),
            "top": top,
            "files": (),
            "filelist": str(path),
            "include_dirs": tuple(include_dirs),
            "defines": tuple(defines),
        }
    return {
        "kind": "rtl_file",
        "requested_path": str(path),
        "top": top,
        "files": (str(path),),
        "filelist": None,
        "include_dirs": tuple(include_dirs),
        "defines": tuple(defines),
    }


def _analysis_findings(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for candidate in analysis.get("candidates") or []:
        predictions = candidate.get("predicted_improvement_percent") or {}
        evidence_sources = ["deterministic_rule"]
        if any(value is not None for value in predictions.values()):
            evidence_sources.append("calibrated_model")
        findings.append(
            {
                "finding_id": candidate.get("finding_id"),
                "candidate_id": candidate.get("candidate_id"),
                "rank": candidate.get("rank"),
                "transformation_id": candidate.get("transformation_id"),
                "family": candidate.get("family"),
                "source": candidate.get("source"),
                "eligible_in_diagnostic_model": bool(candidate.get("eligible")),
                "expected_improvement_percent": predictions,
                "evidence_sources": evidence_sources,
                "limitations": list(candidate.get("rejection_reasons") or []),
            }
        )
    return findings


def _review_decision(
    analysis: dict[str, Any],
    *,
    model_release_status: str,
) -> tuple[str, str, str]:
    if analysis.get("decision") == "unsupported":
        return "blocked", "unsupported", "input or tool support is incomplete"
    if analysis.get("gate", {}).get("status") != "calibrated":
        return "blocked", "failed", "the calibrated gate model is unavailable"
    if model_release_status != "ready":
        return (
            "blocked",
            "failed",
            "the installed analysis model is diagnostic-only",
        )
    if analysis.get("decision") == "recommend":
        return "completed", "recommended", "an eligible candidate cleared the gate"
    return "completed", "no_change", "no candidate cleared the release gate"


def _input_integrity(files: Sequence[dict[str, Any]]) -> dict[str, Any]:
    mismatches = []
    for item in files:
        path = Path(str(item.get("path", "")))
        expected = str(item.get("sha256", ""))
        actual = _file_hash(path) if path.is_file() else None
        if actual != expected:
            mismatches.append(
                {
                    "path": str(path),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                }
            )
    return {"ok": not mismatches, "mismatches": mismatches}


def agent_review(
    config: ProjectConfig,
    input_path: str,
    *,
    objective: str,
    top: str | None = None,
    include_dirs: Sequence[str] = (),
    defines: Sequence[str] = (),
    gate_model_path: str | None = None,
    force: bool = False,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    try:
        profile = OBJECTIVE_PROFILES[objective]
    except KeyError as exc:
        raise AgentAPIError(
            f"unsupported objective: {objective}", code="unsupported_objective"
        ) from exc
    resolved = _resolve_input(
        config,
        input_path,
        top=top,
        include_dirs=include_dirs,
        defines=defines,
    )
    try:
        analysis, analysis_path = analyze_live_rtl(
            config,
            top=resolved["top"],
            files=tuple(resolved["files"]),
            filelist=resolved["filelist"],
            include_dirs=tuple(resolved["include_dirs"]),
            defines=tuple(resolved["defines"]),
            profile_id=profile,
            mode="calibrated",
            gate_model_path=gate_model_path,
            force=force,
        )
    except AdvisorV2Error as exc:
        raise AgentAPIError(str(exc), code="analysis_failed") from exc

    try:
        design = json.loads(
            (analysis_path.parent / "input.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentAPIError(
            f"analysis input artifact is invalid: {exc}",
            code="invalid_analysis_artifact",
        ) from exc
    if not isinstance(design, dict):
        raise AgentAPIError(
            "analysis input artifact must be an object",
            code="invalid_analysis_artifact",
        )

    current_model = next(item for item in MODEL_REGISTRY if item["model_id"] == "v2")
    status, decision, reason = _review_decision(
        analysis,
        model_release_status=str(current_model["release_status"]),
    )
    analysis_sha256 = _file_hash(analysis_path)
    identity = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "design_hash": design.get("design_hash"),
        "objective": objective,
        "analysis_sha256": analysis_sha256,
        "model_release_status": current_model["release_status"],
    }
    run_id = f"review-{_json_hash(identity)[:20]}"
    output_path = config.artifacts_dir / "agent" / run_id / "review.json"
    limitations = [
        "The current V2 analysis model is diagnostic-only.",
        "Predicted PPA is not target-flow measurement.",
        "Only generated or explicitly approved open RTL is in scope.",
    ]
    gate_reason = analysis.get("gate", {}).get("reason")
    if gate_reason:
        limitations.append(str(gate_reason))
    payload = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "document_type": "rtl-advisor.agent.review",
        "flow_version": AGENT_FLOW_VERSION,
        "status": status,
        "decision": decision,
        "status_reason": reason,
        "run_id": run_id,
        "objective": objective,
        "profile": profile,
        "input": {
            "kind": resolved["kind"],
            "requested_path": resolved["requested_path"],
            "top": design.get("top"),
            "design_hash": design.get("design_hash"),
            "files": design.get("files") or [],
            "include_dirs": design.get("include_dirs") or [],
            "defines": design.get("defines") or [],
            "filelists": design.get("filelists") or [],
            "source_integrity": _input_integrity(design.get("files") or []),
        },
        "findings": _analysis_findings(analysis),
        "selected_candidate_id": analysis.get("selected_candidate_id"),
        "candidate_generation_allowed": (
            status == "completed" and decision == "recommended"
        ),
        "evidence": {
            "analysis_schema_version": analysis.get("schema_version"),
            "analysis_flow_version": analysis.get("flow_version"),
            "analysis_sha256": analysis_sha256,
            "gate": analysis.get("gate"),
            "model_id": current_model["model_id"],
            "model_release_status": current_model["release_status"],
        },
        "limitations": limitations,
        "artifacts": {
            "review": str(output_path),
            "analysis": str(analysis_path),
            "input": str(analysis_path.parent / "input.json"),
            "root": str(output_path.parent),
        },
        "command": list(normalized_command),
    }
    return _write_document(output_path, payload)


def _run_root(config: ProjectConfig, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise AgentAPIError(f"invalid run ID: {run_id!r}", code="invalid_run_id")
    return config.artifacts_dir / "agent" / run_id


def agent_candidate(
    config: ProjectConfig,
    run_id: str,
    *,
    finding_id: str,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    root = _run_root(config, run_id)
    review = _read_document(
        root / "review.json", document_type="rtl-advisor.agent.review"
    )
    if not review.get("candidate_generation_allowed"):
        raise AgentAPIError(
            "review is not eligible for candidate generation",
            code="review_not_eligible",
        )
    integrity = _input_integrity(review.get("input", {}).get("files") or [])
    if not integrity["ok"]:
        raise AgentAPIError(
            "input source hashes changed after review", code="source_hash_mismatch"
        )
    selected_id = review.get("selected_candidate_id")
    finding = next(
        (
            item
            for item in review.get("findings") or []
            if item.get("finding_id") == finding_id
            and item.get("candidate_id") == selected_id
        ),
        None,
    )
    if finding is None:
        raise AgentAPIError(
            "finding is not the selected eligible candidate",
            code="finding_not_eligible",
        )
    analysis_path = Path(review["artifacts"]["analysis"])
    if not analysis_path.is_file() or _file_hash(analysis_path) != review["evidence"].get(
        "analysis_sha256"
    ):
        raise AgentAPIError(
            "analysis artifact changed after review",
            code="artifact_hash_mismatch",
        )
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        emission = emit_selected_candidate(
            config,
            analysis,
            analysis_path,
            candidate_source="templates",
            verify_formal=False,
        )
    except (OSError, json.JSONDecodeError, CandidateV2Error, RTLInputError) as exc:
        raise AgentAPIError(str(exc), code="candidate_failed") from exc

    candidate_id = str(emission.get("candidate_id") or selected_id)
    output_path = root / "candidates" / f"{candidate_id}.json"
    payload = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "document_type": "rtl-advisor.agent.candidate",
        "flow_version": AGENT_FLOW_VERSION,
        "status": emission.get("status"),
        "run_id": run_id,
        "finding_id": finding_id,
        "candidate_id": candidate_id,
        "safe": False,
        "source_integrity": emission.get("source_integrity"),
        "candidate": emission,
        "artifacts": {
            "candidate_record": str(output_path),
            "candidate_root": emission.get("artifact_root"),
            "diff": emission.get("diff_path"),
        },
        "command": list(normalized_command),
    }
    return _write_document(output_path, payload)


def agent_verify(
    config: ProjectConfig,
    run_id: str,
    *,
    candidate_id: str,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise AgentAPIError(
            f"invalid candidate ID: {candidate_id!r}", code="invalid_candidate_id"
        )
    root = _run_root(config, run_id)
    review = _read_document(
        root / "review.json", document_type="rtl-advisor.agent.review"
    )
    candidate_record = _read_document(
        root / "candidates" / f"{candidate_id}.json",
        document_type="rtl-advisor.agent.candidate",
    )
    if candidate_record.get("run_id") != run_id:
        raise AgentAPIError("candidate run ID mismatch", code="invalid_artifact")
    integrity = _input_integrity(review.get("input", {}).get("files") or [])
    if not integrity["ok"]:
        raise AgentAPIError(
            "input source hashes changed after review", code="source_hash_mismatch"
        )
    try:
        verification = verify_emitted_candidate(
            config,
            Path(review["artifacts"]["analysis"]),
            candidate_id,
        )
    except (CandidateV2Error, RTLInputError) as exc:
        raise AgentAPIError(str(exc), code="verification_failed") from exc
    passed = bool(verification.get("safe"))
    output_path = root / "verification" / f"{candidate_id}.json"
    payload = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "document_type": "rtl-advisor.agent.verification",
        "flow_version": AGENT_FLOW_VERSION,
        "status": "passed" if passed else "failed",
        "run_id": run_id,
        "candidate_id": candidate_id,
        "safe": passed,
        "source_integrity": verification.get("source_integrity"),
        "formal": verification.get("formal"),
        "candidate_design_hash": verification.get("candidate_design_hash"),
        "artifacts": {
            "verification": str(output_path),
            "candidate_root": verification.get("artifact_root"),
            "formal": str(Path(verification["artifact_root"]) / "formal.json"),
            "diff": verification.get("diff_path"),
        },
        "command": list(normalized_command),
    }
    return _write_document(output_path, payload)


def agent_error_payload(
    operation: str,
    error: AgentAPIError,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    payload = {
        "schema_version": AGENT_SCHEMA_VERSION,
        "document_type": "rtl-advisor.agent.error",
        "flow_version": AGENT_FLOW_VERSION,
        "status": "failed",
        "operation": operation,
        "error": {"code": error.code, "message": str(error)},
        "command": list(normalized_command),
    }
    payload["semantic_hash"] = _json_hash(payload)
    return payload


def agent_exit_code(payload: dict[str, Any]) -> int:
    document_type = payload.get("document_type")
    status = payload.get("status")
    if document_type == "rtl-advisor.agent.capabilities":
        return 0
    if document_type == "rtl-advisor.agent.review":
        return 0 if status == "completed" else 3
    if document_type == "rtl-advisor.agent.candidate":
        return 0 if status == "prepared" else 4
    if document_type == "rtl-advisor.agent.verification":
        return 0 if status == "passed" else 4
    return 2
