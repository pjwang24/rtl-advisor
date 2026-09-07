"""Provenance and post-run audits for the installed-plugin A/B experiment."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from scripts.plugin_abc_measured import (
    ROOT, MeasuredRunError, _load, _sha256, _tree_sha256,
)


def _identity(experiment: Path) -> dict[str, Any]:
    manifest = _load(experiment / "manifest.json")
    sources = {str((ROOT / path).resolve()): _sha256(ROOT / path)
               for case in manifest["cases"] for path in case["sources"]}
    for case in manifest["cases"]:
        if sources[str((ROOT / case["sources"][0]).resolve())] != case["source_sha256"]:
            raise MeasuredRunError(f"frozen source changed: {case['case_id']}")
    return {
        "sources": sources,
        "source_tree_sha256": _tree_sha256(ROOT / "src/rtl_advisor"),
        "plugin_tree_sha256": _tree_sha256(ROOT / "plugins/rtl-advisor"),
        "harness_sha256": _sha256(ROOT / "scripts/plugin_abc_measured.py"),
        "audit_harness_sha256": _sha256(Path(__file__)),
        "config_sha256": _sha256(ROOT / "rtl-advisor.toml"),
        "manifest_sha256": _sha256(experiment / "manifest.json"),
        "canonical_run_tree_sha256": _tree_sha256(experiment / "runs"),
    }


def preflight(experiment: Path, series_root: Path, arm: str,
              repetition: int, codex_bin: str) -> dict[str, Any]:
    version = _load(ROOT / "plugins/rtl-advisor/.codex-plugin/plugin.json")["version"]
    plugin = Path.home() / ".codex/plugins/cache/personal/rtl-advisor" / version
    if (
        not plugin.is_dir()
        or _tree_sha256(plugin) != _tree_sha256(ROOT / "plugins/rtl-advisor")
    ):
        raise MeasuredRunError("installed plugin differs from the repository checkpoint")
    inventory = subprocess.run(
        [codex_bin, "plugin", "list"], capture_output=True, text=True, check=True
    ).stdout
    if not any("rtl-advisor@personal" in line and "installed, enabled" in line
               and version in line for line in inventory.splitlines()):
        raise MeasuredRunError("expected installed plugin is not enabled")
    identity = _identity(experiment)
    identity["installed_plugin_tree_sha256"] = _tree_sha256(plugin)
    identity["tool_versions"] = {
        name: subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        for name, command in {
            "codex": [codex_bin, "--version"], "yosys": ["yosys", "-V"],
            "verilator": ["verilator", "--version"],
        }.items()
    }
    pin_path = series_root / "source-pin.json"
    if pin_path.exists():
        if _load(pin_path) != identity:
            raise MeasuredRunError("experiment source or tool identity changed; start a new series")
    else:
        with pin_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    order = [("A", 1), ("B", 1), ("A", 2), ("B", 2)]
    index = order.index((arm, repetition))
    telemetry = experiment / "evaluations/installed-acceptance" / series_root.name / "telemetry.json"
    rows = _load(telemetry)["runs"] if telemetry.exists() else []
    if any(not any(row["arm"] == a and row["repetition"] == r and row["task_completed"]
                   for row in rows) for a, r in order[:index]):
        raise MeasuredRunError("complete the preceding A1/B1/A2/B2 samples first")
    snapshot = snapshot_artifacts(series_root / "candidate/artifacts")
    if arm == "B" and repetition == 1 and snapshot:
        raise MeasuredRunError("B1 requires cold artifacts; interrupted cold execution needs a new series")
    if arm == "B" and repetition == 2 and not snapshot:
        raise MeasuredRunError("B2 requires the completed B1 artifacts")
    return {
        "identity": identity, "experiment": str(experiment), "plugin_version": version,
        "installed_skill": str(plugin / "skills/analyze-rtl/SKILL.md"),
        "installed_runner": str(plugin / "skills/analyze-rtl/scripts/run_rtl_advisor.py"),
        "skill_sha256": _sha256(plugin / "skills/analyze-rtl/SKILL.md"),
        "runner_sha256": _sha256(plugin / "skills/analyze-rtl/scripts/run_rtl_advisor.py"),
        "arm": arm, "repetition": repetition,
    }


def snapshot_artifacts(root: Path) -> dict[str, str]:
    """Hash immutable evidence; latest pointers and report aliases may advance."""
    return {str(path.relative_to(root)): _sha256(path)
            for path in sorted(root.rglob("*")) if path.is_file()
            and not path.name.endswith("-latest.json")
            and path.name not in {"latest.json", "report.json", "report.html"}}


def _installed_batch_commands(
    commands: list[dict[str, Any]], runner: str
) -> list[dict[str, Any]]:
    """Return executions, not commands that merely inspect the runner source."""
    invocation = f"python3 {runner}"
    return [
        item for item in commands
        if invocation in item["command"] and "workflow batch" in item["command"]
    ]


def _compact_batch_digest(command: dict[str, Any]) -> bool:
    output = command.get("aggregated_output", "")
    if not isinstance(output, str) or len(output.encode("utf-8")) > 32 * 1024:
        return False
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    items = payload.get("items")
    forbidden = {"source_text", "candidate_text", "stdout", "stderr", "report"}
    return (
        payload.get("document_type") == "rtl-advisor.workflow.batch-summary"
        and payload.get("status") == "completed"
        and isinstance(items, list)
        and len(items) == 24
        and all(isinstance(item, dict) and forbidden.isdisjoint(item) for item in items)
    )


def audit_run(provenance: dict[str, Any], events_path: Path,
              config: Path | None, before: dict[str, str]) -> dict[str, Any]:
    events = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    commands = [event["item"] for event in events if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "command_execution"]
    texts = [item["command"] for item in commands]
    current = _identity(Path(provenance["experiment"]))
    checks = {"source_and_harness_unchanged": all(
        value == provenance["identity"][key] for key, value in current.items())}
    batch_commands = _installed_batch_commands(commands, provenance["installed_runner"])
    runner_inspections = [
        text for text in texts
        if provenance["installed_runner"] in text
        and not any(text == item["command"] for item in batch_commands)
    ]
    contract_reads = [text for text in texts if "references/cli-contract.md" in text]
    skill_reads = [text for text in texts if provenance["installed_skill"] in text]
    details: dict[str, Any] = {
        "batch_commands": [item["command"] for item in batch_commands],
        "runner_inspections": runner_inspections,
        "contract_reads": contract_reads,
        "commands": texts,
    }
    if provenance["arm"] == "A":
        checks["no_plugin_runner"] = not any("run_rtl_advisor.py" in text for text in texts)
    else:
        checks["installed_skill_loaded_once"] = len(skill_reads) == 1
        checks["one_installed_batch_call"] = len(batch_commands) == 1
        checks["explicit_measure_authorization"] = len(batch_commands) == 1 and all(
            flag in batch_commands[0]["command"]
            for flag in ("--authorized-through measure", "--first-eligible", "--jobs 4")
        )
        checks["compact_batch_digest"] = (
            len(batch_commands) == 1 and _compact_batch_digest(batch_commands[0])
        )
        checks["no_redundant_plugin_artifact_loading"] = (
            not runner_inspections and not contract_reads
        )
        checks["installed_runner_unchanged"] = (
            _sha256(Path(provenance["installed_runner"]))
            == provenance["runner_sha256"]
        )
        assert config is not None
        after = snapshot_artifacts(config.parent / "artifacts")
        checks["prior_immutable_evidence_preserved"] = all(
            after.get(path) == digest for path, digest in before.items()
        )
        details["prior_immutable_file_count"] = len(before)
        details["after_immutable_file_count"] = len(after)
        # Audit the CLI-owned results locally, without loading them into the benchmark thread.
        summaries = list(events_path.parent.rglob("summary.json"))
        summaries += list((config.parent / "artifacts/workflow-batches-v1").glob("*/summary.json"))
        batches = [_load(path) for path in summaries]
        batches = [
            item
            for item in batches
            if item.get("document_type") == "rtl-advisor.workflow.batch-summary"
        ]
        checks["complete_batch_evidence"] = bool(batches) and all(
            item["status"] == "completed" and item["counts"]["items"] == 24 and item["counts"]["failed"] == 0
            for item in batches)
        checks["formal_before_measurement"] = bool(batches) and all(
            row.get("measurement_status") == "not_run"
            or (row.get("formal_status") == "formal_passed" and row.get("safe") is True)
            for item in batches for row in item["items"])
        details["batch_results"] = [
            {"item_id": row["item_id"], "decision": row.get("decision"),
             "formal_status": row.get("formal_status"), "measurement_status": row.get("measurement_status")}
            for row in (batches[-1]["items"] if batches else [])]
    return {"passed": all(checks.values()), "checks": checks, **details}
