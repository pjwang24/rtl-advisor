from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_registry import (
    CorpusRegistryV1,
    ReferenceManifestV1,
    parse_reference_manifest,
)
from rtl_advisor.mvp_schema import (
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.sequential_equivalence import (
    SequentialEquivalenceError,
    _classify_sby_result,
    collect_p2_tool_identity,
)
from rtl_advisor.tools import ToolExecutionError, run_command
from rtl_advisor.tranche_lock import load_tranche_lock


BEHAVIOR_PLAN_SCHEMA_ID = "rtl-advisor-behavior-plan-v1"
BEHAVIOR_PLAN_DOCUMENT_TYPE = "rtl-advisor.behavior-plan"
BEHAVIOR_RESULT_DOCUMENT_TYPE = "rtl-advisor.behavior-result"
BEHAVIOR_SUMMARY_DOCUMENT_TYPE = "rtl-advisor.behavior-summary"
BEHAVIOR_SCHEMA_VERSION = 1
CHECK_TYPES = ("proof_artifact", "formal_task", "cocotb_make")


class CorpusBehaviorError(RuntimeError):
    """Raised when reference behavior evidence cannot be trusted."""

    def __init__(self, message: str, *, code: str = "corpus_behavior_failed") -> None:
        super().__init__(message)
        self.code = code


def _relative_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusBehaviorError(f"{context} must be a non-empty path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ""}:
        raise CorpusBehaviorError(
            f"{context} must stay within the project workspace",
            code="unsafe_path",
        )
    return path.as_posix()


def _validate_inputs(raw: Any, context: str) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise CorpusBehaviorError(f"{context} requires hashed inputs")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise CorpusBehaviorError(f"{context}[{index}] must contain path and sha256")
        path = _relative_path(item.get("path"), f"{context}[{index}].path")
        digest = item.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CorpusBehaviorError(f"{context}[{index}].sha256 is invalid")
        if path in seen:
            raise CorpusBehaviorError(f"duplicate behavior input: {path}")
        seen.add(path)
        result.append({"path": path, "sha256": digest})
    return result


def load_behavior_plan(
    path: str | Path,
    *,
    project_root: str | Path,
    tranche_lock: Mapping[str, Any],
) -> dict[str, Any]:
    plan_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusBehaviorError(f"invalid behavior plan {plan_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CorpusBehaviorError("behavior plan must be a JSON object")
    expected_hash = raw.get("semantic_hash")
    core = {key: value for key, value in raw.items() if key != "semantic_hash"}
    if not isinstance(expected_hash, str) or stable_hash(core) != expected_hash:
        raise CorpusBehaviorError(
            "behavior plan semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    if (
        raw.get("schema_version") != BEHAVIOR_SCHEMA_VERSION
        or raw.get("schema") != BEHAVIOR_PLAN_SCHEMA_ID
        or raw.get("document_type") != BEHAVIOR_PLAN_DOCUMENT_TYPE
    ):
        raise CorpusBehaviorError("unsupported behavior plan schema", code="unsupported_schema")
    if (
        raw.get("tranche_id") != tranche_lock.get("tranche_id")
        or raw.get("tranche_semantic_hash") != tranche_lock.get("semantic_hash")
    ):
        raise CorpusBehaviorError("behavior plan does not match the frozen tranche")
    baseline = raw.get("baseline_summary")
    if not isinstance(baseline, dict) or set(baseline) != {"path", "semantic_hash"}:
        raise CorpusBehaviorError("behavior plan requires a pinned baseline summary")
    _relative_path(baseline.get("path"), "baseline_summary.path")
    tools = raw.get("tool_contract")
    if not isinstance(tools, dict) or set(tools) != {
        "sby_version",
        "yosys_version",
        "iverilog_version",
        "cocotb_version",
    }:
        raise CorpusBehaviorError("behavior plan requires exact tool-version fragments")
    if any(not isinstance(value, str) or not value for value in tools.values()):
        raise CorpusBehaviorError("tool-version fragments must be non-empty strings")
    shared_inputs = _validate_inputs(raw.get("shared_inputs"), "shared_inputs")
    references = raw.get("references")
    locked = tranche_lock.get("references")
    if not isinstance(references, list) or not isinstance(locked, list):
        raise CorpusBehaviorError("behavior references must be an array")
    locked_ids = [item.get("reference_id") for item in locked]
    actual_ids = [item.get("reference_id") for item in references if isinstance(item, dict)]
    if actual_ids != locked_ids:
        raise CorpusBehaviorError(
            "behavior plan must preserve every frozen reference in order",
            code="tranche_membership_changed",
        )
    for index, item in enumerate(references):
        if not isinstance(item, dict):
            raise CorpusBehaviorError(f"references[{index}] must be an object")
        disposition = item.get("disposition")
        checks = item.get("checks")
        if disposition == "blocked":
            if not isinstance(item.get("reason"), str) or not item["reason"]:
                raise CorpusBehaviorError(f"references[{index}] blocked entry lacks a reason")
            if checks != []:
                raise CorpusBehaviorError(f"references[{index}] blocked entry cannot have checks")
            continue
        if disposition != "qualify" or not isinstance(checks, list) or not checks:
            raise CorpusBehaviorError(f"references[{index}] has an invalid disposition")
        if item.get("reason") is not None:
            raise CorpusBehaviorError(f"references[{index}] qualifying entry has a reason")
        for check_index, check in enumerate(checks):
            context = f"references[{index}].checks[{check_index}]"
            if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
                raise CorpusBehaviorError(f"{context} has an invalid check type")
            if check.get("basis_kind") not in {
                "upstream_tests",
                "assertions",
                "documented_pair",
            }:
                raise CorpusBehaviorError(f"{context} has an invalid basis kind")
            if not isinstance(check.get("description"), str) or not check["description"]:
                raise CorpusBehaviorError(f"{context} lacks a description")
            _validate_inputs(check.get("inputs"), f"{context}.inputs")
            if check["type"] == "proof_artifact":
                _relative_path(check.get("path"), f"{context}.path")
                if not isinstance(check.get("assertions"), dict) or not check["assertions"]:
                    raise CorpusBehaviorError(f"{context} requires result assertions")
            elif check["type"] == "formal_task":
                _relative_path(check.get("config"), f"{context}.config")
                if not isinstance(check.get("task"), str) or not check["task"]:
                    raise CorpusBehaviorError(f"{context} requires a formal task")
            else:
                _relative_path(check.get("cwd"), f"{context}.cwd")
                _relative_path(check.get("results_xml"), f"{context}.results_xml")
                variables = check.get("make_variables")
                if not isinstance(variables, list) or any(
                    not isinstance(value, str) or not value for value in variables
                ):
                    raise CorpusBehaviorError(f"{context} has invalid make variables")
    root = Path(project_root).expanduser().resolve()
    for input_item in shared_inputs:
        candidate = (root / input_item["path"]).resolve()
        if not candidate.is_file() or file_sha256(candidate) != input_item["sha256"]:
            raise CorpusBehaviorError(
                f"behavior input is unavailable or changed: {input_item['path']}",
                code="stale_behavior_input",
            )
    for item in references:
        for check in item["checks"]:
            for input_item in check["inputs"]:
                candidate = (root / input_item["path"]).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError as exc:
                    raise CorpusBehaviorError("behavior input escapes the workspace") from exc
                if not candidate.is_file() or file_sha256(candidate) != input_item["sha256"]:
                    raise CorpusBehaviorError(
                        f"behavior input is unavailable or changed: {input_item['path']}",
                        code="stale_behavior_input",
                    )
    return raw


def _command_identity(command: str, version_args: tuple[str, ...]) -> dict[str, str]:
    resolved = shutil.which(command)
    if resolved is None:
        raise CorpusBehaviorError(f"required behavior tool is unavailable: {command}", code="missing_tool")
    completed = run_command((command, *version_args), timeout_seconds=30)
    transcript = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        raise CorpusBehaviorError(f"could not identify behavior tool: {command}")
    return {
        "command": command,
        "path": str(Path(resolved).resolve()),
        "sha256": file_sha256(Path(resolved).resolve()),
        "version": next((line.strip() for line in transcript.splitlines() if line.strip()), ""),
    }


def _tool_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    try:
        formal = collect_p2_tool_identity("sby")
        iverilog = _command_identity("iverilog", ("-V",))
        cocotb = _command_identity("cocotb-config", ("--version",))
    except (SequentialEquivalenceError, ToolExecutionError) as exc:
        raise CorpusBehaviorError(str(exc), code=getattr(exc, "code", "missing_tool")) from exc
    actual = {
        "sby_version": formal["sby"]["version"],
        "yosys_version": formal["yosys"]["version"],
        "iverilog_version": iverilog["version"],
        "cocotb_version": cocotb["version"],
    }
    for name, expected in plan["tool_contract"].items():
        if expected not in actual[name]:
            raise CorpusBehaviorError(
                f"behavior tool mismatch for {name}: expected {expected!r}, got {actual[name]!r}",
                code="stale_behavior_tool",
            )
    identity = {"formal": formal, "iverilog": iverilog, "cocotb": cocotb}
    return {**identity, "identity_hash": stable_hash(identity)}


def _proof_artifact_check(config: ProjectConfig, check: Mapping[str, Any]) -> dict[str, Any]:
    path = (config.root / str(check["path"])).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusBehaviorError(f"invalid proof artifact {path}: {exc}") from exc
    if stable_hash({key: value for key, value in payload.items() if key != "semantic_hash"}) != payload.get(
        "semantic_hash"
    ):
        raise CorpusBehaviorError("proof artifact semantic hash mismatch", code="artifact_hash_mismatch")
    for field, expected in check["assertions"].items():
        if payload.get(field) != expected:
            raise CorpusBehaviorError(
                f"proof artifact {field} mismatch: expected {expected!r}, got {payload.get(field)!r}"
            )
    return {
        "type": "proof_artifact",
        "status": "passed",
        "source_path": str(check["path"]),
        "source_semantic_hash": payload["semantic_hash"],
    }


def _formal_task_check(
    config: ProjectConfig,
    check: Mapping[str, Any],
    output: Path,
) -> dict[str, Any]:
    formal_output = output / "formal"
    transcript_path = output / "transcript.log"
    command = (
        "sby",
        "-f",
        "-d",
        str(formal_output),
        str((config.root / str(check["config"])).resolve()),
        str(check["task"]),
    )
    try:
        completed = run_command(
            command,
            timeout_seconds=max(config.tools.timeout_seconds, 120),
            cwd=config.root,
        )
        transcript = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        returncode = completed.returncode
    except ToolExecutionError as exc:
        transcript = str(exc)
        returncode = -1
    output.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(transcript + ("\n" if transcript else ""), encoding="utf-8")
    relation = _classify_sby_result(returncode, formal_output, transcript)
    if relation != "equivalent":
        raise CorpusBehaviorError(
            f"reference property proof {check['task']} did not pass ({relation})",
            code="behavior_check_failed",
        )
    marker = formal_output / "PASS"
    return {
        "type": "formal_task",
        "status": "passed",
        "task": check["task"],
        "command": list(command),
        "transcript_sha256": file_sha256(transcript_path),
        "formal_marker_sha256": file_sha256(marker),
    }


def _cocotb_make_check(
    config: ProjectConfig,
    check: Mapping[str, Any],
    output: Path,
) -> dict[str, Any]:
    cwd = (config.root / str(check["cwd"])).resolve()
    output.mkdir(parents=True, exist_ok=True)
    transcripts: list[str] = []
    commands = [("make", "clean"), ("make", *tuple(check["make_variables"]))]
    for command in commands:
        try:
            completed = run_command(
                command,
                timeout_seconds=max(config.tools.timeout_seconds, 180),
                cwd=cwd,
            )
        except ToolExecutionError as exc:
            raise CorpusBehaviorError(str(exc), code="behavior_check_failed") from exc
        transcript = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        transcripts.append(transcript)
        if completed.returncode != 0:
            raise CorpusBehaviorError(
                f"upstream test command failed in {check['cwd']}: {' '.join(command)}",
                code="behavior_check_failed",
            )
    transcript_path = output / "transcript.log"
    transcript_path.write_text("\n".join(transcripts) + "\n", encoding="utf-8")
    results_path = cwd / str(check["results_xml"])
    try:
        root = ET.parse(results_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CorpusBehaviorError(f"invalid cocotb results {results_path}: {exc}") from exc
    cases = list(root.iter("testcase"))
    failures = sum(
        case.find("failure") is not None or case.find("error") is not None for case in cases
    )
    skipped = sum(case.find("skipped") is not None for case in cases)
    if not cases or failures or skipped:
        raise CorpusBehaviorError(
            f"upstream test result is incomplete: tests={len(cases)}, failures={failures}, skipped={skipped}",
            code="behavior_check_failed",
        )
    return {
        "type": "cocotb_make",
        "status": "passed",
        "commands": [list(command) for command in commands],
        "tests": len(cases),
        "failures": failures,
        "skipped": skipped,
        "results_xml_sha256": file_sha256(results_path),
        "transcript_sha256": file_sha256(transcript_path),
    }


def _behavior_manifest(
    current: ReferenceManifestV1,
    *,
    result_path: str,
    result_hash: str,
    checks: list[Mapping[str, Any]],
) -> ReferenceManifestV1:
    payload = current.to_dict()
    payload["qualification"] = {"state": "behavior_baselined", "status": "active", "reason": None}
    payload["behavioral_basis"] = [
        {
            "kind": check["basis_kind"],
            "location": result_path,
            "description": f"{check['description']} Evidence hash: {result_hash}.",
        }
        for check in checks
    ]
    return parse_reference_manifest(payload)


def _qualified_manifest(current: ReferenceManifestV1) -> ReferenceManifestV1:
    payload = current.to_dict()
    payload["qualification"] = {"state": "reference_qualified", "status": "active", "reason": None}
    return parse_reference_manifest(payload)


def _blocked_manifest(current: ReferenceManifestV1, reason: str) -> ReferenceManifestV1:
    payload = current.to_dict()
    payload["qualification"] = {
        "state": current.qualification.state,
        "status": "blocked",
        "reason": reason,
    }
    return parse_reference_manifest(payload)


def _record_registry(
    registry: CorpusRegistryV1,
    *,
    plan: Mapping[str, Any],
    results: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for plan_reference in plan["references"]:
        reference_id = str(plan_reference["reference_id"])
        current = {item.reference_id: item for item in registry.references()}[reference_id]
        if plan_reference["disposition"] == "blocked":
            if current.qualification.status == "active":
                events.append(registry.advance_manifest(_blocked_manifest(current, plan_reference["reason"])))
            continue
        if current.qualification.status != "active":
            raise CorpusBehaviorError(f"cannot qualify terminal reference {reference_id}")
        if current.qualification.state == "build_reproduced":
            result = results[reference_id]
            current = _behavior_manifest(
                current,
                result_path=str(result["record_path"]),
                result_hash=str(result["semantic_hash"]),
                checks=plan_reference["checks"],
            )
            events.append(registry.advance_manifest(current))
        if current.qualification.state == "behavior_baselined":
            current = _qualified_manifest(current)
            events.append(registry.advance_manifest(current))
        if current.qualification.state != "reference_qualified":
            raise CorpusBehaviorError(f"unexpected qualification state for {reference_id}")
    return events


def qualify_tranche_behavior(
    config: ProjectConfig,
    *,
    tranche_lock_path: str | Path,
    behavior_plan_path: str | Path,
    registry: CorpusRegistryV1,
    record_registry: bool = False,
) -> dict[str, Any]:
    lock = load_tranche_lock(tranche_lock_path)
    plan = load_behavior_plan(behavior_plan_path, project_root=config.root, tranche_lock=lock)
    baseline_path = (config.root / str(plan["baseline_summary"]["path"])).resolve()
    baseline = read_hashed_json(
        baseline_path,
        document_type="rtl-advisor.corpus-baseline-summary",
        schema_version=1,
    )
    if (
        baseline.get("semantic_hash") != plan["baseline_summary"]["semantic_hash"]
        or baseline.get("status") != "passed"
        or baseline.get("candidate_synthesis_enabled") is not False
    ):
        raise CorpusBehaviorError("baseline synthesis evidence is missing or stale")
    records = {item.reference_id: item for item in registry.references()}
    if set(records) != {item["reference_id"] for item in plan["references"]}:
        raise CorpusBehaviorError("registry and behavior plan reference sets differ")
    identity = _tool_identity(plan)
    run_core = {
        "tranche_id": lock["tranche_id"],
        "tranche_semantic_hash": lock["semantic_hash"],
        "behavior_plan_hash": plan["semantic_hash"],
        "baseline_semantic_hash": baseline["semantic_hash"],
        "tool_identity_hash": identity["identity_hash"],
    }
    run_hash = stable_hash(run_core)
    output_root = config.artifacts_dir / "corpus-behavior" / str(lock["tranche_id"]) / run_hash
    summary_path = output_root / "summary.json"
    if summary_path.is_file():
        cached = read_hashed_json(
            summary_path,
            document_type=BEHAVIOR_SUMMARY_DOCUMENT_TYPE,
            schema_version=BEHAVIOR_SCHEMA_VERSION,
        )
        result_map = {item["reference_id"]: item for item in cached["results"]}
        events = _record_registry(registry, plan=plan, results=result_map) if record_registry else []
        return {**cached, "cached": True, "registry_events": events}

    results: list[dict[str, Any]] = []
    for input_item in plan["shared_inputs"]:
        path = config.root / input_item["path"]
        if not path.is_file() or file_sha256(path) != input_item["sha256"]:
            raise CorpusBehaviorError(
                f"shared behavior input changed before execution: {input_item['path']}",
                code="stale_behavior_input",
            )
    for item in plan["references"]:
        reference_id = str(item["reference_id"])
        if item["disposition"] == "blocked":
            results.append(
                {
                    "reference_id": reference_id,
                    "status": "blocked",
                    "reason": item["reason"],
                    "checks": [],
                    "record_path": None,
                    "semantic_hash": None,
                }
            )
            continue
        check_results: list[dict[str, Any]] = []
        reference_root = output_root / reference_id
        for index, check in enumerate(item["checks"], start=1):
            for input_item in check["inputs"]:
                path = config.root / input_item["path"]
                if not path.is_file() or file_sha256(path) != input_item["sha256"]:
                    raise CorpusBehaviorError(
                        f"behavior input changed before execution: {input_item['path']}",
                        code="stale_behavior_input",
                    )
            output = reference_root / f"check-{index:02d}-{check['type']}"
            if check["type"] == "proof_artifact":
                result = _proof_artifact_check(config, check)
            elif check["type"] == "formal_task":
                result = _formal_task_check(config, check, output)
            else:
                result = _cocotb_make_check(config, check, output)
            check_results.append(
                {
                    **result,
                    "basis_kind": check["basis_kind"],
                    "description": check["description"],
                    "input_hash": stable_hash(check["inputs"]),
                }
            )
        record_path = reference_root / "result.json"
        record = write_hashed_json(
            record_path,
            {
                "schema_version": BEHAVIOR_SCHEMA_VERSION,
                "document_type": BEHAVIOR_RESULT_DOCUMENT_TYPE,
                "reference_id": reference_id,
                "run_hash": run_hash,
                "status": "passed",
                "source_hashes": dict(records[reference_id].source_hashes),
                "compile_context_hash": records[
                    reference_id
                ].compile_context_hashes.compile_context_hash,
                "checks": check_results,
            },
            exclusive=True,
        )
        results.append(
            {
                "reference_id": reference_id,
                "status": "passed",
                "reason": None,
                "checks": [check["type"] for check in check_results],
                "record_path": str(record_path.relative_to(config.root)),
                "semantic_hash": record["semantic_hash"],
            }
        )
    qualified = sum(item["status"] == "passed" for item in results)
    blocked = sum(item["status"] == "blocked" for item in results)
    summary = write_hashed_json(
        summary_path,
        {
            "schema_version": BEHAVIOR_SCHEMA_VERSION,
            "document_type": BEHAVIOR_SUMMARY_DOCUMENT_TYPE,
            "status": "passed" if qualified >= 8 else "failed",
            **run_core,
            "run_hash": run_hash,
            "tool_identity": identity,
            "reference_count": len(results),
            "qualified_count": qualified,
            "blocked_count": blocked,
            "minimum_qualified": 8,
            "results": results,
        },
        exclusive=True,
    )
    result_map = {item["reference_id"]: item for item in results}
    events = _record_registry(registry, plan=plan, results=result_map) if record_registry else []
    return {**summary, "cached": False, "registry_events": events}
