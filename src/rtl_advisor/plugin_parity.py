from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Sequence

from rtl_advisor.config import ProjectConfig, load_config
from rtl_advisor.corpus import load_manifest


PARITY_SCHEMA_VERSION = 1
DEFAULT_RUNNER = Path(
    "plugins/rtl-advisor/skills/analyze-rtl/scripts/run_rtl_advisor.py"
)


@dataclass(frozen=True)
class ExpectedValue:
    path: str
    value: Any


@dataclass(frozen=True)
class ParityScenario:
    scenario_id: str
    description: str
    operation: str
    arguments: tuple[str, ...]
    config_path: Path
    expected_exit_code: int
    expected_document_type: str
    expected_values: tuple[ExpectedValue, ...] = ()
    source_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ProcessResult:
    command: tuple[str, ...]
    exit_code: int
    payload: Any
    stderr: str
    parse_error: str | None = None


class ParityError(RuntimeError):
    """Raised when the parity harness itself cannot run."""


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_hashes(paths: Sequence[Path]) -> dict[str, str | None]:
    return {str(path.resolve()): _file_hash(path.resolve()) for path in paths}


def _lookup(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted_path)
        current = current[part]
    return current


def _semantic_hash_valid(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    expected = payload.get("semantic_hash")
    core = {key: value for key, value in payload.items() if key != "semantic_hash"}
    return isinstance(expected, str) and expected == _json_hash(core)


def _run_json(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> ProcessResult:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
        parse_error = None
    except json.JSONDecodeError as exc:
        payload = None
        parse_error = str(exc)
    return ProcessResult(
        command=tuple(command),
        exit_code=completed.returncode,
        payload=payload,
        stderr=completed.stderr.strip(),
        parse_error=parse_error,
    )


def _process_record(result: ProcessResult) -> dict[str, Any]:
    return {
        "command": list(result.command),
        "exit_code": result.exit_code,
        "payload": result.payload,
        "stderr": result.stderr,
        "parse_error": result.parse_error,
    }


def compare_scenario(
    scenario: ParityScenario,
    *,
    repo_root: Path,
    runner_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    environment = dict(os.environ)
    source_dir = str(repo_root / "src")
    current_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_dir
        if not current_pythonpath
        else os.pathsep.join((source_dir, current_pythonpath))
    )
    environment["RTL_ADVISOR_BIN"] = shlex.join(
        (sys.executable, "-m", "rtl_advisor")
    )

    terminal_command = (
        sys.executable,
        "-m",
        "rtl_advisor",
        "--config",
        str(scenario.config_path),
        "agent",
        scenario.operation,
        *scenario.arguments,
        "--json",
    )
    plugin_command = (
        sys.executable,
        str(runner_path),
        "--config",
        str(scenario.config_path),
        scenario.operation,
        *scenario.arguments,
    )

    source_before = _source_hashes(scenario.source_paths)
    terminal = _run_json(
        terminal_command,
        cwd=repo_root,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    source_after_terminal = _source_hashes(scenario.source_paths)
    plugin = _run_json(
        plugin_command,
        cwd=repo_root,
        environment=environment,
        timeout_seconds=timeout_seconds,
    )
    source_after_plugin = _source_hashes(scenario.source_paths)

    errors: list[str] = []
    if terminal.parse_error:
        errors.append(f"terminal returned malformed JSON: {terminal.parse_error}")
    if plugin.parse_error:
        errors.append(f"plugin runner returned malformed JSON: {plugin.parse_error}")
    if terminal.exit_code != scenario.expected_exit_code:
        errors.append(
            "terminal exit code "
            f"{terminal.exit_code} != expected {scenario.expected_exit_code}"
        )
    if plugin.exit_code != scenario.expected_exit_code:
        errors.append(
            "plugin runner exit code "
            f"{plugin.exit_code} != expected {scenario.expected_exit_code}"
        )
    if terminal.exit_code != plugin.exit_code:
        errors.append("terminal and plugin runner exit codes differ")
    if terminal.payload != plugin.payload:
        errors.append("terminal and plugin runner JSON payloads differ")
    if not _semantic_hash_valid(terminal.payload):
        errors.append("terminal semantic hash is invalid")
    if not _semantic_hash_valid(plugin.payload):
        errors.append("plugin runner semantic hash is invalid")

    terminal_document = (
        terminal.payload.get("document_type")
        if isinstance(terminal.payload, dict)
        else None
    )
    plugin_document = (
        plugin.payload.get("document_type")
        if isinstance(plugin.payload, dict)
        else None
    )
    if terminal_document != scenario.expected_document_type:
        errors.append(
            f"terminal document {terminal_document!r} != expected "
            f"{scenario.expected_document_type!r}"
        )
    if plugin_document != scenario.expected_document_type:
        errors.append(
            f"plugin runner document {plugin_document!r} != expected "
            f"{scenario.expected_document_type!r}"
        )

    if isinstance(terminal.payload, dict):
        for expectation in scenario.expected_values:
            try:
                actual = _lookup(terminal.payload, expectation.path)
            except KeyError:
                errors.append(f"missing expected field {expectation.path}")
                continue
            if actual != expectation.value:
                errors.append(
                    f"{expectation.path}={actual!r} != expected "
                    f"{expectation.value!r}"
                )

    sources_unchanged = (
        source_before == source_after_terminal == source_after_plugin
    )
    if not sources_unchanged:
        errors.append("source hash changed during parity scenario")

    return {
        "scenario_id": scenario.scenario_id,
        "description": scenario.description,
        "operation": scenario.operation,
        "arguments": list(scenario.arguments),
        "expected": {
            "exit_code": scenario.expected_exit_code,
            "document_type": scenario.expected_document_type,
            "values": {
                expectation.path: expectation.value
                for expectation in scenario.expected_values
            },
        },
        "terminal": _process_record(terminal),
        "plugin_runner": _process_record(plugin),
        "comparison": {
            "exit_code_equal": terminal.exit_code == plugin.exit_code,
            "payload_equal": terminal.payload == plugin.payload,
            "semantic_hash_equal": (
                isinstance(terminal.payload, dict)
                and isinstance(plugin.payload, dict)
                and terminal.payload.get("semantic_hash")
                == plugin.payload.get("semantic_hash")
            ),
            "semantic_hash_valid": (
                _semantic_hash_valid(terminal.payload)
                and _semantic_hash_valid(plugin.payload)
            ),
            "sources_unchanged": sources_unchanged,
        },
        "source_hashes": {
            "before": source_before,
            "after_terminal": source_after_terminal,
            "after_plugin": source_after_plugin,
        },
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value))


def write_missing_tools_config(
    base: ProjectConfig,
    *,
    path: Path,
) -> Path:
    artifact_dir = path.parent / "artifacts"
    content = f"""[project]
artifacts_dir = {_toml_string(artifact_dir)}
corpus_dir = {_toml_string(base.corpus_dir)}

[tools]
verilator = "rtl-advisor-missing-verilator-sentinel"
yosys = "rtl-advisor-missing-yosys-sentinel"
codex = {_toml_string(base.tools.codex)}
timeout_seconds = {base.tools.timeout_seconds}

[synthesis]
driving_cell = {_toml_string(base.synthesis.driving_cell)}
output_load_ff = {base.synthesis.output_load_ff}

[codex]
model = {_toml_string(base.codex.model)}
default_effort = {_toml_string(base.codex.default_effort)}
timeout_seconds = {base.codex.timeout_seconds}

[liberty]
name = {_toml_string(base.liberty.name)}
path = {_toml_string(base.liberty.path)}
url = {_toml_string(base.liberty.url)}
sha256 = {_toml_string(base.liberty.sha256)}
license_path = {_toml_string(base.liberty.license_path)}
license_url = {_toml_string(base.liberty.license_url)}
source_commit = {_toml_string(base.liberty.source_commit)}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _review_source_paths(review_input: Path) -> tuple[Path, ...]:
    resolved = review_input.resolve()
    if resolved.is_dir() or resolved.name == "manifest.json":
        manifest = load_manifest(resolved)
        return (manifest.path.resolve(), manifest.variant_path(manifest.baseline).resolve())
    if resolved.name == "input.json":
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            files = tuple(Path(str(item["path"])).resolve() for item in payload["files"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return (resolved,)
        return (resolved, *files)
    return (resolved,)


def build_scenarios(
    *,
    config: ProjectConfig,
    repo_root: Path,
    runtime_dir: Path,
    review_input: Path | None,
) -> list[ParityScenario]:
    fixture = repo_root / "tests/fixtures/plugin_parity/minimal.sv"
    missing_input = repo_root / "tests/fixtures/plugin_parity/does-not-exist.sv"
    missing_tools_config = write_missing_tools_config(
        config,
        path=runtime_dir / "missing-tools/rtl-advisor.toml",
    )
    scenarios = [
        ParityScenario(
            scenario_id="capabilities",
            description="Current tools, models, and operations",
            operation="capabilities",
            arguments=(),
            config_path=config.config_path,
            expected_exit_code=0,
            expected_document_type="rtl-advisor.agent.capabilities",
            expected_values=(
                ExpectedValue("status", "ok"),
                ExpectedValue("analysis.live_recommendation_ready", False),
                ExpectedValue("operations.source_mutation.available", False),
            ),
        ),
        ParityScenario(
            scenario_id="missing_tools",
            description="Missing Yosys and Verilator capability state",
            operation="capabilities",
            arguments=(),
            config_path=missing_tools_config,
            expected_exit_code=0,
            expected_document_type="rtl-advisor.agent.capabilities",
            expected_values=(
                ExpectedValue("tools.yosys.status", "missing"),
                ExpectedValue("tools.verilator.status", "missing"),
                ExpectedValue("operations.review.available", False),
                ExpectedValue("operations.formal_verification.available", False),
            ),
        ),
        ParityScenario(
            scenario_id="missing_input",
            description="Review rejects a nonexistent RTL input",
            operation="review",
            arguments=(str(missing_input), "--objective", "timing"),
            config_path=config.config_path,
            expected_exit_code=2,
            expected_document_type="rtl-advisor.agent.error",
            expected_values=(ExpectedValue("error.code", "input_not_found"),),
        ),
        ParityScenario(
            scenario_id="top_required",
            description="Standalone RTL review requires an explicit top",
            operation="review",
            arguments=(str(fixture), "--objective", "balanced"),
            config_path=config.config_path,
            expected_exit_code=2,
            expected_document_type="rtl-advisor.agent.error",
            expected_values=(ExpectedValue("error.code", "top_required"),),
            source_paths=(fixture,),
        ),
        ParityScenario(
            scenario_id="invalid_run_id",
            description="Candidate preparation rejects an invalid review ID",
            operation="candidate",
            arguments=("not-a-review-id", "--finding", "finding01"),
            config_path=config.config_path,
            expected_exit_code=2,
            expected_document_type="rtl-advisor.agent.error",
            expected_values=(ExpectedValue("error.code", "invalid_run_id"),),
        ),
        ParityScenario(
            scenario_id="missing_candidate_record",
            description="Verification rejects a review with no stored artifacts",
            operation="verify",
            arguments=(
                "review-00000000000000000000",
                "--candidate",
                "candidate01",
            ),
            config_path=config.config_path,
            expected_exit_code=2,
            expected_document_type="rtl-advisor.agent.error",
            expected_values=(ExpectedValue("error.code", "invalid_artifact"),),
        ),
    ]
    if review_input is not None:
        scenarios.append(
            ParityScenario(
                scenario_id="diagnostic_only_review",
                description="Diagnostic model remains unavailable for live advice",
                operation="review",
                arguments=(str(review_input), "--objective", "timing"),
                config_path=config.config_path,
                expected_exit_code=3,
                expected_document_type="rtl-advisor.agent.review",
                expected_values=(
                    ExpectedValue("status", "blocked"),
                    ExpectedValue("decision", "failed"),
                    ExpectedValue("candidate_generation_allowed", False),
                    ExpectedValue("evidence.model_release_status", "diagnostic_only"),
                    ExpectedValue("input.source_integrity.ok", True),
                ),
                source_paths=_review_source_paths(review_input),
            )
        )
    return scenarios


def _scenario_summary(result: dict[str, Any]) -> str:
    payload = result.get("terminal", {}).get("payload")
    if not isinstance(payload, dict):
        return "malformed result"
    if payload.get("document_type") == "rtl-advisor.agent.error":
        return str(payload.get("error", {}).get("code", "agent error"))
    decision = payload.get("decision")
    if decision:
        return str(decision)
    missing = [
        name
        for name in ("yosys", "verilator")
        if payload.get("tools", {}).get(name, {}).get("status") == "missing"
    ]
    if missing:
        return "missing " + ", ".join(missing)
    return str(payload.get("status", "unknown"))


def render_markdown(report: dict[str, Any]) -> str:
    rows = []
    for result in report["scenarios"]:
        comparison = result["comparison"]
        rows.append(
            "| {scenario} | {summary} | {terminal} | {plugin} | {semantic} | "
            "{source} | **{status}** |".format(
                scenario=result["scenario_id"],
                summary=_scenario_summary(result),
                terminal=result["terminal"]["exit_code"],
                plugin=result["plugin_runner"]["exit_code"],
                semantic="yes" if comparison["semantic_hash_equal"] else "no",
                source="yes" if comparison["sources_unchanged"] else "no",
                status=result["status"],
            )
        )
    return "\n".join(
        (
            "# RTL Advisor Plugin Transport Parity",
            "",
            f"Overall status: **{report['status']}**",
            "",
            "This report compares the direct agent CLI with the command runner "
            "bundled in the Codex plugin. Matching payloads prove transport "
            "parity; conversational claim auditing is tracked separately.",
            "",
            "| Scenario | Result state | Terminal exit | Plugin exit | Same "
            "semantic hash | Source unchanged | Outcome |",
            "| --- | --- | ---: | ---: | --- | --- | --- |",
            *rows,
            "",
            f"JSON evidence: `{report['artifacts']['json']}`",
            "",
        )
    )


def run_parity(
    *,
    config_path: Path,
    runner_path: Path,
    review_input: Path | None,
    output_json: Path,
    output_markdown: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    config = load_config(config_path)
    repo_root = config.root
    runner_path = runner_path.resolve()
    if not runner_path.is_file():
        raise ParityError(f"plugin runner not found: {runner_path}")
    if review_input is not None:
        review_input = review_input.expanduser().resolve()
        if not review_input.exists():
            raise ParityError(f"review input not found: {review_input}")

    scenarios = build_scenarios(
        config=config,
        repo_root=repo_root,
        runtime_dir=output_json.parent / "runtime",
        review_input=review_input,
    )
    results = [
        compare_scenario(
            scenario,
            repo_root=repo_root,
            runner_path=runner_path,
            timeout_seconds=timeout_seconds,
        )
        for scenario in scenarios
    ]
    payload = {
        "schema_version": PARITY_SCHEMA_VERSION,
        "document_type": "rtl-advisor.plugin-parity",
        "status": "passed"
        if all(result["status"] == "passed" for result in results)
        else "failed",
        "config": str(config.config_path),
        "runner": str(runner_path),
        "review_input": str(review_input) if review_input is not None else None,
        "scenario_count": len(results),
        "scenarios": results,
        "artifacts": {
            "json": str(output_json.resolve()),
            "markdown": str(output_markdown.resolve()),
        },
    }
    payload["semantic_hash"] = _json_hash(payload)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.write_text(render_markdown(payload), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare direct RTL Advisor agent results with the plugin runner.",
    )
    parser.add_argument("--config", default="rtl-advisor.toml")
    parser.add_argument("--runner", default=str(DEFAULT_RUNNER))
    parser.add_argument("--review-input")
    parser.add_argument(
        "--output-json",
        default="artifacts/plugin-parity/phase3.json",
    )
    parser.add_argument(
        "--output-markdown",
        default="artifacts/plugin-parity/phase3.md",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--json",
        action="store_true",
        dest="print_json",
        help="print the complete report instead of a concise summary",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    try:
        report = run_parity(
            config_path=Path(args.config).expanduser().resolve(),
            runner_path=Path(args.runner).expanduser().resolve(),
            review_input=(
                Path(args.review_input) if args.review_input is not None else None
            ),
            output_json=Path(args.output_json).expanduser().resolve(),
            output_markdown=Path(args.output_markdown).expanduser().resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    except (ParityError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"plugin parity failed: {exc}", file=sys.stderr)
        return 2
    if args.print_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"Plugin transport parity: {report['status']} "
            f"({report['scenario_count']} scenarios)"
        )
        print(f"JSON: {report['artifacts']['json']}")
        print(f"Markdown: {report['artifacts']['markdown']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
