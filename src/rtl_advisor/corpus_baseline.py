from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_registry import CorpusRegistryV1
from rtl_advisor.mvp_measure import (
    MEASUREMENT_FLOW_VERSION,
    SYNTHESIS_PROFILES,
    _environment,
    _recipe_definition,
)
from rtl_advisor.mvp_schema import file_sha256, read_hashed_json, stable_hash, write_hashed_json
from rtl_advisor.synthesis import SynthesisError, _parse_abc_metrics, _parse_stat_metrics
from rtl_advisor.tools import ToolExecutionError, run_command
from rtl_advisor.tranche_lock import load_tranche_lock


BASELINE_SCHEMA_VERSION = 1
BASELINE_DOCUMENT_TYPE = "rtl-advisor.corpus-baseline"
BASELINE_SUMMARY_DOCUMENT_TYPE = "rtl-advisor.corpus-baseline-summary"


class CorpusBaselineError(RuntimeError):
    """Raised when frozen corpus baseline synthesis cannot be trusted."""

    def __init__(self, message: str, *, code: str = "corpus_baseline_failed") -> None:
        super().__init__(message)
        self.code = code


def _quote(value: str | Path) -> str:
    raw = str(value)
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise CorpusBaselineError("synthesis paths may not contain control characters")
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _slang_token(value: str | Path) -> str:
    raw = str(value)
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise CorpusBaselineError(
            "the pinned Slang CLI requires workspace-relative paths without whitespace",
            code="unsafe_compile_context",
        )
    return raw


def _load_qualification_plan(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path).expanduser().resolve()
    try:
        return read_hashed_json(
            plan_path,
            document_type="rtl-advisor.qualification-plan",
            schema_version=1,
        )
    except Exception as exc:
        raise CorpusBaselineError(f"invalid qualification plan {plan_path}: {exc}") from exc


def _parameter_argument(name: str, value: Any) -> str:
    if not isinstance(name, str) or not name:
        raise CorpusBaselineError("parameter names must be non-empty strings")
    if isinstance(value, bool):
        rendered = "1" if value else "0"
    elif isinstance(value, (int, float, str)):
        rendered = str(value)
    else:
        raise CorpusBaselineError(f"unsupported synthesis parameter {name!r}")
    return f"-G {name}={rendered}"


def _synthesis_script(
    *,
    top: str,
    sources: list[Path],
    include_dirs: list[Path],
    defines: list[str],
    parameters: Mapping[str, Any],
    profile: str,
    slang_plugin: Path,
    liberty: Path,
    abc: Path,
    constraints: Path,
    stat: Path,
    netlist: Path,
) -> str:
    read_parts = ["read_slang", f"--top {top}"]
    read_parts.extend(_parameter_argument(name, parameters[name]) for name in sorted(parameters))
    read_parts.extend(f"-D {definition}" for definition in defines)
    read_parts.extend(f"-I {_slang_token(path)}" for path in include_dirs)
    read_parts.extend(_slang_token(path) for path in sources)
    if profile == "standard":
        optimization = [f"synth -top {top} -flatten -noabc"]
    elif profile == "stronger":
        optimization = [
            f"synth -top {top} -flatten -noabc -run begin:fine",
            "share -aggressive",
            "opt -full",
            "clean",
            f"synth -top {top} -flatten -noabc -run fine:check",
        ]
    else:
        raise CorpusBaselineError(f"unknown synthesis profile: {profile}")
    return "\n".join(
        (
            f"plugin -i {_quote(slang_plugin)}",
            f"read_liberty -lib {_quote(liberty)}",
            " ".join(read_parts),
            f"hierarchy -check -top {top}",
            *optimization,
            f"dfflibmap -liberty {_quote(liberty)}",
            f"abc -exe {_quote(abc)} -liberty {_quote(liberty)} -constr {_quote(constraints)}",
            "clean",
            "check -assert",
            f"tee -o {_quote(stat)} stat -top {top} -liberty {_quote(liberty)} -json",
            f"write_verilog -noattr -noexpr {_quote(netlist)}",
            "",
        )
    )


def _run_profile(
    config: ProjectConfig,
    *,
    reference: Mapping[str, Any],
    root: Path,
    upstream_root: Path,
    profile: str,
    environment: Mapping[str, Any],
    slang_plugin: Path,
) -> dict[str, Any]:
    output = root / profile
    output.mkdir(parents=True, exist_ok=True)
    constraints = output / "abc.constr"
    constraints.write_text(
        f"set_driving_cell {config.synthesis.driving_cell}\n"
        f"set_load {config.synthesis.output_load_ff}\n",
        encoding="utf-8",
    )
    script_path = output / "synthesis.ys"
    log_path = output / "synthesis.log"
    stat_path = output / "stat.json"
    netlist_path = output / "mapped.v"
    sources = [upstream_root / item for item in reference["sources"]]
    include_dirs = [upstream_root / item for item in reference["include_dirs"]]
    script = _synthesis_script(
        top=str(reference["top"]),
        sources=sources,
        include_dirs=include_dirs,
        defines=list(reference["defines"]),
        parameters=reference["parameters"],
        profile=profile,
        slang_plugin=slang_plugin,
        liberty=Path(str(environment["liberty_path"])),
        abc=Path(str(environment["abc_path"])),
        constraints=constraints,
        stat=stat_path,
        netlist=netlist_path,
    )
    script_path.write_text(script, encoding="utf-8")
    command = (config.tools.yosys, "-Q", "-s", str(script_path))
    try:
        completed = run_command(
            command,
            timeout_seconds=max(config.tools.timeout_seconds, 120),
            cwd=config.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        raise CorpusBaselineError(str(exc), code="synthesis_tool_error") from exc
    transcript = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    log_path.write_text(transcript + ("\n" if transcript else ""), encoding="utf-8")
    if completed.returncode != 0:
        raise CorpusBaselineError(
            f"{profile} baseline synthesis failed for {reference['reference_id']}; see {log_path}",
            code="synthesis_failed",
        )
    try:
        abc_gates, abc_area, delay = _parse_abc_metrics(transcript)
        area, sequential_area, cells, raw_cells, cells_by_type = _parse_stat_metrics(
            stat_path, str(reference["top"])
        )
    except SynthesisError as exc:
        raise CorpusBaselineError(str(exc), code="invalid_synthesis_metrics") from exc
    if not netlist_path.is_file():
        raise CorpusBaselineError("baseline synthesis did not produce a mapped netlist")
    warnings = [line.strip() for line in transcript.splitlines() if "warning" in line.lower()]
    recipe = _recipe_definition(
        profile,
        top=str(reference["top"]),
        config=config,
        yosys_version=str(environment["yosys_version"]),
        yosys_sha256=str(environment["yosys_sha256"]),
        abc_version=str(environment["abc_version"]),
        abc_sha256=str(environment["abc_sha256"]),
        liberty_sha256=str(environment["liberty_sha256"]),
    )
    return {
        "status": "passed",
        "profile": profile,
        "recipe": recipe,
        "metrics": {
            "critical_delay_ps": delay,
            "area_total": area,
            "area_sequential": sequential_area,
            "area_combinational": round(area - sequential_area, 6),
            "cell_count": cells,
            "raw_cell_count": raw_cells,
            "abc_gate_count": abc_gates,
            "abc_area_combinational": abc_area,
            "cells_by_type": dict(sorted(cells_by_type.items())),
        },
        "artifacts": {
            "script": {"path": str(script_path), "sha256": hashlib.sha256(script.encode()).hexdigest()},
            "constraints": {"path": str(constraints), "sha256": file_sha256(constraints)},
            "log": {"path": str(log_path), "sha256": file_sha256(log_path)},
            "stat": {"path": str(stat_path), "sha256": file_sha256(stat_path)},
            "netlist": {"path": str(netlist_path), "sha256": file_sha256(netlist_path)},
        },
        "warnings": {"count": len(warnings), "sha256": stable_hash(warnings)},
    }


def characterize_tranche_baselines(
    config: ProjectConfig,
    *,
    tranche_lock_path: str | Path,
    qualification_plan_path: str | Path,
    registry: CorpusRegistryV1,
) -> dict[str, Any]:
    lock = load_tranche_lock(tranche_lock_path)
    plan = _load_qualification_plan(qualification_plan_path)
    if plan.get("tranche_id") != lock["tranche_id"] or plan.get(
        "tranche_semantic_hash"
    ) != lock["semantic_hash"]:
        raise CorpusBaselineError("qualification plan does not match the tranche lock")
    references = {item.reference_id: item for item in registry.references()}
    plan_references = list(plan["references"])
    if {item["reference_id"] for item in plan_references} != set(references):
        raise CorpusBaselineError("registry and qualification plan reference sets differ")
    if any(item.qualification.state != "build_reproduced" for item in references.values()):
        raise CorpusBaselineError("baseline synthesis requires build_reproduced references")
    if plan["tool_contract"].get("candidate_synthesis_enabled") is not False:
        raise CorpusBaselineError("Wave 2 baseline run must keep candidate synthesis disabled")

    environment = _environment(config)
    slang_plugin = Path(str(plan["tool_contract"]["yosys_slang_plugin"]))
    if not slang_plugin.is_file():
        raise CorpusBaselineError(
            f"pinned Yosys Slang plugin is unavailable: {slang_plugin}",
            code="missing_tool",
        )
    run_core = {
        "tranche_id": lock["tranche_id"],
        "tranche_semantic_hash": lock["semantic_hash"],
        "qualification_plan_hash": plan["semantic_hash"],
        "flow_version": MEASUREMENT_FLOW_VERSION,
        "yosys_sha256": environment["yosys_sha256"],
        "abc_sha256": environment["abc_sha256"],
        "liberty_sha256": environment["liberty_sha256"],
        "slang_plugin_sha256": file_sha256(slang_plugin),
    }
    run_hash = stable_hash(run_core)
    output_root = config.artifacts_dir / "corpus-baseline" / str(lock["tranche_id"]) / run_hash
    summary_path = output_root / "summary.json"
    if summary_path.is_file():
        cached = read_hashed_json(
            summary_path,
            document_type=BASELINE_SUMMARY_DOCUMENT_TYPE,
            schema_version=BASELINE_SCHEMA_VERSION,
        )
        return {**cached, "cached": True}

    results: list[dict[str, Any]] = []
    for reference in sorted(plan_references, key=lambda item: str(item["reference_id"])):
        reference_id = str(reference["reference_id"])
        upstream_id = references[reference_id].lineage.upstream_project_id
        upstream_root = (config.root / str(plan["upstream_roots"][upstream_id])).resolve()
        source_hashes = {
            item["path"]: item["sha256"] for item in references[reference_id].to_dict()["source_hashes"]
        }
        for relative in reference["sources"]:
            path = upstream_root / relative
            if not path.is_file() or file_sha256(path) != source_hashes[relative]:
                raise CorpusBaselineError(
                    f"source changed before baseline synthesis: {reference_id}/{relative}",
                    code="stale_source_hashes",
                )
        reference_root = output_root / reference_id
        profiles = {
            profile: _run_profile(
                config,
                reference=reference,
                root=reference_root,
                upstream_root=upstream_root,
                profile=profile,
                environment=environment,
                slang_plugin=slang_plugin,
            )
            for profile in SYNTHESIS_PROFILES
        }
        record = write_hashed_json(
            reference_root / "baseline.json",
            {
                "schema_version": BASELINE_SCHEMA_VERSION,
                "document_type": BASELINE_DOCUMENT_TYPE,
                "reference_id": reference_id,
                "run_hash": run_hash,
                "source_hashes": source_hashes,
                "compile_context_hash": references[reference_id].compile_context_hashes.compile_context_hash,
                "profiles": profiles,
            },
            exclusive=True,
        )
        results.append(
            {
                "reference_id": reference_id,
                "status": "passed",
                "record_path": str(reference_root / "baseline.json"),
                "semantic_hash": record["semantic_hash"],
            }
        )
    summary = write_hashed_json(
        summary_path,
        {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "document_type": BASELINE_SUMMARY_DOCUMENT_TYPE,
            "status": "passed",
            **run_core,
            "run_hash": run_hash,
            "candidate_synthesis_enabled": False,
            "reference_count": len(results),
            "results": results,
        },
        exclusive=True,
    )
    return {**summary, "cached": False}
