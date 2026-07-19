from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.rtl_input import (
    DesignInputV2,
    SourceFileV2,
    lint_with_pyslang,
)
from rtl_advisor.tools import ToolExecutionError, run_command


CANDIDATE_FLOW_VERSION = "rtl-advisor-isolated-candidate-v2"


class CandidateV2Error(RuntimeError):
    """Raised when candidate emission cannot be performed safely."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _design_hash(design: DesignInputV2) -> str:
    core = {
        "schema_version": design.schema_version,
        "top": design.top,
        "files": [
            {"path": source.path, "sha256": source.sha256}
            for source in design.files
        ],
        "include_dirs": list(design.include_dirs),
        "defines": list(design.defines),
        "filelists": list(design.filelists),
    }
    return _json_hash(core)


def _source_integrity(design: DesignInputV2) -> dict[str, Any]:
    mismatches: list[dict[str, str | None]] = []
    for source in design.files:
        path = Path(source.path)
        actual = _sha256(path) if path.is_file() else None
        if actual != source.sha256:
            mismatches.append(
                {
                    "path": source.path,
                    "expected_sha256": source.sha256,
                    "actual_sha256": actual,
                }
            )
    return {"ok": not mismatches, "mismatches": mismatches}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateV2Error(f"invalid candidate artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CandidateV2Error(f"candidate artifact must be an object: {path}")
    return payload


def _yosys_quote(value: str | Path) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _design_from_artifact(path: Path) -> DesignInputV2:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DesignInputV2(
            schema_version=int(raw["schema_version"]),
            top=str(raw["top"]),
            files=tuple(
                SourceFileV2(path=str(item["path"]), sha256=str(item["sha256"]))
                for item in raw["files"]
            ),
            include_dirs=tuple(str(item) for item in raw["include_dirs"]),
            defines=tuple(str(item) for item in raw["defines"]),
            filelists=tuple(str(item) for item in raw["filelists"]),
            design_hash=str(raw["design_hash"]),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CandidateV2Error(f"invalid design input artifact {path}: {exc}") from exc


def _source_location(candidate: dict[str, Any]) -> Path | None:
    source = candidate.get("source") or {}
    for location in source.get("locations") or []:
        raw = location.get("file")
        if raw:
            return Path(str(raw)).expanduser().resolve()
    return None


def _generated_sibling_candidate(
    design: DesignInputV2,
    candidate: dict[str, Any],
) -> tuple[Path, str] | None:
    selected_source = _source_location(candidate)
    template_id = str(candidate.get("template_id", ""))
    if selected_source is None or selected_source.name != "v0.sv":
        return None
    if template_id not in {"v1", "v2", "v3"}:
        return None
    sibling = selected_source.with_name(f"{template_id}.sv")
    if not sibling.is_file() or "_v0_" not in design.top:
        return None
    source = sibling.read_text(encoding="utf-8")
    candidate_top = design.top.replace("_v0_", f"_{template_id}_")
    candidate_kernel = candidate_top.removesuffix("_top") + "_kernel"
    baseline_kernel = design.top.removesuffix("_top") + "_kernel"
    source = re.sub(rf"\b{re.escape(candidate_top)}\b", design.top, source)
    source = re.sub(
        rf"\b{re.escape(candidate_kernel)}\b", baseline_kernel, source
    )
    return selected_source, source


def _copy_design(
    design: DesignInputV2,
    destination: Path,
    replacement: tuple[Path, str],
) -> DesignInputV2:
    common_root = Path(
        os.path.commonpath([str(Path(source.path).parent) for source in design.files])
    )
    replacement_path, replacement_text = replacement
    copied: list[SourceFileV2] = []
    for source in design.files:
        original = Path(source.path).resolve()
        try:
            relative = original.relative_to(common_root)
        except ValueError:
            relative = Path(source.sha256[:12]) / original.name
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if original == replacement_path:
            target.write_text(replacement_text, encoding="utf-8")
        else:
            shutil.copy2(original, target)
        copied.append(SourceFileV2(path=str(target), sha256=_sha256(target)))
    copied_design = DesignInputV2(
        schema_version=design.schema_version,
        top=design.top,
        files=tuple(copied),
        include_dirs=design.include_dirs,
        defines=design.defines,
        filelists=design.filelists,
        design_hash="",
    )
    return DesignInputV2(
        schema_version=copied_design.schema_version,
        top=copied_design.top,
        files=copied_design.files,
        include_dirs=copied_design.include_dirs,
        defines=copied_design.defines,
        filelists=copied_design.filelists,
        design_hash=_design_hash(copied_design),
    )


def _write_candidate_diff(
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    path: Path,
) -> None:
    chunks: list[str] = []
    for original, rewritten in zip(baseline.files, candidate.files, strict=True):
        original_path = Path(original.path)
        rewritten_path = Path(rewritten.path)
        chunks.extend(
            difflib.unified_diff(
                original_path.read_text(encoding="utf-8").splitlines(keepends=True),
                rewritten_path.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile=original.path,
                tofile=rewritten.path,
            )
        )
    path.write_text("".join(chunks), encoding="utf-8")


def _read_command(design: DesignInputV2) -> str:
    parts = ["read_verilog", "-sv"]
    parts.extend(f"-I{_yosys_quote(path)}" for path in design.include_dirs)
    parts.extend(f"-D{definition}" for definition in design.defines)
    parts.extend(_yosys_quote(source.path) for source in design.files)
    return " ".join(parts)


def _prove_equivalence(
    config: ProjectConfig,
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    output_dir: Path,
) -> dict[str, Any]:
    script_path = output_dir / "equivalence.ys"
    log_path = output_dir / "equivalence.log"
    script = "\n".join(
        (
            _read_command(baseline),
            f"prep -top {baseline.top}",
            "design -stash baseline_design",
            "design -reset",
            _read_command(candidate),
            f"prep -top {candidate.top}",
            "design -stash candidate_design",
            "design -reset",
            f"design -copy-from baseline_design -as gold {baseline.top}",
            f"design -copy-from candidate_design -as gate {candidate.top}",
            "equiv_make gold gate equiv",
            "hierarchy -top equiv",
            "equiv_simple",
            "equiv_induct -seq 8",
            "equiv_status -assert",
            "",
        )
    )
    script_path.write_text(script, encoding="utf-8")
    command = (config.tools.yosys, "-Q", "-s", str(script_path))
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
        passed = completed.returncode == 0
        detail = None if passed else combined or f"Yosys exited {completed.returncode}"
        return {
            "status": "passed" if passed else "failed",
            "returncode": completed.returncode,
            "command": list(command),
            "script_path": str(script_path),
            "log_path": str(log_path),
            "detail": detail,
            "baseline_design_hash": baseline.design_hash,
            "candidate_design_hash": candidate.design_hash,
            "baseline_source_hashes": {
                source.path: source.sha256 for source in baseline.files
            },
            "candidate_source_hashes": {
                source.path: source.sha256 for source in candidate.files
            },
        }
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "status": "error",
            "returncode": None,
            "command": list(command),
            "script_path": str(script_path),
            "log_path": str(log_path),
            "detail": str(exc),
            "baseline_design_hash": baseline.design_hash,
            "candidate_design_hash": candidate.design_hash,
            "baseline_source_hashes": {
                source.path: source.sha256 for source in baseline.files
            },
            "candidate_source_hashes": {
                source.path: source.sha256 for source in candidate.files
            },
        }


def _verilator_lint(
    config: ProjectConfig,
    design: DesignInputV2,
    output_dir: Path,
) -> dict[str, Any]:
    log_path = output_dir / "verilator.log"
    command = (
        config.tools.verilator,
        "--lint-only",
        "--language",
        "1800-2017",
        "--Wall",
        "--Wno-fatal",
        "--Wno-DECLFILENAME",
        "--top-module",
        design.top,
        *(f"-I{path}" for path in design.include_dirs),
        *(f"-D{definition}" for definition in design.defines),
        *(source.path for source in design.files),
    )
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "command": list(command),
            "log_path": str(log_path),
            "detail": None if completed.returncode == 0 else combined,
        }
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        return {
            "status": "error",
            "returncode": None,
            "command": list(command),
            "log_path": str(log_path),
            "detail": str(exc),
        }


def emit_selected_candidate(
    config: ProjectConfig,
    analysis: dict[str, Any],
    analysis_path: Path,
    *,
    candidate_source: str = "templates",
    verify_formal: bool = True,
) -> dict[str, Any]:
    selected_id = analysis.get("selected_candidate_id")
    selected = next(
        (
            candidate
            for candidate in analysis.get("candidates") or []
            if candidate.get("candidate_id") == selected_id
        ),
        None,
    )
    if selected is None:
        return {
            "status": "rejected",
            "flow_version": CANDIDATE_FLOW_VERSION,
            "reason": "no conservatively selected candidate",
        }
    design = _design_from_artifact(analysis_path.parent / "input.json")
    replacement = _generated_sibling_candidate(design, selected)
    if replacement is None:
        reason = "no unambiguous deterministic template matched the source"
        if candidate_source == "templates+codex":
            reason += "; Codex fallback requires a registered source rewriter"
        return {
            "status": "rejected",
            "flow_version": CANDIDATE_FLOW_VERSION,
            "candidate_id": selected_id,
            "reason": reason,
        }
    candidate_root = (
        analysis_path.parent / "candidates" / str(selected_id)
    )
    source_root = candidate_root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    candidate_design = _copy_design(design, source_root, replacement)
    candidate_input_path = candidate_root / "input.json"
    _write_json(candidate_input_path, candidate_design.to_dict())
    diff_path = candidate_root / "candidate.diff"
    _write_candidate_diff(design, candidate_design, diff_path)
    slang = lint_with_pyslang(candidate_design)
    _write_json(candidate_root / "slang.json", slang.to_dict())
    verilator = _verilator_lint(config, candidate_design, candidate_root)
    _write_json(candidate_root / "verilator.json", verilator)
    formal = (
        _prove_equivalence(config, design, candidate_design, candidate_root)
        if verify_formal and slang.ok and verilator["status"] == "passed"
        else {
            "status": "not_run",
            "detail": (
                "candidate lint did not pass"
                if not slang.ok or verilator["status"] != "passed"
                else "formal verification was not requested"
            ),
        }
    )
    formal["proof_semantic_hash"] = _json_hash(formal)
    _write_json(candidate_root / "formal.json", formal)
    lint_passed = slang.ok and verilator["status"] == "passed"
    accepted = lint_passed and formal["status"] == "passed"
    status = (
        "accepted"
        if accepted
        else "prepared"
        if lint_passed and not verify_formal
        else "rejected"
    )
    result = {
        "status": status,
        "flow_version": CANDIDATE_FLOW_VERSION,
        "candidate_id": selected_id,
        "template_id": selected.get("template_id"),
        "transformation_id": selected.get("transformation_id"),
        "source": candidate_source,
        "artifact_root": str(candidate_root),
        "baseline_input_path": str(analysis_path.parent / "input.json"),
        "candidate_input_path": str(candidate_input_path),
        "candidate_files": [as_source.path for as_source in candidate_design.files],
        "candidate_design_hash": candidate_design.design_hash,
        "diff_path": str(diff_path),
        "diff_sha256": _sha256(diff_path),
        "lint": {
            "slang": slang.to_dict(),
            "verilator": verilator,
        },
        "formal": formal,
        "original_source_hashes": {
            source.path: source.sha256 for source in design.files
        },
        "candidate_source_hashes": {
            source.path: source.sha256 for source in candidate_design.files
        },
        "source_integrity": {
            "original": _source_integrity(design),
            "candidate": _source_integrity(candidate_design),
        },
    }
    _write_json(candidate_root / "summary.json", result)
    return result


def verify_emitted_candidate(
    config: ProjectConfig,
    analysis_path: Path,
    candidate_id: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", candidate_id):
        raise CandidateV2Error(f"invalid candidate ID: {candidate_id!r}")
    analysis_path = analysis_path.expanduser().resolve()
    candidate_root = (analysis_path.parent / "candidates" / candidate_id).resolve()
    expected_root = (analysis_path.parent / "candidates").resolve()
    if not candidate_root.is_relative_to(expected_root):
        raise CandidateV2Error("candidate path escapes the analysis workspace")
    summary_path = candidate_root / "summary.json"
    summary = _read_json(summary_path)
    if summary.get("candidate_id") != candidate_id:
        raise CandidateV2Error("candidate summary ID mismatch")

    baseline = _design_from_artifact(analysis_path.parent / "input.json")
    candidate = _design_from_artifact(candidate_root / "input.json")
    baseline_integrity = _source_integrity(baseline)
    candidate_integrity = _source_integrity(candidate)
    integrity_ok = baseline_integrity["ok"] and candidate_integrity["ok"]

    if integrity_ok:
        slang = lint_with_pyslang(candidate)
        _write_json(candidate_root / "slang.json", slang.to_dict())
        verilator = _verilator_lint(config, candidate, candidate_root)
        _write_json(candidate_root / "verilator.json", verilator)
    else:
        slang = None
        verilator = {
            "status": "not_run",
            "detail": "source integrity check failed",
        }

    lint_passed = (
        slang is not None
        and slang.ok
        and verilator.get("status") == "passed"
    )
    formal = (
        _prove_equivalence(config, baseline, candidate, candidate_root)
        if lint_passed
        else {
            "status": "not_run",
            "detail": (
                "source integrity check failed"
                if not integrity_ok
                else "candidate lint did not pass"
            ),
            "baseline_design_hash": baseline.design_hash,
            "candidate_design_hash": candidate.design_hash,
        }
    )
    formal["proof_semantic_hash"] = _json_hash(formal)
    _write_json(candidate_root / "formal.json", formal)

    accepted = integrity_ok and lint_passed and formal["status"] == "passed"
    result = {
        **summary,
        "status": "accepted" if accepted else "rejected",
        "lint": {
            "slang": (
                slang.to_dict()
                if slang is not None
                else {"status": "not_run", "ok": False}
            ),
            "verilator": verilator,
        },
        "formal": formal,
        "source_integrity": {
            "original": baseline_integrity,
            "candidate": candidate_integrity,
        },
        "safe": accepted,
    }
    _write_json(summary_path, result)
    return result
