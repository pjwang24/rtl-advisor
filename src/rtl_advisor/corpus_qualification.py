from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import shlex
from typing import Any, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_registry import (
    CorpusRegistryError,
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
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command
from rtl_advisor.tranche_lock import load_tranche_lock, verify_tranche_sources


QUALIFICATION_PLAN_SCHEMA_ID = "rtl-advisor-qualification-plan-v1"
QUALIFICATION_PLAN_DOCUMENT_TYPE = "rtl-advisor.qualification-plan"
QUALIFICATION_RESULT_DOCUMENT_TYPE = "rtl-advisor.qualification-result"
QUALIFICATION_SCHEMA_VERSION = 1


class CorpusQualificationError(RuntimeError):
    """Raised when a frozen corpus qualification run cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "corpus_qualification_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


def _relative_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusQualificationError(f"{context} must be a non-empty path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ""}:
        raise CorpusQualificationError(f"{context} must stay within its upstream root")
    return path.as_posix()


def load_qualification_plan(
    path: str | Path,
    *,
    tranche_lock: Mapping[str, Any],
) -> dict[str, Any]:
    plan_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusQualificationError(f"invalid qualification plan {plan_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CorpusQualificationError("qualification plan must be a JSON object")
    expected_hash = raw.get("semantic_hash")
    core = {key: value for key, value in raw.items() if key != "semantic_hash"}
    if not isinstance(expected_hash, str) or stable_hash(core) != expected_hash:
        raise CorpusQualificationError(
            "qualification plan semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    if (
        raw.get("schema_version") != QUALIFICATION_SCHEMA_VERSION
        or raw.get("schema") != QUALIFICATION_PLAN_SCHEMA_ID
        or raw.get("document_type") != QUALIFICATION_PLAN_DOCUMENT_TYPE
    ):
        raise CorpusQualificationError(
            "unsupported qualification plan schema",
            code="unsupported_schema",
        )
    if (
        raw.get("tranche_id") != tranche_lock.get("tranche_id")
        or raw.get("tranche_semantic_hash") != tranche_lock.get("semantic_hash")
    ):
        raise CorpusQualificationError(
            "qualification plan does not match the frozen tranche",
            code="stale_qualification_plan",
        )
    tool_contract = raw.get("tool_contract")
    if not isinstance(tool_contract, dict):
        raise CorpusQualificationError("qualification plan lacks a tool contract")
    if tool_contract.get("candidate_synthesis_enabled") is not False:
        raise CorpusQualificationError(
            "qualification plan must keep candidate synthesis disabled",
            code="ppa_visible_before_freeze",
        )
    upstream_roots = raw.get("upstream_roots")
    if not isinstance(upstream_roots, dict):
        raise CorpusQualificationError("qualification plan lacks upstream roots")
    for upstream_id, root in upstream_roots.items():
        if not isinstance(upstream_id, str):
            raise CorpusQualificationError("upstream root IDs must be strings")
        _relative_path(root, f"upstream_roots.{upstream_id}")

    tranche_references = tranche_lock.get("references")
    plan_references = raw.get("references")
    if not isinstance(tranche_references, list) or not isinstance(plan_references, list):
        raise CorpusQualificationError("tranche and plan references must be arrays")
    expected_ids = [item.get("reference_id") for item in tranche_references]
    actual_ids = [item.get("reference_id") for item in plan_references if isinstance(item, dict)]
    if actual_ids != expected_ids:
        raise CorpusQualificationError(
            "qualification plan must preserve the frozen reference order",
            code="tranche_membership_changed",
        )
    for index, item in enumerate(plan_references):
        if not isinstance(item, dict):
            raise CorpusQualificationError(f"references[{index}] must be an object")
        if not isinstance(item.get("top"), str) or not item["top"]:
            raise CorpusQualificationError(f"references[{index}] lacks a top module")
        sources = item.get("sources")
        include_dirs = item.get("include_dirs")
        defines = item.get("defines")
        parameters = item.get("parameters")
        if not isinstance(sources, list) or not sources:
            raise CorpusQualificationError(f"references[{index}] lacks sources")
        if not isinstance(include_dirs, list) or not isinstance(defines, list):
            raise CorpusQualificationError(f"references[{index}] has invalid compile options")
        if not isinstance(parameters, dict):
            raise CorpusQualificationError(f"references[{index}] has invalid parameters")
        for source_index, source in enumerate(sources):
            _relative_path(source, f"references[{index}].sources[{source_index}]")
        for include_index, include_dir in enumerate(include_dirs):
            _relative_path(
                include_dir,
                f"references[{index}].include_dirs[{include_index}]",
            )
    return raw


def _tree_hash(path: Path) -> str:
    entries: list[dict[str, str]] = []
    for entry in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        if entry.is_symlink():
            raise CorpusQualificationError(
                f"include tree contains a symbolic link: {entry}",
                code="unsupported_compile_context",
            )
        if entry.is_file():
            entries.append(
                {
                    "path": entry.relative_to(path).as_posix(),
                    "sha256": file_sha256(entry),
                }
            )
    return stable_hash(entries)


def _source_pinned_manifest(
    current: ReferenceManifestV1,
    plan_reference: Mapping[str, Any],
    upstream_root: Path,
    plan_hash: str,
) -> ReferenceManifestV1:
    payload = current.to_dict()
    sources = [str(source) for source in plan_reference["sources"]]
    include_dirs = [str(path) for path in plan_reference["include_dirs"]]
    source_hashes = [
        {"path": source, "sha256": file_sha256(upstream_root / source)}
        for source in sources
    ]
    include_hashes = [
        {"path": path, "sha256": _tree_hash(upstream_root / path)}
        for path in include_dirs
    ]
    context_core = {
        "plan_semantic_hash": plan_hash,
        "top": plan_reference["top"],
        "sources": source_hashes,
        "include_tree_hashes": include_hashes,
        "defines": list(plan_reference["defines"]),
        "parameters": dict(plan_reference["parameters"]),
        "clocks": payload["compile_context"]["clocks"],
        "resets": payload["compile_context"]["resets"],
    }
    payload["qualification"] = {
        "state": "source_pinned",
        "status": "active",
        "reason": None,
    }
    payload["compile_context"].update(
        {
            "top": plan_reference["top"],
            "sources": sources,
            "filelist": None,
            "include_dirs": include_dirs,
            "defines": list(plan_reference["defines"]),
            "parameters": dict(plan_reference["parameters"]),
            "generated_inputs": [],
            "frontend": None,
            "frontend_version": None,
            "build_commands": [],
            "lint_commands": [],
            "test_commands": [],
        }
    )
    payload["compile_context_hashes"] = {
        "filelist_sha256": None,
        "include_tree_hashes": include_hashes,
        "generated_input_hashes": [],
        "compile_context_hash": stable_hash(context_core),
    }
    payload["source_hashes"] = source_hashes
    return parse_reference_manifest(payload)


def _terminal_manifest(
    current: ReferenceManifestV1,
    *,
    reason: str,
) -> ReferenceManifestV1:
    payload = current.to_dict()
    payload["qualification"] = {
        "state": current.qualification.state,
        "status": "blocked",
        "reason": reason,
    }
    return parse_reference_manifest(payload)


def _resume_manifest(current: ReferenceManifestV1) -> ReferenceManifestV1:
    payload = current.to_dict()
    payload["qualification"] = {
        "state": current.qualification.state,
        "status": "active",
        "reason": None,
    }
    return parse_reference_manifest(payload)


def _build_reproduced_manifest(
    current: ReferenceManifestV1,
    *,
    frontend_version: str,
    build_command: tuple[str, ...],
    lint_command: tuple[str, ...],
) -> ReferenceManifestV1:
    payload = current.to_dict()
    payload["qualification"] = {
        "state": "build_reproduced",
        "status": "active",
        "reason": None,
    }
    payload["compile_context"].update(
        {
            "frontend": "verilator+yosys-slang",
            "frontend_version": frontend_version,
            "build_commands": [shlex.join(build_command)],
            "lint_commands": [shlex.join(lint_command)],
        }
    )
    return parse_reference_manifest(payload)


def _yosys_quote(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise CorpusQualificationError("Yosys argument contains a control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _commands(
    config: ProjectConfig,
    plan: Mapping[str, Any],
    plan_reference: Mapping[str, Any],
    upstream_root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    def workspace_path(path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(config.root).as_posix()
        except ValueError as exc:
            raise CorpusQualificationError(
                f"compile input is outside the project workspace: {resolved}",
                code="unsafe_path",
            ) from exc

    sources = tuple(
        workspace_path(upstream_root / source)
        for source in plan_reference["sources"]
    )
    include_dirs = tuple(
        workspace_path(upstream_root / include_dir)
        for include_dir in plan_reference["include_dirs"]
    )
    defines = tuple(str(value) for value in plan_reference["defines"])
    parameters = tuple(sorted(plan_reference["parameters"].items()))
    top = str(plan_reference["top"])
    lint_command = (
        config.tools.verilator,
        "--lint-only",
        "--language",
        "1800-2017",
        "--Wall",
        "--Wno-fatal",
        *(f"-D{value}" for value in defines),
        *(f"-I{path}" for path in include_dirs),
        *(f"-G{name}={value}" for name, value in parameters),
        "--top-module",
        top,
        *sources,
    )
    read_slang = ["read_slang", "--top", top]
    read_slang.extend(f"-G {name}={value}" for name, value in parameters)
    read_slang.extend(f"-D {value}" for value in defines)
    read_slang.extend(f"-I {path}" for path in include_dirs)
    read_slang.extend(sources)
    script = (
        " ".join(read_slang)
        + f"; hierarchy -check -top {top}; proc; check"
    )
    plugin = str(plan["tool_contract"]["yosys_slang_plugin"])
    build_command = (config.tools.yosys, "-m", plugin, "-Q", "-p", script)
    return lint_command, build_command


def _run_and_log(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: int,
    log_path: Path,
) -> dict[str, Any]:
    try:
        result = run_command(command, timeout_seconds=timeout_seconds, cwd=cwd)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        log_path.write_text(output + ("\n" if output else ""), encoding="utf-8")
        return {
            "status": "passed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "command": list(command),
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
            "detail": None if result.returncode == 0 else output[-4000:],
        }
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "status": "error",
            "returncode": None,
            "command": list(command),
            "log_path": str(log_path),
            "log_sha256": file_sha256(log_path),
            "detail": str(exc),
        }


def qualify_tranche(
    config: ProjectConfig,
    *,
    tranche_lock_path: str | Path,
    qualification_plan_path: str | Path,
    registry: CorpusRegistryV1,
) -> dict[str, Any]:
    lock = load_tranche_lock(tranche_lock_path)
    plan = load_qualification_plan(qualification_plan_path, tranche_lock=lock)
    source_roots = {
        upstream_id: (config.root / str(root)).resolve()
        for upstream_id, root in plan["upstream_roots"].items()
    }
    integrity = verify_tranche_sources(lock, source_roots)
    if integrity["status"] != "passed":
        raise CorpusQualificationError(
            "frozen source integrity verification failed",
            code="source_integrity_failed",
        )

    artifact_root = (
        config.artifacts_dir
        / "corpus-qualification"
        / str(plan["plan_id"])
        / str(plan["semantic_hash"])
    )
    summary_path = artifact_root / "summary.json"
    if summary_path.is_file():
        return read_hashed_json(
            summary_path,
            document_type=QUALIFICATION_RESULT_DOCUMENT_TYPE,
            schema_version=QUALIFICATION_SCHEMA_VERSION,
        )
    artifact_root.mkdir(parents=True, exist_ok=True)

    current_references = {item.reference_id: item for item in registry.references()}
    plan_upstream = {
        item["reference_id"]: next(
            reference["upstream_project_id"]
            for reference in lock["references"]
            if reference["reference_id"] == item["reference_id"]
        )
        for item in plan["references"]
    }
    source_pin_transitions: list[dict[str, Any]] = []
    for plan_reference in plan["references"]:
        reference_id = str(plan_reference["reference_id"])
        current = current_references.get(reference_id)
        if current is None:
            raise CorpusQualificationError(
                f"tranche reference is not registered: {reference_id}",
                code="missing_corpus_record",
            )
        if current.qualification.status == "blocked":
            if (
                current.qualification.state == "source_pinned"
                and plan["tool_contract"].get("resume_blocked_after_tool_fix") is True
            ):
                successor = _resume_manifest(current)
                source_pin_transitions.append(registry.advance_manifest(successor))
                current_references[reference_id] = successor
                current = successor
            else:
                raise CorpusQualificationError(
                    f"reference {reference_id!r} is blocked and this plan does not "
                    "record an approved tool-fix retry",
                    code="qualification_state_mismatch",
                )
        if current.qualification.status != "active":
            raise CorpusQualificationError(
                f"reference {reference_id!r} is not active",
                code="qualification_state_mismatch",
            )
        if current.qualification.state == "license_reviewed":
            upstream_root = source_roots[plan_upstream[reference_id]]
            successor = _source_pinned_manifest(
                current,
                plan_reference,
                upstream_root,
                str(plan["semantic_hash"]),
            )
            source_pin_transitions.append(registry.advance_manifest(successor))
            current_references[reference_id] = successor
        elif current.qualification.state != "source_pinned":
            raise CorpusQualificationError(
                f"reference {reference_id!r} is already beyond source pinning; "
                "remove stale qualification artifacts or use the stored result",
                code="qualification_state_mismatch",
            )

    version_results = (
        run_command((config.tools.verilator, "--version"), timeout_seconds=30, cwd=config.root),
        run_command((config.tools.yosys, "-V"), timeout_seconds=30, cwd=config.root),
    )
    tool_versions = [first_output_line(result) or "unknown" for result in version_results]
    frontend_version = "; ".join(tool_versions)
    plugin_path = Path(str(plan["tool_contract"]["yosys_slang_plugin"]))
    if not plugin_path.is_file():
        raise CorpusQualificationError(
            f"yosys-slang plugin is unavailable: {plugin_path}",
            code="missing_tool",
        )

    results: list[dict[str, Any]] = []
    final_transitions: list[dict[str, Any]] = []
    for plan_reference in plan["references"]:
        reference_id = str(plan_reference["reference_id"])
        current = current_references[reference_id]
        reference_dir = artifact_root / reference_id
        reference_dir.mkdir(parents=True, exist_ok=True)
        upstream_root = source_roots[plan_upstream[reference_id]]
        lint_command, build_command = _commands(
            config,
            plan,
            plan_reference,
            upstream_root,
        )
        lint = _run_and_log(
            lint_command,
            cwd=config.root,
            timeout_seconds=config.tools.timeout_seconds,
            log_path=reference_dir / "lint.log",
        )
        build = _run_and_log(
            build_command,
            cwd=config.root,
            timeout_seconds=config.tools.timeout_seconds,
            log_path=reference_dir / "build.log",
        )
        passed = lint["status"] == "passed" and build["status"] == "passed"
        if passed:
            successor = _build_reproduced_manifest(
                current,
                frontend_version=frontend_version,
                build_command=build_command,
                lint_command=lint_command,
            )
        else:
            failed_stages = [
                name
                for name, evidence in (("lint", lint), ("build", build))
                if evidence["status"] != "passed"
            ]
            successor = _terminal_manifest(
                current,
                reason="Compile-context reproduction failed at: " + ", ".join(failed_stages),
            )
        transition = registry.advance_manifest(successor)
        final_transitions.append(transition)
        current_references[reference_id] = successor
        results.append(
            {
                "reference_id": reference_id,
                "status": "build_reproduced" if passed else "blocked",
                "compile_context_hash": current.compile_context_hashes.compile_context_hash,
                "lint": lint,
                "build": build,
                "registry_transition": transition,
            }
        )

    failed = [result for result in results if result["status"] != "build_reproduced"]
    core = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "schema": QUALIFICATION_PLAN_SCHEMA_ID,
        "document_type": QUALIFICATION_RESULT_DOCUMENT_TYPE,
        "plan_id": plan["plan_id"],
        "plan_semantic_hash": plan["semantic_hash"],
        "tranche_id": lock["tranche_id"],
        "tranche_semantic_hash": lock["semantic_hash"],
        "status": "passed" if not failed else "completed_with_blockers",
        "reference_count": len(results),
        "build_reproduced_count": len(results) - len(failed),
        "blocked_count": len(failed),
        "source_integrity": {
            "status": integrity["status"],
            "verified_file_count": integrity["verified_file_count"],
        },
        "tools": {
            "versions": tool_versions,
            "yosys_slang_plugin": str(plugin_path),
            "yosys_slang_plugin_sha256": file_sha256(plugin_path),
        },
        "source_pin_transitions": source_pin_transitions,
        "final_transitions": final_transitions,
        "results": results,
        "registry_root": str(registry.root),
    }
    return write_hashed_json(summary_path, core, exclusive=True)
