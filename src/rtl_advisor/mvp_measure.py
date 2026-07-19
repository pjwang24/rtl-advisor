from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_schema import (
    MVPSchemaError,
    compile_context_snapshot,
    compile_contexts_compatible,
    source_integrity,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.rtl_input import DesignInputV2
from rtl_advisor.synthesis import (
    SynthesisError,
    _parse_abc_metrics,
    _parse_stat_metrics,
    _yosys_version,
)
from rtl_advisor.tools import (
    ToolExecutionError,
    first_output_line,
    run_command,
    sha256_file,
)


MEASUREMENT_SCHEMA_VERSION = 1
MEASUREMENT_DOCUMENT_TYPE = "rtl-advisor.measurement"
MEASUREMENT_FLOW_VERSION = "rtl-advisor-mvp-yosys-abc-v1"
REQUIRED_YOSYS_VERSION = "0.63"
REQUIRED_ABC_VERSION = "1.01"
SYNTHESIS_PROFILES = ("standard", "stronger")
OBJECTIVES = ("timing", "area", "balanced")


class MVPMeasurementError(RuntimeError):
    """Raised when synthesis evidence cannot be produced or trusted."""

    def __init__(self, message: str, *, code: str = "measurement_failed") -> None:
        super().__init__(message)
        self.code = code


def _metric(metrics: Mapping[str, Any] | Any, *names: str) -> float:
    for name in names:
        if isinstance(metrics, Mapping) and name in metrics:
            raw = metrics[name]
        elif hasattr(metrics, name):
            raw = getattr(metrics, name)
        else:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise MVPMeasurementError(
                f"synthesis metric {name!r} must be numeric"
            ) from exc
        if not math.isfinite(value):
            raise MVPMeasurementError(
                f"synthesis metric {name!r} must be finite"
            )
        return value
    raise MVPMeasurementError(
        f"missing synthesis metric; expected one of {', '.join(names)}"
    )


def _improvement_percent(baseline: float, candidate: float, name: str) -> float:
    if baseline <= 0.0:
        raise MVPMeasurementError(f"baseline {name} must be positive")
    if candidate < 0.0:
        raise MVPMeasurementError(f"candidate {name} must not be negative")
    return (baseline - candidate) / baseline * 100.0


def _at_least(value: float, boundary: float) -> bool:
    return value > boundary or math.isclose(value, boundary, abs_tol=1e-9)


def _at_most(value: float, boundary: float) -> bool:
    return value < boundary or math.isclose(value, boundary, abs_tol=1e-9)


def _strictly_less(value: float, boundary: float) -> bool:
    return value < boundary and not math.isclose(value, boundary, abs_tol=1e-9)


def _recipe_directions(
    baseline_metrics: Mapping[str, Any] | Any,
    candidate_metrics: Mapping[str, Any] | Any,
) -> tuple[float, float]:
    baseline_delay = _metric(
        baseline_metrics, "critical_delay_ps", "delay_ps", "delay"
    )
    candidate_delay = _metric(
        candidate_metrics, "critical_delay_ps", "delay_ps", "delay"
    )
    baseline_area = _metric(baseline_metrics, "area_total", "area")
    candidate_area = _metric(candidate_metrics, "area_total", "area")
    return (
        _improvement_percent(baseline_delay, candidate_delay, "delay"),
        _improvement_percent(baseline_area, candidate_area, "area"),
    )


def classify_recipe(
    objective: str,
    baseline_metrics: Mapping[str, Any] | Any,
    candidate_metrics: Mapping[str, Any] | Any,
) -> str:
    """Classify one baseline/candidate synthesis comparison.

    Positive percentages mean that the candidate is smaller or faster. Boundary
    behavior follows the frozen MVP rules exactly: timing uses a 3% improvement
    threshold and 10% area guardrail; area uses a 5% improvement threshold and
    2% delay guardrail.
    """

    if objective not in OBJECTIVES:
        raise MVPMeasurementError(f"unsupported objective: {objective!r}")
    delay, area = _recipe_directions(baseline_metrics, candidate_metrics)
    timing_improved = _at_least(delay, 3.0) and _at_least(area, -10.0)
    area_improved = _at_least(area, 5.0) and _at_least(delay, -2.0)

    if objective == "timing":
        if timing_improved:
            return "improved"
        if _at_most(delay, -3.0) or _strictly_less(area, -10.0):
            return "regressed"
        return "neutral"

    if objective == "area":
        if area_improved:
            return "improved"
        if _at_most(area, -5.0) or _strictly_less(delay, -2.0):
            return "regressed"
        return "neutral"

    if timing_improved or area_improved:
        return "improved"
    if _strictly_less(area, -10.0) or _strictly_less(delay, -2.0):
        return "regressed"
    return "neutral"


def aggregate_measurements(standard_class: str, stronger_class: str) -> str:
    """Combine the two fixed synthesis-recipe classifications."""

    allowed = {"improved", "neutral", "regressed"}
    invalid = [
        value for value in (standard_class, stronger_class) if value not in allowed
    ]
    if invalid:
        raise MVPMeasurementError(
            f"unsupported recipe classification: {invalid[0]!r}"
        )
    if standard_class == stronger_class == "improved":
        return "measured_improvement"
    if standard_class == stronger_class == "neutral":
        return "synthesis_handles"
    if "regressed" in {standard_class, stronger_class}:
        return "regression"
    return "flow_dependent"


def _yosys_quote(value: str | Path) -> str:
    raw = str(value)
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise MVPMeasurementError(
            "Yosys arguments may not contain control characters",
            code="unsafe_compile_context",
        )
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_command(design: DesignInputV2) -> str:
    parts = ["read_verilog", "-sv"]
    parts.extend(f"-I{_yosys_quote(path)}" for path in design.include_dirs)
    parts.extend(f"-D{_yosys_quote(definition)}" for definition in design.defines)
    parts.extend(_yosys_quote(source.path) for source in design.files)
    return " ".join(parts)


def _recipe_definition(
    profile: str,
    *,
    top: str,
    config: ProjectConfig,
    yosys_version: str,
    yosys_sha256: str,
    abc_version: str,
    abc_sha256: str,
    liberty_sha256: str,
) -> dict[str, Any]:
    if profile == "standard":
        passes = [
            "hierarchy -check",
            "synth -flatten -noabc",
            "dfflibmap",
            "abc -exe -liberty -constr",
            "clean",
            "check -assert",
        ]
        optimization_template = [f"synth -top {top} -flatten -noabc"]
    elif profile == "stronger":
        passes = [
            "hierarchy -check",
            "synth -flatten -noabc -run begin:fine",
            "share -aggressive",
            "opt -full",
            "clean",
            "synth -flatten -noabc -run fine:check",
            "dfflibmap",
            "abc -exe -liberty -constr",
            "clean",
            "check -assert",
        ]
        optimization_template = [
            f"synth -top {top} -flatten -noabc -run begin:fine",
            "share -aggressive",
            "opt -full",
            "clean",
            f"synth -top {top} -flatten -noabc -run fine:check",
        ]
    else:
        raise MVPMeasurementError(f"unknown synthesis profile: {profile!r}")
    script_template = [
        "read_liberty -lib <LIBERTY>",
        "read_verilog -sv <INCLUDE_DIRS> <DEFINES> <SOURCES>",
        f"hierarchy -check -top {top}",
        *optimization_template,
        "dfflibmap -liberty <LIBERTY>",
        "abc -exe <ABC> -liberty <LIBERTY> -constr <CONSTRAINTS>",
        "clean",
        "check -assert",
        f"stat -top {top} -liberty <LIBERTY> -json",
        "write_verilog -noattr -noexpr <NETLIST>",
    ]
    core = {
        "flow_version": MEASUREMENT_FLOW_VERSION,
        "profile": profile,
        "top": top,
        "frontend": "read_verilog -sv",
        "passes": passes,
        "script_template": script_template,
        "script_template_sha256": stable_hash(script_template),
        "yosys_version": yosys_version,
        "yosys_sha256": yosys_sha256,
        "abc_provider": "explicit adjacent yosys-abc",
        "abc_version": abc_version,
        "abc_sha256": abc_sha256,
        "liberty_sha256": liberty_sha256,
        "driving_cell": config.synthesis.driving_cell,
        "output_load_ff": config.synthesis.output_load_ff,
    }
    return {**core, "recipe_hash": stable_hash(core)}


def _synthesis_script(
    design: DesignInputV2,
    *,
    profile: str,
    liberty: Path,
    abc_executable: Path,
    constraints: Path,
    stat_json: Path,
    netlist: Path,
) -> str:
    prefix = [
        f"read_liberty -lib {_yosys_quote(liberty)}",
        _read_command(design),
        f"hierarchy -check -top {design.top}",
    ]
    if profile == "standard":
        optimization = [f"synth -top {design.top} -flatten -noabc"]
    elif profile == "stronger":
        optimization = [
            f"synth -top {design.top} -flatten -noabc -run begin:fine",
            "share -aggressive",
            "opt -full",
            "clean",
            f"synth -top {design.top} -flatten -noabc -run fine:check",
        ]
    else:
        raise MVPMeasurementError(f"unknown synthesis profile: {profile!r}")
    suffix = [
        f"dfflibmap -liberty {_yosys_quote(liberty)}",
        (
            f"abc -exe {_yosys_quote(abc_executable)} "
            f"-liberty {_yosys_quote(liberty)} "
            f"-constr {_yosys_quote(constraints)}"
        ),
        "clean",
        "check -assert",
        (
            f"tee -o {_yosys_quote(stat_json)} stat -top {design.top} "
            f"-liberty {_yosys_quote(liberty)} -json"
        ),
        f"write_verilog -noattr -noexpr {_yosys_quote(netlist)}",
        "",
    ]
    return "\n".join((*prefix, *optimization, *suffix))


def _design_core(design: DesignInputV2) -> dict[str, Any]:
    return {
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


def _validate_design(design: DesignInputV2, role: str) -> dict[str, Any]:
    actual_design_hash = stable_hash(_design_core(design))
    if design.design_hash != actual_design_hash:
        raise MVPMeasurementError(
            f"{role} design hash is invalid",
            code="stale_design_hash",
        )
    integrity = source_integrity(_design_core(design)["files"])
    if not integrity["ok"]:
        raise MVPMeasurementError(
            f"{role} source hashes changed before synthesis",
            code="stale_source_hashes",
        )
    try:
        return compile_context_snapshot(design)
    except MVPSchemaError as exc:
        raise MVPMeasurementError(str(exc), code=exc.code) from exc


def _validate_compile_context(
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    baseline_context: Mapping[str, Any],
    candidate_context: Mapping[str, Any],
) -> None:
    try:
        compatible = compile_contexts_compatible(
            baseline_context, candidate_context
        )
    except MVPSchemaError as exc:
        raise MVPMeasurementError(str(exc), code=exc.code) from exc
    if not compatible:
        raise MVPMeasurementError(
            "baseline and candidate logical compile contexts differ",
            code="compile_context_mismatch",
        )


def _validate_hashed_payload(payload: Mapping[str, Any], field: str) -> None:
    expected = payload.get(field)
    if expected is None:
        return
    core = {key: value for key, value in payload.items() if key != field}
    if expected != stable_hash(core):
        raise MVPMeasurementError(
            f"verification {field.replace('_', ' ')} mismatch",
            code="stale_formal_proof",
        )


def _require_formal(
    verification: Mapping[str, Any],
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    baseline_context: Mapping[str, Any],
    candidate_context: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    _validate_hashed_payload(verification, "semantic_hash")
    if verification.get("status") not in {"passed", "formal_passed"}:
        raise MVPMeasurementError(
            "a passing formal verification record is required",
            code="formal_prerequisite_missing",
        )
    if "safe" in verification and verification.get("safe") is not True:
        raise MVPMeasurementError(
            "formal verification record is not marked safe",
            code="formal_prerequisite_missing",
        )
    formal = verification.get("formal")
    if not isinstance(formal, Mapping) or formal.get("status") != "passed":
        raise MVPMeasurementError(
            "formal proof did not pass",
            code="formal_prerequisite_missing",
        )
    _validate_hashed_payload(formal, "proof_semantic_hash")
    baseline_record = verification.get("baseline")
    candidate_record = verification.get("candidate")
    baseline_hash = formal.get("baseline_design_hash") or verification.get(
        "baseline_design_hash"
    )
    if baseline_hash is None and isinstance(baseline_record, Mapping):
        baseline_hash = baseline_record.get("design_hash") or baseline_record.get(
            "baseline_design_hash"
        )
    candidate_hash = formal.get("candidate_design_hash") or verification.get(
        "candidate_design_hash"
    )
    if candidate_hash is None and isinstance(candidate_record, Mapping):
        candidate_hash = candidate_record.get(
            "candidate_design_hash"
        ) or candidate_record.get("design_hash")
    if baseline_hash != baseline.design_hash:
        raise MVPMeasurementError(
            "formal proof has a stale baseline design hash",
            code="stale_formal_proof",
        )
    if candidate_hash != candidate.design_hash:
        raise MVPMeasurementError(
            "formal proof has a stale candidate design hash",
            code="stale_formal_proof",
        )
    formal_context = verification.get("compile_context")
    if not isinstance(formal_context, Mapping):
        raise MVPMeasurementError(
            "formal proof has no compile-context snapshots",
            code="stale_formal_proof",
        )
    if formal_context.get("baseline") != dict(baseline_context):
        raise MVPMeasurementError(
            "formal proof has a stale baseline compile context",
            code="stale_formal_proof",
        )
    if formal_context.get("candidate") != dict(candidate_context):
        raise MVPMeasurementError(
            "formal proof has a stale candidate compile context",
            code="stale_formal_proof",
        )
    proof_hash = str(
        formal.get("proof_semantic_hash")
        or verification.get("semantic_hash")
        or stable_hash(formal)
    )
    return formal, proof_hash


def _resolved_yosys_path(config: ProjectConfig) -> Path:
    configured_yosys = Path(config.tools.yosys).expanduser()
    if configured_yosys.is_absolute() or "/" in config.tools.yosys:
        resolved_yosys = configured_yosys.resolve()
    else:
        discovered = shutil.which(config.tools.yosys)
        resolved_yosys = Path(discovered).resolve() if discovered else configured_yosys
    if not resolved_yosys.is_file():
        raise MVPMeasurementError(
            "Yosys executable could not be content-hashed",
            code="yosys_unavailable",
        )
    return resolved_yosys


def _yosys_identity(config: ProjectConfig) -> dict[str, Any]:
    """Return the exact Yosys identity accepted by formal and synthesis."""

    try:
        yosys_version = _yosys_version(config)
    except SynthesisError as exc:
        raise MVPMeasurementError(str(exc), code="yosys_unavailable") from exc
    version_token = yosys_version.removeprefix("Yosys ").split()[0]
    # Official YosysHQ daily bundles identify a build from the pinned 0.63
    # release line as ``0.63+N``. The complete version string and executable
    # digest below still make that binary identity exact and hash-stable.
    if not (
        version_token == REQUIRED_YOSYS_VERSION
        or version_token.startswith(f"{REQUIRED_YOSYS_VERSION}+")
    ):
        raise MVPMeasurementError(
            f"MVP requires the Yosys {REQUIRED_YOSYS_VERSION} release line; found {yosys_version}",
            code="unsupported_yosys_version",
        )
    resolved_yosys = _resolved_yosys_path(config)
    return {
        "yosys_version": yosys_version,
        "yosys_path": str(resolved_yosys),
        "yosys_sha256": sha256_file(resolved_yosys),
    }


def _abc_identity(config: ProjectConfig, yosys_path: str | Path) -> dict[str, Any]:
    """Pin the adjacent ``yosys-abc`` binary used by the Yosys ``abc`` pass."""

    candidate = Path(yosys_path).resolve().with_name("yosys-abc")
    try:
        resolved_abc = candidate.resolve(strict=True)
    except OSError as exc:
        raise MVPMeasurementError(
            f"adjacent yosys-abc executable is unavailable: {candidate}",
            code="abc_unavailable",
        ) from exc
    if not resolved_abc.is_file() or resolved_abc.parent != Path(yosys_path).resolve().parent:
        raise MVPMeasurementError(
            f"yosys-abc must be a regular executable adjacent to Yosys: {candidate}",
            code="abc_unavailable",
        )
    try:
        result = run_command(
            (str(resolved_abc), "-q", "version"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        raise MVPMeasurementError(str(exc), code="abc_unavailable") from exc
    version = first_output_line(result)
    if result.returncode != 0 or version is None:
        raise MVPMeasurementError(
            result.stderr or result.stdout or "yosys-abc version probe failed",
            code="abc_unavailable",
        )
    match = re.search(r"\bABC\s+([0-9]+(?:\.[0-9]+)+)\b", version)
    version_token = match.group(1) if match else ""
    if version_token != REQUIRED_ABC_VERSION:
        raise MVPMeasurementError(
            f"MVP requires ABC {REQUIRED_ABC_VERSION}; found {version}",
            code="unsupported_abc_version",
        )
    return {
        "abc_version": version,
        "abc_version_token": version_token,
        "abc_path": str(resolved_abc),
        "abc_sha256": sha256_file(resolved_abc),
    }


def _toolchain_identity(config: ProjectConfig) -> dict[str, Any]:
    yosys = _yosys_identity(config)
    return {**yosys, **_abc_identity(config, str(yosys["yosys_path"]))}


def _environment(config: ProjectConfig) -> dict[str, Any]:
    liberty = config.liberty.path.expanduser().resolve()
    if not liberty.is_file():
        raise MVPMeasurementError(
            "Liberty file is missing; run rtl-advisor setup first",
            code="missing_liberty",
        )
    liberty_sha256 = sha256_file(liberty)
    if liberty_sha256 != config.liberty.sha256:
        raise MVPMeasurementError(
            "configured Liberty checksum mismatch",
            code="liberty_hash_mismatch",
        )
    toolchain = _toolchain_identity(config)
    return {
        **toolchain,
        "liberty_path": str(liberty),
        "liberty_name": config.liberty.name,
        "liberty_sha256": liberty_sha256,
        "liberty_source_commit": config.liberty.source_commit,
    }


def _actual_environment_observation(config: ProjectConfig) -> dict[str, Any]:
    liberty = config.liberty.path.expanduser().resolve()
    try:
        toolchain = _toolchain_identity(config)
    except MVPMeasurementError as exc:
        try:
            yosys = _resolved_yosys_path(config)
        except MVPMeasurementError:
            yosys = Path(config.tools.yosys).expanduser()
        adjacent_abc = yosys.with_name("yosys-abc")
        toolchain = {
            "yosys_path": str(yosys),
            "yosys_sha256": sha256_file(yosys) if yosys.is_file() else None,
            "yosys_version": None,
            "abc_path": str(adjacent_abc),
            "abc_sha256": sha256_file(adjacent_abc) if adjacent_abc.is_file() else None,
            "abc_version": None,
        }
        toolchain_error = {"code": exc.code, "detail": str(exc)}
    else:
        toolchain_error = None
    return {
        **toolchain,
        "toolchain_error": toolchain_error,
        "liberty_path": str(liberty),
        "liberty_sha256": sha256_file(liberty) if liberty.is_file() else None,
        "expected_liberty_sha256": config.liberty.sha256,
    }


def _actual_design_observation(design: DesignInputV2) -> dict[str, Any]:
    integrity = source_integrity(_design_core(design)["files"])
    try:
        context = compile_context_snapshot(design)
    except MVPSchemaError as exc:
        context = None
        error = {"code": exc.code, "detail": str(exc)}
    else:
        error = None
    return {
        "design_hash": design.design_hash,
        "source_integrity": integrity,
        "compile_context": context,
        "compile_context_error": error,
    }


def _abc_provenance(
    log: str, environment: Mapping[str, Any]
) -> dict[str, Any]:
    lines = [line.strip() for line in log.splitlines() if line.startswith("ABC:")]
    command_line = next(
        (
            line.removeprefix("ABC:").strip()
            for line in lines
            if "command line" in line.lower()
        ),
        None,
    )
    return {
        "provider": "Yosys abc pass with explicit -exe",
        "executable": environment["abc_path"],
        "version": environment["abc_version"],
        "sha256": environment["abc_sha256"],
        "command_line": command_line,
        "transcript_sha256": stable_hash(lines),
    }


def _run_synthesis(
    config: ProjectConfig,
    design: DesignInputV2,
    *,
    profile: str,
    role: str,
    profile_root: Path,
    environment: Mapping[str, Any],
    recipe: Mapping[str, Any],
) -> dict[str, Any]:
    output_dir = profile_root / role
    output_dir.mkdir(parents=True, exist_ok=True)
    constraints_path = profile_root / "abc.constr"
    stat_path = output_dir / "stat.json"
    netlist_path = output_dir / "mapped.v"
    script_path = output_dir / "synthesis.ys"
    log_path = output_dir / "synthesis.log"
    script = _synthesis_script(
        design,
        profile=profile,
        liberty=Path(str(environment["liberty_path"])),
        abc_executable=Path(str(environment["abc_path"])),
        constraints=constraints_path,
        stat_json=stat_path,
        netlist=netlist_path,
    )
    script_path.write_text(script, encoding="utf-8")
    command = (config.tools.yosys, "-Q", "-s", str(script_path))
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        raise MVPMeasurementError(str(exc), code="synthesis_tool_error") from exc
    combined = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    )
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    if completed.returncode != 0:
        raise MVPMeasurementError(
            f"{profile} synthesis failed for {role}; see {log_path}",
            code="synthesis_failed",
        )
    try:
        abc_gates, abc_area, critical_delay_ps = _parse_abc_metrics(combined)
        area_total, area_sequential, cells, raw_cells, cells_by_type = (
            _parse_stat_metrics(stat_path, design.top)
        )
    except SynthesisError as exc:
        raise MVPMeasurementError(str(exc), code="invalid_synthesis_metrics") from exc
    if not netlist_path.is_file():
        raise MVPMeasurementError(
            f"{profile} synthesis did not produce a mapped netlist",
            code="missing_synthesis_artifact",
        )
    metrics = {
        "critical_delay_ps": critical_delay_ps,
        "area_total": area_total,
        "area_combinational": round(area_total - area_sequential, 6),
        "area_sequential": area_sequential,
        "abc_area_combinational": abc_area,
        "cell_count": cells,
        "raw_cell_count": raw_cells,
        "abc_gate_count": abc_gates,
        "cells_by_type": dict(sorted(cells_by_type.items())),
    }
    cell_signature = stable_hash(
        {
            "cells_by_type": metrics["cells_by_type"],
            "cell_count": cells,
            "abc_gate_count": abc_gates,
        }
    )
    warning_lines = [
        line.strip() for line in combined.splitlines() if "warning" in line.lower()
    ]
    return {
        "status": "passed",
        "role": role,
        "design_hash": design.design_hash,
        "top": design.top,
        "source_hashes": {
            source.path: source.sha256 for source in design.files
        },
        "metrics": metrics,
        "netlist": {
            "path": str(netlist_path),
            "sha256": sha256_file(netlist_path),
            "cell_signature": cell_signature,
        },
        "constraints": {
            "driving_cell": config.synthesis.driving_cell,
            "output_load_ff": config.synthesis.output_load_ff,
            "sha256": sha256_file(constraints_path),
        },
        "provenance": {
            "flow_version": MEASUREMENT_FLOW_VERSION,
            "profile": profile,
            "recipe_hash": recipe["recipe_hash"],
            "yosys_version": environment["yosys_version"],
            "yosys_path": environment.get("yosys_path"),
            "yosys_sha256": environment.get("yosys_sha256"),
            "abc": _abc_provenance(combined, environment),
            "abc_version": environment["abc_version"],
            "abc_path": environment["abc_path"],
            "abc_sha256": environment["abc_sha256"],
            "liberty_name": environment["liberty_name"],
            "liberty_path": environment["liberty_path"],
            "liberty_sha256": environment["liberty_sha256"],
            "liberty_source_commit": environment["liberty_source_commit"],
            "command": list(command),
            "script_path": str(script_path),
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "constraints_path": str(constraints_path),
            "log_path": str(log_path),
            "log_sha256": sha256_file(log_path),
            "warnings": {
                "count": len(warning_lines),
                "sha256": stable_hash(warning_lines),
            },
            "stat_path": str(stat_path),
            "stat_sha256": sha256_file(stat_path),
        },
    }


def _comparison(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for output_name, aliases in (
        ("critical_delay_ps", ("critical_delay_ps", "delay_ps", "delay")),
        ("area_total", ("area_total", "area")),
        ("cell_count", ("cell_count", "cells")),
    ):
        baseline_value = _metric(baseline, *aliases)
        candidate_value = _metric(candidate, *aliases)
        improvement = _improvement_percent(
            baseline_value, candidate_value, output_name
        )
        output[output_name] = {
            "baseline": round(baseline_value, 6),
            "candidate": round(candidate_value, 6),
            "delta": round(candidate_value - baseline_value, 6),
            "improvement_percent": round(improvement, 6),
        }
    return output


def _artifact_observation(result: Mapping[str, Any]) -> dict[str, Any]:
    netlist = result.get("netlist")
    provenance = result.get("provenance")
    constraints = result.get("constraints")
    if not all(isinstance(item, Mapping) for item in (netlist, provenance, constraints)):
        return {"ok": False, "mismatches": ["missing artifact provenance"]}
    assert isinstance(netlist, Mapping)
    assert isinstance(provenance, Mapping)
    assert isinstance(constraints, Mapping)
    specifications = (
        ("netlist", netlist.get("path"), netlist.get("sha256")),
        ("constraints", provenance.get("constraints_path"), constraints.get("sha256")),
        ("script", provenance.get("script_path"), provenance.get("script_sha256")),
        ("log", provenance.get("log_path"), provenance.get("log_sha256")),
        ("stat", provenance.get("stat_path"), provenance.get("stat_sha256")),
    )
    actual: dict[str, Any] = {}
    mismatches: list[str] = []
    for label, raw_path, expected in specifications:
        path = Path(str(raw_path or ""))
        digest = sha256_file(path) if path.is_file() else None
        actual[label] = {
            "path": str(path),
            "expected_sha256": expected,
            "actual_sha256": digest,
        }
        if not isinstance(expected, str) or digest != expected:
            mismatches.append(label)
    log_path = Path(str(provenance.get("log_path") or ""))
    warnings = provenance.get("warnings")
    if isinstance(warnings, Mapping) and log_path.is_file():
        warning_lines = [
            line.strip()
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if "warning" in line.lower()
        ]
        actual_warning_hash = stable_hash(warning_lines)
        actual["warnings"] = {
            "expected_sha256": warnings.get("sha256"),
            "actual_sha256": actual_warning_hash,
            "count": len(warning_lines),
        }
        if warnings.get("sha256") != actual_warning_hash:
            mismatches.append("warnings")
    return {"ok": not mismatches, "mismatches": mismatches, "actual": actual}


def _persist_flow_invalidated(
    root: Path,
    *,
    reason: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> Path:
    payload = {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "document_type": "rtl-advisor.flow-invalidated",
        "flow_version": MEASUREMENT_FLOW_VERSION,
        "status": "flow_invalidated",
        "reason": reason,
        "before": dict(before),
        "after": dict(after),
    }
    identifier = stable_hash(payload)[:16]
    path = root / f"flow-invalidated-{identifier}.json"
    if not path.is_file():
        write_hashed_json(path, payload)
    return path


def _raise_flow_invalidated(
    root: Path,
    *,
    reason: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    path = _persist_flow_invalidated(root, reason=reason, before=before, after=after)
    raise MVPMeasurementError(
        f"measurement flow was invalidated: {reason}; see {path}",
        code="flow_invalidated",
    )


def measure_candidate(
    config: ProjectConfig,
    baseline_design: DesignInputV2,
    candidate_design: DesignInputV2,
    verification_record: Mapping[str, Any],
    artifact_root: str | Path,
    *,
    objective: str = "balanced",
) -> dict[str, Any]:
    """Measure a formally proven candidate with the two fixed MVP recipes.

    ``verification_record`` must have a ``passed`` or ``formal_passed`` status,
    ``safe: true`` when the field is present, and a nested passing ``formal``
    record. The baseline and candidate design hashes are required either in that
    formal record, at verification-record top level, or in nested ``baseline``
    and ``candidate`` records. All hashes are checked against the current design
    inputs before either synthesis recipe runs.
    """

    baseline_context = _validate_design(baseline_design, "baseline")
    candidate_context = _validate_design(candidate_design, "candidate")
    _validate_compile_context(
        baseline_design,
        candidate_design,
        baseline_context,
        candidate_context,
    )
    formal, proof_hash = _require_formal(
        verification_record,
        baseline_design,
        candidate_design,
        baseline_context,
        candidate_context,
    )
    if objective not in OBJECTIVES:
        raise MVPMeasurementError(f"unsupported objective: {objective!r}")
    environment = _environment(config)
    measurement_core = {
        "flow_version": MEASUREMENT_FLOW_VERSION,
        "baseline_design_hash": baseline_design.design_hash,
        "candidate_design_hash": candidate_design.design_hash,
        "baseline_compile_context_hash": baseline_context["compile_context_hash"],
        "candidate_compile_context_hash": candidate_context["compile_context_hash"],
        "formal_proof_hash": proof_hash,
        "objective": objective,
        "yosys_version": environment["yosys_version"],
        "yosys_sha256": environment.get("yosys_sha256"),
        "abc_version": environment["abc_version"],
        "abc_sha256": environment["abc_sha256"],
        "liberty_sha256": environment["liberty_sha256"],
        "driving_cell": config.synthesis.driving_cell,
        "output_load_ff": config.synthesis.output_load_ff,
    }
    measurement_id = stable_hash(measurement_core)[:24]
    root = (
        Path(artifact_root).expanduser().resolve()
        / "measurements"
        / measurement_id
    )
    result_path = root / "measurement.json"
    if result_path.is_file():
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MVPMeasurementError(
                f"invalid cached measurement {result_path}: {exc}"
            ) from exc
        semantic_hash = cached.get("semantic_hash")
        cached_core = {
            key: value for key, value in cached.items() if key != "semantic_hash"
        }
        if semantic_hash != stable_hash(cached_core):
            raise MVPMeasurementError(
                "cached measurement semantic hash mismatch",
                code="artifact_hash_mismatch",
            )
        artifact_observations = {
            f"{profile}:{role}": _artifact_observation(
                cached["profiles"][profile][role]
            )
            for profile in SYNTHESIS_PROFILES
            for role in ("baseline", "candidate")
        }
        if any(not item["ok"] for item in artifact_observations.values()):
            _raise_flow_invalidated(
                root,
                reason="cached synthesis artifacts changed",
                before={"environment": environment},
                after={"artifacts": artifact_observations},
            )
        return cached

    root.mkdir(parents=True, exist_ok=True)
    constraints_text = (
        f"set_driving_cell {config.synthesis.driving_cell}\n"
        f"set_load {config.synthesis.output_load_ff}\n"
    )
    profiles: dict[str, Any] = {}
    for profile in SYNTHESIS_PROFILES:
        profile_root = root / profile
        profile_root.mkdir(parents=True, exist_ok=True)
        constraints_path = profile_root / "abc.constr"
        constraints_path.write_text(constraints_text, encoding="utf-8")
        recipe = _recipe_definition(
            profile,
            top=baseline_design.top,
            config=config,
            yosys_version=str(environment["yosys_version"]),
            yosys_sha256=str(environment["yosys_sha256"]),
            abc_version=str(environment["abc_version"]),
            abc_sha256=str(environment["abc_sha256"]),
            liberty_sha256=str(environment["liberty_sha256"]),
        )
        baseline_result = _run_synthesis(
            config,
            baseline_design,
            profile=profile,
            role="baseline",
            profile_root=profile_root,
            environment=environment,
            recipe=recipe,
        )
        candidate_result = _run_synthesis(
            config,
            candidate_design,
            profile=profile,
            role="candidate",
            profile_root=profile_root,
            environment=environment,
            recipe=recipe,
        )
        if baseline_result["constraints"] != candidate_result["constraints"]:
            raise MVPMeasurementError(
                f"{profile} baseline/candidate constraints differ",
                code="recipe_parity_failed",
            )
        if (
            baseline_result["provenance"]["recipe_hash"]
            != candidate_result["provenance"]["recipe_hash"]
            or baseline_result["provenance"]["recipe_hash"]
            != recipe["recipe_hash"]
        ):
            raise MVPMeasurementError(
                f"{profile} baseline/candidate recipes differ",
                code="recipe_parity_failed",
            )
        for role, result, expected_design in (
            ("baseline", baseline_result, baseline_design),
            ("candidate", candidate_result, candidate_design),
        ):
            provenance = result.get("provenance")
            if result.get("design_hash") != expected_design.design_hash or not isinstance(
                provenance, Mapping
            ):
                raise MVPMeasurementError(
                    f"{profile} {role} result is not bound to its design",
                    code="recipe_parity_failed",
                )
            if any(
                provenance.get(field) != environment.get(field)
                for field in (
                    "yosys_version",
                    "yosys_path",
                    "yosys_sha256",
                    "abc_version",
                    "abc_path",
                    "abc_sha256",
                    "liberty_sha256",
                )
            ):
                raise MVPMeasurementError(
                    f"{profile} {role} result has mismatched tool provenance",
                    code="recipe_parity_failed",
                )
        profiles[profile] = {
            "recipe": recipe,
            "baseline": baseline_result,
            "candidate": candidate_result,
        }

    try:
        final_baseline_context = _validate_design(baseline_design, "baseline")
        final_candidate_context = _validate_design(candidate_design, "candidate")
        final_environment = _environment(config)
    except MVPMeasurementError as exc:
        _raise_flow_invalidated(
            root,
            reason=str(exc),
            before={
                "baseline_compile_context": baseline_context,
                "candidate_compile_context": candidate_context,
                "environment": environment,
            },
            after={
                "validation_error": {"code": exc.code, "detail": str(exc)},
                "baseline": _actual_design_observation(baseline_design),
                "candidate": _actual_design_observation(candidate_design),
                "environment": _actual_environment_observation(config),
            },
        )
    artifact_observations = {
        f"{profile}:{role}": _artifact_observation(profiles[profile][role])
        for profile in SYNTHESIS_PROFILES
        for role in ("baseline", "candidate")
    }
    flow_changed = (
        final_baseline_context != baseline_context
        or final_candidate_context != candidate_context
        or final_environment != environment
        or any(not item["ok"] for item in artifact_observations.values())
    )
    if flow_changed:
        _raise_flow_invalidated(
            root,
            reason="inputs, tool identity, or synthesis artifacts changed during measurement",
            before={
                "baseline_compile_context": baseline_context,
                "candidate_compile_context": candidate_context,
                "environment": environment,
            },
            after={
                "baseline_compile_context": final_baseline_context,
                "candidate_compile_context": final_candidate_context,
                "environment": final_environment,
                "artifacts": artifact_observations,
            },
        )

    for profile in SYNTHESIS_PROFILES:
        baseline_result = profiles[profile]["baseline"]
        candidate_result = profiles[profile]["candidate"]
        profiles[profile]["classification"] = classify_recipe(
            objective,
            baseline_result["metrics"],
            candidate_result["metrics"],
        )
        profiles[profile]["comparison"] = _comparison(
            baseline_result["metrics"], candidate_result["metrics"]
        )
    try:
        decision_baseline_context = _validate_design(baseline_design, "baseline")
        decision_candidate_context = _validate_design(candidate_design, "candidate")
        decision_environment = _environment(config)
    except MVPMeasurementError as exc:
        _raise_flow_invalidated(
            root,
            reason=f"flow changed while classifying results: {exc}",
            before={
                "baseline_compile_context": baseline_context,
                "candidate_compile_context": candidate_context,
                "environment": environment,
            },
            after={
                "validation_error": {"code": exc.code, "detail": str(exc)},
                "baseline": _actual_design_observation(baseline_design),
                "candidate": _actual_design_observation(candidate_design),
                "environment": _actual_environment_observation(config),
            },
        )
    decision_artifacts = {
        f"{profile}:{role}": _artifact_observation(profiles[profile][role])
        for profile in SYNTHESIS_PROFILES
        for role in ("baseline", "candidate")
    }
    if (
        decision_baseline_context != baseline_context
        or decision_candidate_context != candidate_context
        or decision_environment != environment
        or any(not item["ok"] for item in decision_artifacts.values())
    ):
        _raise_flow_invalidated(
            root,
            reason="flow changed while classifying synthesis results",
            before={
                "baseline_compile_context": baseline_context,
                "candidate_compile_context": candidate_context,
                "environment": environment,
            },
            after={
                "baseline_compile_context": decision_baseline_context,
                "candidate_compile_context": decision_candidate_context,
                "environment": decision_environment,
                "artifacts": decision_artifacts,
            },
        )
    decision = aggregate_measurements(
        profiles["standard"]["classification"],
        profiles["stronger"]["classification"],
    )
    payload = {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "document_type": MEASUREMENT_DOCUMENT_TYPE,
        "flow_version": MEASUREMENT_FLOW_VERSION,
        "measurement_id": measurement_id,
        "status": decision,
        "decision": decision,
        "objective": objective,
        "baseline_design_hash": baseline_design.design_hash,
        "candidate_design_hash": candidate_design.design_hash,
        "compile_context": {
            "baseline": baseline_context,
            "candidate": candidate_context,
        },
        "source_integrity": {
            "baseline": source_integrity(_design_core(baseline_design)["files"]),
            "candidate": source_integrity(_design_core(candidate_design)["files"]),
        },
        "formal": {
            "status": formal["status"],
            "proof_semantic_hash": proof_hash,
            "baseline_design_hash": baseline_design.design_hash,
            "candidate_design_hash": candidate_design.design_hash,
        },
        "measurements": profiles,
        "profiles": profiles,
        "environment": environment,
        "thresholds": {
            "timing_improvement_percent": 3.0,
            "timing_area_guardrail_percent": -10.0,
            "area_improvement_percent": 5.0,
            "area_delay_guardrail_percent": -2.0,
        },
        "limitations": [
            "Results apply only to the pinned Yosys/ABC recipes and Liberty file.",
            "Delay is an ABC constrained combinational estimate, not routed timing.",
            "This evidence does not predict Genus, Design Compiler, or physical PPA.",
        ],
        "artifacts": {
            "root": str(root),
            "measurement": str(result_path),
        },
    }
    return write_hashed_json(result_path, payload)
