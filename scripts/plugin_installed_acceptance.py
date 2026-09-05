#!/usr/bin/env python3
"""Run and validate a fresh-thread acceptance check of the installed plugin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.plugin_abc_measured import (  # noqa: E402
    MeasuredRunError,
    _event_diagnostics,
    _parse_events,
    _tree_sha256,
    _terminal_failure,
    _thread_id,
)


DEFAULT_OUTPUT = (
    ROOT
    / "experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance"
)
INPUT = ROOT / "experiments/plugin-abc-v1/cases/generated/g01_add8_left.sv"
PHASE7_EVALUATION = (
    ROOT / "experiments/plugin-abc-v1/evaluations/phase7/phase7-evaluation.json"
)
EXPECTED_VERSION = "0.2.0-alpha.1+codex.20260903154742"


def _hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasuredRunError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MeasuredRunError(f"{path} must contain a JSON object")
    return payload


def _attempt_dir(root: Path) -> tuple[int, Path]:
    attempt = 1
    while (root / f"attempt-{attempt:03d}").exists():
        attempt += 1
    return attempt, root / f"attempt-{attempt:03d}"


def _write_config(path: Path, artifacts_dir: Path) -> None:
    with (ROOT / "rtl-advisor.toml").open("rb") as stream:
        source = tomllib.load(stream)
    project = source["project"]
    tools = source["tools"]
    synthesis = source["synthesis"]
    codex = source.get("codex", {})
    liberty = source["liberty"]
    quote = lambda value: json.dumps(str(value))
    content = "\n".join(
        (
            "[project]",
            f"artifacts_dir = {quote(artifacts_dir)}",
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
    path.write_text(content, encoding="utf-8")


def _events(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MeasuredRunError(
                f"malformed Codex JSONL at {path}:{line_number}: {exc}"
            ) from exc
        if isinstance(event, dict):
            result.append(event)
    return result


def _completed_commands(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, Mapping)
            and item.get("type") == "command_execution"
        ):
            commands.append(dict(item))
    return commands


def _runner_digest(commands: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], int]:
    matches = [
        item
        for item in commands
        if "run_rtl_advisor.py" in str(item.get("command", ""))
    ]
    if len(matches) != 1:
        raise MeasuredRunError(
            f"expected one RTL Advisor runner command, observed {len(matches)}"
        )
    output = str(matches[0].get("aggregated_output", "")).strip()
    try:
        digest = json.loads(output)
    except json.JSONDecodeError as exc:
        raise MeasuredRunError(f"runner did not return a JSON digest: {exc}") from exc
    if not isinstance(digest, dict):
        raise MeasuredRunError("runner digest must be an object")
    if digest.get("semantic_hash") != _hash(
        {key: value for key, value in digest.items() if key != "semantic_hash"}
    ):
        raise MeasuredRunError("runner result semantic hash mismatch")
    if digest.get("document_type") != "rtl-advisor.workflow.digest":
        raise MeasuredRunError(f"runner returned a non-digest result: {digest.get('error')}")
    if matches[0].get("exit_code") != 0:
        raise MeasuredRunError("runner returned a nonzero exit code")
    return digest, len(output.encode("utf-8"))


def _installed_skill(commands: Sequence[Mapping[str, Any]]) -> tuple[Path, Path]:
    skill_paths: list[Path] = []
    for item in commands:
        command = str(item.get("command", ""))
        marker = "/skills/analyze-rtl/SKILL.md"
        if marker not in command:
            continue
        prefix = command.split(marker, 1)[0]
        token = prefix.rsplit(" ", 1)[-1].strip("'\"")
        skill_paths.append(Path(token + marker).resolve())
    if len(skill_paths) != 1:
        raise MeasuredRunError(
            f"expected one installed analyze-rtl skill read, observed {len(skill_paths)}"
        )
    skill_path = skill_paths[0]
    runner_path = skill_path.parent / "scripts/run_rtl_advisor.py"
    if not skill_path.is_file() or not runner_path.is_file():
        raise MeasuredRunError("installed skill or runner path is unavailable")
    return skill_path, runner_path


def _comparison(usage: Mapping[str, int], elapsed: float) -> dict[str, Any]:
    phase7 = _load(PHASE7_EVALUATION)
    baseline = phase7["efficiency_gate"]["arms"]["B"]
    total = usage["input_tokens"] + usage["output_tokens"]
    uncached = total - usage["cached_input_tokens"]
    return {
        "baseline": {
            "name": "phase7_arm_b_two-run_mean_24-case_batch",
            "mean_total_tokens": baseline["mean_total_tokens"],
            "mean_uncached_token_volume": baseline["mean_uncached_token_volume"],
            "mean_wall_time_seconds": baseline["mean_wall_time_seconds"],
        },
        "observed": {
            "total_tokens": total,
            "uncached_token_volume": uncached,
            "wall_time_seconds": elapsed,
        },
        "performance_regression_assessment": "not_comparable",
        "comparability": (
            "No efficiency reduction or regression can be inferred: this acceptance run is one candidate-ceiling case; "
            "the checked-in Phase 7 Arm B mean is a 24-case measure-ceiling batch."
        ),
    }


def _record_failure(output: Path, elapsed: float, exit_code: int, error: Exception) -> dict[str, Any]:
    events_path = output / "codex-events.jsonl"
    try:
        usage, thread_id = _parse_events(events_path)
    except MeasuredRunError:
        usage, thread_id = None, _thread_id(events_path)
    record = {
        "schema_version": 1,
        "document_type": "rtl-advisor.installed-plugin-acceptance-failure",
        "status": "failed",
        "thread_id": thread_id,
        "wall_time_seconds": elapsed,
        "exit_code": exit_code,
        "usage_available": usage is not None,
        "usage": usage,
        "error": str(error),
        "terminal_event": _terminal_failure(events_path),
        "diagnostics": _event_diagnostics(events_path),
        "events_path": str(events_path),
    }
    record["semantic_hash"] = _hash(record)
    with (output / "failure.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def run_acceptance(
    *, output_root: Path = DEFAULT_OUTPUT, codex_bin: str = "codex"
) -> dict[str, Any]:
    attempt, output = _attempt_dir(output_root)
    output.mkdir(parents=True)
    prompt_path = output / "prompt.txt"
    config_path = output / "rtl-advisor.toml"
    events_path = output / "codex-events.jsonl"
    stderr_path = output / "codex-stderr.log"
    response_path = output / "response.md"
    record_path = output / "acceptance.json"
    artifacts_dir = output / "artifacts"
    _write_config(config_path, artifacts_dir)
    prompt = f"""Use RTL Advisor to review the generated RTL at {INPUT}, top module g01_add8_left, for balanced PPA and prepare the first eligible isolated candidate.

Authorization stops at candidate preparation. Do not run formal verification or synthesis measurement, and do not modify the source. Use the installed RTL Advisor plugin, its normal compact workflow, and only the returned compact digest in your answer. Do not open stage artifacts, reports, logs, capabilities payloads, or the source after a successful runner call.

The exact authorization prompt is stored at {prompt_path}; pass that path as the workflow prompt file. Keep any model-created session files under {output}.
"""
    prompt_path.write_text(prompt, encoding="utf-8")
    source_before = _file_sha256(INPUT)
    command = [
        codex_bin,
        "exec",
        "--json",
        "--ephemeral",
        "--approve-for-me",
        "--cd",
        str(ROOT),
        "--model",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="xhigh"',
        "--output-last-message",
        str(response_path),
        prompt,
    ]
    environment = dict(os.environ)
    environment["RTL_ADVISOR_CONFIG"] = str(config_path)
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
        parsed_events = _events(events_path)
        commands = _completed_commands(parsed_events)
        digest, digest_bytes = _runner_digest(commands)
        skill_path, runner_path = _installed_skill(commands)
    except MeasuredRunError as exc:
        return _record_failure(output, elapsed, completed.returncode, exc)
    command_texts = [str(item.get("command", "")) for item in commands]
    runner_command = next(text for text in command_texts if "run_rtl_advisor.py" in text)

    summary_path = Path(str(digest.get("artifacts", {}).get("summary", ""))).resolve()
    if not summary_path.is_relative_to(artifacts_dir):
        return _record_failure(output, elapsed, completed.returncode,
                               MeasuredRunError("digest summary escaped the isolated artifact root"))
    workflow_root = summary_path.parent.parent
    authorization_pointer = _load(workflow_root / "authorization-latest.json")
    authorization = _load(Path(str(authorization_pointer["authorization_path"])))
    stage_names = sorted(path.stem for path in (workflow_root / "stages").glob("*.json"))
    forbidden_stages = sorted(set(stage_names) & {"verify", "measure"})
    runner_index = command_texts.index(runner_command)
    commands_after_runner = command_texts[runner_index + 1 :]
    redundant_artifact_reads = [
        text
        for text in commands_after_runner
        if any(
            marker in text
            for marker in (
                "/stages/",
                "/reports/",
                "/capabilities/",
                "/summaries/",
                "summary-latest.json",
            )
        )
    ]
    separate_capability_calls = [
        text
        for text in command_texts
        if "run_rtl_advisor.py capabilities" in text
    ]
    expected_runner_flags = all(
        marker in runner_command
        for marker in (
            "workflow prepare",
            "--input-kind generated_rtl",
            "--objective balanced",
            "--top g01_add8_left",
            "--authorized-through candidate",
            "--first-eligible",
            "--start",
            "--compact",
            str(prompt_path),
        )
    )
    installed_root = skill_path.parents[2]
    repository_plugin = ROOT / "plugins/rtl-advisor"
    checks = {
        "codex_exit_zero": completed.returncode == 0,
        "installed_analyze_rtl_skill_selected": (
            EXPECTED_VERSION in str(skill_path)
            and str(skill_path).startswith(
                "/Users/peter/.codex/plugins/cache/personal/rtl-advisor/"
            )
        ),
        "installed_tree_matches_repository": (
            _tree_sha256(installed_root) == _tree_sha256(repository_plugin)
        ),
        "single_deterministic_runner_call": sum(
            "run_rtl_advisor.py" in text for text in command_texts
        )
        == 1,
        "runner_used_expected_compact_route": expected_runner_flags,
        "compact_digest_under_4kb": (
            digest.get("document_type") == "rtl-advisor.workflow.digest"
            and digest_bytes < 4096
        ),
        "candidate_ceiling_preserved": (
            authorization.get("authorized_through") == "candidate"
            and authorization.get("candidate_selection")
            == {"mode": "first_eligible"}
            and digest.get("decision") == "candidate_prepared"
            and digest.get("formal_status") == "not_run"
            and digest.get("measurement_status") == "not_run"
            and digest.get("safe") is False
            and not forbidden_stages
        ),
        "prompt_hash_bound": (
            authorization.get("basis")
            == {"kind": "direct_prompt", "prompt_sha256": _file_sha256(prompt_path)}
        ),
        "no_separate_capability_call": not separate_capability_calls,
        "no_redundant_artifact_loading": not commands_after_runner,
        "source_unchanged": source_before == _file_sha256(INPUT),
    }
    comparison = _comparison(usage, elapsed)
    record: dict[str, Any] = {
        "schema_version": 1,
        "schema": "rtl-advisor-installed-plugin-acceptance-v1",
        "document_type": "rtl-advisor.installed-plugin-acceptance",
        "status": "passed" if completed.returncode == 0 and all(checks.values()) else "failed",
        "attempt": attempt,
        "thread_id": thread_id,
        "plugin": {
            "version": EXPECTED_VERSION,
            "skill_path": str(skill_path),
            "runner_path": str(runner_path),
            "installed_tree_sha256": _tree_sha256(installed_root),
            "repository_tree_sha256": _tree_sha256(repository_plugin),
        },
        "workflow": {
            "workflow_id": digest.get("workflow_id"),
            "digest_semantic_hash": digest.get("semantic_hash"),
            "digest_bytes": digest_bytes,
            "decision": digest.get("decision"),
            "action": digest.get("action"),
            "formal_status": digest.get("formal_status"),
            "measurement_status": digest.get("measurement_status"),
            "safe": digest.get("safe"),
            "stage_names": stage_names,
            "runner_command": runner_command,
        },
        "checks": checks,
        "telemetry": {
            **usage,
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "uncached_token_volume": (
                usage["input_tokens"]
                + usage["output_tokens"]
                - usage["cached_input_tokens"]
            ),
            "wall_time_seconds": elapsed,
            **_event_diagnostics(events_path),
        },
        "phase7_comparison": comparison,
        "artifacts": {
            "prompt": str(prompt_path),
            "config": str(config_path),
            "events": str(events_path),
            "stderr": str(stderr_path),
            "response": str(response_path),
            "digest": str(
                workflow_root
                / "digests"
                / f"{digest.get('parent', {}).get('summary_semantic_hash')}.json"
            ),
            "authorization": str(authorization_pointer["authorization_path"]),
            "record": str(record_path),
        },
    }
    record["semantic_hash"] = _hash(record)
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    latest_path = output_root / "latest.json"
    latest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "document_type": "rtl-advisor.installed-plugin-acceptance-latest",
                "attempt": attempt,
                "record": str(record_path),
                "record_semantic_hash": record["semantic_hash"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--replay-record", type=Path,
                        help="Audit deterministic reuse of an existing acceptance workflow; no Codex call")
    args = parser.parse_args(argv)
    try:
        record = replay_acceptance(args.replay_record.resolve()) if args.replay_record else run_acceptance(
            output_root=args.output_root.resolve(), codex_bin=args.codex_bin
        )
    except MeasuredRunError as exc:
        print(f"installed plugin acceptance failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if record["status"] == "passed" else 4


def replay_acceptance(record_path: Path) -> dict[str, Any]:
    """Repeat the authorized runner operation and compare all stored file contents."""
    acceptance = _load(record_path)
    if acceptance.get("semantic_hash") != _hash(
        {key: value for key, value in acceptance.items() if key != "semantic_hash"}
    ) or acceptance.get("status") != "passed":
        raise MeasuredRunError("replay requires a valid passed acceptance record")
    output = record_path.parent
    replay_path = output / "replay.json"
    if replay_path.exists():
        raise MeasuredRunError(f"replay evidence already exists: {replay_path}")
    artifacts_dir = output / "artifacts"

    def snapshot() -> dict[str, str]:
        return {str(path.relative_to(artifacts_dir)): _file_sha256(path)
                for path in sorted(artifacts_dir.rglob("*")) if path.is_file()}

    before = snapshot()
    source_before = _file_sha256(INPUT)
    command = [
        sys.executable, acceptance["plugin"]["runner_path"],
        "--config", acceptance["artifacts"]["config"],
        "workflow", "prepare", str(INPUT), "--input-kind", "generated_rtl",
        "--objective", "balanced", "--authorized-through", "candidate",
        "--prompt-file", acceptance["artifacts"]["prompt"], "--top", "g01_add8_left",
        "--first-eligible", "--start", "--compact",
    ]
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    elapsed = round(time.monotonic() - started, 3)
    digest, digest_bytes = _runner_digest([{
        "command": " ".join(command), "aggregated_output": completed.stdout,
        "exit_code": completed.returncode,
    }])
    after = snapshot()
    checks = {
        "all_artifact_bytes_unchanged": before == after,
        "same_digest_semantic_hash": digest["semantic_hash"] == acceptance["workflow"]["digest_semantic_hash"],
        "same_workflow_id": digest["workflow_id"] == acceptance["workflow"]["workflow_id"],
        "authorization_ceiling_preserved": digest["formal_status"] == "not_run" and digest["measurement_status"] == "not_run",
        "source_unchanged": source_before == _file_sha256(INPUT),
    }
    result = {
        "schema_version": 1, "document_type": "rtl-advisor.installed-plugin-replay-audit",
        "status": "passed" if all(checks.values()) else "failed",
        "acceptance_semantic_hash": acceptance["semantic_hash"],
        "checks": checks, "artifact_file_count": len(before),
        "artifact_tree_sha256": _hash(before),
        "wall_time_seconds": elapsed, "usage_available": False,
        "measurement_scope": "deterministic runner only; no model token measurement",
        "digest_bytes": digest_bytes, "command": command,
    }
    result["semantic_hash"] = _hash(result)
    with replay_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
