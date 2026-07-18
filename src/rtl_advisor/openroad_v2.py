from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
from typing import Any

from rtl_advisor.advisor_v2 import PROFILES
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import load_manifest
from rtl_advisor.tools import ToolExecutionError, run_command


OPENROAD_PLAN_SCHEMA_VERSION = 3
OPENROAD_LOCK_SCHEMA_VERSION = 1
OPENROAD_RESULT_SCHEMA_VERSION = 1
OPENROAD_REPORT_SCHEMA_VERSION = 1
OPENROAD_FLOW_VERSION = "rtl-advisor-orfs-crosscheck-v2"
ORFS_COMMIT = "036d106273e66855cd5214d49518fd0f0df7de61"
DEFAULT_ORFS_IMAGE = "openroad/orfs:latest"
TARGET_UTILIZATION = 0.35
MINIMUM_DIE_SIDE_UM = 100.0
CORE_MARGIN_UM = 10.0
PHYSICAL_GATE_MINIMUM_COMPLETE_CASES = 24
PHYSICAL_GATE_MINIMUM_FAMILY_CASES = 2
PHYSICAL_GATE_ACTION_AGREEMENT = 0.80
PHYSICAL_GATE_DIRECTION_AGREEMENT = 0.75
_VARIANTS = ("v0", "v1", "v2", "v3")
_METRICS = ("delay", "area", "cell_count")


class OpenROADV2Error(RuntimeError):
    """Raised when the frozen OpenROAD cross-check cannot be prepared."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenROADV2Error(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OpenROADV2Error(f"expected a JSON object in {path}")
    return value


def _container_path(config: ProjectConfig, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(config.root.resolve())
    except ValueError as exc:
        raise OpenROADV2Error(
            f"OpenROAD input must be inside the project root: {path}"
        ) from exc
    return f"/workspace/{relative.as_posix()}"


def _load_suite(path: Path, split: str) -> dict[str, Any]:
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenROADV2Error(f"invalid suite {path}: {exc}") from exc
    if suite.get("split") != split:
        raise OpenROADV2Error(f"expected {split} suite at {path}")
    return suite


def selected_crosscheck_cases(
    calibration_suite: dict[str, Any],
    blind_suite: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    selected = []
    families = sorted({case["family"] for case in calibration_suite.get("cases") or []})
    for family in families:
        calibration = sorted(
            [case for case in calibration_suite["cases"] if case["family"] == family],
            key=lambda case: case["case_id"],
        )
        blind = sorted(
            [case for case in blind_suite["cases"] if case["family"] == family],
            key=lambda case: case["case_id"],
        )
        if len(calibration) != 40 or len(blind) != 8:
            raise OpenROADV2Error(f"unexpected v2 split counts for {family}")
        selected.extend(
            (
                {**calibration[0], "crosscheck_source": "calibration_lowest"},
                {
                    **calibration[len(calibration) // 2],
                    "crosscheck_source": "calibration_median",
                },
                {**blind[0], "crosscheck_source": "blind_lowest"},
            )
        )
    if len(selected) != 27:
        raise OpenROADV2Error(f"cross-check must select 27 cases, got {len(selected)}")
    return tuple(selected)


def fixed_die_side(maximum_cell_area: float) -> float:
    if maximum_cell_area <= 0:
        raise OpenROADV2Error("mapped cell area must be positive")
    raw = math.sqrt(maximum_cell_area / TARGET_UTILIZATION) + 2 * CORE_MARGIN_UM
    return max(MINIMUM_DIE_SIDE_UM, math.ceil(raw / 10.0) * 10.0)


def _summary(config: ProjectConfig, case_id: str) -> dict[str, Any]:
    path = config.artifacts_dir / "cases" / case_id / "synthesis/summary.json"
    if not path.is_file():
        raise OpenROADV2Error(f"synthesis summary missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenROADV2Error(f"invalid synthesis summary {path}: {exc}") from exc


def _case_manifest_path(
    case: dict[str, Any],
    calibration_path: Path,
    blind_path: Path,
) -> Path:
    root = blind_path.parent if case["split"] == "heldout-v2" else calibration_path.parent
    return root / str(case["manifest"])


def _sdc() -> str:
    return """set clk_ports [get_ports -quiet clk]
if {[llength $clk_ports] > 0} {
  create_clock -name vclk -period 10.0 $clk_ports
} else {
  create_clock -name vclk -period 10.0
}
set_input_delay 0.0 -clock vclk [all_inputs]
set_output_delay 0.0 -clock vclk [all_outputs]
"""


def create_openroad_plan(config: ProjectConfig) -> Path:
    calibration_path = config.corpus_dir / "calibration-v2/suite.json"
    blind_path = config.corpus_dir / "heldout-v2/suite.json"
    calibration = _load_suite(calibration_path, "calibration-v2")
    blind = _load_suite(blind_path, "heldout-v2")
    cases = selected_crosscheck_cases(calibration, blind)
    root = config.artifacts_dir / "openroad/v2"
    time_shim = root / "bin/time"
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
    runs = []
    for case in cases:
        manifest_path = _case_manifest_path(case, calibration_path, blind_path)
        manifest = load_manifest(manifest_path)
        summary = _summary(config, manifest.case_id)
        results = {
            result["variant_id"]: result for result in summary.get("results") or []
        }
        if not all(variant in results for variant in ("v0", "v1", "v2", "v3")):
            raise OpenROADV2Error(
                f"complete v0-v3 synthesis results missing for {manifest.case_id}"
            )
        maximum_area = max(
            float(results[variant]["metrics"]["area_total"])
            for variant in ("v0", "v1", "v2", "v3")
        )
        side = fixed_die_side(maximum_area)
        for variant_id in ("v0", "v1", "v2", "v3"):
            variant = manifest.variant(variant_id)
            run_id = f"{manifest.case_id}__{variant_id}"
            run_root = root / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            sdc_path = run_root / "constraint.sdc"
            config_path = run_root / "config.mk"
            sdc_path.write_text(_sdc(), encoding="utf-8")
            source = manifest.variant_path(variant)
            config_path.write_text(
                "\n".join(
                    (
                        f"export DESIGN_NAME = {variant.wrapper_top}",
                        "export PLATFORM = nangate45",
                        f"export VERILOG_FILES = {_container_path(config, source)}",
                        f"export SDC_FILE = {_container_path(config, sdc_path)}",
                        f"export DIE_AREA = 0 0 {side:g} {side:g}",
                        (
                            "export CORE_AREA = "
                            f"{CORE_MARGIN_UM:g} {CORE_MARGIN_UM:g} "
                            f"{side - CORE_MARGIN_UM:g} {side - CORE_MARGIN_UM:g}"
                        ),
                        "export PLACE_DENSITY = 0.35",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            runs.append(
                {
                    "run_id": run_id,
                    "case_id": manifest.case_id,
                    "family": manifest.family,
                    "crosscheck_source": case["crosscheck_source"],
                    "variant_id": variant_id,
                    "top": variant.wrapper_top,
                    "source": str(source),
                    "source_sha256": _file_hash(source),
                    "config": str(config_path),
                    "config_sha256": _file_hash(config_path),
                    "sdc": str(sdc_path),
                    "sdc_sha256": _file_hash(sdc_path),
                    "maximum_yosys_area": maximum_area,
                    "die_side_um": side,
                    "status": "planned",
                }
            )
    payload = {
        "schema_version": OPENROAD_PLAN_SCHEMA_VERSION,
        "flow_version": OPENROAD_FLOW_VERSION,
        "platform": "nangate45",
        "clock_period_ns": 10.0,
        "target_utilization": TARGET_UTILIZATION,
        "time_shim": {
            "path": str(time_shim),
            "sha256": _file_hash(time_shim),
            "purpose": "portable pass-through for tool images without GNU time",
        },
        "metadata_rules": {
            "path": str(metadata_rules),
            "sha256": _file_hash(metadata_rules),
            "purpose": "require the expected clock while generating complete metadata",
        },
        "case_count": len(cases),
        "run_count": len(runs),
        "runs": runs,
    }
    payload["plan_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    plan_path = root / "plan.json"
    _write_json(plan_path, payload)
    return plan_path


def _docker_json(command: tuple[str, ...], *, timeout_seconds: int = 30) -> Any:
    try:
        completed = run_command(command, timeout_seconds=timeout_seconds)
    except ToolExecutionError as exc:
        raise OpenROADV2Error(str(exc)) from exc
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or f"exit {completed.returncode}"
        raise OpenROADV2Error(f"Docker command failed: {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OpenROADV2Error(f"Docker returned invalid JSON: {completed.stdout}") from exc


def _host_orfs_provenance(path: Path) -> dict[str, Any]:
    root = path.expanduser().resolve()
    if not (root / "flow/Makefile").is_file():
        raise OpenROADV2Error(f"ORFS flow/Makefile not found under {root}")
    revision = run_command(
        ("git", "-C", str(root), "rev-parse", "HEAD"), timeout_seconds=30
    )
    tree = run_command(
        ("git", "-C", str(root), "rev-parse", "HEAD^{tree}"), timeout_seconds=30
    )
    status = run_command(
        ("git", "-C", str(root), "status", "--porcelain"), timeout_seconds=30
    )
    if revision.returncode != 0 or revision.stdout != ORFS_COMMIT:
        raise OpenROADV2Error(
            f"ORFS checkout must be exactly {ORFS_COMMIT}; got {revision.stdout or revision.stderr}"
        )
    if status.returncode != 0 or status.stdout:
        raise OpenROADV2Error("ORFS checkout must be clean before it can be locked")
    return {
        "kind": "host_checkout",
        "host_path": str(root),
        "container_flow_path": "/orfs-source/flow",
        "commit": revision.stdout,
        "tree": tree.stdout,
        "clean": True,
    }


def _image_orfs_provenance(image_id: str, labels: dict[str, Any]) -> dict[str, Any]:
    script = (
        "for p in /OpenROAD-flow-scripts /openroad-flow-scripts /orfs /opt/orfs; do "
        "if [ -f \"$p/flow/Makefile\" ]; then "
        "rev=$(git -C \"$p\" rev-parse HEAD 2>/dev/null || true); "
        "printf '%s|%s\\n' \"$p/flow\" \"$rev\"; exit 0; fi; done; exit 7"
    )
    try:
        probe = run_command(
            (
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                image_id,
                "-c",
                script,
            ),
            timeout_seconds=60,
        )
    except ToolExecutionError as exc:
        raise OpenROADV2Error(str(exc)) from exc
    if probe.returncode != 0 or "|" not in probe.stdout:
        raise OpenROADV2Error(
            "the image does not expose a detectable OpenROAD-flow-scripts checkout; "
            "provide --orfs-root with a clean pinned checkout"
        )
    flow_path, revision = probe.stdout.splitlines()[-1].split("|", 1)
    label_revision = str(labels.get("org.opencontainers.image.revision") or "")
    evidence_revision = revision or label_revision
    if evidence_revision != ORFS_COMMIT:
        raise OpenROADV2Error(
            f"ORFS image must prove commit {ORFS_COMMIT}; got {evidence_revision or 'none'}"
        )
    return {
        "kind": "image_checkout",
        "container_flow_path": flow_path,
        "commit": evidence_revision,
        "commit_evidence": "git" if revision else "OCI label",
    }


def _image_tool_provenance(image_id: str) -> dict[str, Any]:
    script = (
        "y=/OpenROAD-flow-scripts/tools/install/yosys/bin/yosys; "
        "[ -x \"$y\" ] || y=$(command -v yosys 2>/dev/null || true); "
        "o=/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad; "
        "[ -x \"$o\" ] || o=$(command -v openroad 2>/dev/null || true); "
        "k=$(command -v klayout 2>/dev/null || true); "
        "t=$(command -v time 2>/dev/null || true); "
        "printf 'yosys|%s\\nopenroad|%s\\nklayout|%s\\ntime|%s\\n' "
        "\"$y\" \"$o\" \"$k\" \"$t\"; "
        "\"$y\" -V 2>/dev/null | head -1; \"$o\" -version 2>/dev/null | head -1"
    )
    try:
        probe = run_command(
            ("docker", "run", "--rm", "--entrypoint", "sh", image_id, "-c", script),
            timeout_seconds=60,
        )
    except ToolExecutionError as exc:
        raise OpenROADV2Error(str(exc)) from exc
    if probe.returncode != 0:
        raise OpenROADV2Error(probe.stderr or "could not inspect image tools")
    lines = probe.stdout.splitlines()
    paths = {}
    for line in lines[:4]:
        name, separator, path = line.partition("|")
        if not separator or (name != "time" and not path):
            raise OpenROADV2Error(f"locked image is missing required tool: {name}")
        paths[name] = path
    return {
        "paths": paths,
        "yosys_version": lines[4] if len(lines) > 4 else "unknown",
        "openroad_version": lines[5] if len(lines) > 5 else "unknown",
    }


def create_openroad_lock(
    config: ProjectConfig,
    *,
    image: str = DEFAULT_ORFS_IMAGE,
    orfs_root: str | Path | None = None,
) -> Path:
    if config.liberty.source_commit != ORFS_COMMIT:
        raise OpenROADV2Error(
            "Nangate45 Liberty source commit does not match the pinned ORFS commit"
        )
    plan_path = create_openroad_plan(config)
    plan = _load_json(plan_path)
    docker_version = _docker_json(
        ("docker", "version", "--format", "{{json .}}"), timeout_seconds=30
    )
    inspection = _docker_json(
        ("docker", "image", "inspect", image), timeout_seconds=30
    )
    if not isinstance(inspection, list) or len(inspection) != 1:
        raise OpenROADV2Error(f"expected one Docker image for {image}")
    image_info = inspection[0]
    image_id = str(image_info.get("Id") or "")
    if not image_id.startswith("sha256:"):
        raise OpenROADV2Error(f"Docker image {image} has no immutable image ID")
    labels = ((image_info.get("Config") or {}).get("Labels") or {})
    provenance = (
        _host_orfs_provenance(Path(orfs_root))
        if orfs_root is not None
        else _image_orfs_provenance(image_id, labels)
    )
    core = {
        "schema_version": OPENROAD_LOCK_SCHEMA_VERSION,
        "flow_version": OPENROAD_FLOW_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "orfs_commit": ORFS_COMMIT,
        "orfs_source": provenance,
        "plan": {
            "path": str(plan_path.resolve()),
            "file_sha256": _file_hash(plan_path),
            "plan_hash": plan["plan_hash"],
            "run_count": plan["run_count"],
        },
        "image": {
            "requested_reference": image,
            "id": image_id,
            "repo_digests": image_info.get("RepoDigests") or [],
            "os": image_info.get("Os"),
            "architecture": image_info.get("Architecture"),
            "labels": labels,
            "tools": _image_tool_provenance(image_id),
        },
        "docker_version": docker_version,
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "liberty": {
            "path": str(config.liberty.path.resolve()),
            "sha256": _file_hash(config.liberty.path),
            "expected_sha256": config.liberty.sha256,
            "source_commit": config.liberty.source_commit,
        },
    }
    if core["liberty"]["sha256"] != config.liberty.sha256:
        raise OpenROADV2Error("Nangate45 Liberty hash does not match project configuration")
    lock = {**core, "lock_hash": _json_hash(core)}
    path = config.artifacts_dir / "openroad/v2/lock.json"
    _write_json(path, lock)
    return path


def verify_openroad_lock(config: ProjectConfig, path: str | Path | None = None) -> dict[str, Any]:
    lock_path = Path(path or config.artifacts_dir / "openroad/v2/lock.json").resolve()
    lock = _load_json(lock_path)
    core = {key: value for key, value in lock.items() if key != "lock_hash"}
    if lock.get("lock_hash") != _json_hash(core):
        raise OpenROADV2Error("OpenROAD lock content hash mismatch")
    if lock.get("orfs_commit") != ORFS_COMMIT:
        raise OpenROADV2Error("OpenROAD lock uses the wrong ORFS commit")
    plan_item = lock["plan"]
    plan_path = Path(plan_item["path"])
    if not plan_path.is_file() or _file_hash(plan_path) != plan_item["file_sha256"]:
        raise OpenROADV2Error("OpenROAD plan changed after lock")
    plan = _load_json(plan_path)
    if plan.get("plan_hash") != plan_item["plan_hash"]:
        raise OpenROADV2Error("OpenROAD plan hash changed after lock")
    time_shim = Path(plan["time_shim"]["path"])
    if not time_shim.is_file() or _file_hash(time_shim) != plan["time_shim"]["sha256"]:
        raise OpenROADV2Error("OpenROAD portable time shim changed after lock")
    metadata_rules = Path(plan["metadata_rules"]["path"])
    if (
        not metadata_rules.is_file()
        or _file_hash(metadata_rules) != plan["metadata_rules"]["sha256"]
    ):
        raise OpenROADV2Error("OpenROAD metadata rules changed after lock")
    if _file_hash(config.liberty.path) != lock["liberty"]["sha256"]:
        raise OpenROADV2Error("locked Liberty file changed")
    inspection = _docker_json(
        ("docker", "image", "inspect", lock["image"]["id"]), timeout_seconds=30
    )
    if not inspection or inspection[0].get("Id") != lock["image"]["id"]:
        raise OpenROADV2Error("locked Docker image ID is unavailable")
    source = lock["orfs_source"]
    if source["kind"] == "host_checkout":
        current = _host_orfs_provenance(Path(source["host_path"]))
        if current["tree"] != source["tree"]:
            raise OpenROADV2Error("locked ORFS source tree changed")
    return lock


def _find_metrics(work_root: Path) -> Path | None:
    candidates = sorted(
        [*work_root.rglob("metrics.json"), *work_root.rglob("metadata.json")]
    )
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def _route_finished(work_root: Path) -> bool:
    return any(work_root.rglob("*final*.gds")) or any(work_root.rglob("6_final.gds"))


def _run_one(
    config: ProjectConfig,
    lock: dict[str, Any],
    run: dict[str, Any],
    *,
    retry_failed: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    root = config.artifacts_dir / "openroad/v2"
    result_path = root / "results" / f"{run['run_id']}.json"
    if result_path.is_file():
        previous = _load_json(result_path)
        if previous.get("lock_hash") != lock["lock_hash"]:
            raise OpenROADV2Error(
                f"result {result_path} belongs to a different OpenROAD lock"
            )
        if previous.get("usable"):
            return {**previous, "cached": True}
        if not retry_failed:
            return {**previous, "cached": True, "retry_required": True}
    work_root = root / "work" / run["run_id"]
    log_root = root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    source = lock["orfs_source"]
    flow_root = source["container_flow_path"]
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
            flow_root,
            lock["image"]["id"],
            "make",
            "--no-print-directory",
            f"DESIGN_CONFIG={_container_path(config, Path(run['config']))}",
            f"FLOW_VARIANT={run['run_id']}",
            f"WORK_HOME={_container_path(config, work_root)}",
            "TIME_BIN=" + (
                lock["image"]["tools"]["paths"].get("time")
                or _container_path(config, root / "bin/time")
            ),
            f"RULES_JSON={_container_path(config, Path(plan_metadata_rules(lock)))}",
            "finish",
            "metadata",
        )
    )
    started = datetime.now(timezone.utc).isoformat()
    try:
        completed = run_command(tuple(command), timeout_seconds=timeout_seconds)
        stdout, stderr = completed.stdout, completed.stderr
        returncode = completed.returncode
        error = None
    except ToolExecutionError as exc:
        stdout, stderr, returncode, error = "", "", None, str(exc)
    stdout_path = log_root / f"{run['run_id']}.stdout.log"
    stderr_path = log_root / f"{run['run_id']}.stderr.log"
    stdout_path.write_text(stdout + ("\n" if stdout else ""), encoding="utf-8")
    stderr_path.write_text(stderr + ("\n" if stderr else ""), encoding="utf-8")
    metrics_path = _find_metrics(work_root)
    metrics = None
    metrics_error = None
    if metrics_path is not None:
        try:
            metrics = parse_openroad_metrics(metrics_path)
        except OpenROADV2Error as exc:
            metrics_error = str(exc)
    route_finished = _route_finished(work_root)
    slack = metrics.get("worst_slack_ns") if metrics else None
    required_metrics = bool(
        metrics
        and slack is not None
        and math.isfinite(float(slack))
        and -1000.0 < float(slack) < 10.0
        and metrics["cell_area_um2"] is not None
        and metrics["cell_count"] is not None
        and metrics["drc_count"] is not None
    )
    drc_clean = bool(metrics and metrics["drc_count"] == 0)
    usable = returncode == 0 and route_finished and required_metrics and drc_clean
    result = {
        "schema_version": OPENROAD_RESULT_SCHEMA_VERSION,
        "flow_version": OPENROAD_FLOW_VERSION,
        "lock_hash": lock["lock_hash"],
        "run_id": run["run_id"],
        "case_id": run["case_id"],
        "family": run["family"],
        "variant_id": run["variant_id"],
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "attempt_kind": "audited_retry" if retry_failed else "initial",
        "returncode": returncode,
        "route_finished": route_finished,
        "metrics_path": str(metrics_path) if metrics_path else None,
        "metrics": metrics,
        "metrics_error": metrics_error,
        "drc_clean": drc_clean,
        "usable": usable,
        "status": "usable" if usable else "failed",
        "error": error,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "cached": False,
    }
    _write_json(result_path, result)
    return result


def plan_metadata_rules(lock: dict[str, Any]) -> str:
    """Return the metadata-rule path covered by the locked plan hash."""
    plan = _load_json(Path(lock["plan"]["path"]))
    return str(plan["metadata_rules"]["path"])


def run_openroad_v2(
    config: ProjectConfig,
    *,
    workers: int = 2,
    retry_failed: bool = False,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    if workers < 1 or workers > 8:
        raise OpenROADV2Error("OpenROAD workers must be between 1 and 8")
    lock = verify_openroad_lock(config)
    plan = _load_json(Path(lock["plan"]["path"]))
    runs = list(plan.get("runs") or [])
    if len(runs) != 108:
        raise OpenROADV2Error(f"locked OpenROAD plan must contain 108 runs, got {len(runs)}")
    root = config.artifacts_dir / "openroad/v2"
    if retry_failed:
        audit_path = root / "retry-audit.json"
        audit = _load_json(audit_path) if audit_path.is_file() else {
            "schema_version": 1,
            "events": [],
        }
        failed_ids = []
        for run in runs:
            result_path = root / "results" / f"{run['run_id']}.json"
            if result_path.is_file():
                result = _load_json(result_path)
                if result.get("lock_hash") != lock["lock_hash"]:
                    raise OpenROADV2Error(
                        f"result {result_path} belongs to a different OpenROAD lock"
                    )
                if not result.get("usable"):
                    failed_ids.append(run["run_id"])
        event = {
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "lock_hash": lock["lock_hash"],
            "failed_run_ids": failed_ids,
            "failed_run_set_hash": _json_hash(failed_ids),
        }
        audit["events"].append(event)
        _write_json(audit_path, audit)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _run_one,
                config,
                lock,
                run,
                retry_failed=retry_failed,
                timeout_seconds=timeout_seconds,
            ): run
            for run in runs
        }
        for future in as_completed(future_map):
            results.append(future.result())
    results.sort(key=lambda row: row["run_id"])
    summary = {
        "schema_version": 1,
        "flow_version": OPENROAD_FLOW_VERSION,
        "lock_hash": lock["lock_hash"],
        "run_count": len(results),
        "usable_count": sum(bool(row["usable"]) for row in results),
        "failed_count": sum(not bool(row["usable"]) for row in results),
        "fresh_count": sum(not bool(row.get("cached")) for row in results),
        "cached_count": sum(bool(row.get("cached")) for row in results),
        "retry_required_count": sum(bool(row.get("retry_required")) for row in results),
        "retry_failed": retry_failed,
        "workers": workers,
        "results": results,
    }
    summary["status"] = "passed" if summary["usable_count"] == len(results) else "incomplete"
    summary_path = root / "run-summary.json"
    summary["summary_path"] = str(summary_path.resolve())
    _write_json(summary_path, summary)
    return summary


def parse_openroad_metrics(path: str | Path) -> dict[str, float | int | None]:
    metrics_path = Path(path)
    try:
        raw = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenROADV2Error(f"invalid OpenROAD metrics {metrics_path}: {exc}") from exc

    def first(*keys: str) -> Any:
        for key in keys:
            if key in raw:
                return raw[key]
        return None

    def number(value: Any, *, integer: bool = False) -> float | int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(float(value)) if integer else float(value)
        except (TypeError, ValueError):
            return None

    return {
        "worst_slack_ns": number(first("timing__setup__ws", "finish__timing__setup__ws")),
        "cell_area_um2": number(first("design__instance__area", "finish__design__instance__area")),
        "cell_count": number(
            first(
                "finish__design__instance__count__stdcell",
                "design__instance__count",
                "finish__design__instance__count",
            ),
            integer=True,
        ),
        "wirelength_um": number(
            first(
                "detailedroute__route__wirelength",
                "route__wirelength",
                "finish__route__wirelength",
            )
        ),
        "drc_count": number(
            first(
                "detailedroute__route__drc_errors",
                "route__drc_errors",
                "finish__route__drc_errors",
            ),
            integer=True,
        ),
        "congestion_overflow": number(
            first("route__detailed__congestion", "finish__route__detailed__congestion")
        ),
    }


def _improvements(baseline: dict[str, float], candidate: dict[str, float]) -> dict[str, float]:
    values = {}
    for metric in _METRICS:
        denominator = baseline[metric]
        if denominator <= 0:
            raise OpenROADV2Error(f"baseline {metric} must be positive")
        values[metric] = 100.0 * (denominator - candidate[metric]) / denominator
    return values


def _direction(value: float) -> str:
    """Classify a percent improvement using the frozen V2 neutral band."""
    if value > 1.0:
        return "improve"
    if value < -1.0:
        return "degrade"
    return "neutral"


def _best_actions(comparisons: dict[str, dict[str, float]]) -> set[str]:
    profile = PROFILES["balanced"]
    eligible = {
        variant: metrics
        for variant, metrics in comparisons.items()
        if profile.eligible(metrics["delay"], metrics["area"])
    }
    if not eligible:
        return {"abstain"}
    utilities = {
        variant: profile.utility(metrics["delay"], metrics["area"], metrics["cell_count"])
        for variant, metrics in eligible.items()
    }
    best = max(utilities.values())
    return {variant for variant, value in utilities.items() if abs(value - best) <= 1e-6}


def evaluate_physical_gate(case_rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [row for row in case_rows if row["complete"]]
    per_family = Counter(row["family"] for row in complete)
    action_agreement = (
        sum(row["action_agreement"] for row in complete) / len(complete) if complete else 0.0
    )
    direction = {}
    for metric in _METRICS:
        pairs = [
            pair
            for row in complete
            for pair in row["direction_pairs"]
            if pair["metric"] == metric
        ]
        direction[metric] = {
            "pair_count": len(pairs),
            "agreement": (
                sum(pair["agreement"] for pair in pairs) / len(pairs) if pairs else 0.0
            ),
        }
    checks = {
        "complete_case_count": len(complete) >= PHYSICAL_GATE_MINIMUM_COMPLETE_CASES,
        "family_coverage": bool(per_family)
        and all(count >= PHYSICAL_GATE_MINIMUM_FAMILY_CASES for count in per_family.values())
        and len(per_family) == 9,
        "candidate_action_agreement": action_agreement >= PHYSICAL_GATE_ACTION_AGREEMENT,
        **{
            f"{metric}_direction_agreement": direction[metric]["agreement"]
            >= PHYSICAL_GATE_DIRECTION_AGREEMENT
            for metric in _METRICS
        },
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "complete_case_count": len(complete),
        "required_complete_case_count": PHYSICAL_GATE_MINIMUM_COMPLETE_CASES,
        "complete_cases_per_family": dict(sorted(per_family.items())),
        "minimum_cases_per_family": PHYSICAL_GATE_MINIMUM_FAMILY_CASES,
        "candidate_action_agreement": action_agreement,
        "required_candidate_action_agreement": PHYSICAL_GATE_ACTION_AGREEMENT,
        "direction_agreement": direction,
        "required_direction_agreement": PHYSICAL_GATE_DIRECTION_AGREEMENT,
    }


def build_openroad_report(config: ProjectConfig) -> dict[str, Any]:
    lock = verify_openroad_lock(config)
    plan = _load_json(Path(lock["plan"]["path"]))
    root = config.artifacts_dir / "openroad/v2"
    by_case: dict[str, list[dict[str, Any]]] = {}
    for run in plan["runs"]:
        by_case.setdefault(run["case_id"], []).append(run)
    case_rows = []
    for case_id, runs in sorted(by_case.items()):
        result_by_variant = {}
        for run in runs:
            path = root / "results" / f"{run['run_id']}.json"
            if path.is_file():
                result = _load_json(path)
                if result.get("lock_hash") != lock["lock_hash"]:
                    raise OpenROADV2Error(
                        f"result {path} belongs to a different OpenROAD lock"
                    )
                result_by_variant[run["variant_id"]] = result
        complete = all(
            variant in result_by_variant and result_by_variant[variant].get("usable")
            for variant in _VARIANTS
        )
        row: dict[str, Any] = {
            "case_id": case_id,
            "family": runs[0]["family"],
            "crosscheck_source": runs[0]["crosscheck_source"],
            "complete": complete,
            "usable_variants": sorted(
                variant for variant, result in result_by_variant.items() if result.get("usable")
            ),
            "action_agreement": False,
            "direction_pairs": [],
        }
        if complete:
            physical_absolute = {
                variant: {
                    "delay": 10.0 - float(result_by_variant[variant]["metrics"]["worst_slack_ns"]),
                    "area": float(result_by_variant[variant]["metrics"]["cell_area_um2"]),
                    "cell_count": float(result_by_variant[variant]["metrics"]["cell_count"]),
                }
                for variant in _VARIANTS
            }
            physical = {
                variant: _improvements(physical_absolute["v0"], physical_absolute[variant])
                for variant in ("v1", "v2", "v3")
            }
            synthesis_summary = _summary(config, case_id)
            synthesis = {
                comparison["candidate_id"]: {
                    "delay": float(comparison["critical_delay_ps"]["improvement_percent"]),
                    "area": float(comparison["area_total"]["improvement_percent"]),
                    "cell_count": float(comparison["cell_count"]["improvement_percent"]),
                }
                for comparison in synthesis_summary["comparisons"]
            }
            physical_actions = _best_actions(physical)
            synthesis_actions = _best_actions(synthesis)
            row.update(
                {
                    "physical_improvement_percent": physical,
                    "synthesis_improvement_percent": synthesis,
                    "physical_best_actions": sorted(physical_actions),
                    "synthesis_best_actions": sorted(synthesis_actions),
                    "action_agreement": bool(physical_actions & synthesis_actions),
                    "direction_pairs": [
                        {
                            "variant_id": variant,
                            "metric": metric,
                            "synthesis": _direction(synthesis[variant][metric]),
                            "physical": _direction(physical[variant][metric]),
                            "agreement": _direction(synthesis[variant][metric])
                            == _direction(physical[variant][metric]),
                        }
                        for variant in ("v1", "v2", "v3")
                        for metric in _METRICS
                    ],
                }
            )
        case_rows.append(row)
    gate = evaluate_physical_gate(case_rows)
    core = {
        "schema_version": OPENROAD_REPORT_SCHEMA_VERSION,
        "flow_version": OPENROAD_FLOW_VERSION,
        "lock_hash": lock["lock_hash"],
        "case_count": len(case_rows),
        "cases": case_rows,
        "physical_evidence_gate": gate,
        "blind_v21_allowed": gate["passed"],
    }
    report = {**core, "report_hash": _json_hash(core)}
    json_path = root / "report.json"
    markdown_path = root / "report.md"
    report["json_path"] = str(json_path.resolve())
    report["markdown_path"] = str(markdown_path.resolve())
    _write_json(json_path, report)
    markdown = [
        "# OpenROAD V2 Physical Cross-check",
        "",
        f"Physical-evidence gate: **{'PASS' if gate['passed'] else 'FAIL'}**",
        "",
        f"- Complete cases: {gate['complete_case_count']}/27 (required {gate['required_complete_case_count']})",
        f"- Candidate-action agreement: {gate['candidate_action_agreement']:.1%} (required 80%)",
    ]
    for metric in _METRICS:
        markdown.append(
            f"- {metric} direction agreement: "
            f"{gate['direction_agreement'][metric]['agreement']:.1%} (required 75%)"
        )
    markdown.extend(("", f"Report hash: `{report['report_hash']}`", ""))
    markdown_path.write_text("\n".join(markdown), encoding="utf-8")
    return report
