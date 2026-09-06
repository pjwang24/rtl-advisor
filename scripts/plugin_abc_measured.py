#!/usr/bin/env python3
"""Run a frozen plugin A/B packet and capture authoritative Codex telemetry."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.plugin_abc import (
    DEFAULT_EXPERIMENT,
    FrameworkError,
    validate_arm_result,
)


TELEMETRY_SCHEMA = "rtl-advisor-plugin-abc-telemetry-v1"
SUPPORTED_ARMS = {"A", "B"}


class MeasuredRunError(RuntimeError):
    """Raised when a measured Codex run is unsafe or incomplete."""


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasuredRunError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MeasuredRunError(f"{path} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _phase7_source_pin(arm: str, phase: str) -> tuple[list[str], dict[str, str]]:
    if phase != "phase7" or arm != "B":
        return [], {}
    skill_path = (
        ROOT / "plugins/rtl-advisor/skills/analyze-rtl"
    ).resolve()
    if not (skill_path / "SKILL.md").is_file():
        raise MeasuredRunError(f"Phase 7 source-pinned skill is missing: {skill_path}")
    skill_text = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    instructions = (
        "Benchmark-only source pin. Apply the following analyze-rtl skill exactly. "
        f"Interpret <skill-dir> as {skill_path}. Do not use any installed or cached "
        "RTL Advisor plugin instructions.\n\n"
        + skill_text
    )
    config_value = "developer_instructions=" + json.dumps(instructions)
    return [
        "--ignore-user-config",
        "--strict-config",
        "--config",
        config_value,
    ], {
        "skill_source": "workspace_path",
        "skill_path": str(skill_path),
        "skill_sha256": _tree_sha256(skill_path),
    }


def _phase7_candidate_config(experiment: Path, *, root: Path | None = None) -> Path:
    """Pin corrected B repetitions to one isolated cold-then-warm artifact root."""
    source_path = ROOT / "rtl-advisor.toml"
    with source_path.open("rb") as stream:
        source = tomllib.load(stream)
    root = root or (
        experiment
        / "instrumented/phase7/release-candidate-jobs4-v1"
    ).resolve()
    config_path = root / "rtl-advisor.toml"
    project = source["project"]
    tools = source["tools"]
    synthesis = source["synthesis"]
    codex = source.get("codex", {})
    liberty = source["liberty"]
    quote = lambda value: json.dumps(str(value))
    content = "\n".join(
        (
            "[project]",
            f"artifacts_dir = {quote(root / 'artifacts')}",
            f"corpus_dir = {quote((ROOT / str(project['corpus_dir'])).resolve())}",
            "",
            "[tools]",
            f"verilator = {quote(tools['verilator'])}",
            f"yosys = {quote(tools['yosys'])}",
            f"codex = {quote(tools['codex'])}",
            f"timeout_seconds = {int(tools['timeout_seconds'])}",
            "",
            "[synthesis]",
            f"driving_cell = {quote(synthesis['driving_cell'])}",
            f"output_load_ff = {float(synthesis['output_load_ff'])}",
            "",
            "[codex]",
            f"model = {quote(codex.get('model', 'gpt-5.6-sol'))}",
            f"default_effort = {quote(codex.get('default_effort', 'xhigh'))}",
            f"timeout_seconds = {int(codex.get('timeout_seconds', 600))}",
            "",
            "[liberty]",
            f"name = {quote(liberty['name'])}",
            f"path = {quote((ROOT / str(liberty['path'])).resolve())}",
            f"url = {quote(liberty['url'])}",
            f"sha256 = {quote(liberty['sha256'])}",
            f"license_path = {quote((ROOT / str(liberty['license_path'])).resolve())}",
            f"license_url = {quote(liberty['license_url'])}",
            f"source_commit = {quote(liberty['source_commit'])}",
            "",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    if config_path.is_file() and config_path.read_text(encoding="utf-8") != content:
        raise MeasuredRunError(f"Phase 7 candidate config changed: {config_path}")
    if not config_path.is_file():
        config_path.write_text(content, encoding="utf-8")
    return config_path


def _prompt(
    *, packet_path: Path, output_root: Path, result_path: Path, arm: str, phase: str,
    candidate_config: Path | None = None,
) -> str:
    access_reminder = {
        "A": (
            "RTL Advisor plugins are disabled for this invocation. Use ordinary RTL "
            "reasoning and raw EDA tools only."
        ),
        "B": (
            (
                "Use the source-pinned workspace analyze-rtl skill and its local CLI "
                "exactly; do not inspect or use any installed marketplace copy. "
                "This Phase 7 run explicitly prioritizes lower latency: execute the "
                "independent batch workflows with --jobs 4."
                if phase == "phase7"
                else "Use the installed rtl-advisor:analyze-rtl skill and its released "
                "CLI exactly; do not invent unsupported transformations."
            )
        ),
    }[arm]
    access_scope = (
        "The packet's installed-plugin access sentence is replaced only for this "
        "Phase 7 measurement by the source-pin instruction below."
        if arm == "B" and phase == "phase7"
        else "The packet access rule remains unchanged."
    )
    if phase == "installed-acceptance" and arm == "B":
        access_reminder += (
            " This run explicitly prioritizes lower latency: execute the independent "
            "batch workflows with --jobs 4. Review, first-eligible candidate preparation, "
            "formal verification, and both pinned synthesis measurements are authorized. "
            "The installed SKILL.md contains the complete command contract needed for this "
            "run. Invoke its `python3 <skill-dir>/scripts/run_rtl_advisor.py --config ... "
            "workflow batch ...` form exactly once and use its compact JSON digest directly. "
            "Do not open cli-contract.md or inspect the runner source unless that invocation "
            "returns a structured failure requiring diagnosis."
        )
    config_scope = (
        f"Every RTL Advisor runner invocation must pass --config {candidate_config}. "
        "CLI-managed workflow artifacts may be written only under that config's "
        "artifacts_dir; write all model-created session files under the run directory."
        if candidate_config is not None
        else ""
    )
    output_scope = (
        "- Write model-created manifests, authorization evidence, summaries, and "
        f"scratch files only under {output_root}."
        if candidate_config is not None
        else f"- Write candidates, evidence, and scratch files only under {output_root}."
    )
    return f"""Execute the frozen A/B benchmark packet at {packet_path}.

Read the packet completely and obey its cases, access rule, model contract,
decision vocabulary, proof rules, and manifest order. {access_scope}
{access_reminder}
{config_scope}

For this instrumented repetition only, the packet's run-directory isolation
sentence is replaced by these stricter paths:
- Do not read any existing experiments/plugin-abc-v1/runs/arm-* directory.
- Do not read any other instrumented arm or repetition.
{output_scope}
- Do not mutate any baseline source or frozen packet.

All generated cases and the packet's pinned open-source cases are explicitly
authorized for this local benchmark. Do not read the oracle or any evaluation
artifact. Complete every assigned case in one session. Return only one JSON
object matching the packet's result schema, with its exact experiment_id, arm,
repetition, model, reasoning_effort, manifest_sha256, and ordered case list.
The final JSON will be stored at {result_path} by the harness.
"""


def _instrumented_packet(packet: Mapping[str, Any], output_root: Path) -> Path:
    """Materialize the frozen packet with only its conflicting path rules replaced."""
    effective = dict(packet)
    effective["isolation_rules"] = [
        "Do not read experiments/plugin-abc-v1/oracle.json.",
        "Do not read or write any experiments/plugin-abc-v1/runs/arm-* directory.",
        "Do not read any other instrumented arm, repetition, or attempt.",
        "Write all candidates, evidence, manifests, summaries, and scratch files "
        f"only under {output_root}.",
        "Do not mutate baseline RTL or any frozen packet.",
        "Do not recommend any unproven candidate.",
        "Report every case in manifest order, including unsupported and no-change outcomes.",
    ]
    path = output_root / "effective-packet.json"
    path.write_text(json.dumps(effective, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _parse_events(path: Path) -> tuple[dict[str, int], str | None]:
    usage: dict[str, int] | None = None
    thread_id: str | None = None
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MeasuredRunError(
                f"malformed Codex JSONL at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(event, Mapping):
            raise MeasuredRunError(f"Codex event {line_number} is not an object")
        if event.get("type") == "thread.started" and isinstance(
            event.get("thread_id"), str
        ):
            thread_id = str(event["thread_id"])
        if event.get("type") == "turn.completed":
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, Mapping):
                raise MeasuredRunError("turn.completed event has no usage object")
            usage = {}
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                value = raw_usage.get(name, 0)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise MeasuredRunError(f"invalid Codex usage field {name}")
                usage[name] = value
    if usage is None:
        raise MeasuredRunError("Codex JSONL has no completed turn with usage")
    return usage, thread_id


def _terminal_failure(path: Path) -> dict[str, str] | None:
    """Return the last authoritative Codex failure without inventing usage."""
    failure: dict[str, str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "error" and isinstance(event.get("message"), str):
            failure = {"event_type": "error", "message": str(event["message"])}
        elif event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                failure = {
                    "event_type": "turn.failed",
                    "message": str(error["message"]),
                }
    return failure


def _write_attempt_failure(
    path: Path,
    *,
    arm: str,
    repetition: int,
    attempt: int,
    elapsed: float,
    exit_code: int,
    events_path: Path,
    stderr_path: Path,
    parse_error: str,
) -> None:
    terminal = _terminal_failure(events_path)
    thread_id = _thread_id(events_path)
    payload: dict[str, Any] = {
        "schema": "rtl-advisor-plugin-abc-attempt-failure-v1",
        "arm": arm,
        "repetition": repetition,
        "attempt": attempt,
        "wall_time_seconds": elapsed,
        "exit_code": exit_code,
        "thread_id": thread_id,
        "usage_available": False,
        "parse_error": parse_error,
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        **_event_diagnostics(events_path),
    }
    if terminal is not None:
        payload["terminal_event"] = terminal
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _thread_id(path: Path) -> str | None:
    """Read thread identity even when a turn did not complete."""
    thread_id: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if (
            isinstance(event, Mapping)
            and event.get("type") == "thread.started"
            and isinstance(event.get("thread_id"), str)
        ):
            thread_id = str(event["thread_id"])
    return thread_id


def _command_category(command: str) -> str:
    lowered = command.lower()
    if "workflow batch" in lowered:
        return "workflow_batch"
    if "workflow" in lowered or "run_rtl_advisor.py" in lowered:
        return "rtl_advisor"
    if "jq " in lowered or "json.tool" in lowered:
        return "artifact_read"
    if "shasum" in lowered or "sha256sum" in lowered:
        return "hash"
    if any(tool in lowered for tool in ("yosys", "verilator", "sby ")):
        return "eda"
    return "other"


def _event_diagnostics(path: Path) -> dict[str, Any]:
    command_count = 0
    command_wave_count = 0
    command_output_bytes = 0
    agent_message_bytes = 0
    active_commands: set[str] = set()
    categories: dict[str, int] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        event = json.loads(raw)
        if not isinstance(event, Mapping) or not isinstance(event.get("item"), Mapping):
            continue
        item = event["item"]
        item_type = item.get("type")
        item_id = str(item.get("id", ""))
        if event.get("type") == "item.started" and item_type == "command_execution":
            if not active_commands:
                command_wave_count += 1
            active_commands.add(item_id)
        if event.get("type") != "item.completed":
            continue
        if item_type == "command_execution":
            command_count += 1
            active_commands.discard(item_id)
            output = item.get("aggregated_output", "")
            if isinstance(output, str):
                command_output_bytes += len(output.encode("utf-8"))
            command = item.get("command", "")
            if isinstance(command, str):
                category = _command_category(command)
                categories[category] = categories.get(category, 0) + 1
        elif item_type == "agent_message" and isinstance(item.get("text"), str):
            agent_message_bytes += len(item["text"].encode("utf-8"))
    if command_count and not command_wave_count:
        command_wave_count = command_count
    return {
        "command_count": command_count,
        "command_wave_count": command_wave_count,
        "command_categories": categories,
        "command_output_bytes": command_output_bytes,
        "agent_message_bytes": agent_message_bytes,
        "output_bytes": command_output_bytes + agent_message_bytes,
    }


def _write_telemetry(path: Path, record: Mapping[str, Any]) -> None:
    if path.is_file():
        payload = _load(path)
        if payload.get("schema") != TELEMETRY_SCHEMA:
            raise MeasuredRunError(f"unexpected telemetry schema in {path}")
    else:
        payload = {
            "schema": TELEMETRY_SCHEMA,
            "experiment_id": "plugin-abc-v1",
            "runs": [],
        }
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise MeasuredRunError("telemetry runs must be an array")
    identity = (record["arm"], record["repetition"])
    if any(
        isinstance(item, Mapping)
        and (item.get("arm"), item.get("repetition")) == identity
        and item.get("task_completed") is True
        and item.get("admitted_to_evaluation", True) is True
        for item in runs
    ):
        raise MeasuredRunError(
            f"telemetry already contains Arm {identity[0]} repetition {identity[1]}"
        )
    updated = {**payload, "runs": [*runs, dict(record)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_measured(
    *,
    arm: str,
    repetition: int,
    experiment: Path = DEFAULT_EXPERIMENT,
    codex_bin: str = "codex",
    phase: str = "phase6",
    series: str = "v1",
) -> dict[str, Any]:
    if arm not in SUPPORTED_ARMS:
        raise MeasuredRunError(f"measured phase supports only Arms A and B, got {arm!r}")
    if repetition not in {1, 2}:
        raise MeasuredRunError("measured repetitions must be 1 or 2")
    if phase not in {"phase6", "phase7", "installed-acceptance"}:
        raise MeasuredRunError("unsupported measured phase")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", series):
        raise MeasuredRunError("series must be a safe directory identifier")
    run_phase = f"{phase}/{series}" if phase == "installed-acceptance" else phase
    telemetry_path = (
        experiment / "evaluations" / "telemetry.json" if phase == "phase6"
        else experiment / "evaluations" / run_phase / "telemetry.json"
    )
    # Refuse duplicate samples before launching a costly model session.
    if telemetry_path.is_file() and any(
        row.get("arm") == arm and row.get("repetition") == repetition
        and row.get("admitted_to_evaluation", True)
        for row in _load(telemetry_path).get("runs", [])
    ):
        raise MeasuredRunError("sample already recorded; use a new series for a new experiment")
    packet_path = experiment / "packets" / f"arm-{arm.lower()}-r{repetition}.json"
    packet = _load(packet_path)
    if packet.get("arm") != arm or packet.get("repetition") != repetition:
        raise MeasuredRunError("packet identity does not match requested run")
    repetition_root = (
        experiment / "instrumented" / run_phase / f"arm-{arm.lower()}" / f"r{repetition}"
    ).resolve()
    attempt = 1
    while (repetition_root / f"attempt-{attempt:03d}").exists():
        attempt += 1
    output_root = repetition_root / f"attempt-{attempt:03d}"
    output_root.mkdir(parents=True)
    result_path = output_root / "result.json"
    events_path = output_root / "codex-events.jsonl"
    stderr_path = output_root / "codex-stderr.log"
    schema_path = (
        ROOT / "schemas/rtl-advisor-plugin-abc-codex-output-v1.schema.json"
    ).resolve()
    command = [
        codex_bin,
        "exec",
        "--json",
        "--ephemeral",
        "--approve-for-me",
        "--cd",
        str(ROOT),
        "--model",
        str(packet["model"]),
        "--config",
        f'model_reasoning_effort="{packet["reasoning_effort"]}"',
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
    ]
    candidate_config = (
        _phase7_candidate_config(experiment)
        if arm == "B" and phase == "phase7"
        else None
    )
    provenance = None
    artifact_snapshot = {}
    session_packet_path = packet_path
    if phase == "installed-acceptance":
        from scripts.plugin_installed_benchmark import preflight, snapshot_artifacts

        series_root = experiment / "instrumented" / run_phase
        provenance = preflight(experiment, series_root, arm, repetition, codex_bin)
        if arm == "B":
            candidate_config = _phase7_candidate_config(
                experiment, root=(series_root / "candidate").resolve()
            )
            artifact_snapshot = snapshot_artifacts(candidate_config.parent / "artifacts")
        session_packet_path = _instrumented_packet(packet, output_root)
    source_pin_arguments, source_pin = _phase7_source_pin(arm, phase)
    command.extend(source_pin_arguments)
    if arm == "A":
        command.append("--ignore-user-config")
        if phase in {"phase7", "installed-acceptance"}:
            command.append("--strict-config")
    command.append(
        _prompt(
            packet_path=session_packet_path.resolve(),
            output_root=output_root,
            result_path=result_path,
            arm=arm,
            phase=phase,
            candidate_config=candidate_config,
        )
    )
    environment = dict(os.environ)
    if candidate_config is not None:
        environment["RTL_ADVISOR_CONFIG"] = str(candidate_config)
    if phase == "installed-acceptance":
        (output_root / "prompt.txt").write_text(command[-1], encoding="utf-8")
        (output_root / "invocation.json").write_text(
            json.dumps(
                {
                    "command": command,
                    "frozen_packet": str(packet_path.resolve()),
                    "frozen_packet_sha256": _sha256(packet_path),
                    "provenance": provenance,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    started = time.monotonic()
    with events_path.open("w", encoding="utf-8") as events, stderr_path.open(
        "w", encoding="utf-8"
    ) as errors:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=events,
            stderr=errors,
            check=False,
        )
    elapsed = round(time.monotonic() - started, 3)
    try:
        usage, thread_id = _parse_events(events_path)
    except MeasuredRunError as exc:
        failure_path = output_root / "failure.json"
        _write_attempt_failure(
            failure_path,
            arm=arm,
            repetition=repetition,
            attempt=attempt,
            elapsed=elapsed,
            exit_code=completed.returncode,
            events_path=events_path,
            stderr_path=stderr_path,
            parse_error=str(exc),
        )
        terminal = _terminal_failure(events_path)
        detail = terminal["message"] if terminal is not None else str(exc)
        raise MeasuredRunError(
            f"{detail}; attempt failure recorded at {failure_path}"
        ) from exc
    diagnostics = _event_diagnostics(events_path)
    task_completed = False
    validation_error: str | None = None
    if completed.returncode == 0 and result_path.is_file():
        try:
            validate_arm_result(result_path, experiment)
            task_completed = True
        except FrameworkError as exc:
            validation_error = str(exc)
    else:
        validation_error = f"Codex exited {completed.returncode} or produced no result"
    record: dict[str, Any] = {
        "arm": arm,
        "repetition": repetition,
        "attempt": attempt,
        "input_tokens": usage["input_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "cache_write_input_tokens": usage["cache_write_input_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_output_tokens": usage["reasoning_output_tokens"],
        "total_tokens": usage["input_tokens"] + usage["output_tokens"],
        "wall_time_seconds": elapsed,
        "task_completed": task_completed,
        "exit_code": completed.returncode,
        "thread_id": thread_id,
        "packet_sha256": _sha256(packet_path),
        "result_path": str(result_path),
        "events_path": str(events_path),
        **diagnostics,
        **source_pin,
    }
    if validation_error is not None:
        record["validation_error"] = validation_error
    if provenance is not None:
        from scripts.plugin_installed_benchmark import audit_run

        audit = audit_run(provenance, events_path, candidate_config, artifact_snapshot)
        audit_path = output_root / "audit.json"
        audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        record["provenance_path"] = str(output_root / "invocation.json")
        record["audit_path"] = str(audit_path)
        if not audit["passed"]:
            record["task_completed"] = False
            record["validation_error"] = "installed benchmark audit failed; see audit_path"
    _write_telemetry(telemetry_path, record)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=sorted(SUPPORTED_ARMS), required=True)
    parser.add_argument("--repetition", type=int, choices=(1, 2), required=True)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--phase", choices=("phase6", "phase7", "installed-acceptance"), default="phase6")
    parser.add_argument("--series", default="v1", help="isolated installed-acceptance experiment identity")
    args = parser.parse_args(argv)
    try:
        record = run_measured(
            arm=args.arm,
            repetition=args.repetition,
            experiment=args.experiment.resolve(),
            codex_bin=args.codex_bin,
            phase=args.phase,
            series=args.series,
        )
    except MeasuredRunError as exc:
        print(f"measured A/B run failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if record["task_completed"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
