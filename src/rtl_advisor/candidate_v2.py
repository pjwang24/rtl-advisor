from __future__ import annotations

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
    return DesignInputV2(
        schema_version=design.schema_version,
        top=design.top,
        files=tuple(copied),
        include_dirs=design.include_dirs,
        defines=design.defines,
        filelists=design.filelists,
        design_hash=design.design_hash,
    )


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
    slang = lint_with_pyslang(candidate_design)
    _write_json(candidate_root / "slang.json", slang.to_dict())
    verilator = _verilator_lint(config, candidate_design, candidate_root)
    _write_json(candidate_root / "verilator.json", verilator)
    formal = (
        _prove_equivalence(config, design, candidate_design, candidate_root)
        if slang.ok and verilator["status"] == "passed"
        else {
            "status": "not_run",
            "detail": "candidate lint did not pass",
        }
    )
    _write_json(candidate_root / "formal.json", formal)
    accepted = (
        slang.ok
        and verilator["status"] == "passed"
        and formal["status"] == "passed"
    )
    result = {
        "status": "accepted" if accepted else "rejected",
        "flow_version": CANDIDATE_FLOW_VERSION,
        "candidate_id": selected_id,
        "template_id": selected.get("template_id"),
        "transformation_id": selected.get("transformation_id"),
        "source": candidate_source,
        "artifact_root": str(candidate_root),
        "candidate_files": [as_source.path for as_source in candidate_design.files],
        "lint": {
            "slang": slang.to_dict(),
            "verilator": verilator,
        },
        "formal": formal,
        "original_source_hashes": {
            source.path: source.sha256 for source in design.files
        },
    }
    _write_json(candidate_root / "summary.json", result)
    return result
