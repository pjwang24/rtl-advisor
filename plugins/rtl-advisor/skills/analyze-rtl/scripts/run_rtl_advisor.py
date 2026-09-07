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
WORKFLOW_SCHEMA_VERSION = 1
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
    evidence = subparsers.add_parser(
        "evidence", help="explore immutable evidence with compact chart specs"
    )
    evidence_subparsers = evidence.add_subparsers(
        dest="evidence_operation", required=True
    )
    evidence_explore = evidence_subparsers.add_parser("explore")
    evidence_explore.add_argument(
        "--workflow-id", action="append", default=[], dest="workflow_ids"
    )
    evidence_explore.add_argument(
        "--run-id", action="append", default=[], dest="run_ids"
    )
    evidence_explore.add_argument(
        "--profile",
        action="append",
        choices=("standard", "stronger"),
        default=[],
        dest="profiles",
    )
    evidence_explore.add_argument(
        "--objective",
        action="append",
        choices=("timing", "area", "balanced"),
        default=[],
        dest="objectives",
    )
    evidence_explore.add_argument(
        "--classification",
        action="append",
        choices=("improved", "neutral", "regressed"),
        default=[],
        dest="classifications",
    )
    evidence_explore.add_argument(
        "--decision",
        action="append",
        choices=(
            "measured_improvement",
            "synthesis_handles",
            "flow_dependent",
            "regression",
        ),
        default=[],
        dest="decisions",
    )
    evidence_explore.add_argument(
        "--transformation", action="append", default=[], dest="transformations"
    )
    evidence_explore.add_argument(
        "--source-kind",
        action="append",
        choices=("agent_v2_run", "family_study"),
        default=[],
        dest="source_kinds",
    )
    evidence_explore.add_argument("--output-dir")
    corpus = subparsers.add_parser(
        "corpus", help="qualify frozen corpus tranches or audit coverage"
    )
    corpus_subparsers = corpus.add_subparsers(
        dest="corpus_operation", required=True
    )
    corpus_coverage = corpus_subparsers.add_parser("coverage")
    corpus_coverage.add_argument(
        "--tier", action="append", choices=("A", "B", "C", "D"), default=[], dest="tiers"
    )
    corpus_coverage.add_argument(
        "--category", action="append", default=[], dest="categories"
    )
    corpus_coverage.add_argument(
        "--split", action="append", default=[], dest="splits"
    )
    corpus_coverage.add_argument(
        "--qualification-state",
        action="append",
        default=[],
        dest="qualification_states",
    )
    corpus_coverage.add_argument(
        "--qualification-status",
        action="append",
        choices=("active", "blocked", "rejected"),
        default=[],
        dest="qualification_statuses",
    )
    corpus_qualify = corpus_subparsers.add_parser("qualify")
    corpus_qualify.add_argument("lock")
    corpus_qualify.add_argument("plan")
    corpus_validate = corpus_subparsers.add_parser("validate")
    corpus_validate.add_argument("lock")
    corpus_register = corpus_subparsers.add_parser("register")
    corpus_register.add_argument("lock")
    for corpus_parser in (
        corpus_coverage,
        corpus_qualify,
        corpus_validate,
        corpus_register,
    ):
        corpus_parser.add_argument("--registry-dir")
    workflow = subparsers.add_parser(
        "workflow", help="run or inspect a deterministic multi-stage workflow"
    )
    workflow_subparsers = workflow.add_subparsers(
        dest="workflow_operation", required=True
    )
    workflow_prepare = workflow_subparsers.add_parser("prepare")
    workflow_prepare.add_argument("input")
    workflow_prepare.add_argument(
        "--input-kind",
        choices=(
            "generated_rtl",
            "explicitly_approved_open_rtl",
            "qualified_corpus_reference",
        ),
        required=True,
    )
    workflow_prepare.add_argument(
        "--objective", choices=("timing", "area", "balanced"), default="balanced"
    )
    workflow_prepare.add_argument(
        "--authorized-through",
        choices=("review", "candidate", "verify", "measure"),
        required=True,
    )
    workflow_prepare.add_argument("--top")
    workflow_prepare.add_argument(
        "-I", action="append", default=[], dest="include_dirs"
    )
    workflow_prepare.add_argument("-D", action="append", default=[], dest="defines")
    basis = workflow_prepare.add_mutually_exclusive_group(required=True)
    basis.add_argument("--prompt-file")
    basis.add_argument("--proposal-file")
    workflow_prepare.add_argument("--confirmation-file")
    selection = workflow_prepare.add_mutually_exclusive_group()
    selection.add_argument("--first-eligible", action="store_true")
    selection.add_argument("--finding-id")
    workflow_prepare.add_argument("--output-dir")
    workflow_prepare.add_argument("--start", action="store_true")
    workflow_prepare.add_argument("--compact", action="store_true")
    workflow_start = workflow_subparsers.add_parser("start")
    workflow_start.add_argument("request")
    workflow_start.add_argument("--authorization", required=True)
    workflow_start.add_argument("--compact", action="store_true")
    workflow_status = workflow_subparsers.add_parser("status")
    workflow_status.add_argument("workflow_id")
    workflow_status.add_argument("--compact", action="store_true")
    workflow_resume = workflow_subparsers.add_parser("resume")
    workflow_resume.add_argument("workflow_id")
    workflow_resume.add_argument("--authorization", required=True)
    workflow_resume.add_argument("--compact", action="store_true")
    workflow_report = workflow_subparsers.add_parser("report")
    workflow_report.add_argument("workflow_id")
    workflow_report.add_argument("--compact", action="store_true")
    workflow_batch = workflow_subparsers.add_parser("batch")
    workflow_batch.add_argument("manifest")
    workflow_batch.add_argument(
        "--authorized-through",
        choices=("review", "candidate", "verify", "measure"),
        required=True,
    )
    batch_basis = workflow_batch.add_mutually_exclusive_group(required=True)
    batch_basis.add_argument("--prompt-file")
    batch_basis.add_argument("--proposal-file")
    workflow_batch.add_argument("--confirmation-file")
    workflow_batch.add_argument("--first-eligible", action="store_true")
    workflow_batch.add_argument("--output-dir")
    workflow_batch.add_argument("--jobs", type=int, choices=(1, 2, 3, 4), default=1)
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
    if args.operation == "evidence":
        result = [args.evidence_operation]
        for option, values in (
            ("--workflow-id", args.workflow_ids),
            ("--run-id", args.run_ids),
            ("--profile", args.profiles),
            ("--objective", args.objectives),
            ("--classification", args.classifications),
            ("--decision", args.decisions),
            ("--transformation", args.transformations),
            ("--source-kind", args.source_kinds),
        ):
            for value in values:
                result.extend((option, value))
        if args.output_dir:
            result.extend(
                ("--output-dir", str(Path(args.output_dir).expanduser().resolve()))
            )
        return result
    if args.operation == "corpus":
        result = [args.corpus_operation]
        if args.corpus_operation == "coverage":
            for option, values in (
                ("--tier", args.tiers),
                ("--category", args.categories),
                ("--split", args.splits),
                ("--qualification-state", args.qualification_states),
                ("--qualification-status", args.qualification_statuses),
            ):
                for value in values:
                    result.extend((option, value))
        elif args.corpus_operation == "qualify":
            result.extend(
                (
                    str(Path(args.lock).expanduser().resolve()),
                    str(Path(args.plan).expanduser().resolve()),
                )
            )
        else:
            result.append(str(Path(args.lock).expanduser().resolve()))
        if args.registry_dir:
            result.extend(
                ("--registry-dir", str(Path(args.registry_dir).expanduser().resolve()))
            )
        return result
    if args.operation == "workflow":
        if args.workflow_operation == "batch":
            result = [
                "batch",
                str(Path(args.manifest).expanduser().resolve()),
                "--authorized-through",
                args.authorized_through,
            ]
            for option, value in (
                ("--prompt-file", args.prompt_file),
                ("--proposal-file", args.proposal_file),
                ("--confirmation-file", args.confirmation_file),
                ("--output-dir", args.output_dir),
            ):
                if value:
                    result.extend((option, str(Path(value).expanduser().resolve())))
            if args.first_eligible:
                result.append("--first-eligible")
            result.extend(("--jobs", str(args.jobs)))
            return result
        if args.workflow_operation == "prepare":
            result = [
                args.workflow_operation,
                str(Path(args.input).expanduser().resolve()),
                "--input-kind",
                args.input_kind,
                "--objective",
                args.objective,
                "--authorized-through",
                args.authorized_through,
            ]
            if args.top:
                result.extend(("--top", args.top))
            for include_dir in args.include_dirs:
                result.extend(("-I", str(Path(include_dir).expanduser().resolve())))
            for definition in args.defines:
                result.extend(("-D", definition))
            for option, value in (
                ("--prompt-file", args.prompt_file),
                ("--proposal-file", args.proposal_file),
                ("--confirmation-file", args.confirmation_file),
                ("--output-dir", args.output_dir),
            ):
                if value:
                    result.extend((option, str(Path(value).expanduser().resolve())))
            if args.first_eligible:
                result.append("--first-eligible")
            if args.finding_id:
                result.extend(("--finding-id", args.finding_id))
            if args.start:
                result.append("--start")
            if args.compact:
                result.append("--compact")
            return result
        if args.workflow_operation == "start":
            result = [
                args.workflow_operation,
                str(Path(args.request).expanduser().resolve()),
                "--authorization",
                str(Path(args.authorization).expanduser().resolve()),
            ]
            if args.compact:
                result.append("--compact")
            return result
        if args.workflow_operation == "resume":
            result = [
                args.workflow_operation,
                args.workflow_id,
                "--authorization",
                str(Path(args.authorization).expanduser().resolve()),
            ]
            if args.compact:
                result.append("--compact")
            return result
        result = [args.workflow_operation, args.workflow_id]
        if args.compact:
            result.append("--compact")
        return result
    return [args.run_id]


def _validate_payload(payload: Any, operation: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunnerError("CLI JSON root must be an object", code="invalid_json")
    if operation == "corpus":
        if payload.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
            raise RunnerError(
                f"unsupported corpus schema: {payload.get('schema_version')!r}",
                code="unsupported_schema",
            )
        document_type = payload.get("document_type")
        if document_type not in {
            "rtl-advisor.corpus.coverage",
            "rtl-advisor.corpus.qualification",
            "rtl-advisor.corpus.tranche-validation",
            "rtl-advisor.corpus.registration",
            "rtl-advisor.corpus.error",
        }:
            raise RunnerError(
                f"unexpected corpus document type: {document_type!r}",
                code="unexpected_document",
            )
        expected_hash = payload.get("semantic_hash")
        core = {
            key: value for key, value in payload.items() if key != "semantic_hash"
        }
        if expected_hash != _json_hash(core):
            raise RunnerError(
                "corpus result semantic hash mismatch",
                code="semantic_hash_mismatch",
            )
        if document_type == "rtl-advisor.corpus.coverage":
            for name in (
                "coverage_id",
                "status",
                "read_only",
                "counting_unit",
                "population",
                "breakdowns",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"corpus coverage is missing {name}",
                        code="invalid_corpus_coverage",
                    )
            if payload.get("read_only") is not True or payload.get(
                "counting_unit"
            ) != "independent_design_lineage":
                raise RunnerError(
                    "corpus coverage does not preserve lineage-aware read-only counting",
                    code="invalid_corpus_coverage",
                )
        elif document_type == "rtl-advisor.corpus.qualification":
            for name in (
                "qualification_id",
                "status",
                "registry_mutation",
                "source_result_semantic_hash",
                "summary",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"corpus qualification is missing {name}",
                        code="invalid_corpus_qualification",
                    )
            if payload.get("registry_mutation") != "append_only":
                raise RunnerError(
                    "corpus qualification does not declare append-only registry mutation",
                    code="invalid_corpus_qualification",
                )
        elif document_type == "rtl-advisor.corpus.tranche-validation":
            for name in (
                "validation_id",
                "status",
                "read_only",
                "tranche",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"corpus tranche validation is missing {name}",
                        code="invalid_corpus_tranche_validation",
                    )
            if payload.get("read_only") is not True:
                raise RunnerError(
                    "corpus tranche validation is not read-only",
                    code="invalid_corpus_tranche_validation",
                )
        elif document_type == "rtl-advisor.corpus.registration":
            for name in (
                "registration_id",
                "status",
                "registry_mutation",
                "registered_count",
                "record_semantic_hashes",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"corpus registration is missing {name}",
                        code="invalid_corpus_registration",
                    )
            if payload.get("registry_mutation") != "append_only":
                raise RunnerError(
                    "corpus registration does not declare append-only registry mutation",
                    code="invalid_corpus_registration",
                )
        return payload
    if operation == "evidence":
        if payload.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
            raise RunnerError(
                f"unsupported evidence schema: {payload.get('schema_version')!r}",
                code="unsupported_schema",
            )
        document_type = payload.get("document_type")
        if document_type not in {
            "rtl-advisor.evidence.exploration",
            "rtl-advisor.evidence.error",
        }:
            raise RunnerError(
                f"unexpected evidence document type: {document_type!r}",
                code="unexpected_document",
            )
        expected_hash = payload.get("semantic_hash")
        core = {
            key: value for key, value in payload.items() if key != "semantic_hash"
        }
        if expected_hash != _json_hash(core):
            raise RunnerError(
                "evidence result semantic hash mismatch",
                code="semantic_hash_mismatch",
            )
        if document_type == "rtl-advisor.evidence.exploration":
            for name in (
                "exploration_id",
                "status",
                "read_only",
                "dataset_semantic_hash",
                "summary",
                "charts",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"evidence exploration is missing {name}",
                        code="invalid_evidence_exploration",
                    )
            if payload.get("read_only") is not True:
                raise RunnerError(
                    "evidence exploration is not read-only",
                    code="invalid_evidence_exploration",
                )
        return payload
    if operation == "workflow":
        if payload.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
            raise RunnerError(
                f"unsupported workflow schema: {payload.get('schema_version')!r}",
                code="unsupported_schema",
            )
        document_type = payload.get("document_type")
        if document_type not in {
            "rtl-advisor.workflow.preparation",
            "rtl-advisor.workflow.summary",
            "rtl-advisor.workflow.digest",
            "rtl-advisor.workflow.batch-summary",
            "rtl-advisor.workflow.error",
        }:
            raise RunnerError(
                f"unexpected workflow document type: {document_type!r}",
                code="unexpected_document",
            )
        expected_hash = payload.get("semantic_hash")
        core = {
            key: value for key, value in payload.items() if key != "semantic_hash"
        }
        if expected_hash != _json_hash(core):
            raise RunnerError(
                "workflow result semantic hash mismatch",
                code="semantic_hash_mismatch",
            )
        if document_type == "rtl-advisor.workflow.preparation":
            for name in (
                "workflow_id",
                "request_semantic_hash",
                "authorization_semantic_hash",
                "capabilities_semantic_hash",
                "authorized_through",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"workflow preparation is missing {name}",
                        code="invalid_workflow_preparation",
                    )
            if payload.get("status") != "prepared":
                raise RunnerError(
                    "workflow preparation has an invalid status",
                    code="invalid_workflow_preparation",
                )
        elif document_type == "rtl-advisor.workflow.summary":
            for name in (
                "workflow_id",
                "state_semantic_hash",
                "status",
                "decision",
                "completed_stages",
                "artifacts",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"workflow summary is missing {name}",
                        code="invalid_workflow_summary",
                    )
        elif document_type == "rtl-advisor.workflow.digest":
            for name in (
                "workflow_id",
                "status",
                "decision",
                "action",
                "scope",
                "source_locations",
                "rationale",
                "formal_status",
                "measurement_status",
                "profile_results",
                "evidence_complete",
                "next_action",
                "parent",
                "artifacts",
                "reproduce",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"workflow digest is missing {name}",
                        code="invalid_workflow_digest",
                    )
        elif document_type == "rtl-advisor.workflow.batch-summary":
            for name in (
                "batch_id",
                "status",
                "request_semantic_hash",
                "authorization_semantic_hash",
                "capabilities_semantic_hash",
                "counts",
                "items",
                "artifacts",
                "command",
            ):
                if name not in payload:
                    raise RunnerError(
                        f"workflow batch summary is missing {name}",
                        code="invalid_workflow_batch_summary",
                    )
            if payload.get("status") not in {"completed", "partial"}:
                raise RunnerError(
                    "workflow batch summary has an invalid status",
                    code="invalid_workflow_batch_summary",
                )
        return payload
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
    if operation == "corpus":
        if payload.get("document_type") == "rtl-advisor.corpus.error":
            return 2
        if payload.get("document_type") == "rtl-advisor.corpus.qualification":
            return 0 if payload.get("status") == "passed" else 4
        return 0
    if operation == "evidence":
        if payload.get("document_type") == "rtl-advisor.evidence.error":
            return 2
        return 4 if payload.get("status") == "partial" else 0
    if operation == "workflow":
        if payload.get("document_type") == "rtl-advisor.workflow.error":
            return 2
        if payload.get("status") in {"failed", "untrusted"}:
            return 2
        if payload.get("status") in {"blocked", "partial"} or payload.get("decision") in {
            "formal_failed",
            "formal_inconclusive",
            "evidence_incomplete",
        }:
            return 4
        return 0
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
        str(
            WORKFLOW_SCHEMA_VERSION
            if args.operation in {"workflow", "evidence", "corpus"}
            else SCHEMA_VERSION
        ),
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
    compact = (
        getattr(args, "operation", None) == "workflow"
        and (
            getattr(args, "workflow_operation", None) == "batch"
            or getattr(args, "compact", False)
        )
    )
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
