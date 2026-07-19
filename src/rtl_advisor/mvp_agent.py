from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import re
import shlex
import shutil
from typing import Any, Iterable, Mapping, Sequence

from rtl_advisor import __version__
from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_schema import (
    AGENT_V2_FLOW_VERSION,
    AGENT_V2_SCHEMA_VERSION,
    OBJECTIVES,
    RUN_SCHEMA_ID,
    RUN_SCHEMA_VERSION,
    SYNTHESIS_PROFILES,
    TRANSFORMATION_ID,
    TRANSFORMATION_VERSION,
    MVPSchemaError,
    compile_context_snapshot,
    file_sha256,
    load_pilot_manifest,
    read_hashed_json,
    source_integrity,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.rtl_input import (
    DESIGN_INPUT_SCHEMA_VERSION,
    DesignInputV2,
    SourceFileV2,
    RTLInputError,
    normalize_design_input,
)


_RUN_ID = re.compile(r"^mvp-[0-9a-f]{20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


class MVPAgentError(RuntimeError):
    """Raised when an Agent V2 stage cannot produce trustworthy evidence."""

    def __init__(self, message: str, *, code: str = "mvp_agent_error") -> None:
        super().__init__(message)
        self.code = code


def _command_status(command: str) -> dict[str, Any]:
    try:
        executable = shlex.split(command)[0]
    except (ValueError, IndexError):
        return {"status": "missing", "configured_command": command, "path": None}
    candidate = Path(executable).expanduser()
    if candidate.is_absolute() or "/" in executable:
        resolved = candidate.resolve() if candidate.exists() else None
    else:
        found = shutil.which(executable)
        resolved = Path(found).resolve() if found else None
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
        return {"status": "missing", "path": str(path), "sha256": None}
    from rtl_advisor.mvp_schema import file_sha256

    actual = file_sha256(path)
    return {
        "status": "available" if actual == config.liberty.sha256 else "mismatch",
        "path": str(path),
        "sha256": actual,
        "expected_sha256": config.liberty.sha256,
    }


def _verilator_status(config: ProjectConfig) -> dict[str, Any]:
    installed = _command_status(config.tools.verilator)
    if installed["status"] != "available":
        return installed
    from rtl_advisor.mvp_rewriter import MVPRewriteError, _verilator_identity

    try:
        identity = _verilator_identity(config)
    except MVPRewriteError as exc:
        return {
            **installed,
            "status": "unavailable",
            "error_code": exc.code,
            "detail": str(exc),
        }
    return {**installed, "status": "available", **identity}


def _yosys_status(config: ProjectConfig) -> dict[str, Any]:
    """Report whether the exact MVP Yosys/ABC environment is callable."""

    from rtl_advisor.mvp_measure import (
        REQUIRED_ABC_VERSION,
        REQUIRED_YOSYS_VERSION,
        MVPMeasurementError,
        _toolchain_identity,
    )

    installed = _command_status(config.tools.yosys)
    if installed["status"] != "available":
        return {
            **installed,
            "version": None,
            "required_version": REQUIRED_YOSYS_VERSION,
            "version_status": "missing",
            "abc_status": "unknown",
            "required_abc_version": REQUIRED_ABC_VERSION,
        }
    try:
        identity = _toolchain_identity(config)
    except MVPMeasurementError as exc:
        mismatch = exc.code in {
            "unsupported_yosys_version",
            "unsupported_abc_version",
        }
        return {
            **installed,
            "status": "mismatch" if mismatch else "unavailable",
            "version": None,
            "required_version": REQUIRED_YOSYS_VERSION,
            "version_status": (
                "mismatch" if exc.code == "unsupported_yosys_version" else "unknown"
            ),
            "abc_status": (
                "mismatch" if exc.code == "unsupported_abc_version" else "unknown"
            ),
            "required_abc_version": REQUIRED_ABC_VERSION,
            "error_code": exc.code,
            "detail": str(exc),
        }
    return {
        **installed,
        "status": "available",
        "version": identity["yosys_version"],
        "required_version": REQUIRED_YOSYS_VERSION,
        "version_status": "matched",
        "sha256": identity["yosys_sha256"],
        "path": identity["yosys_path"],
        "abc_status": "available",
        "abc_version": identity["abc_version"],
        "required_abc_version": REQUIRED_ABC_VERSION,
        "abc_path": identity["abc_path"],
        "abc_sha256": identity["abc_sha256"],
    }


def _agent_root(config: ProjectConfig) -> Path:
    return config.artifacts_dir / "agent-v2"


def _run_root(config: ProjectConfig, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise MVPAgentError(f"invalid MVP run ID: {run_id!r}", code="invalid_run_id")
    return _agent_root(config) / "runs" / run_id


def _candidate_id(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise MVPAgentError(f"invalid candidate ID: {value!r}", code="invalid_candidate_id")
    return value


def _agent_record(
    *,
    document_type: str,
    status: str,
    command: Sequence[str],
    run_id: str | None = None,
    **values: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": AGENT_V2_SCHEMA_VERSION,
        "run_schema": RUN_SCHEMA_ID,
        "document_type": document_type,
        "flow_version": AGENT_V2_FLOW_VERSION,
        "status": status,
        **values,
        "command": list(command),
    }
    if run_id is not None:
        payload["run_id"] = run_id
    return payload


def _write_design(path: Path, design: DesignInputV2) -> dict[str, Any]:
    context = compile_context_snapshot(design)
    return _write_immutable_stage(
        path,
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.run.design-input",
            "design_input_schema_version": design.schema_version,
            "top": design.top,
            "files": [asdict(source) for source in design.files],
            "include_dirs": list(design.include_dirs),
            "defines": list(design.defines),
            "filelists": list(design.filelists),
            "design_hash": design.design_hash,
            "compile_context": context,
        },
    )


def _write_immutable_stage(
    path: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a stage record once, or return the byte-equivalent record."""

    expected = dict(payload)
    expected.pop("semantic_hash", None)
    expected["semantic_hash"] = stable_hash(expected)
    if path.is_file():
        existing = read_hashed_json(path)
        if existing != expected:
            raise MVPAgentError(
                f"append-only stage already exists with different content: {path}",
                code="append_only_conflict",
            )
        return existing
    try:
        return write_hashed_json(path, payload, exclusive=True)
    except MVPSchemaError as exc:
        if exc.code != "append_only_conflict":
            raise
        # Another writer may have won the exclusive-create race. Accept only
        # the exact record this caller intended to create.
        existing = read_hashed_json(path)
        if existing == expected:
            return existing
        raise MVPAgentError(
            f"append-only stage already exists with different content: {path}",
            code="append_only_conflict",
        ) from exc


def _design_from_record(record: Mapping[str, Any]) -> DesignInputV2:
    try:
        return DesignInputV2(
            schema_version=DESIGN_INPUT_SCHEMA_VERSION,
            top=str(record["top"]),
            files=tuple(
                SourceFileV2(path=str(item["path"]), sha256=str(item["sha256"]))
                for item in record["files"]
            ),
            include_dirs=tuple(str(item) for item in record.get("include_dirs", [])),
            defines=tuple(str(item) for item in record.get("defines", [])),
            filelists=tuple(str(item) for item in record.get("filelists", [])),
            design_hash=str(record["design_hash"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MVPAgentError(f"invalid design record: {exc}", code="invalid_artifact") from exc


def _load_run_design(root: Path) -> tuple[dict[str, Any], DesignInputV2]:
    record = read_hashed_json(
        root / "input.json",
        document_type="rtl-advisor.run.design-input",
        schema_version=1,
    )
    return record, _design_from_record(record)


def _load_normalized_input(path: Path) -> DesignInputV2:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        design = _design_from_record(raw)
    except (OSError, json.JSONDecodeError, MVPAgentError) as exc:
        raise MVPAgentError(f"invalid normalized input {path}: {exc}", code="invalid_input") from exc
    integrity = source_integrity(asdict(source) for source in design.files)
    if not integrity["ok"]:
        raise MVPAgentError("normalized input source hashes are stale", code="stale_source_hashes")
    return design


def _resolve_design(
    config: ProjectConfig,
    input_path: str,
    *,
    top: str | None,
    include_dirs: Sequence[str],
    defines: Sequence[str],
) -> tuple[DesignInputV2, dict[str, Any], str | None]:
    path = Path(input_path).expanduser()
    if not path.is_absolute():
        path = config.root / path
    path = path.resolve()
    if not path.is_file():
        raise MVPAgentError(f"input not found: {path}", code="input_not_found")
    if path.suffix.lower() == ".json":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MVPAgentError(f"invalid JSON input {path}: {exc}", code="invalid_input") from exc
        if isinstance(raw, dict) and raw.get("document_type") == "rtl-advisor.pilot-manifest":
            try:
                manifest, design = load_pilot_manifest(path)
            except MVPSchemaError as exc:
                raise MVPAgentError(str(exc), code=exc.code) from exc
            return design, {
                "kind": "pilot_manifest",
                "requested_path": str(path),
                "provenance": asdict(manifest.provenance),
                "objective": manifest.objective,
                "synthesis_profiles": list(manifest.synthesis_profiles),
            }, manifest.objective
        if isinstance(raw, dict) and "design_hash" in raw and "files" in raw:
            return _load_normalized_input(path), {
                "kind": "normalized_design_input",
                "requested_path": str(path),
            }, None
        raise MVPAgentError(
            "JSON input is neither a PilotManifest nor a normalized design input",
            code="invalid_input",
        )
    if top is None:
        raise MVPAgentError("--top is required for RTL or filelist input", code="top_required")
    try:
        if path.suffix.lower() in {".f", ".flist", ".lst"}:
            design = normalize_design_input(
                top=top,
                filelist=path,
                include_dirs=include_dirs,
                defines=defines,
                base=config.root,
            )
            kind = "filelist"
        else:
            design = normalize_design_input(
                top=top,
                files=(path,),
                include_dirs=include_dirs,
                defines=defines,
                base=config.root,
            )
            kind = "rtl_file"
    except RTLInputError as exc:
        raise MVPAgentError(str(exc), code="invalid_input") from exc
    return design, {
        "kind": kind,
        "requested_path": str(path),
        "provenance": {"scope": "explicitly_approved_open_or_generated"},
    }, None


def agent_v2_capabilities(
    config: ProjectConfig,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    tools = {
        "pyslang": _pyslang_status(),
        "verilator": _verilator_status(config),
        "yosys": _yosys_status(config),
        "liberty": _liberty_status(config),
        "eqy": {"status": "deferred", "path": None},
    }
    # Review and isolated rewrite are implemented by the conservative source
    # scanner and do not execute external tools. Verification performs both
    # Verilator lint and Yosys equivalence. Measurement additionally requires
    # the exact pinned Yosys/ABC environment and Liberty checksum.
    review_ready = True
    candidate_ready = True
    formal_ready = (
        tools["verilator"]["status"] == "available"
        and tools["yosys"]["status"] == "available"
    )
    measure_ready = (
        tools["yosys"]["status"] == "available"
        and tools["liberty"]["status"] == "available"
    )
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.capabilities",
        status="ok",
        command=normalized_command,
        cli_version=__version__,
        transformation={
            "id": TRANSFORMATION_ID,
            "version": TRANSFORMATION_VERSION,
            "analysis_supported": True,
            "rewriter_available": candidate_ready,
            "sequential_supported": False,
        },
        objectives=list(OBJECTIVES),
        synthesis_profiles=list(SYNTHESIS_PROFILES),
        operations={
            "review": {"available": review_ready},
            "candidate": {"available": candidate_ready},
            "verify": {"available": formal_ready, "required_before_measure": True},
            "measure": {"available": measure_ready},
            "report": {"available": True},
        },
        tools=tools,
        model={
            "id": "v22",
            "release_status": "diagnostic_only",
            "affects_mvp_decision": False,
        },
        limitations=[
            "Only unsigned fixed-width combinational addition chains are supported.",
            "Formal equivalence uses two-state RTL semantics.",
            "Measurements describe the pinned Yosys/ABC recipes, not a target flow.",
            "EQY, sequential proof, and technology-netlist LEC are deferred.",
        ],
    )
    return write_hashed_json(_agent_root(config) / "capabilities.json", payload)


def agent_v2_review(
    config: ProjectConfig,
    input_path: str,
    *,
    objective: str,
    top: str | None = None,
    include_dirs: Sequence[str] = (),
    defines: Sequence[str] = (),
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    if objective not in OBJECTIVES:
        raise MVPAgentError(f"unsupported objective: {objective!r}", code="unsupported_objective")
    design, input_context, manifest_objective = _resolve_design(
        config,
        input_path,
        top=top,
        include_dirs=include_dirs,
        defines=defines,
    )
    if manifest_objective is not None and manifest_objective != objective:
        raise MVPAgentError(
            f"requested objective {objective!r} does not match frozen manifest objective {manifest_objective!r}",
            code="objective_mismatch",
        )
    from rtl_advisor import mvp_rewriter

    try:
        scan_analysis = getattr(mvp_rewriter, "scan_addition_analysis", None)
        if callable(scan_analysis):
            analysis = scan_analysis(design)
            if not isinstance(analysis, Mapping):
                raise MVPAgentError(
                    "structured scanner result must be an object",
                    code="invalid_scanner_result",
                )
            findings = list(analysis.get("findings") or [])
            exclusions = list(analysis.get("exclusions") or [])
        else:
            findings = mvp_rewriter.scan_addition_sites(design)
            exclusions = []
    except mvp_rewriter.MVPRewriteError as exc:
        raise MVPAgentError(str(exc), code=exc.code) from exc
    identity = {
        "run_schema": RUN_SCHEMA_ID,
        "design_hash": design.design_hash,
        "compile_context_hash": compile_context_snapshot(design)["compile_context_hash"],
        "objective": objective,
        "transformation_version": TRANSFORMATION_VERSION,
        "input_context": input_context,
    }
    run_id = f"mvp-{stable_hash(identity)[:20]}"
    root = _run_root(config, run_id)
    design_record = _write_design(root / "input.json", design)
    decision = "candidate_available" if findings else "unsupported"
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.review",
        status="completed",
        command=normalized_command,
        run_id=run_id,
        decision=decision,
        objective=objective,
        input={
            **input_context,
            "top": design.top,
            "design_hash": design.design_hash,
            "files": [asdict(source) for source in design.files],
            "include_dirs": list(design.include_dirs),
            "defines": list(design.defines),
            "compile_context_hash": design_record["compile_context"]["compile_context_hash"],
            "source_integrity": source_integrity(asdict(source) for source in design.files),
        },
        findings=findings,
        exclusions=exclusions,
        coverage={
            "eligible_site_count": len(findings),
            "excluded_site_count": len(exclusions),
        },
        candidate_generation_allowed=bool(findings),
        evidence={
            "rules_only": True,
            "model_used": False,
            "input_semantic_hash": design_record["semantic_hash"],
        },
        limitations=[
            "A finding is only a candidate to evaluate, not a recommendation.",
            "The source is unchanged; any candidate must be requested explicitly.",
        ],
        artifacts={"root": str(root), "review": str(root / "review.json"), "input": str(root / "input.json")},
    )
    return _write_immutable_stage(root / "review.json", payload)


def _read_review(root: Path) -> dict[str, Any]:
    review = read_hashed_json(
        root / "review.json",
        document_type="rtl-advisor.agent.v2.review",
        schema_version=AGENT_V2_SCHEMA_VERSION,
    )
    _validate_stage_identity(review, root=root, stage="review")
    input_record, _ = _load_run_design(root)
    input_parent = (review.get("evidence") or {}).get("input_semantic_hash")
    if input_parent != input_record.get("semantic_hash"):
        raise MVPAgentError(
            "review input parent hash does not match the current input record",
            code="artifact_parent_mismatch",
        )
    findings = review.get("findings")
    if not isinstance(findings, list):
        raise MVPAgentError("review findings must be an array", code="invalid_artifact")
    finding_ids = [
        str(item.get("finding_id", ""))
        for item in findings
        if isinstance(item, Mapping)
    ]
    if len(finding_ids) != len(findings) or any(
        not _IDENTIFIER.fullmatch(item) for item in finding_ids
    ):
        raise MVPAgentError("review contains an invalid finding", code="invalid_artifact")
    if len(set(finding_ids)) != len(finding_ids):
        raise MVPAgentError("review contains duplicate finding IDs", code="invalid_artifact")
    expected_decision = "candidate_available" if findings else "unsupported"
    if review.get("decision") != expected_decision:
        raise MVPAgentError(
            "review decision does not match its eligible findings",
            code="invalid_artifact",
        )
    return review


def _validate_stage_identity(
    record: Mapping[str, Any],
    *,
    root: Path,
    stage: str,
    candidate_id: str | None = None,
) -> None:
    if record.get("run_schema") != RUN_SCHEMA_ID:
        raise MVPAgentError(
            f"{stage} record has the wrong run schema", code="invalid_artifact"
        )
    if record.get("run_id") != root.name:
        raise MVPAgentError(
            f"{stage} record belongs to a different run", code="invalid_artifact"
        )
    if candidate_id is not None and record.get("candidate_id") != candidate_id:
        raise MVPAgentError(
            f"{stage} record belongs to a different candidate",
            code="invalid_artifact",
        )


def _require_parent_hash(
    record: Mapping[str, Any],
    *,
    key: str,
    expected: Any,
    stage: str,
) -> None:
    if (record.get("parents") or {}).get(key) != expected:
        raise MVPAgentError(
            f"{stage} parent hash mismatch for {key}",
            code="artifact_parent_mismatch",
        )


def _linked_evidence_record(
    record: Mapping[str, Any],
    *,
    candidate_root: Path,
    document_type: str,
    schema_version: int,
    stage: str,
) -> dict[str, Any] | None:
    """Load a hash-linked low-level evidence record when one is advertised."""

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts.get("evidence_record"):
        return None
    evidence_path = Path(str(artifacts["evidence_record"])).expanduser().resolve()
    try:
        evidence_path.relative_to(candidate_root.resolve())
    except ValueError as exc:
        raise MVPAgentError(
            f"{stage} evidence escapes its candidate workspace",
            code="invalid_artifact_path",
        ) from exc
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise MVPAgentError(
            f"{stage} evidence is missing or not a regular file: {evidence_path}",
            code="missing_stage_evidence",
        )
    evidence = read_hashed_json(
        evidence_path,
        document_type=document_type,
        schema_version=schema_version,
    )
    expected_hash = artifacts.get("evidence_semantic_hash")
    if expected_hash is not None and evidence.get("semantic_hash") != expected_hash:
        raise MVPAgentError(
            f"{stage} low-level evidence hash mismatch",
            code="artifact_parent_mismatch",
        )
    return evidence


def _require_current_design(root: Path) -> tuple[dict[str, Any], DesignInputV2]:
    design_record, design = _load_run_design(root)
    integrity = source_integrity(asdict(source) for source in design.files)
    if not integrity["ok"]:
        raise MVPAgentError("source hashes changed; run review again", code="stale_source_hashes")
    expected_context = design_record.get("compile_context")
    if not isinstance(expected_context, Mapping):
        raise MVPAgentError(
            "run input has no compile-context snapshot",
            code="invalid_artifact",
        )
    try:
        current_context = compile_context_snapshot(design)
    except MVPSchemaError as exc:
        raise MVPAgentError(str(exc), code=exc.code) from exc
    if current_context != expected_context:
        raise MVPAgentError(
            "compile context changed; run review again",
            code="stale_compile_context",
        )
    return design_record, design


def agent_v2_candidate(
    config: ProjectConfig,
    run_id: str,
    *,
    finding_id: str,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    root = _run_root(config, run_id)
    review = _read_review(root)
    _, design = _require_current_design(root)
    findings = {str(item.get("finding_id")): item for item in review.get("findings", [])}
    if finding_id not in findings:
        raise MVPAgentError(f"unknown finding ID: {finding_id}", code="finding_not_found")
    from rtl_advisor.mvp_rewriter import MVPRewriteError, prepare_addition_candidate

    try:
        prepared = prepare_addition_candidate(
            design,
            finding_id,
            root / "candidates",
        )
    except MVPRewriteError as exc:
        raise MVPAgentError(str(exc), code=exc.code) from exc
    candidate_id = _candidate_id(str(prepared["candidate_id"]))
    candidate_root = root / "candidates" / candidate_id
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.candidate",
        status="candidate_prepared",
        command=normalized_command,
        run_id=run_id,
        decision="candidate_prepared",
        objective=review["objective"],
        candidate_id=candidate_id,
        finding=findings[finding_id],
        source_integrity=prepared.get("source_integrity"),
        candidate=prepared,
        parents={"review_semantic_hash": review["semantic_hash"]},
        limitations=["The isolated candidate is unproven until formal verification passes."],
        artifacts={
            "candidate_record": str(candidate_root / "candidate.json"),
            "diff": prepared.get("diff_path"),
            "root": str(candidate_root),
        },
    )
    return _write_immutable_stage(candidate_root / "candidate.json", payload)


def _candidate_record(
    root: Path,
    candidate_id: str,
    *,
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_id = _candidate_id(candidate_id)
    candidate = read_hashed_json(
        root / "candidates" / _candidate_id(candidate_id) / "candidate.json",
        document_type="rtl-advisor.agent.v2.candidate",
        schema_version=AGENT_V2_SCHEMA_VERSION,
    )
    review = review or _read_review(root)
    _validate_stage_identity(
        candidate, root=root, stage="candidate", candidate_id=candidate_id
    )
    _require_parent_hash(
        candidate,
        key="review_semantic_hash",
        expected=review.get("semantic_hash"),
        stage="candidate",
    )
    findings = {
        str(item.get("finding_id")): item
        for item in review.get("findings", [])
        if isinstance(item, Mapping)
    }
    finding = candidate.get("finding")
    finding_id = str(finding.get("finding_id", "")) if isinstance(finding, Mapping) else ""
    prepared = candidate.get("candidate")
    prepared_finding_id = (
        str(prepared.get("finding_id", "")) if isinstance(prepared, Mapping) else ""
    )
    prepared_candidate_id = (
        str(prepared.get("candidate_id", "")) if isinstance(prepared, Mapping) else ""
    )
    if (
        finding_id not in findings
        or finding != findings[finding_id]
        or prepared_finding_id != finding_id
        or prepared_candidate_id != candidate_id
    ):
        raise MVPAgentError(
            "candidate does not match an eligible review finding",
            code="artifact_parent_mismatch",
        )
    # Validate the nested low-level rewrite record and its current isolated
    # sources. This prevents report generation from silently accepting a
    # tampered candidate workspace.
    from rtl_advisor.mvp_rewriter import MVPRewriteError, candidate_design_from_record

    try:
        candidate_design_from_record(prepared)
    except MVPRewriteError as exc:
        raise MVPAgentError(str(exc), code=exc.code) from exc
    diff_path = Path(str(prepared.get("diff_path", ""))).expanduser().resolve()
    candidate_root = (root / "candidates" / candidate_id).resolve()
    try:
        diff_path.relative_to(candidate_root)
    except ValueError as exc:
        raise MVPAgentError(
            "candidate diff escapes its artifact workspace",
            code="invalid_artifact_path",
        ) from exc
    if (
        not diff_path.is_file()
        or diff_path.is_symlink()
        or file_sha256(diff_path) != prepared.get("diff_sha256")
    ):
        raise MVPAgentError("candidate diff is stale", code="stale_candidate")
    return candidate


def _verification_record(
    root: Path,
    candidate_id: str,
    *,
    candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = candidate or _candidate_record(root, candidate_id)
    verification = read_hashed_json(
        root / "candidates" / candidate_id / "verification.json",
        document_type="rtl-advisor.agent.v2.verification",
        schema_version=AGENT_V2_SCHEMA_VERSION,
    )
    _validate_stage_identity(
        verification, root=root, stage="verification", candidate_id=candidate_id
    )
    _require_parent_hash(
        verification,
        key="candidate_semantic_hash",
        expected=candidate.get("semantic_hash"),
        stage="verification",
    )
    status = str(verification.get("status", ""))
    formal_status = str((verification.get("formal") or {}).get("status", ""))
    expected_formal = {
        "formal_passed": "passed",
        "formal_failed": "failed",
        "formal_inconclusive": "inconclusive",
    }.get(status)
    if expected_formal != formal_status:
        raise MVPAgentError(
            "verification status disagrees with its formal evidence",
            code="invalid_artifact",
        )
    if (verification.get("safe") is True) != (status == "formal_passed"):
        raise MVPAgentError(
            "verification safety flag disagrees with its status",
            code="invalid_artifact",
        )
    candidate_root = root / "candidates" / candidate_id
    evidence = _linked_evidence_record(
        verification,
        candidate_root=candidate_root,
        document_type="rtl-advisor.formal-result",
        schema_version=RUN_SCHEMA_VERSION,
        stage="verification",
    )
    if evidence is not None:
        evidence_fields = (
            "status",
            "safe",
            "baseline_design_hash",
            "candidate_design_hash",
            "compile_context",
            "lint",
            "formal",
        )
        if any(evidence.get(field) != verification.get(field) for field in evidence_fields):
            raise MVPAgentError(
                "verification disagrees with its hash-linked formal evidence",
                code="artifact_parent_mismatch",
            )
    if status == "formal_passed":
        formal = verification.get("formal")
        assert isinstance(formal, Mapping)
        lint = verification.get("lint")
        verilator = lint.get("verilator") if isinstance(lint, Mapping) else None
        baseline_lint = (
            verilator.get("baseline") if isinstance(verilator, Mapping) else None
        )
        candidate_lint = (
            verilator.get("candidate") if isinstance(verilator, Mapping) else None
        )
        baseline_identity = (
            baseline_lint.get("identity")
            if isinstance(baseline_lint, Mapping)
            else None
        )
        candidate_identity = (
            candidate_lint.get("identity")
            if isinstance(candidate_lint, Mapping)
            else None
        )
        if (
            not isinstance(baseline_identity, Mapping)
            or baseline_identity != candidate_identity
        ):
            raise MVPAgentError(
                "passing verification has incomplete or mismatched Verilator identity",
                code="invalid_artifact",
            )
        identity = formal.get("tool_identity")
        if (
            not isinstance(identity, Mapping)
            or identity.get("yosys_version") != formal.get("yosys_version")
            or identity.get("yosys_path") != formal.get("yosys_path")
            or identity.get("yosys_sha256") != formal.get("yosys_sha256")
            or formal.get("success_marker_seen") is not True
        ):
            raise MVPAgentError(
                "passing verification has incomplete Yosys identity or transcript evidence",
                code="invalid_artifact",
            )
        for label in ("script", "log"):
            artifact_path = Path(str(formal.get(f"{label}_path", ""))).expanduser().resolve()
            try:
                artifact_path.relative_to(candidate_root.resolve())
            except ValueError as exc:
                raise MVPAgentError(
                    f"formal {label} escapes its candidate workspace",
                    code="invalid_artifact_path",
                ) from exc
            expected_hash = formal.get(f"{label}_sha256")
            if (
                artifact_path.is_symlink()
                or not artifact_path.is_file()
                or not isinstance(expected_hash, str)
                or file_sha256(artifact_path) != expected_hash
            ):
                raise MVPAgentError(
                    f"formal {label} artifact is stale",
                    code="stale_formal_artifact",
                )
    return verification


def _measurement_record(
    root: Path,
    candidate_id: str,
    *,
    verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    verification = verification or _verification_record(root, candidate_id)
    measurement = read_hashed_json(
        root / "candidates" / candidate_id / "measurement.json",
        document_type="rtl-advisor.agent.v2.measurement",
        schema_version=AGENT_V2_SCHEMA_VERSION,
    )
    _validate_stage_identity(
        measurement, root=root, stage="measurement", candidate_id=candidate_id
    )
    _require_parent_hash(
        measurement,
        key="verification_semantic_hash",
        expected=verification.get("semantic_hash"),
        stage="measurement",
    )
    if verification.get("status") != "formal_passed" or verification.get("safe") is not True:
        raise MVPAgentError(
            "measurement is not backed by a safe formal proof",
            code="artifact_parent_mismatch",
        )
    if (measurement.get("formal") or {}).get("semantic_hash") != verification.get(
        "semantic_hash"
    ):
        raise MVPAgentError(
            "measurement formal evidence hash mismatch",
            code="artifact_parent_mismatch",
        )
    evidence = _linked_evidence_record(
        measurement,
        candidate_root=root / "candidates" / candidate_id,
        document_type="rtl-advisor.measurement",
        schema_version=1,
        stage="measurement",
    )
    if evidence is not None:
        if any(
            evidence.get(field) != measurement.get(field)
            for field in ("decision", "objective", "measurements")
        ) or (evidence.get("formal") or {}).get(
            "proof_semantic_hash"
        ) != verification.get("semantic_hash"):
            raise MVPAgentError(
                "measurement disagrees with its hash-linked synthesis evidence",
                code="artifact_parent_mismatch",
            )
    profiles = measurement.get("measurements")
    if not isinstance(profiles, Mapping) or set(profiles) != set(SYNTHESIS_PROFILES):
        raise MVPAgentError(
            "measurement does not contain both frozen synthesis profiles",
            code="invalid_artifact",
        )
    classifications = []
    for profile in SYNTHESIS_PROFILES:
        value = profiles.get(profile)
        if not isinstance(value, Mapping):
            raise MVPAgentError(
                f"measurement profile {profile!r} is invalid",
                code="invalid_artifact",
            )
        classifications.append(str(value.get("classification", "")))
    from rtl_advisor.mvp_measure import (
        MVPMeasurementError,
        _artifact_observation,
        aggregate_measurements,
        classify_recipe,
    )

    if evidence is not None:
        objective = str(measurement.get("objective", ""))
        for profile in SYNTHESIS_PROFILES:
            value = profiles[profile]
            assert isinstance(value, Mapping)
            baseline = value.get("baseline")
            candidate_result = value.get("candidate")
            if not isinstance(baseline, Mapping) or not isinstance(
                candidate_result, Mapping
            ):
                raise MVPAgentError(
                    f"measurement profile {profile!r} has no baseline/candidate evidence",
                    code="invalid_artifact",
                )
            observations = {
                "baseline": _artifact_observation(baseline),
                "candidate": _artifact_observation(candidate_result),
            }
            if any(not item["ok"] for item in observations.values()):
                raise MVPAgentError(
                    f"measurement profile {profile!r} has stale synthesis artifacts",
                    code="stale_measurement_artifact",
                )
            try:
                expected_class = classify_recipe(
                    objective,
                    baseline.get("metrics"),
                    candidate_result.get("metrics"),
                )
            except MVPMeasurementError as exc:
                raise MVPAgentError(str(exc), code=exc.code) from exc
            if value.get("classification") != expected_class:
                raise MVPAgentError(
                    f"measurement profile {profile!r} classification disagrees with its metrics",
                    code="invalid_artifact",
                )

    try:
        expected_decision = aggregate_measurements(*classifications)
    except MVPMeasurementError as exc:
        raise MVPAgentError(str(exc), code=exc.code) from exc
    if measurement.get("decision") != expected_decision:
        raise MVPAgentError(
            "measurement decision disagrees with its recipe evidence",
            code="invalid_artifact",
        )
    return measurement


def agent_v2_verify(
    config: ProjectConfig,
    run_id: str,
    *,
    candidate_id: str,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    root = _run_root(config, run_id)
    _read_review(root)
    _require_current_design(root)
    candidate = _candidate_record(root, candidate_id)
    from rtl_advisor.mvp_rewriter import MVPRewriteError, verify_addition_candidate

    try:
        verification = verify_addition_candidate(
            config,
            candidate["candidate"],
            root / "candidates",
        )
    except MVPRewriteError as exc:
        raise MVPAgentError(str(exc), code=exc.code) from exc
    formal_evidence = dict(verification.get("formal") or verification)
    formal_status = str(formal_evidence.get("status", verification.get("status", "inconclusive")))
    low_level_safe = verification.get("safe") is True
    if formal_status == "passed" and low_level_safe:
        status = "formal_passed"
    elif formal_status == "failed":
        status = "formal_failed"
    else:
        status = "formal_inconclusive"
    if formal_status == "passed" and not low_level_safe:
        formal_evidence = {
            **formal_evidence,
            "status": "inconclusive",
            "detail": "low-level verification did not mark the proof safe",
        }
    output_path = root / "candidates" / candidate_id / "verification.json"
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.verification",
        status=status,
        command=normalized_command,
        run_id=run_id,
        decision=status,
        candidate_id=candidate_id,
        baseline_design_hash=verification.get("baseline_design_hash"),
        candidate_design_hash=verification.get("candidate_design_hash"),
        compile_context=verification.get("compile_context"),
        source_integrity=verification.get("source_integrity"),
        lint=verification.get("lint"),
        formal=formal_evidence,
        safe=status == "formal_passed" and low_level_safe,
        parents={"candidate_semantic_hash": candidate["semantic_hash"]},
        limitations=["The proof covers two-state combinational RTL semantics."],
        artifacts={
            "verification": str(output_path),
            "evidence_record": verification.get("record_path"),
            "evidence_semantic_hash": verification.get("semantic_hash"),
            **(verification.get("artifacts") or {}),
        },
    )
    return _write_immutable_stage(output_path, payload)


def agent_v2_measure(
    config: ProjectConfig,
    run_id: str,
    *,
    candidate_id: str,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    root = _run_root(config, run_id)
    review = _read_review(root)
    _, baseline = _require_current_design(root)
    candidate = _candidate_record(root, candidate_id, review=review)
    verification = _verification_record(
        root, candidate_id, candidate=candidate
    )
    if verification.get("status") != "formal_passed" or verification.get("safe") is not True:
        raise MVPAgentError("a current passing formal proof is required", code="formal_required")
    from rtl_advisor.mvp_rewriter import MVPRewriteError, candidate_design_from_record
    from rtl_advisor.mvp_measure import MVPMeasurementError, measure_candidate

    try:
        candidate_design = candidate_design_from_record(candidate["candidate"])
        measurement = measure_candidate(
            config,
            baseline,
            candidate_design,
            verification,
            root / "candidates" / candidate_id / "measurement",
            objective=str(review["objective"]),
        )
    except (MVPRewriteError, MVPMeasurementError) as exc:
        failure_core = {
            "candidate_id": candidate_id,
            "verification_semantic_hash": verification["semantic_hash"],
            "error": {"code": exc.code, "message": str(exc)},
        }
        failure_id = stable_hash(failure_core)[:16]
        failure_path = (
            root
            / "candidates"
            / candidate_id
            / "measurement-failures"
            / f"{failure_id}.json"
        )
        _write_immutable_stage(
            failure_path,
            _agent_record(
                document_type="rtl-advisor.agent.v2.measurement-failure",
                status="failed",
                command=normalized_command,
                run_id=run_id,
                decision="measurement_failed",
                objective=review["objective"],
                candidate_id=candidate_id,
                error=failure_core["error"],
                parents={
                    "verification_semantic_hash": verification["semantic_hash"]
                },
                artifacts={"failure": str(failure_path)},
            ),
        )
        raise MVPAgentError(
            f"{exc}; failure evidence: {failure_path}", code=exc.code
        ) from exc
    decision = str(measurement["decision"])
    output_path = root / "candidates" / candidate_id / "measurement.json"
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.measurement",
        status="completed",
        command=normalized_command,
        run_id=run_id,
        decision=decision,
        objective=review["objective"],
        candidate_id=candidate_id,
        source_integrity=measurement.get("source_integrity"),
        formal={"status": "passed", "semantic_hash": verification["semantic_hash"]},
        measurements=measurement.get("measurements"),
        parents={"verification_semantic_hash": verification["semantic_hash"]},
        limitations=[
            "This decision describes two pinned Yosys/ABC recipes and is not target-flow PPA.",
        ],
        artifacts={
            **(measurement.get("artifacts") or {}),
            "evidence_record": (measurement.get("artifacts") or {}).get("measurement"),
            "evidence_semantic_hash": measurement.get("semantic_hash"),
            "measurement": str(output_path),
        },
    )
    return _write_immutable_stage(output_path, payload)


def _candidate_records(
    root: Path, review: Mapping[str, Any]
) -> list[dict[str, Any]]:
    candidate_root = root / "candidates"
    if not candidate_root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for candidate_dir in sorted(candidate_root.iterdir(), key=lambda path: path.name):
        if candidate_dir.is_symlink() or not candidate_dir.is_dir():
            raise MVPAgentError(
                f"unexpected candidate artifact entry: {candidate_dir}",
                code="invalid_artifact",
            )
        candidate_id = _candidate_id(candidate_dir.name)
        candidate_path = candidate_dir / "candidate.json"
        if not candidate_path.is_file() or candidate_path.is_symlink():
            raise MVPAgentError(
                f"candidate evidence is missing: {candidate_path}",
                code="missing_candidate_evidence",
            )
        candidate = _candidate_record(root, candidate_id, review=review)
        verification_path = candidate_dir / "verification.json"
        measurement_path = candidate_dir / "measurement.json"
        failure_root = candidate_dir / "measurement-failures"
        entry: dict[str, Any] = {"candidate": candidate}
        if verification_path.is_file():
            entry["verification"] = _verification_record(
                root, candidate_id, candidate=candidate
            )
        if measurement_path.is_file():
            if "verification" not in entry:
                raise MVPAgentError(
                    f"measurement exists without verification for {candidate_id}",
                    code="missing_parent_artifact",
                )
            entry["measurement"] = _measurement_record(
                root,
                candidate_id,
                verification=entry["verification"],
            )
        if failure_root.is_dir():
            if "verification" not in entry:
                raise MVPAgentError(
                    f"measurement failure exists without verification for {candidate_id}",
                    code="missing_parent_artifact",
                )
            failures: list[dict[str, Any]] = []
            for failure_path in sorted(failure_root.iterdir(), key=lambda path: path.name):
                if (
                    failure_path.is_symlink()
                    or not failure_path.is_file()
                    or failure_path.suffix != ".json"
                ):
                    raise MVPAgentError(
                        f"unexpected measurement-failure artifact: {failure_path}",
                        code="invalid_artifact",
                    )
                failure = read_hashed_json(
                    failure_path,
                    document_type="rtl-advisor.agent.v2.measurement-failure",
                    schema_version=AGENT_V2_SCHEMA_VERSION,
                )
                _validate_stage_identity(
                    failure,
                    root=root,
                    stage="measurement failure",
                    candidate_id=candidate_id,
                )
                _require_parent_hash(
                    failure,
                    key="verification_semantic_hash",
                    expected=entry["verification"].get("semantic_hash"),
                    stage="measurement failure",
                )
                if failure.get("status") != "failed" or failure.get(
                    "decision"
                ) != "measurement_failed":
                    raise MVPAgentError(
                        "measurement-failure record has an invalid state",
                        code="invalid_artifact",
                    )
                failures.append(failure)
            if failures:
                entry["measurement_failures"] = failures
        entry["candidate_id"] = candidate_id
        records.append(entry)
    finding_ids = [
        str((record["candidate"].get("finding") or {}).get("finding_id", ""))
        for record in records
    ]
    duplicates = sorted(
        finding_id
        for finding_id, count in Counter(finding_ids).items()
        if finding_id and count > 1
    )
    if duplicates:
        raise MVPAgentError(
            f"multiple candidates exist for eligible site(s): {', '.join(duplicates)}",
            code="duplicate_candidate_evidence",
        )
    return records


def _evidence_summary(
    records: Sequence[Mapping[str, Any]], review: Mapping[str, Any]
) -> dict[str, Any]:
    eligible_ids = [
        str(item.get("finding_id"))
        for item in review.get("findings", [])
        if isinstance(item, Mapping)
    ]
    candidates_by_finding = {
        str((record["candidate"].get("finding") or {}).get("finding_id", "")): record
        for record in records
    }
    missing_candidate_ids = sorted(set(eligible_ids) - set(candidates_by_finding))
    missing_formal_ids: list[str] = []
    missing_measurement_ids: list[str] = []
    formal_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    terminal_count = 0
    for record in records:
        candidate_id = str(record.get("candidate_id", ""))
        verification = record.get("verification")
        measurement = record.get("measurement")
        if not isinstance(verification, Mapping):
            missing_formal_ids.append(candidate_id)
            continue
        formal_status = str(verification.get("status", "unknown"))
        formal_counts[formal_status] += 1
        if formal_status == "formal_passed":
            if not isinstance(measurement, Mapping):
                missing_measurement_ids.append(candidate_id)
            else:
                decision_counts[str(measurement.get("decision", "unknown"))] += 1
                terminal_count += 1
        else:
            terminal_count += 1
    complete = not (
        missing_candidate_ids or missing_formal_ids or missing_measurement_ids
    )
    terminal_outcomes = {
        *formal_counts,
        *decision_counts,
    }
    terminal_outcomes.discard("formal_passed")
    summary = {
        "complete": complete,
        "eligible_site_count": len(eligible_ids),
        "candidate_count": len(records),
        "formal_count": sum(formal_counts.values()),
        "measurement_count": sum(decision_counts.values()),
        "terminal_candidate_count": terminal_count,
        "missing_candidate_finding_ids": missing_candidate_ids,
        "missing_formal_candidate_ids": sorted(missing_formal_ids),
        "missing_measurement_candidate_ids": sorted(missing_measurement_ids),
        "formal_status_counts": dict(sorted(formal_counts.items())),
        "measurement_decision_counts": dict(sorted(decision_counts.items())),
        "mixed_outcomes": len(terminal_outcomes) > 1,
    }
    return summary


def _overall_decision(
    records: Iterable[Mapping[str, Any]],
    review: Mapping[str, Any],
    completion: Mapping[str, Any] | None = None,
) -> str:
    records = list(records)
    completion = completion or _evidence_summary(records, review)
    decisions = [
        str(record["measurement"]["decision"])
        for record in records
        if isinstance(record.get("measurement"), Mapping)
    ]
    statuses = [
        str(record["verification"]["status"])
        for record in records
        if isinstance(record.get("verification"), Mapping)
    ]
    # Adverse evidence always wins. A positive candidate must never hide a
    # regression, a failed proof, or an inconclusive proof from another site.
    if "regression" in decisions:
        return "regression"
    if "formal_failed" in statuses:
        return "formal_failed"
    if "formal_inconclusive" in statuses:
        return "formal_inconclusive"
    # Once any site reaches a terminal result, missing evidence elsewhere must
    # be explicit instead of presenting that partial result as the run result.
    if completion.get("complete") is not True and decisions:
        return "incomplete"
    if "flow_dependent" in decisions:
        return "flow_dependent"
    if "measured_improvement" in decisions:
        return "measured_improvement"
    if decisions and set(decisions) == {"synthesis_handles"}:
        return "synthesis_handles"
    if "formal_passed" in statuses:
        return "formal_passed"
    if records:
        return "candidate_prepared"
    return str(review.get("decision", "unsupported"))


def _render_report_html(report: Mapping[str, Any]) -> str:
    decision = html.escape(str(report.get("decision", "unknown")).replace("_", " ").title())
    run_id = html.escape(str(report.get("run_id", "")))
    objective = html.escape(str(report.get("objective", "")))
    completion = report.get("completion") or {}
    completeness = "Complete" if completion.get("complete") else "Incomplete"
    coverage = (
        f"{int(completion.get('terminal_candidate_count', 0))}/"
        f"{int(completion.get('eligible_site_count', 0))} eligible sites reached a terminal result"
    )
    rows: list[str] = []
    for record in report.get("candidates", []):
        candidate = record.get("candidate") or {}
        verification = record.get("verification") or {}
        measurement = record.get("measurement") or {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(record.get('candidate_id', '')))}</td>"
            f"<td>{html.escape(str(verification.get('status', 'not run')).replace('_', ' '))}</td>"
            f"<td>{html.escape(str(measurement.get('decision', 'not run')).replace('_', ' '))}</td>"
            f"<td><code>{html.escape(str((candidate.get('artifacts') or {}).get('diff', '')))}</code></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>RTL Advisor — {run_id}</title><style>
body{{font:15px system-ui;background:#0f1720;color:#e8eef5;margin:0;padding:40px}}main{{max-width:1100px;margin:auto}}
.card{{background:#172330;border:1px solid #304154;border-radius:14px;padding:24px;margin:18px 0}}.label{{color:#8fa3b8;text-transform:uppercase;font-size:12px;letter-spacing:.1em}}
h1{{margin:.3em 0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #304154}}code{{color:#7dd3fc;word-break:break-all}}
</style></head><body><main><span class=\"label\">RTL Advisor developer preview</span><h1>{decision}</h1>
<div class=\"card\"><strong>Run</strong> <code>{run_id}</code><br><strong>Objective</strong> {objective}</div>
<div class=\"card\"><strong>Evidence</strong> {html.escape(completeness)}<br>{html.escape(coverage)}</div>
<div class=\"card\"><table><thead><tr><th>Candidate</th><th>Formal</th><th>Synthesis</th><th>Diff</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan=\"4\">No candidate artifacts yet.</td></tr>'}</tbody></table></div>
<p>Results apply only to the recorded Yosys/ABC recipes. Original RTL remains unchanged.</p></main></body></html>"""


def agent_v2_report(
    config: ProjectConfig,
    run_id: str,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    root = _run_root(config, run_id)
    review = _read_review(root)
    _, current_design = _require_current_design(root)
    records = _candidate_records(root, review)
    completion = _evidence_summary(records, review)
    decision = _overall_decision(records, review, completion)
    output_path = root / "report.json"
    html_path = root / "report.html"
    parent_hashes = {
        "review_semantic_hash": review["semantic_hash"],
        "candidates": {
            str(record["candidate_id"]): {
                "candidate_semantic_hash": record["candidate"]["semantic_hash"],
                "verification_semantic_hash": (
                    record["verification"]["semantic_hash"]
                    if isinstance(record.get("verification"), Mapping)
                    else None
                ),
                "measurement_semantic_hash": (
                    record["measurement"]["semantic_hash"]
                    if isinstance(record.get("measurement"), Mapping)
                    else None
                ),
            }
            for record in records
        },
    }
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.report",
        status="completed" if completion["complete"] else "incomplete",
        command=normalized_command,
        run_id=run_id,
        decision=decision,
        objective=review["objective"],
        review=review,
        candidates=records,
        completion=completion,
        result_counts={
            "formal": completion["formal_status_counts"],
            "synthesis": completion["measurement_decision_counts"],
        },
        parents=parent_hashes,
        source_integrity=source_integrity(
            asdict(source) for source in current_design.files
        ),
        limitations=[
            "A finding is not a recommendation until formal and both synthesis recipes support it.",
            "Results apply only to the recorded Yosys/ABC recipes.",
            *(
                ["The run is incomplete; no positive final conclusion is supported."]
                if not completion["complete"]
                else []
            ),
        ],
        artifacts={
            "report": str(output_path),
            "html": str(html_path),
            "root": str(root),
            "snapshots": str(root / "reports"),
        },
    )
    rendered = _render_report_html(payload)
    html_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    payload["artifacts"]["html_sha256"] = html_sha256
    # Immutable report snapshots retain each chain state. ``report.json`` and
    # ``report.html`` remain compatibility mirrors for existing clients, while
    # the hashed latest pointer identifies the exact immutable snapshot.
    report_hash = stable_hash(payload)
    snapshots = root / "reports"
    snapshot_path = snapshots / f"{report_hash}.json"
    written = _write_immutable_stage(snapshot_path, payload)
    snapshot_html = snapshots / f"{written['semantic_hash']}.html"
    rendered = _render_report_html(written)
    if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != html_sha256:
        raise MVPAgentError(
            "report HTML hash changed while finalizing the immutable snapshot",
            code="artifact_hash_mismatch",
        )
    if snapshot_html.is_file() and snapshot_html.read_text(encoding="utf-8") != rendered:
        raise MVPAgentError(
            f"immutable report HTML snapshot conflicts: {snapshot_html}",
            code="append_only_conflict",
        )
    snapshot_html.parent.mkdir(parents=True, exist_ok=True)
    snapshot_html.write_text(rendered, encoding="utf-8")
    write_hashed_json(output_path, payload)
    html_path.write_text(rendered, encoding="utf-8")
    write_hashed_json(
        snapshots / "latest.json",
        {
            "schema_version": 1,
            "document_type": "rtl-advisor.run.report-latest",
            "run_id": run_id,
            "report_semantic_hash": written["semantic_hash"],
            "report_snapshot": str(snapshot_path),
            "html_snapshot": str(snapshot_html),
            "html_sha256": html_sha256,
        },
    )
    return written


def agent_v2_error_payload(
    operation: str,
    error: MVPAgentError | MVPSchemaError,
    *,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    payload = _agent_record(
        document_type="rtl-advisor.agent.v2.error",
        status="failed",
        command=normalized_command,
        operation=operation,
        error={"code": getattr(error, "code", "mvp_agent_error"), "message": str(error)},
    )
    payload["semantic_hash"] = stable_hash(payload)
    return payload


def agent_v2_exit_code(payload: Mapping[str, Any]) -> int:
    document_type = str(payload.get("document_type", ""))
    status = str(payload.get("status", ""))
    if document_type.endswith("capabilities"):
        return 0
    if document_type.endswith("error"):
        return 2
    if document_type.endswith("review"):
        return 0
    if document_type.endswith("candidate"):
        return 0 if status == "candidate_prepared" else 4
    if document_type.endswith("verification"):
        return 0 if status == "formal_passed" else 4
    if document_type.endswith(("measurement", "report")):
        return 0 if status == "completed" else 4
    return 2
