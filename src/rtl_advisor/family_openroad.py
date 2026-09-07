from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Mapping

from rtl_advisor.arbiter_family_execution import family_designs_from_candidate
from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_measure import (
    _read_command,
    _yosys_identity,
    classify_recipe,
)
from rtl_advisor.mvp_schema import (
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.openroad_v2 import (
    CORE_MARGIN_UM,
    DEFAULT_ORFS_IMAGE,
    OPENROAD_LOCK_SCHEMA_VERSION,
    ORFS_COMMIT,
    TARGET_UTILIZATION,
    _container_path,
    _find_metrics,
    _json_hash,
    _load_json,
    _route_finished,
    _run_one,
    _write_json,
    fixed_die_side,
    parse_openroad_metrics,
)
from rtl_advisor.realistic_openroad import (
    _materialize_pinned_flow,
    _verify_base_lock_for_m2,
)
from rtl_advisor.tools import ToolExecutionError, run_command, sha256_file
from rtl_advisor.transformation_executor import DEFAULT_EXECUTOR_REGISTRY


FAMILY_M2_FLOW_VERSION = "rtl-advisor-arbiter-family-orfs-m2-v1"
FAMILY_M2_SOURCES_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-m2-sources"
FAMILY_M2_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-m2-evidence"
FAMILY_M2_REPRODUCIBILITY_DOCUMENT_TYPE = (
    "rtl-advisor.arbiter-family-m2-reproducibility"
)
FAMILY_M2_ARTIFACT_SUBDIR = "openroad/family-m2-v1"
FAMILY_TOP = "rtl_advisor_family_m2_top"
SLANG_PLUGIN_PATH = "/opt/oss-cad-suite/share/yosys/plugins/slang.so"
PREPPA_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-preppa-evidence"
M01_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-m0-m1-evidence"


class FamilyOpenROADError(RuntimeError):
    """Raised when family M2 evidence cannot be produced or trusted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "family_openroad_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


def _pair_map(study: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item["pair_id"]): item
        for item in study.get("primary_cohort") or []
        if isinstance(item, Mapping)
    }


def _new_m2_selection(
    study: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    pairs = _pair_map(study)
    selected: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for sample in (study.get("m2_selection") or {}).get("primary") or []:
        if not isinstance(sample, Mapping):
            raise FamilyOpenROADError("invalid frozen M2 selection")
        pair = pairs.get(str(sample.get("pair_id")))
        if pair is None:
            raise FamilyOpenROADError("frozen M2 selection references unknown pair")
        if str(pair["reference_id"]) == "opentitan-prim-arbiter-ppc":
            continue
        selected.append((pair, sample))
    if len(selected) != 4:
        raise FamilyOpenROADError(
            "the family wave must add four M2 samples to the existing pair"
        )
    return selected


def _measurement_map(
    evidence: Mapping[str, Any],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (str(item["reference_id"]), str(item["configuration_id"])): item
        for item in evidence.get("results") or []
        if isinstance(item, Mapping)
    }


def _preppa_map(
    evidence: Mapping[str, Any],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (str(item["reference_id"]), str(item["configuration_id"])): item
        for item in evidence.get("results") or []
        if isinstance(item, Mapping)
    }


def prepare_family_m2_sources(
    config: ProjectConfig,
    study: Mapping[str, Any],
    preppa: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    repeat_root: Path,
) -> dict[str, Any]:
    output_path = repeat_root / "m2-sources.json"
    if output_path.is_file():
        cached = read_hashed_json(
            output_path,
            document_type=FAMILY_M2_SOURCES_DOCUMENT_TYPE,
            schema_version=1,
        )
        for configuration in cached.get("configurations") or []:
            for role in ("baseline", "candidate"):
                source = Path(str(configuration[role]["path"]))
                if (
                    not source.is_file()
                    or file_sha256(source) != configuration[role]["sha256"]
                ):
                    raise FamilyOpenROADError(
                        "cached family M2 source changed",
                        code="stale_m2_source",
                    )
        return cached
    if preppa.get("document_type") != PREPPA_DOCUMENT_TYPE:
        raise FamilyOpenROADError("invalid pre-PPA evidence")
    if measurements.get("document_type") != M01_DOCUMENT_TYPE:
        raise FamilyOpenROADError("invalid M0/M1 evidence")
    if (
        measurements.get("preppa_semantic_hash")
        != preppa.get("semantic_hash")
    ):
        raise FamilyOpenROADError("M0/M1 evidence is stale")
    plugin = Path(SLANG_PLUGIN_PATH)
    if not plugin.is_file():
        raise FamilyOpenROADError(
            "family M2 preparation requires the pinned Slang plugin",
            code="missing_synthesis_frontend",
        )

    preppa_results = _preppa_map(preppa)
    measured_results = _measurement_map(measurements)
    candidate_root = config.artifacts_dir / str(preppa["freeze_version"]) / "candidates"
    rows: list[dict[str, Any]] = []
    for pair, sample in _new_m2_selection(study):
        key = (
            str(pair["reference_id"]),
            str(sample["configuration_id"]),
        )
        formal_record = preppa_results.get(key)
        measurement = measured_results.get(key)
        if (
            formal_record is None
            or formal_record.get("formal_status") != "formal_passed"
            or formal_record.get("safe") is not True
        ):
            raise FamilyOpenROADError(
                f"current formal pass is missing for {key[0]}/{key[1]}",
                code="formal_required",
            )
        if measurement is None:
            raise FamilyOpenROADError(
                f"M0/M1 evidence is missing for {key[0]}/{key[1]}",
                code="missing_measurement",
            )
        candidate_id = str(formal_record["candidate_id"])
        candidate = read_hashed_json(
            candidate_root / candidate_id / "candidate-core.json",
            document_type="rtl-advisor.candidate",
            schema_version=1,
        )
        formal = read_hashed_json(
            candidate_root / candidate_id / "formal" / "formal.json",
            document_type="rtl-advisor.formal-result",
            schema_version=1,
        )
        if (
            formal["semantic_hash"] != formal_record["formal_semantic_hash"]
            or formal.get("candidate_id") != candidate_id
            or formal.get("candidate_design_hash")
            != (candidate.get("candidate_design") or {}).get("design_hash")
            or formal.get("proof_contract_hash")
            != candidate.get("proof_contract_hash")
        ):
            raise FamilyOpenROADError(
                f"candidate or formal link changed for {key[0]}/{key[1]}",
                code="stale_candidate",
            )
        executor = DEFAULT_EXECUTOR_REGISTRY.for_candidate(candidate)
        baseline, alternative = family_designs_from_candidate(
            candidate,
            executor.spec,
        )
        row: dict[str, Any] = {
            "pair_id": pair["pair_id"],
            "reference_id": key[0],
            "configuration_id": key[1],
            "parameters": candidate["configuration"],
            "candidate_id": candidate_id,
            "formal_semantic_hash": formal_record["formal_semantic_hash"],
            "m01_decision": measurement["decision"],
            "m01_profiles": measurement["profiles"],
        }
        for role, design in (
            ("baseline", baseline),
            ("candidate", alternative),
        ):
            role_root = (
                repeat_root
                / "m2"
                / "preelaborated"
                / str(pair["pair_id"])
                / key[1]
                / role
            )
            role_root.mkdir(parents=True, exist_ok=True)
            source_path = role_root / f"{FAMILY_TOP}.v"
            script_path = role_root / "elaborate.ys"
            log_path = role_root / "elaborate.log"
            script = "\n".join(
                (
                    f'plugin -i "{SLANG_PLUGIN_PATH}"',
                    _read_command(
                        design,
                        frontend="yosys-slang",
                        path_base=config.root,
                    ),
                    f"hierarchy -check -top {design.top}",
                    "proc",
                    "flatten",
                    "opt_clean",
                    f"rename {design.top} {FAMILY_TOP}",
                    f'write_verilog -noattr "{source_path}"',
                    "",
                )
            )
            script_path.write_text(script, encoding="utf-8")
            try:
                completed = run_command(
                    (config.tools.yosys, "-Q", "-s", str(script_path)),
                    timeout_seconds=config.tools.timeout_seconds,
                    cwd=config.root,
                )
            except ToolExecutionError as exc:
                raise FamilyOpenROADError(
                    str(exc),
                    code="m2_elaboration_failed",
                ) from exc
            transcript = "\n".join(
                part
                for part in (completed.stdout, completed.stderr)
                if part
            )
            log_path.write_text(
                transcript + ("\n" if transcript else ""),
                encoding="utf-8",
            )
            if completed.returncode != 0 or not source_path.is_file():
                raise FamilyOpenROADError(
                    f"M2 source elaboration failed for {key[0]}/{key[1]} "
                    f"{role}; see {log_path}",
                    code="m2_elaboration_failed",
                )
            row[role] = {
                "path": str(source_path),
                "sha256": file_sha256(source_path),
                "design_hash": design.design_hash,
                "script_path": str(script_path),
                "script_sha256": file_sha256(script_path),
                "log_path": str(log_path),
                "log_sha256": file_sha256(log_path),
            }
        rows.append(row)
    core = {
        "schema_version": 1,
        "document_type": FAMILY_M2_SOURCES_DOCUMENT_TYPE,
        "study_id": study["study_id"],
        "family_id": study["family_id"],
        "repeat_id": measurements["repeat_id"],
        "study_manifest_hash": study["manifest_hash"],
        "preppa_semantic_hash": preppa["semantic_hash"],
        "m01_semantic_hash": measurements["semantic_hash"],
        "source_kind": "slang_elaborated_rtlil_verilog",
        "frontend": {
            "kind": "yosys-slang",
            "plugin_path": SLANG_PLUGIN_PATH,
            "plugin_sha256": sha256_file(plugin),
        },
        "yosys": _yosys_identity(config),
        "configurations": rows,
        "limitations": [
            (
                "Slang elaboration normalizes SystemVerilog for the pinned "
                "OpenROAD-flow-scripts Yosys frontend."
            ),
            (
                "The M2 input is pre-elaborated RTL, not a technology-mapped "
                "M0/M1 netlist."
            ),
        ],
    }
    return write_hashed_json(output_path, core, exclusive=True)


def _sdc() -> str:
    return """set clk_ports [get_ports -quiet clk_i]
if {[llength $clk_ports] > 0} {
  create_clock -name vclk -period 10.0 $clk_ports
} else {
  create_clock -name vclk -period 10.0
}
set_input_delay 0.0 -clock vclk [all_inputs]
set_output_delay 0.0 -clock vclk [all_outputs]
"""


def _create_plan(
    config: ProjectConfig,
    sources: Mapping[str, Any],
    *,
    repeat_root: Path,
) -> Path:
    root = repeat_root / FAMILY_M2_ARTIFACT_SUBDIR
    time_shim = root / "bin" / "time"
    time_shim.parent.mkdir(parents=True, exist_ok=True)
    time_shim.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"-f\" ]; then shift 2; fi\nexec \"$@\"\n",
        encoding="utf-8",
    )
    time_shim.chmod(0o755)
    metadata_rules = root / "metadata-rules.json"
    _write_json(
        metadata_rules,
        {"constraints__clocks__count": {"value": 1, "compare": "=="}},
    )
    runs: list[dict[str, Any]] = []
    for configuration in sources.get("configurations") or []:
        profiles = configuration["m01_profiles"]
        maximum_area = max(
            float(profiles["M1"][role]["metrics"]["area_total"])
            for role in ("baseline", "candidate")
        )
        side = fixed_die_side(maximum_area)
        for role in ("baseline", "candidate"):
            source = Path(str(configuration[role]["path"])).resolve()
            if (
                not source.is_file()
                or file_sha256(source) != configuration[role]["sha256"]
            ):
                raise FamilyOpenROADError(
                    f"M2 source changed for "
                    f"{configuration['reference_id']}/"
                    f"{configuration['configuration_id']} {role}",
                    code="stale_m2_source",
                )
            run_id = (
                f"{configuration['pair_id']}__"
                f"{configuration['configuration_id']}__{role}"
            )
            run_root = root / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            sdc_path = run_root / "constraint.sdc"
            config_path = run_root / "config.mk"
            sdc_path.write_text(_sdc(), encoding="utf-8")
            config_path.write_text(
                "\n".join(
                    (
                        f"export DESIGN_NAME = {FAMILY_TOP}",
                        "export PLATFORM = nangate45",
                        f"export VERILOG_FILES = {_container_path(config, source)}",
                        f"export SDC_FILE = {_container_path(config, sdc_path)}",
                        f"export DIE_AREA = 0 0 {side:g} {side:g}",
                        (
                            "export CORE_AREA = "
                            f"{CORE_MARGIN_UM:g} {CORE_MARGIN_UM:g} "
                            f"{side - CORE_MARGIN_UM:g} "
                            f"{side - CORE_MARGIN_UM:g}"
                        ),
                        f"export PLACE_DENSITY = {TARGET_UTILIZATION:g}",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            runs.append(
                {
                    "run_id": run_id,
                    "case_id": (
                        f"{configuration['pair_id']}/"
                        f"{configuration['configuration_id']}"
                    ),
                    "family": "same_cycle_arbiter_topology",
                    "crosscheck_source": sources["study_id"],
                    "variant_id": role,
                    "top": FAMILY_TOP,
                    "source": str(source),
                    "source_sha256": file_sha256(source),
                    "config": str(config_path),
                    "config_sha256": file_sha256(config_path),
                    "sdc": str(sdc_path),
                    "sdc_sha256": file_sha256(sdc_path),
                    "maximum_yosys_area": maximum_area,
                    "die_side_um": side,
                    "status": "planned",
                }
            )
    core = {
        "schema_version": 1,
        "flow_version": FAMILY_M2_FLOW_VERSION,
        "artifact_subdir": FAMILY_M2_ARTIFACT_SUBDIR,
        "study_id": sources["study_id"],
        "repeat_id": sources["repeat_id"],
        "platform": "nangate45",
        "clock_period_ns": 10.0,
        "target_utilization": TARGET_UTILIZATION,
        "fixed_die_calculation": {
            "formula": (
                "max(100um, ceil_10um(sqrt(max_m1_area/0.35) + "
                "2*10um margin))"
            ),
        },
        "time_shim": {
            "path": str(time_shim),
            "sha256": file_sha256(time_shim),
        },
        "metadata_rules": {
            "path": str(metadata_rules),
            "sha256": file_sha256(metadata_rules),
        },
        "run_count": len(runs),
        "runs": runs,
    }
    plan = {**core, "plan_hash": stable_hash(core)}
    path = root / "plan.json"
    _write_json(path, plan)
    return path


def _create_lock(
    config: ProjectConfig,
    sources: Mapping[str, Any],
    *,
    repeat_root: Path,
    base_lock_path: Path | None,
    defer_runtime_image_validation: bool = False,
) -> Path:
    path = repeat_root / FAMILY_M2_ARTIFACT_SUBDIR / "lock.json"
    if path.is_file():
        return path
    resolved_base_lock = (
        base_lock_path or config.artifacts_dir / "openroad/v2/lock.json"
    )
    base = (
        _verify_base_lock_without_docker(config, resolved_base_lock)
        if defer_runtime_image_validation
        else _verify_base_lock_for_m2(config, resolved_base_lock)
    )
    if base["image"]["requested_reference"] != DEFAULT_ORFS_IMAGE:
        raise FamilyOpenROADError(
            "base OpenROAD lock does not use the frozen ORFS image"
        )
    plan_path = _create_plan(config, sources, repeat_root=repeat_root)
    plan = _load_json(plan_path)
    pinned_source = _materialize_pinned_flow(base, repeat_root)
    core = {
        "schema_version": OPENROAD_LOCK_SCHEMA_VERSION,
        "flow_version": FAMILY_M2_FLOW_VERSION,
        "artifact_subdir": FAMILY_M2_ARTIFACT_SUBDIR,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "orfs_commit": ORFS_COMMIT,
        "orfs_source": pinned_source,
        "plan": {
            "path": str(plan_path),
            "file_sha256": file_sha256(plan_path),
            "plan_hash": plan["plan_hash"],
            "run_count": plan["run_count"],
        },
        "image": base["image"],
        "docker_version": base["docker_version"],
        "host": base["host"],
        "liberty": base["liberty"],
        "derived_from_lock_hash": base["lock_hash"],
        "m2_flow_tree_verification": base["m2_flow_tree_verification"],
        "runtime_image_validation": base.get(
            "runtime_image_validation",
            {
                "mode": "verified_during_lock",
                "expected_image_id": base["image"]["id"],
            },
        ),
    }
    lock = {**core, "lock_hash": _json_hash(core)}
    _write_json(path, lock)
    return path


def _verify_base_lock_without_docker(
    config: ProjectConfig,
    path: Path,
) -> dict[str, Any]:
    """Verify every static input while deferring only Docker socket access.

    The eventual command still names the immutable image ID. Collection accepts
    evidence only when the expected routed artifacts and metrics exist.
    """

    base = _load_json(path)
    core = {key: value for key, value in base.items() if key != "lock_hash"}
    if base.get("lock_hash") != _json_hash(core):
        raise FamilyOpenROADError("base OpenROAD lock hash mismatch")
    if base.get("orfs_commit") != ORFS_COMMIT:
        raise FamilyOpenROADError("base OpenROAD lock uses the wrong ORFS commit")
    if file_sha256(config.liberty.path) != base["liberty"]["sha256"]:
        raise FamilyOpenROADError("base OpenROAD Liberty changed")
    source = base.get("orfs_source") or {}
    if source.get("kind") != "host_checkout":
        raise FamilyOpenROADError("family M2 requires the pinned ORFS checkout")
    checkout = Path(str(source["host_path"])).resolve()
    commands = {
        "revision": ("git", "-C", str(checkout), "rev-parse", "HEAD"),
        "tree": ("git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"),
        "listing": (
            "git",
            "-C",
            str(checkout),
            "ls-tree",
            "-r",
            "-z",
            "HEAD",
            "flow",
        ),
        "flow_tree": (
            "git",
            "-C",
            str(checkout),
            "rev-parse",
            f"{ORFS_COMMIT}:flow",
        ),
    }
    completed = {
        name: run_command(command, timeout_seconds=180)
        for name, command in commands.items()
    }
    if (
        completed["revision"].returncode != 0
        or completed["revision"].stdout != ORFS_COMMIT
        or completed["tree"].returncode != 0
        or completed["tree"].stdout != source.get("tree")
        or completed["listing"].returncode != 0
        or completed["flow_tree"].returncode != 0
    ):
        raise FamilyOpenROADError(
            "locked ORFS revision or tree no longer matches"
        )
    tracked = [
        record
        for record in completed["listing"].stdout.split("\x00")
        if record
    ]
    base["m2_flow_tree_verification"] = {
        "tracked_entry_count": len(tracked),
        "flow_tree": completed["flow_tree"].stdout,
        "scope": "Git objects under the pinned ORFS flow/ tree",
    }
    base["runtime_image_validation"] = {
        "mode": "deferred_to_immutable_run",
        "expected_image_id": base["image"]["id"],
        "reason": "Docker socket unavailable to the planning process",
    }
    return base


def create_family_m2_lock(
    config: ProjectConfig,
    sources: Mapping[str, Any],
    *,
    repeat_root: Path,
    base_lock_path: Path | None = None,
    defer_runtime_image_validation: bool = False,
) -> Path:
    return _create_lock(
        config,
        sources,
        repeat_root=repeat_root,
        base_lock_path=base_lock_path,
        defer_runtime_image_validation=defer_runtime_image_validation,
    )


def family_m2_run_commands(
    config: ProjectConfig,
    *,
    repeat_root: Path,
) -> list[dict[str, Any]]:
    lock_path = repeat_root / FAMILY_M2_ARTIFACT_SUBDIR / "lock.json"
    lock = _load_json(lock_path)
    plan = _load_json(Path(str(lock["plan"]["path"])))
    commands: list[dict[str, Any]] = []
    for run in plan["runs"]:
        work_root = (
            repeat_root
            / FAMILY_M2_ARTIFACT_SUBDIR
            / "work"
            / str(run["run_id"])
        )
        source = lock["orfs_source"]
        command = [
            "docker",
            "run",
            "--rm",
            "--platform",
            f"{lock['image']['os']}/{lock['image']['architecture']}",
            "-v",
            f"{config.root.resolve()}:/workspace",
            "-e",
            f"YOSYS_EXE={lock['image']['tools']['paths']['yosys']}",
            "-e",
            f"OPENROAD_EXE={lock['image']['tools']['paths']['openroad']}",
            "-e",
            f"KLAYOUT_CMD={lock['image']['tools']['paths']['klayout']}",
        ]
        if source["kind"] == "host_checkout":
            command.extend(("-v", f"{source['host_path']}:/orfs-source:ro"))
        command.extend(
            (
                "-w",
                source["container_flow_path"],
                lock["image"]["id"],
                "make",
                "--no-print-directory",
                f"DESIGN_CONFIG={_container_path(config, Path(run['config']))}",
                f"FLOW_VARIANT={run['run_id']}",
                f"WORK_HOME={_container_path(config, work_root)}",
                "TIME_BIN="
                + (
                    lock["image"]["tools"]["paths"].get("time")
                    or _container_path(
                        config,
                        repeat_root
                        / FAMILY_M2_ARTIFACT_SUBDIR
                        / "bin/time",
                    )
                ),
                (
                    "RULES_JSON="
                    + _container_path(
                        config,
                        Path(plan["metadata_rules"]["path"]),
                    )
                ),
                "finish",
                "metadata",
            )
        )
        commands.append({"run_id": run["run_id"], "argv": command})
    return commands


def collect_family_m2_runs(
    config: ProjectConfig,
    *,
    repeat_root: Path,
) -> dict[str, Any]:
    """Collect direct pinned Docker runs into standard OpenROAD result records."""

    lock_path = repeat_root / FAMILY_M2_ARTIFACT_SUBDIR / "lock.json"
    lock = _load_json(lock_path)
    plan = _load_json(Path(str(lock["plan"]["path"])))
    root = repeat_root / FAMILY_M2_ARTIFACT_SUBDIR
    collected: list[dict[str, Any]] = []
    for run in plan["runs"]:
        work_root = root / "work" / str(run["run_id"])
        metrics_path = _find_metrics(work_root)
        metrics = None
        metrics_error = None
        if metrics_path is not None:
            try:
                metrics = parse_openroad_metrics(metrics_path)
            except Exception as exc:  # preserve parser detail in evidence
                metrics_error = str(exc)
        route_finished = _route_finished(work_root)
        slack = metrics.get("worst_slack_ns") if metrics else None
        required_metrics = bool(
            metrics
            and slack is not None
            and math.isfinite(float(slack))
            and -1000.0 < float(slack) < 10.0
            and metrics.get("cell_area_um2") is not None
            and metrics.get("cell_count") is not None
            and metrics.get("drc_count") is not None
        )
        drc_clean = bool(metrics and metrics.get("drc_count") == 0)
        usable = route_finished and required_metrics and drc_clean
        result = {
            "schema_version": 1,
            "flow_version": FAMILY_M2_FLOW_VERSION,
            "lock_hash": lock["lock_hash"],
            "run_id": run["run_id"],
            "case_id": run["case_id"],
            "family": run["family"],
            "variant_id": run["variant_id"],
            "started_at": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "attempt_kind": "direct_immutable_image_run",
            "returncode": 0 if usable else None,
            "route_finished": route_finished,
            "metrics_path": str(metrics_path) if metrics_path else None,
            "metrics": metrics,
            "metrics_error": metrics_error,
            "drc_clean": drc_clean,
            "usable": usable,
            "status": "usable" if usable else "failed",
            "error": None if usable else "routed result or required metrics missing",
            "stdout_log": None,
            "stderr_log": None,
            "cached": False,
            "runtime_image_id": lock["image"]["id"],
        }
        result_path = root / "results" / f"{run['run_id']}.json"
        _write_json(result_path, result)
        collected.append(result)
    return {
        "status": (
            "completed"
            if all(item["usable"] for item in collected)
            else "completed_with_failures"
        ),
        "run_count": len(collected),
        "usable_count": sum(bool(item["usable"]) for item in collected),
        "results": collected,
    }


def _physical_metrics(result: Mapping[str, Any]) -> dict[str, float]:
    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        raise FamilyOpenROADError("usable family M2 result lacks metrics")
    slack = metrics.get("worst_slack_ns")
    area = metrics.get("cell_area_um2")
    if slack is None or area is None:
        raise FamilyOpenROADError(
            "usable family M2 result lacks timing or area"
        )
    return {
        "critical_delay_ps": (10.0 - float(slack)) * 1000.0,
        "area_total": float(area),
    }


def run_family_m2(
    config: ProjectConfig,
    sources: Mapping[str, Any],
    *,
    repeat_root: Path,
    base_lock_path: Path | None = None,
    workers: int = 2,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    if workers < 1 or workers > 4:
        raise FamilyOpenROADError("family M2 workers must be between 1 and 4")
    result_path = repeat_root / "m2.json"
    if result_path.is_file():
        return read_hashed_json(
            result_path,
            document_type=FAMILY_M2_DOCUMENT_TYPE,
            schema_version=1,
        )
    lock_path = _create_lock(
        config,
        sources,
        repeat_root=repeat_root,
        base_lock_path=base_lock_path,
    )
    lock = _load_json(lock_path)
    plan = _load_json(Path(str(lock["plan"]["path"])))
    m2_config = replace(config, artifacts_dir=repeat_root)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _run_one,
                m2_config,
                lock,
                run,
                retry_failed=False,
                timeout_seconds=timeout_seconds,
            ): run
            for run in plan["runs"]
        }
        for future in as_completed(futures):
            results.append(future.result())
    by_key = {
        (str(item["case_id"]), str(item["variant_id"])): item
        for item in results
    }
    configurations: list[dict[str, Any]] = []
    for source in sources.get("configurations") or []:
        case_id = f"{source['pair_id']}/{source['configuration_id']}"
        baseline = by_key[(case_id, "baseline")]
        candidate = by_key[(case_id, "candidate")]
        usable = bool(baseline.get("usable") and candidate.get("usable"))
        if usable:
            baseline_metrics = _physical_metrics(baseline)
            candidate_metrics = _physical_metrics(candidate)
            classification = classify_recipe(
                "timing",
                baseline_metrics,
                candidate_metrics,
            )
            comparison = {
                metric: {
                    "baseline": baseline_metrics[metric],
                    "candidate": candidate_metrics[metric],
                    "improvement_percent": (
                        (
                            baseline_metrics[metric]
                            - candidate_metrics[metric]
                        )
                        / baseline_metrics[metric]
                        * 100.0
                    ),
                }
                for metric in ("critical_delay_ps", "area_total")
            }
        else:
            classification = "failed"
            comparison = None
        m01_direction = {
            "measured_improvement": "improved",
            "synthesis_handles": "neutral",
            "regression": "regressed",
            "flow_dependent": "flow_dependent",
        }.get(str(source["m01_decision"]), "unavailable")
        configurations.append(
            {
                "pair_id": source["pair_id"],
                "reference_id": source["reference_id"],
                "configuration_id": source["configuration_id"],
                "candidate_id": source["candidate_id"],
                "formal_semantic_hash": source["formal_semantic_hash"],
                "usable": usable,
                "m01_decision": source["m01_decision"],
                "m01_direction": m01_direction,
                "classification": classification,
                "direction_agreement": (
                    usable and classification == m01_direction
                ),
                "comparison": comparison,
                "baseline": baseline,
                "candidate": candidate,
            }
        )
    core = {
        "schema_version": 1,
        "document_type": FAMILY_M2_DOCUMENT_TYPE,
        "flow_version": FAMILY_M2_FLOW_VERSION,
        "study_id": sources["study_id"],
        "family_id": sources["family_id"],
        "repeat_id": sources["repeat_id"],
        "measurement_level": "M2",
        "source_semantic_hash": sources["semantic_hash"],
        "status": (
            "completed"
            if all(item["usable"] for item in configurations)
            else "completed_with_failures"
        ),
        "lock_hash": lock["lock_hash"],
        "configurations": configurations,
        "limitations": [
            (
                "M2 is a pinned Nangate45 OpenROAD cross-check, not "
                "production PPA."
            ),
            "M2 uses pre-elaborated RTL and a fixed 10 ns constraint.",
        ],
    }
    return write_hashed_json(result_path, core, exclusive=True)


def compare_family_m2_repeats(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    left = {
        (str(item["pair_id"]), str(item["configuration_id"])): item
        for item in first.get("configurations") or []
    }
    right = {
        (str(item["pair_id"]), str(item["configuration_id"])): item
        for item in second.get("configurations") or []
    }
    rows: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        first_row = left.get(key)
        second_row = right.get(key)
        available = bool(
            first_row
            and second_row
            and first_row.get("usable")
            and second_row.get("usable")
        )
        drift: dict[str, float] = {}
        within_tolerance = available
        direction_agreement = bool(
            available
            and first_row.get("classification")
            == second_row.get("classification")
        )
        if available:
            for metric in ("critical_delay_ps", "area_total"):
                for role in ("baseline", "candidate"):
                    baseline_value = float(
                        first_row["comparison"][metric][role]
                    )
                    second_value = float(
                        second_row["comparison"][metric][role]
                    )
                    relative = (
                        abs(second_value - baseline_value)
                        / max(abs(baseline_value), 1e-12)
                        * 100.0
                    )
                    drift[f"{metric}:{role}"] = relative
                    within_tolerance = (
                        within_tolerance
                        and (
                            relative <= 2.0
                            or math.isclose(relative, 2.0, abs_tol=1e-9)
                        )
                    )
        rows.append(
            {
                "pair_id": key[0],
                "configuration_id": key[1],
                "available": available,
                "repeat_direction_agreement": direction_agreement,
                "within_2_percent": within_tolerance,
                "drift_percent": drift,
            }
        )
    core = {
        "schema_version": 1,
        "document_type": FAMILY_M2_REPRODUCIBILITY_DOCUMENT_TYPE,
        "study_id": first["study_id"],
        "family_id": first["family_id"],
        "status": (
            "passed"
            if rows
            and all(
                item["available"]
                and item["repeat_direction_agreement"]
                and item["within_2_percent"]
                for item in rows
            )
            else "failed"
        ),
        "configurations": rows,
    }
    return {**core, "semantic_hash": stable_hash(core)}
