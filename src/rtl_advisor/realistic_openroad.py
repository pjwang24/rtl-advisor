from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tarfile
from typing import Any, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_measure import classify_recipe
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
    _docker_json,
    _json_hash,
    _load_json,
    _run_one,
    _write_json,
    fixed_die_side,
)
from rtl_advisor.realistic_study import STUDY_ID
from rtl_advisor.tools import ToolExecutionError, run_command


M2_FLOW_VERSION = "rtl-advisor-realistic-arbiter-orfs-m2-v2"
M2_DOCUMENT_TYPE = "rtl-advisor.realistic-evidence-m2"
M2_ARTIFACT_SUBDIR = "openroad/realistic-m2-v2"


class RealisticOpenROADError(RuntimeError):
    """Raised when the frozen realistic M2 cross-check cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "realistic_openroad_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


def _verify_base_lock_for_m2(
    config: ProjectConfig,
    path: Path,
) -> dict[str, Any]:
    """Verify the reusable lock without a full, slow macOS worktree status."""

    base = _load_json(path)
    core = {key: value for key, value in base.items() if key != "lock_hash"}
    if base.get("lock_hash") != _json_hash(core):
        raise RealisticOpenROADError("base OpenROAD lock hash mismatch")
    if base.get("orfs_commit") != ORFS_COMMIT:
        raise RealisticOpenROADError("base OpenROAD lock uses the wrong ORFS commit")
    if file_sha256(config.liberty.path) != base["liberty"]["sha256"]:
        raise RealisticOpenROADError("base OpenROAD Liberty changed")
    source = base.get("orfs_source") or {}
    if source.get("kind") != "host_checkout":
        raise RealisticOpenROADError(
            "M2 requires the previously locked host ORFS checkout"
        )
    checkout = Path(str(source["host_path"])).resolve()
    try:
        revision = run_command(
            ("git", "-C", str(checkout), "rev-parse", "HEAD"),
            timeout_seconds=30,
        )
        tree = run_command(
            ("git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"),
            timeout_seconds=30,
        )
        listing = run_command(
            ("git", "-C", str(checkout), "ls-tree", "-r", "-z", "HEAD", "flow"),
            timeout_seconds=30,
        )
    except ToolExecutionError as exc:
        raise RealisticOpenROADError(str(exc)) from exc
    if (
        revision.returncode != 0
        or revision.stdout != ORFS_COMMIT
        or tree.returncode != 0
        or tree.stdout != source.get("tree")
        or listing.returncode != 0
    ):
        raise RealisticOpenROADError(
            "locked ORFS revision or tree no longer matches"
        )
    tracked = [record for record in listing.stdout.split("\x00") if record]
    flow_tree = run_command(
        ("git", "-C", str(checkout), "rev-parse", f"{ORFS_COMMIT}:flow"),
        timeout_seconds=30,
    )
    if flow_tree.returncode != 0:
        raise RealisticOpenROADError("cannot resolve the pinned ORFS flow tree")
    inspection = _docker_json(
        ("docker", "image", "inspect", base["image"]["id"]),
        timeout_seconds=30,
    )
    if not inspection or inspection[0].get("Id") != base["image"]["id"]:
        raise RealisticOpenROADError("locked ORFS Docker image is unavailable")
    base["m2_flow_tree_verification"] = {
        "tracked_entry_count": len(tracked),
        "flow_tree": flow_tree.stdout,
        "scope": "Git objects under the pinned ORFS flow/ tree",
    }
    return base


def _materialize_pinned_flow(
    base: Mapping[str, Any],
    repeat_root: Path,
) -> dict[str, Any]:
    source = base["orfs_source"]
    checkout = Path(str(source["host_path"])).resolve()
    verification = base["m2_flow_tree_verification"]
    snapshot_root = (
        repeat_root
        / "openroad"
        / f"orfs-snapshot-{str(verification['flow_tree'])[:12]}"
    )
    archive_path = snapshot_root.parent / f"{snapshot_root.name}.tar"
    manifest_path = snapshot_root / "snapshot.json"
    if manifest_path.is_file():
        manifest = _load_json(manifest_path)
        if (
            manifest.get("commit") != ORFS_COMMIT
            or manifest.get("flow_tree") != verification["flow_tree"]
            or not (snapshot_root / "flow/Makefile").is_file()
        ):
            raise RealisticOpenROADError("existing ORFS snapshot is stale")
        return {
            "kind": "host_checkout",
            "host_path": str(snapshot_root),
            "container_flow_path": "/orfs-source/flow",
            "commit": ORFS_COMMIT,
            "tree": source["tree"],
            "clean": True,
            "materialization": manifest,
        }
    if snapshot_root.exists():
        raise RealisticOpenROADError(
            "incomplete ORFS snapshot exists; use a clean repeat workspace"
        )
    snapshot_root.mkdir(parents=True)
    archive = run_command(
        (
            "git",
            "-C",
            str(checkout),
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            ORFS_COMMIT,
            "flow",
        ),
        timeout_seconds=300,
    )
    if archive.returncode != 0 or not archive_path.is_file():
        raise RealisticOpenROADError(
            archive.stderr or archive.stdout or "git archive failed"
        )
    with tarfile.open(archive_path, mode="r") as bundle:
        bundle.extractall(snapshot_root, filter="data")
    if not (snapshot_root / "flow/Makefile").is_file():
        raise RealisticOpenROADError("materialized ORFS flow is incomplete")
    manifest = {
        "commit": ORFS_COMMIT,
        "flow_tree": verification["flow_tree"],
        "archive_sha256": file_sha256(archive_path),
        "archive_size_bytes": archive_path.stat().st_size,
        "source_checkout": str(checkout),
        "source_checkout_tree": source["tree"],
    }
    _write_json(manifest_path, manifest)
    return {
        "kind": "host_checkout",
        "host_path": str(snapshot_root),
        "container_flow_path": "/orfs-source/flow",
        "commit": ORFS_COMMIT,
        "tree": source["tree"],
        "clean": True,
        "materialization": manifest,
    }


def _sdc() -> str:
    return """create_clock -name clk -period 10.0 [get_ports clk_i]
set_input_delay 0.0 -clock clk [get_ports rst_ni]
set_input_delay 0.0 -clock clk [get_ports req_i]
set_input_delay 0.0 -clock clk [get_ports data_i]
set_input_delay 0.0 -clock clk [get_ports ready_i]
set_output_delay 0.0 -clock clk [all_outputs]
"""


def _measurement_configuration(
    study: Mapping[str, Any],
    configuration_id: str,
) -> Mapping[str, Any]:
    for item in (study.get("normalized") or {}).get("configurations") or []:
        if (
            isinstance(item, Mapping)
            and item.get("configuration_id") == configuration_id
        ):
            return item
    raise RealisticOpenROADError(
        f"study lacks M0/M1 configuration {configuration_id}",
        code="missing_measurement",
    )


def create_m2_plan(
    config: ProjectConfig,
    study: Mapping[str, Any],
    sources: Mapping[str, Any],
) -> Path:
    repeat_root = Path(str((study.get("artifacts") or {}).get("root", ""))).resolve()
    root = repeat_root / M2_ARTIFACT_SUBDIR
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
        if not isinstance(configuration, Mapping):
            raise RealisticOpenROADError("invalid M2 source configuration")
        configuration_id = str(configuration["configuration_id"])
        measured = _measurement_configuration(study, configuration_id)
        profiles = (measured.get("measurement") or {}).get("profiles") or {}
        stronger = profiles.get("stronger") or {}
        maximum_area = max(
            float((stronger[role]["metrics"])["area_total"])
            for role in ("baseline", "candidate")
        )
        side = fixed_die_side(maximum_area)
        for role in ("baseline", "candidate"):
            source = Path(str(configuration[role]["path"])).resolve()
            if (
                not source.is_file()
                or file_sha256(source) != configuration[role]["sha256"]
            ):
                raise RealisticOpenROADError(
                    f"M2 source changed for {configuration_id} {role}",
                    code="stale_m2_source",
                )
            run_id = f"arbiter_{configuration_id}__{role}"
            run_root = root / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            sdc_path = run_root / "constraint.sdc"
            config_path = run_root / "config.mk"
            sdc_path.write_text(_sdc(), encoding="utf-8")
            config_path.write_text(
                "\n".join(
                    (
                        "export DESIGN_NAME = rtl_advisor_arbiter_evidence_top",
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
                    "case_id": configuration_id,
                    "family": "same_cycle_arbiter_topology",
                    "crosscheck_source": STUDY_ID,
                    "variant_id": role,
                    "top": "rtl_advisor_arbiter_evidence_top",
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
        "flow_version": M2_FLOW_VERSION,
        "artifact_subdir": M2_ARTIFACT_SUBDIR,
        "study_id": STUDY_ID,
        "repeat_id": study["repeat_id"],
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
    plan_path = root / "plan.json"
    _write_json(plan_path, plan)
    return plan_path


def create_m2_lock(
    config: ProjectConfig,
    study: Mapping[str, Any],
    sources: Mapping[str, Any],
    *,
    base_lock_path: str | Path | None = None,
) -> Path:
    base_path = Path(
        base_lock_path
        or config.artifacts_dir / "openroad" / "v2" / "lock.json"
    )
    base = _verify_base_lock_for_m2(config, base_path)
    if base["image"]["requested_reference"] != DEFAULT_ORFS_IMAGE:
        raise RealisticOpenROADError(
            "base OpenROAD lock does not use the frozen ORFS image"
        )
    plan_path = create_m2_plan(config, study, sources)
    plan = _load_json(plan_path)
    repeat_root = Path(str((study.get("artifacts") or {}).get("root", ""))).resolve()
    pinned_source = _materialize_pinned_flow(base, repeat_root)
    core = {
        "schema_version": OPENROAD_LOCK_SCHEMA_VERSION,
        "flow_version": M2_FLOW_VERSION,
        "artifact_subdir": M2_ARTIFACT_SUBDIR,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "orfs_commit": base["orfs_commit"],
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
    }
    lock = {**core, "lock_hash": _json_hash(core)}
    path = repeat_root / M2_ARTIFACT_SUBDIR / "lock.json"
    _write_json(path, lock)
    return path


def _physical_metrics(result: Mapping[str, Any]) -> dict[str, float]:
    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        raise RealisticOpenROADError("usable M2 result lacks metrics")
    slack = metrics.get("worst_slack_ns")
    area = metrics.get("cell_area_um2")
    if slack is None or area is None:
        raise RealisticOpenROADError("usable M2 result lacks timing or area")
    return {
        "critical_delay_ps": (10.0 - float(slack)) * 1000.0,
        "area_total": float(area),
    }


def run_m2(
    config: ProjectConfig,
    study: Mapping[str, Any],
    sources: Mapping[str, Any],
    *,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    repeat_root = Path(str((study.get("artifacts") or {}).get("root", ""))).resolve()
    result_path = repeat_root / "m2.json"
    if result_path.is_file():
        return read_hashed_json(
            result_path,
            document_type=M2_DOCUMENT_TYPE,
            schema_version=1,
        )
    lock_path = create_m2_lock(config, study, sources)
    lock = _load_json(lock_path)
    plan = _load_json(Path(str(lock["plan"]["path"])))
    m2_config = replace(config, artifacts_dir=repeat_root)
    results = [
        _run_one(
            m2_config,
            lock,
            run,
            retry_failed=False,
            timeout_seconds=timeout_seconds,
        )
        for run in plan["runs"]
    ]
    by_key = {
        (str(item["case_id"]), str(item["variant_id"])): item
        for item in results
    }
    configurations: list[dict[str, Any]] = []
    for configuration_id in ("n08-dw32", "n16-dw32"):
        baseline = by_key[(configuration_id, "baseline")]
        candidate = by_key[(configuration_id, "candidate")]
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
            classification = "unavailable"
            comparison = None
        measured = _measurement_configuration(study, configuration_id)
        m01_decision = str((measured.get("measurement") or {}).get("decision"))
        m01_direction = {
            "measured_improvement": "improved",
            "synthesis_handles": "neutral",
            "regression": "regressed",
            "flow_dependent": "flow_dependent",
        }.get(m01_decision, "unavailable")
        configurations.append(
            {
                "configuration_id": configuration_id,
                "usable": usable,
                "m01_decision": m01_decision,
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
        "document_type": M2_DOCUMENT_TYPE,
        "flow_version": M2_FLOW_VERSION,
        "study_id": STUDY_ID,
        "repeat_id": study["repeat_id"],
        "measurement_level": "M2",
        "status": (
            "completed"
            if all(item["usable"] for item in configurations)
            else "incomplete"
        ),
        "lock_hash": lock["lock_hash"],
        "configurations": configurations,
        "limitations": [
            "M2 is a pinned Nangate45 OpenROAD cross-check, not production PPA.",
            "M2 uses Slang-elaborated RTL and the fixed 10 ns constraint.",
        ],
    }
    return write_hashed_json(result_path, core, exclusive=True)


def compare_m2_repeats(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    output_path: str | Path,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    first_rows = {
        str(item["configuration_id"]): item
        for item in first.get("configurations") or []
    }
    second_rows = {
        str(item["configuration_id"]): item
        for item in second.get("configurations") or []
    }
    for configuration_id in ("n08-dw32", "n16-dw32"):
        left = first_rows.get(configuration_id)
        right = second_rows.get(configuration_id)
        available = bool(
            isinstance(left, Mapping)
            and isinstance(right, Mapping)
            and left.get("usable")
            and right.get("usable")
        )
        drift: dict[str, float] = {}
        within_tolerance = available
        repeat_direction_agreement = bool(
            available
            and left.get("classification") == right.get("classification")
        )
        m01_direction_agreement = bool(
            available
            and left.get("direction_agreement")
            and right.get("direction_agreement")
        )
        if available:
            for metric in ("critical_delay_ps", "area_total"):
                left_metrics = left["comparison"][metric]
                right_metrics = right["comparison"][metric]
                for role in ("baseline", "candidate"):
                    key = f"{metric}:{role}"
                    denominator = float(left_metrics[role])
                    value = abs(
                        float(right_metrics[role]) - denominator
                    ) / denominator * 100.0
                    drift[key] = value
                    within_tolerance = within_tolerance and value <= 2.0
        rows.append(
            {
                "configuration_id": configuration_id,
                "available": available,
                "repeat_direction_agreement": repeat_direction_agreement,
                "m01_direction_agreement": m01_direction_agreement,
                "within_2_percent": within_tolerance,
                "drift_percent": drift,
            }
        )
    core = {
        "schema_version": 1,
        "document_type": "rtl-advisor.realistic-evidence-m2-reproducibility",
        "study_id": STUDY_ID,
        "status": (
            "passed"
            if all(
                item["available"]
                and item["repeat_direction_agreement"]
                and item["within_2_percent"]
                for item in rows
            )
            else "failed"
        ),
        "configurations": rows,
    }
    return write_hashed_json(Path(output_path), core, exclusive=True)
