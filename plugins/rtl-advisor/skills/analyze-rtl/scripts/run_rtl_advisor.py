#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any, Sequence


SCHEMA_VERSION = 2
RUN_SCHEMA = "rtl-advisor-run-v1"
FLOW_VERSION = "rtl-advisor-agent-v2"
EXPECTED_DOCUMENTS = {
    "capabilities": "rtl-advisor.agent.v2.capabilities",
    "review": "rtl-advisor.agent.v2.review",
    "candidate": "rtl-advisor.agent.v2.candidate",
    "verify": "rtl-advisor.agent.v2.verification",
    "measure": "rtl-advisor.agent.v2.measurement",
    "report": "rtl-advisor.agent.v2.report",
}


class RunnerError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _error_payload(error: RunnerError) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "document_type": "rtl-advisor.runner.error",
        "status": "failed",
        "error": {"code": error.code, "message": str(error)},
    }
    payload["semantic_hash"] = _json_hash(payload)
    return payload


def _find_repo_root() -> Path | None:
    starts = [Path.cwd().resolve(), Path(__file__).resolve().parent]
    visited: set[Path] = set()
    for start in starts:
        for candidate in (start, *start.parents):
            if candidate in visited:
                continue
            visited.add(candidate)
            if (
                (candidate / "pyproject.toml").is_file()
                and (candidate / "src/rtl_advisor/cli.py").is_file()
                and (candidate / "rtl-advisor.toml").is_file()
            ):
                return candidate
    return None


def _split_explicit_command(value: str) -> list[str]:
    try:
        command = shlex.split(value)
    except ValueError as exc:
        raise RunnerError(
            f"invalid RTL_ADVISOR_BIN: {exc}", code="invalid_executable"
        ) from exc
    if not command:
        raise RunnerError("RTL_ADVISOR_BIN is empty", code="invalid_executable")
    return command


def _resolve_cli(repo_root: Path | None) -> tuple[list[str], dict[str, str]]:
    environment = dict(os.environ)
    explicit = environment.get("RTL_ADVISOR_BIN")
    if explicit:
        return _split_explicit_command(explicit), environment

    venv_python = repo_root / ".venv/bin/python" if repo_root is not None else None
    if venv_python is not None and venv_python.is_file():
        source_path = str(repo_root / "src")
        current = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path if not current else os.pathsep.join((source_path, current))
        )
        return [str(venv_python), "-m", "rtl_advisor"], environment

    installed = shutil.which("rtl-advisor")
    if installed:
        return [installed], environment

    uv = shutil.which("uv")
    if uv and repo_root is not None:
        return [uv, "run", "--no-editable", "rtl-advisor"], environment

    raise RunnerError(
        "rtl-advisor is not on PATH and no supported repository environment exists",
        code="executable_not_found",
    )


def _resolve_config(repo_root: Path | None, value: str | None) -> Path:
    configured = value or os.environ.get("RTL_ADVISOR_CONFIG")
    if configured:
        path = Path(configured).expanduser()
    elif repo_root is not None:
        path = repo_root / "rtl-advisor.toml"
    else:
        raise RunnerError(
            "RTL Advisor configuration is required outside its source checkout; "
            "pass --config or set RTL_ADVISOR_CONFIG",
            code="config_not_found",
        )
    if not path.is_absolute():
        path = (repo_root or Path.cwd()) / path
    path = path.resolve()
    if not path.is_file():
        raise RunnerError(
            f"RTL Advisor configuration not found: {path}",
            code="config_not_found",
        )
    return path


def _timeout_seconds() -> int:
    raw = os.environ.get("RTL_ADVISOR_RUNNER_TIMEOUT_SECONDS", "600")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RunnerError(
            "RTL_ADVISOR_RUNNER_TIMEOUT_SECONDS must be an integer",
            code="invalid_timeout",
        ) from exc
    if value <= 0:
        raise RunnerError(
            "RTL_ADVISOR_RUNNER_TIMEOUT_SECONDS must be positive",
            code="invalid_timeout",
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the stable RTL Advisor agent JSON interface.",
    )
    parser.add_argument(
        "--config",
        help="RTL Advisor configuration (default: repo rtl-advisor.toml)",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    subparsers.add_parser("capabilities", help="report current capabilities")

    review = subparsers.add_parser("review", help="run a read-only review")
    review.add_argument("input")
    review.add_argument(
        "--objective",
        choices=("timing", "area", "balanced"),
        default="balanced",
    )
    review.add_argument("--top")
    review.add_argument("-I", action="append", default=[], dest="include_dirs")
    review.add_argument("-D", action="append", default=[], dest="defines")

    candidate = subparsers.add_parser(
        "candidate", help="prepare an isolated candidate"
    )
    candidate.add_argument("run_id")
    candidate.add_argument("--finding", required=True)

    verify = subparsers.add_parser("verify", help="formally verify a candidate")
    verify.add_argument("run_id")
    verify.add_argument("--candidate", required=True)
    measure = subparsers.add_parser(
        "measure", help="measure a formally proven candidate with both recipes"
    )
    measure.add_argument("run_id")
    measure.add_argument("--candidate", required=True)
    report = subparsers.add_parser(
        "report", help="derive the immutable JSON and HTML run report"
    )
    report.add_argument("run_id")
    return parser


def _operation_arguments(args: argparse.Namespace) -> list[str]:
    if args.operation == "capabilities":
        return []
    if args.operation == "review":
        result = [args.input, "--objective", args.objective]
        if args.top:
            result.extend(("--top", args.top))
        for include_dir in args.include_dirs:
            result.extend(("-I", include_dir))
        for definition in args.defines:
            result.extend(("-D", definition))
        return result
    if args.operation == "candidate":
        return [args.run_id, "--finding", args.finding]
    if args.operation in {"verify", "measure"}:
        return [args.run_id, "--candidate", args.candidate]
    return [args.run_id]


def _validate_payload(payload: Any, operation: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunnerError("CLI JSON root must be an object", code="invalid_json")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError(
            f"unsupported agent schema: {payload.get('schema_version')!r}",
            code="unsupported_schema",
        )
    if payload.get("run_schema") != RUN_SCHEMA:
        raise RunnerError(
            f"unsupported run artifact schema: {payload.get('run_schema')!r}",
            code="unsupported_run_schema",
        )
    if payload.get("flow_version") != FLOW_VERSION:
        raise RunnerError(
            f"unsupported agent flow: {payload.get('flow_version')!r}",
            code="unsupported_flow",
        )
    document_type = payload.get("document_type")
    expected_type = EXPECTED_DOCUMENTS[operation]
    if document_type not in {expected_type, "rtl-advisor.agent.v2.error"}:
        raise RunnerError(
            f"unexpected document type {document_type!r} for {operation}",
            code="unexpected_document",
        )
    expected_hash = payload.get("semantic_hash")
    core = {key: value for key, value in payload.items() if key != "semantic_hash"}
    if expected_hash != _json_hash(core):
        raise RunnerError(
            "agent result semantic hash mismatch",
            code="semantic_hash_mismatch",
        )
    if not isinstance(payload.get("command"), list):
        raise RunnerError(
            "agent result does not include its normalized command",
            code="missing_command",
        )
    if document_type == EXPECTED_DOCUMENTS["verify"]:
        status = payload.get("status")
        if payload.get("decision") != status or (payload.get("safe") is True) != (
            status == "formal_passed"
        ):
            raise RunnerError(
                "verification status, decision, and safety flag disagree",
                code="invalid_verification_result",
            )
    return payload


def _expected_exit_code(payload: dict[str, Any], operation: str) -> int:
    if payload.get("document_type") == "rtl-advisor.agent.v2.error":
        return 2
    status = str(payload.get("status", ""))
    if operation in {"capabilities", "review"}:
        return 0
    if operation == "candidate":
        return 0 if status == "candidate_prepared" else 4
    if operation == "verify":
        return 0 if status == "formal_passed" else 4
    if operation in {"measure", "report"}:
        return 0 if status == "completed" else 4
    raise RunnerError(f"unsupported operation: {operation}", code="unsupported_operation")


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo_root = _find_repo_root()
    config_path = _resolve_config(repo_root, args.config)
    executable, environment = _resolve_cli(repo_root)
    working_directory = repo_root or config_path.parent
    command = [
        *executable,
        "--config",
        str(config_path),
        "agent",
        args.operation,
        *_operation_arguments(args),
        "--schema-version",
        str(SCHEMA_VERSION),
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            env=environment,
            text=True,
            capture_output=True,
            timeout=_timeout_seconds(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RunnerError(
            f"RTL Advisor executable was not found: {executable[0]}",
            code="executable_not_found",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(
            f"RTL Advisor operation exceeded the runner timeout: {args.operation}",
            code="timeout",
        ) from exc

    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if len(detail) > 1000:
            detail = detail[:1000] + "..."
        raise RunnerError(
            f"RTL Advisor returned malformed JSON: {detail or exc}",
            code="invalid_json",
        ) from exc
    payload = _validate_payload(raw, args.operation)
    expected_exit_code = _expected_exit_code(payload, args.operation)
    if completed.returncode != expected_exit_code:
        raise RunnerError(
            f"CLI exit code {completed.returncode} disagrees with the {args.operation} result "
            f"(expected {expected_exit_code})",
            code="exit_code_mismatch",
        )
    return payload, completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, exit_code = run(args)
    except RunnerError as exc:
        payload = _error_payload(exc)
        exit_code = 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
