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


SCHEMA_VERSION = 1
EXPECTED_DOCUMENTS = {
    "capabilities": "rtl-advisor.agent.capabilities",
    "review": "rtl-advisor.agent.review",
    "candidate": "rtl-advisor.agent.candidate",
    "verify": "rtl-advisor.agent.verification",
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


def _find_repo_root() -> Path:
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
    raise RunnerError(
        "could not locate the RTL Advisor repository; run inside its checkout",
        code="repository_not_found",
    )


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


def _resolve_cli(repo_root: Path) -> tuple[list[str], dict[str, str]]:
    environment = dict(os.environ)
    explicit = environment.get("RTL_ADVISOR_BIN")
    if explicit:
        return _split_explicit_command(explicit), environment

    installed = shutil.which("rtl-advisor")
    if installed:
        return [installed], environment

    venv_python = repo_root / ".venv/bin/python"
    if venv_python.is_file():
        source_path = str(repo_root / "src")
        current = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path if not current else os.pathsep.join((source_path, current))
        )
        return [str(venv_python), "-m", "rtl_advisor"], environment

    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--no-editable", "rtl-advisor"], environment

    raise RunnerError(
        "rtl-advisor is not on PATH and no supported repository environment exists",
        code="executable_not_found",
    )


def _resolve_config(repo_root: Path, value: str | None) -> Path:
    configured = value or os.environ.get("RTL_ADVISOR_CONFIG")
    path = Path(configured).expanduser() if configured else repo_root / "rtl-advisor.toml"
    if not path.is_absolute():
        path = repo_root / path
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
    review.add_argument("--gate-model")
    review.add_argument("--force", action="store_true")

    candidate = subparsers.add_parser(
        "candidate", help="prepare an isolated candidate"
    )
    candidate.add_argument("run_id")
    candidate.add_argument("--finding", required=True)

    verify = subparsers.add_parser("verify", help="formally verify a candidate")
    verify.add_argument("run_id")
    verify.add_argument("--candidate", required=True)
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
        if args.gate_model:
            result.extend(("--gate-model", args.gate_model))
        if args.force:
            result.append("--force")
        return result
    if args.operation == "candidate":
        return [args.run_id, "--finding", args.finding]
    return [args.run_id, "--candidate", args.candidate]


def _validate_payload(payload: Any, operation: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunnerError("CLI JSON root must be an object", code="invalid_json")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError(
            f"unsupported agent schema: {payload.get('schema_version')!r}",
            code="unsupported_schema",
        )
    document_type = payload.get("document_type")
    expected_type = EXPECTED_DOCUMENTS[operation]
    if document_type not in {expected_type, "rtl-advisor.agent.error"}:
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
    return payload


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo_root = _find_repo_root()
    config_path = _resolve_config(repo_root, args.config)
    executable, environment = _resolve_cli(repo_root)
    command = [
        *executable,
        "--config",
        str(config_path),
        "agent",
        args.operation,
        *_operation_arguments(args),
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
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
