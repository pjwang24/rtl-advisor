from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
from typing import Sequence

from rtl_advisor.agent_api import (
    AgentAPIError,
    agent_candidate,
    agent_capabilities,
    agent_error_payload,
    agent_exit_code,
    agent_review,
    agent_verify,
)
from rtl_advisor.mvp_agent import (
    MVPAgentError,
    agent_v2_candidate,
    agent_v2_capabilities,
    agent_v2_error_payload,
    agent_v2_exit_code,
    agent_v2_measure,
    agent_v2_report,
    agent_v2_review,
    agent_v2_verify,
)
from rtl_advisor.mvp_schema import MVPSchemaError
from rtl_advisor.evidence_explorer import (
    EvidenceExplorerError,
    evidence_error_payload,
    evidence_exit_code,
    explore_evidence,
)
from rtl_advisor.corpus_workflow import (
    CorpusWorkflowError,
    corpus_coverage,
    corpus_error_payload,
    corpus_exit_code,
    qualify_corpus,
    register_corpus_tranche,
    validate_corpus_tranche,
)
from rtl_advisor.workflow_contract import WorkflowContractError
from rtl_advisor.workflow_runner import (
    WorkflowRunnerError,
    workflow_batch,
    workflow_error_payload,
    workflow_exit_code,
    workflow_prepare,
    workflow_report as run_workflow_report,
    workflow_resume,
    workflow_start,
    workflow_status,
)
from rtl_advisor.advisor_v2 import (
    AdvisorV2Error,
    PROFILES,
    analyze_live_rtl,
)
from rtl_advisor.advisor_v21 import (
    AdvisorV21Error,
    write_case_analysis_v21,
)
from rtl_advisor.advisor_v22 import (
    AdvisorV22Error,
    write_case_analysis_v22,
)
from rtl_advisor.advisor_explanation_v2 import (
    AdvisorExplanationError,
    explain_gate_decision,
)
from rtl_advisor.benchmark import (
    ARM_SPECS,
    BENCHMARK_SUITES,
    BenchmarkError,
    generate_benchmark_report,
    run_benchmark,
)
from rtl_advisor.benchmark_v2 import (
    BenchmarkV2Error,
    create_benchmark_lock,
    record_blind_unseal,
    verify_benchmark_lock,
)
from rtl_advisor.benchmark_v21 import (
    BenchmarkV21Error,
    create_benchmark_lock_v21,
    record_blind_unseal_v21,
    verify_benchmark_lock_v21,
)
from rtl_advisor.benchmark_v22 import (
    BenchmarkV22Error,
    create_benchmark_lock_v22,
)
from rtl_advisor.benchmark_runner_v2 import (
    BenchmarkRunnerV2Error,
    build_v2_benchmark_report,
    run_locked_v2_benchmark,
)
from rtl_advisor.benchmark_runner_v21 import (
    BenchmarkRunnerV21Error,
    build_v21_benchmark_report,
    run_locked_v21_benchmark,
)
from rtl_advisor.calibration import CalibrationError, train_v2_models
from rtl_advisor.calibration_v21 import CalibrationV21Error, train_v21_models
from rtl_advisor.calibration_v22 import CalibrationV22Error, train_v22_models
from rtl_advisor.candidate_v2 import CandidateV2Error, emit_selected_candidate
from rtl_advisor.config import ConfigError, ProjectConfig, load_config
from rtl_advisor.diagnostic_v22 import DiagnosticV22Error, diagnose_v22
from rtl_advisor.frontend_api import FrontendAPIError
from rtl_advisor.frontend_server import (
    FrontendServerError,
    serve_frontend,
)
from rtl_advisor.family_study import (
    FamilyStudyError,
    read_family_study,
    report_family_study,
    run_family_study,
)
from rtl_advisor.codex_analysis import CodexAnalysisError, analyze_with_codex
from rtl_advisor.corpus import (
    RESOURCE_SHARING_FAMILY,
    SUPPORTED_SUITES,
    CorpusError,
    available_families,
    default_case_id,
    default_suite_parameters,
    generate_case,
    load_manifest,
)
from rtl_advisor.corpus_registry import (
    CATEGORIES,
    QUALIFICATION_STATES,
    QUALIFICATION_STATUSES,
    SPLIT_ROLES,
    CorpusRegistryError,
    CorpusRegistryV1,
)
from rtl_advisor.corpus_baseline import CorpusBaselineError, characterize_tranche_baselines
from rtl_advisor.corpus_behavior import CorpusBehaviorError, qualify_tranche_behavior
from rtl_advisor.corpus_qualification import (
    CorpusQualificationError,
    qualify_tranche,
)
from rtl_advisor.graph import GraphError, build_graph
from rtl_advisor.models import CheckResult, SetupReport
from rtl_advisor.openroad_v2 import (
    DEFAULT_ORFS_IMAGE,
    OpenROADV2Error,
    build_openroad_report,
    create_openroad_lock,
    create_openroad_plan,
    run_openroad_v2,
)
from rtl_advisor.postmortem_v2 import V2PostmortemError, diagnose_v2
from rtl_advisor.patch_validation import (
    PatchValidationError,
    validate_candidate_patch,
)
from rtl_advisor.rules import write_rule_analysis
from rtl_advisor.synthesis import SynthesisError, synthesize_case
from rtl_advisor.synthesis_redundancy import (
    SynthesisRedundancyError,
    run_redundancy_benchmark,
)
from rtl_advisor.synthesis_robustness_full import (
    SynthesisRobustnessFullError,
    run_full_sweep,
)
from rtl_advisor.sequential_equivalence import (
    SequentialEquivalenceError,
    record_p2_proof,
    run_p2_proof,
)
from rtl_advisor.transaction_equivalence import (
    TransactionEquivalenceError,
    record_p3_proof,
    run_p3_proof,
)
from rtl_advisor.suite import (
    SuiteError,
    generate_suite,
    validate_suite,
)
from rtl_advisor.tools import (
    DownloadError,
    ToolExecutionError,
    download_text,
    download_verified,
    first_output_line,
    run_command,
    sha256_file,
)
from rtl_advisor.tranche_lock import (
    TrancheLockError,
    load_tranche_lock,
    materialize_tranche_references,
    tranche_summary,
    verify_tranche_sources,
)
from rtl_advisor.verification import (
    VerificationError,
    lint_case,
    prove_case_candidates,
)
from rtl_advisor.v2_corpus import V2_SPLITS, V2CorpusError, generate_v2_suite
from rtl_advisor.v2_validation import V2ValidationError, validate_v2_suite
from rtl_advisor.v21_corpus import (
    V21_SPLITS,
    V21CorpusError,
    generate_v21_suite,
)
from rtl_advisor.v21_validation import V21ValidationError, validate_v21_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtl-advisor",
        description="Pre-synthesis RTL analysis and benchmarking harness",
    )
    parser.add_argument(
        "--config",
        default="rtl-advisor.toml",
        help="project configuration file (default: rtl-advisor.toml)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser(
        "setup",
        help="verify required tools and the pinned standard-cell library",
    )
    setup.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="print the environment report as JSON",
    )
    setup.add_argument(
        "--no-download",
        action="store_true",
        help="report a missing Liberty file instead of downloading it",
    )

    corpus = subparsers.add_parser(
        "corpus",
        help="manage generated RTL cases and the qualified source registry",
    )
    corpus_subparsers = corpus.add_subparsers(dest="corpus_command", required=True)
    registry_add = corpus_subparsers.add_parser(
        "add",
        help="validate and append a reference or variant manifest to Corpus Registry V1",
    )
    registry_add.add_argument("manifest", help="reference or variant manifest JSON")
    registry_add.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    registry_add.add_argument("--json", action="store_true", dest="json_output")
    registry_advance = corpus_subparsers.add_parser(
        "advance",
        help="append a qualification or variant-state successor to an existing record",
    )
    registry_advance.add_argument("manifest", help="successor manifest JSON")
    registry_advance.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    registry_advance.add_argument("--json", action="store_true", dest="json_output")
    registry_validate = corpus_subparsers.add_parser(
        "validate",
        help="validate Corpus Registry V1 or one manifest against its records",
    )
    registry_validate.add_argument(
        "manifest",
        nargs="?",
        help="optional manifest to validate without adding it",
    )
    registry_validate.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    registry_validate.add_argument("--json", action="store_true", dest="json_output")
    registry_list = corpus_subparsers.add_parser(
        "list",
        help="list registered references and variants",
    )
    registry_list.add_argument(
        "--kind",
        choices=("all", "references", "variants"),
        default="all",
    )
    registry_list.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    registry_list.add_argument("--json", action="store_true", dest="json_output")
    registry_summary = corpus_subparsers.add_parser(
        "summary",
        help="summarize references by tier, category, lineage, and qualification state",
    )
    registry_summary.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    registry_summary.add_argument("--json", action="store_true", dest="json_output")
    validate_tranche = corpus_subparsers.add_parser(
        "validate-tranche",
        help="validate a frozen corpus tranche lock without running synthesis",
    )
    validate_tranche.add_argument("lock", help="frozen tranche lock JSON")
    validate_tranche.add_argument("--json", action="store_true", dest="json_output")
    register_tranche = corpus_subparsers.add_parser(
        "register-tranche",
        help="append every frozen reference in a tranche to Corpus Registry V1",
    )
    register_tranche.add_argument("lock", help="frozen tranche lock JSON")
    register_tranche.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    register_tranche.add_argument("--json", action="store_true", dest="json_output")
    verify_tranche = corpus_subparsers.add_parser(
        "verify-tranche-sources",
        help="verify frozen archives, licenses, and selected RTL source hashes",
    )
    verify_tranche.add_argument("lock", help="frozen tranche lock JSON")
    verify_tranche.add_argument(
        "--source-root",
        action="append",
        default=[],
        metavar="UPSTREAM_ID=PATH",
        help="acquired upstream root; repeat once per upstream project",
    )
    verify_tranche.add_argument("--json", action="store_true", dest="json_output")
    qualify_tranche_parser = corpus_subparsers.add_parser(
        "qualify-tranche",
        help="pin compile contexts and reproduce lint/build without candidate synthesis",
    )
    qualify_tranche_parser.add_argument("lock", help="frozen tranche lock JSON")
    qualify_tranche_parser.add_argument("plan", help="frozen qualification plan JSON")
    qualify_tranche_parser.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    qualify_tranche_parser.add_argument("--json", action="store_true", dest="json_output")
    baseline_tranche_parser = corpus_subparsers.add_parser(
        "baseline-tranche",
        help="run the two pinned baseline-only synthesis recipes for a frozen tranche",
    )
    baseline_tranche_parser.add_argument("lock", help="frozen tranche lock JSON")
    baseline_tranche_parser.add_argument("plan", help="frozen qualification plan JSON")
    baseline_tranche_parser.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    baseline_tranche_parser.add_argument("--json", action="store_true", dest="json_output")
    behavior_tranche_parser = corpus_subparsers.add_parser(
        "behavior-tranche",
        help="run frozen behavioral checks and qualify or block every tranche reference",
    )
    behavior_tranche_parser.add_argument("lock", help="frozen tranche lock JSON")
    behavior_tranche_parser.add_argument("plan", help="frozen behavior plan JSON")
    behavior_tranche_parser.add_argument(
        "--record-registry",
        action="store_true",
        help="append behavior, qualified, and blocked states to the corpus registry",
    )
    behavior_tranche_parser.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    behavior_tranche_parser.add_argument("--json", action="store_true", dest="json_output")
    prove_p2_parser = corpus_subparsers.add_parser(
        "prove-p2",
        help="run a hash-bound same-cycle sequential equivalence miter",
    )
    prove_p2_parser.add_argument("plan", help="frozen P2 proof plan JSON")
    prove_p2_parser.add_argument(
        "--formal-command",
        default="sby",
        help="formal runner executable (default: sby)",
    )
    prove_p2_parser.add_argument(
        "--record-registry",
        action="store_true",
        help="append declared, prepared, and proof states to the corpus registry",
    )
    prove_p2_parser.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    prove_p2_parser.add_argument("--json", action="store_true", dest="json_output")
    prove_p3_parser = corpus_subparsers.add_parser(
        "prove-p3",
        help="run a hash-bound transaction-ordered sequential proof",
    )
    prove_p3_parser.add_argument("plan", help="frozen P3 proof plan JSON")
    prove_p3_parser.add_argument(
        "--formal-command",
        default="sby",
        help="formal runner executable (default: sby)",
    )
    prove_p3_parser.add_argument(
        "--record-registry",
        action="store_true",
        help="append declared, prepared, and proof states to the corpus registry",
    )
    prove_p3_parser.add_argument(
        "--registry-dir",
        help="registry directory (default: corpus/registry-v1)",
    )
    prove_p3_parser.add_argument("--json", action="store_true", dest="json_output")
    generate = corpus_subparsers.add_parser(
        "generate",
        help="generate a deterministic RTL case from a registered family",
    )
    generate.add_argument(
        "--family",
        choices=available_families(),
        default=RESOURCE_SHARING_FAMILY,
    )
    generate.add_argument(
        "--suite",
        choices=SUPPORTED_SUITES,
        default="development",
    )
    generate.add_argument("--case-id")
    generate.add_argument(
        "--width",
        type=int,
        help="operand width (default: 16 development, 17 heldout)",
    )
    generate.add_argument(
        "--seed",
        type=int,
        help="generation seed (default: suite-specific and disjoint)",
    )
    generate.add_argument(
        "--output-dir",
        help="case output directory (default: corpus/<suite>/<case-id>)",
    )
    generate.add_argument("--force", action="store_true")

    generate_suite_parser = corpus_subparsers.add_parser(
        "generate-suite",
        help="generate the complete deterministic development or held-out suite",
    )
    generate_suite_parser.add_argument(
        "--suite",
        choices=SUPPORTED_SUITES,
        required=True,
    )
    generate_suite_parser.add_argument("--force", action="store_true")

    validate_suite_parser = corpus_subparsers.add_parser(
        "validate-suite",
        help="lint, prove, and synthesize every case in a generated suite",
    )
    validate_suite_parser.add_argument(
        "--suite",
        choices=SUPPORTED_SUITES,
        required=True,
    )
    validate_suite_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    generate_v2_parser = corpus_subparsers.add_parser(
        "generate-suite-v2",
        help="generate the frozen topology-diverse calibration or blind suite",
    )
    generate_v2_parser.add_argument("--split", choices=V2_SPLITS, required=True)
    generate_v2_parser.add_argument("--force", action="store_true")
    generate_v21_parser = corpus_subparsers.add_parser(
        "generate-suite-v21",
        help="generate the disjoint V2.1 calibration or blind topology suite",
    )
    generate_v21_parser.add_argument("--split", choices=V21_SPLITS, required=True)
    generate_v21_parser.add_argument("--force", action="store_true")
    validate_v21_parser = corpus_subparsers.add_parser(
        "validate-suite-v21",
        help="lint/prove a V2.1 split and synthesize calibration ground truth",
    )
    validate_v21_parser.add_argument("--split", choices=V21_SPLITS, required=True)
    validate_v21_parser.add_argument(
        "--synthesize",
        action="store_true",
        help="synthesize v0-v3 (calibration-v21 only before blind unseal)",
    )
    validate_v21_parser.add_argument("--workers", type=int, default=4)
    validate_v21_parser.add_argument("--force", action="store_true")
    validate_v21_parser.add_argument("--json", action="store_true", dest="json_output")
    validate_v2_parser = corpus_subparsers.add_parser(
        "validate-suite-v2",
        help="lint/prove a v2 split and synthesize calibration ground truth",
    )
    validate_v2_parser.add_argument("--split", choices=V2_SPLITS, required=True)
    validate_v2_parser.add_argument(
        "--synthesize",
        action="store_true",
        help="synthesize v0-v3 (allowed for calibration-v2 only before unseal)",
    )
    validate_v2_parser.add_argument("--workers", type=int, default=4)
    validate_v2_parser.add_argument("--force", action="store_true")
    validate_v2_parser.add_argument("--json", action="store_true", dest="json_output")

    lint = subparsers.add_parser("lint", help="lint every RTL variant in a case")
    lint.add_argument("case", help="case directory or manifest path")
    lint.add_argument("--json", action="store_true", dest="json_output")

    equivalence = subparsers.add_parser(
        "equivalence",
        help="formally compare case candidates with the baseline",
    )
    equivalence.add_argument("case", help="case directory or manifest path")
    equivalence.add_argument(
        "--candidate",
        default="all",
        help="candidate variant ID or 'all' (default: all)",
    )
    equivalence.add_argument("--json", action="store_true", dest="json_output")

    synth = subparsers.add_parser(
        "synth",
        help="map proven-equivalent variants and compare delay and area",
    )
    synth.add_argument("case", help="case directory or manifest path")
    synth.add_argument(
        "--variant",
        default="all",
        help="variant ID or 'all' (default: all proven-equivalent variants)",
    )
    synth.add_argument("--force", action="store_true", help="ignore cached results")
    synth.add_argument("--json", action="store_true", dest="json_output")

    graph = subparsers.add_parser(
        "graph",
        help="extract a hierarchy-preserving RTL graph with Yosys",
    )
    graph.add_argument("case", help="case directory or manifest path")
    graph.add_argument("--variant", default="v0", help="variant ID (default: v0)")
    graph.add_argument("--force", action="store_true", help="ignore cached results")
    graph.add_argument("--json", action="store_true", dest="json_output")

    analyze = subparsers.add_parser(
        "analyze",
        help="produce pre-synthesis recommendations from an RTL graph",
    )
    analyze.add_argument("case", help="case directory or manifest path")
    analyze.add_argument("--variant", default="v0", help="variant ID (default: v0)")
    analyze.add_argument(
        "--mode",
        choices=("rules", "codex", "hybrid"),
        default="rules",
        help="analysis engine (default: rules)",
    )
    analyze.add_argument(
        "--effort",
        choices=("xhigh", "ultra"),
        help="Codex reasoning effort (default: configured xhigh)",
    )
    analyze.add_argument(
        "--force",
        action="store_true",
        help="ignore graph and model-result caches",
    )
    analyze.add_argument(
        "--emit-patch",
        action="store_true",
        help="emit and validate an isolated candidate patch",
    )
    analyze.add_argument(
        "--patch-candidate",
        default="v1",
        help="generated candidate to validate when --emit-patch is set (default: v1)",
    )
    analyze.add_argument("--json", action="store_true", dest="json_output")

    analyze_rtl = subparsers.add_parser(
        "analyze-rtl",
        help="analyze authorized RTL files or a filelist with the v2 safety gate",
    )
    analyze_rtl.add_argument("--top", required=True, help="elaboration top module")
    live_input = analyze_rtl.add_mutually_exclusive_group(required=True)
    live_input.add_argument(
        "--file",
        action="append",
        default=[],
        help="RTL source file; repeat for multiple files",
    )
    live_input.add_argument("--filelist", help="Slang-style source filelist")
    analyze_rtl.add_argument(
        "-I",
        action="append",
        default=[],
        dest="include_dirs",
        help="include directory; repeat as needed",
    )
    analyze_rtl.add_argument(
        "-D",
        action="append",
        default=[],
        dest="defines",
        help="preprocessor definition NAME or NAME=VALUE",
    )
    analyze_rtl.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="balanced",
    )
    analyze_rtl.add_argument(
        "--mode",
        choices=("calibrated", "advisor"),
        default="calibrated",
    )
    analyze_rtl.add_argument("--output", help="artifact output directory")
    analyze_rtl.add_argument(
        "--gate-model",
        help="calibrated gate JSON (default: artifacts/models/v2/gate.json)",
    )
    analyze_rtl.add_argument("--emit-candidates", action="store_true")
    analyze_rtl.add_argument(
        "--candidate-source",
        choices=("templates", "templates+codex"),
        default="templates",
    )
    analyze_rtl.add_argument("--allow-model-source", action="store_true")
    analyze_rtl.add_argument("--force", action="store_true")
    analyze_rtl.add_argument("--json", action="store_true", dest="json_output")

    analyze_v21 = subparsers.add_parser(
        "analyze-v21",
        help="run the experimental V2.1 deterministic advisor on a generated case",
    )
    analyze_v21.add_argument("case", help="generated case directory or manifest")
    analyze_v21.add_argument("--mode", choices=("point", "risk", "safe"), default="safe")
    analyze_v21.add_argument("--profile", choices=tuple(PROFILES), default="balanced")
    analyze_v21.add_argument("--output", help="analysis output directory")
    analyze_v21.add_argument("--force-graph", action="store_true")
    analyze_v21.add_argument("--emit-candidate", action="store_true")
    analyze_v21.add_argument("--json", action="store_true", dest="json_output")

    analyze_v22 = subparsers.add_parser(
        "analyze-v22",
        help="run the family-aware V2.2 deterministic advisor on a generated case",
    )
    analyze_v22.add_argument("case", help="generated case directory or manifest")
    analyze_v22.add_argument("--mode", choices=("point", "risk", "safe"), default="safe")
    analyze_v22.add_argument("--profile", choices=tuple(PROFILES), default="balanced")
    analyze_v22.add_argument("--output", help="analysis output directory")
    analyze_v22.add_argument("--force-graph", action="store_true")
    analyze_v22.add_argument("--emit-candidate", action="store_true")
    analyze_v22.add_argument("--json", action="store_true", dest="json_output")

    frontend = subparsers.add_parser(
        "frontend",
        help="serve the local read-only RTL Advisor dashboard",
    )
    frontend.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback bind address (default: 127.0.0.1)",
    )
    frontend.add_argument(
        "--port",
        type=int,
        default=8765,
        help="local HTTP port (default: 8765)",
    )

    agent = subparsers.add_parser(
        "agent",
        help="stable JSON automation interface for terminal and Codex clients",
    )
    agent_subparsers = agent.add_subparsers(dest="agent_command", required=True)
    agent_capabilities_parser = agent_subparsers.add_parser(
        "capabilities",
        help="report supported inputs, tools, models, and operations",
    )
    agent_capabilities_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_capabilities_parser.add_argument(
        "--schema-version", type=int, choices=(1, 2), default=1
    )
    agent_review_parser = agent_subparsers.add_parser(
        "review",
        help="run a read-only RTL review through the current release gates",
    )
    agent_review_parser.add_argument(
        "input",
        help="generated case, manifest, RTL source, filelist, or normalized input.json",
    )
    agent_review_parser.add_argument(
        "--objective",
        choices=("timing", "area", "balanced"),
        default="balanced",
    )
    agent_review_parser.add_argument(
        "--top", help="elaboration top for a source file or filelist"
    )
    agent_review_parser.add_argument(
        "-I", action="append", default=[], dest="include_dirs"
    )
    agent_review_parser.add_argument(
        "-D", action="append", default=[], dest="defines"
    )
    agent_review_parser.add_argument("--gate-model")
    agent_review_parser.add_argument("--force", action="store_true")
    agent_review_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_review_parser.add_argument(
        "--schema-version", type=int, choices=(1, 2), default=1
    )
    agent_candidate_parser = agent_subparsers.add_parser(
        "candidate",
        help="prepare an isolated candidate from an eligible review",
    )
    agent_candidate_parser.add_argument("run_id")
    agent_candidate_parser.add_argument("--finding", required=True)
    agent_candidate_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_candidate_parser.add_argument(
        "--schema-version", type=int, choices=(1, 2), default=1
    )
    agent_verify_parser = agent_subparsers.add_parser(
        "verify",
        help="run current hash-matched lint and formal equivalence",
    )
    agent_verify_parser.add_argument("run_id")
    agent_verify_parser.add_argument("--candidate", required=True)
    agent_verify_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_verify_parser.add_argument(
        "--schema-version", type=int, choices=(1, 2), default=1
    )
    agent_measure_parser = agent_subparsers.add_parser(
        "measure",
        help="measure a formally proven candidate with both pinned synthesis recipes",
    )
    agent_measure_parser.add_argument("run_id")
    agent_measure_parser.add_argument("--candidate", required=True)
    agent_measure_parser.add_argument(
        "--schema-version", type=int, choices=(2,), default=2
    )
    agent_measure_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_report_parser = agent_subparsers.add_parser(
        "report",
        help="derive immutable JSON and HTML reports from stored run artifacts",
    )
    agent_report_parser.add_argument("run_id")
    agent_report_parser.add_argument(
        "--schema-version", type=int, choices=(2,), default=2
    )
    agent_report_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_evidence_parser = agent_subparsers.add_parser(
        "evidence",
        help="explore immutable measurement evidence with compact chart specs",
    )
    evidence_subparsers = agent_evidence_parser.add_subparsers(
        dest="evidence_command", required=True
    )
    evidence_explore_parser = evidence_subparsers.add_parser(
        "explore", help="derive filtered read-only chart data and aggregates"
    )
    evidence_explore_parser.add_argument(
        "--workflow-id", action="append", default=[], dest="workflow_ids"
    )
    evidence_explore_parser.add_argument(
        "--run-id", action="append", default=[], dest="run_ids"
    )
    evidence_explore_parser.add_argument(
        "--profile",
        action="append",
        choices=("standard", "stronger"),
        default=[],
        dest="profiles",
    )
    evidence_explore_parser.add_argument(
        "--objective",
        action="append",
        choices=("timing", "area", "balanced"),
        default=[],
        dest="objectives",
    )
    evidence_explore_parser.add_argument(
        "--classification",
        action="append",
        choices=("improved", "neutral", "regressed"),
        default=[],
        dest="classifications",
    )
    evidence_explore_parser.add_argument(
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
    evidence_explore_parser.add_argument(
        "--transformation",
        action="append",
        default=[],
        dest="transformations",
    )
    evidence_explore_parser.add_argument(
        "--source-kind",
        action="append",
        choices=("agent_v2_run", "family_study"),
        default=[],
        dest="source_kinds",
    )
    evidence_explore_parser.add_argument("--output-dir")
    evidence_explore_parser.add_argument(
        "--schema-version", type=int, choices=(1,), default=1
    )
    evidence_explore_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    agent_corpus_parser = agent_subparsers.add_parser(
        "corpus",
        help="qualify frozen open-RTL tranches and audit lineage-aware coverage",
    )
    agent_corpus_subparsers = agent_corpus_parser.add_subparsers(
        dest="agent_corpus_command", required=True
    )
    corpus_coverage_parser = agent_corpus_subparsers.add_parser(
        "coverage", help="derive a read-only independent-design coverage audit"
    )
    corpus_coverage_parser.add_argument(
        "--tier", action="append", choices=("A", "B", "C", "D"), default=[], dest="tiers"
    )
    corpus_coverage_parser.add_argument(
        "--category", action="append", choices=CATEGORIES, default=[], dest="categories"
    )
    corpus_coverage_parser.add_argument(
        "--split", action="append", choices=SPLIT_ROLES, default=[], dest="splits"
    )
    corpus_coverage_parser.add_argument(
        "--qualification-state",
        action="append",
        choices=QUALIFICATION_STATES,
        default=[],
        dest="qualification_states",
    )
    corpus_coverage_parser.add_argument(
        "--qualification-status",
        action="append",
        choices=QUALIFICATION_STATUSES,
        default=[],
        dest="qualification_statuses",
    )
    corpus_qualify_parser = agent_corpus_subparsers.add_parser(
        "qualify", help="run a frozen append-only corpus qualification plan"
    )
    corpus_qualify_parser.add_argument("lock", help="frozen tranche lock JSON")
    corpus_qualify_parser.add_argument("plan", help="frozen qualification plan JSON")
    corpus_validate_parser = agent_corpus_subparsers.add_parser(
        "validate", help="validate an already-frozen corpus tranche lock"
    )
    corpus_validate_parser.add_argument("lock", help="frozen tranche lock JSON")
    corpus_register_parser = agent_corpus_subparsers.add_parser(
        "register", help="append a validated frozen tranche to Corpus Registry V1"
    )
    corpus_register_parser.add_argument("lock", help="frozen tranche lock JSON")
    for corpus_parser in (
        corpus_coverage_parser,
        corpus_qualify_parser,
        corpus_validate_parser,
        corpus_register_parser,
    ):
        corpus_parser.add_argument(
            "--registry-dir",
            help="registry directory (default: corpus/registry-v1)",
        )
        corpus_parser.add_argument(
            "--schema-version", type=int, choices=(1,), default=1
        )
        corpus_parser.add_argument("--json", action="store_true", dest="json_output")
    agent_workflow_parser = agent_subparsers.add_parser(
        "workflow",
        help="run or inspect a deterministic multi-stage Agent V2 workflow",
    )
    workflow_subparsers = agent_workflow_parser.add_subparsers(
        dest="workflow_command", required=True
    )
    workflow_prepare_parser = workflow_subparsers.add_parser(
        "prepare",
        help="normalize bounded intent into hashed request and authorization records",
    )
    workflow_prepare_parser.add_argument("input")
    workflow_prepare_parser.add_argument(
        "--input-kind",
        choices=(
            "generated_rtl",
            "explicitly_approved_open_rtl",
            "qualified_corpus_reference",
        ),
        required=True,
    )
    workflow_prepare_parser.add_argument(
        "--objective", choices=("timing", "area", "balanced"), default="balanced"
    )
    workflow_prepare_parser.add_argument(
        "--authorized-through",
        choices=("review", "candidate", "verify", "measure"),
        required=True,
    )
    workflow_prepare_parser.add_argument("--top")
    workflow_prepare_parser.add_argument(
        "-I", action="append", default=[], dest="include_dirs"
    )
    workflow_prepare_parser.add_argument(
        "-D", action="append", default=[], dest="defines"
    )
    authorization_basis = workflow_prepare_parser.add_mutually_exclusive_group(
        required=True
    )
    authorization_basis.add_argument("--prompt-file")
    authorization_basis.add_argument("--proposal-file")
    workflow_prepare_parser.add_argument("--confirmation-file")
    candidate_selection = workflow_prepare_parser.add_mutually_exclusive_group()
    candidate_selection.add_argument("--first-eligible", action="store_true")
    candidate_selection.add_argument("--finding-id")
    workflow_prepare_parser.add_argument("--output-dir")
    workflow_prepare_parser.add_argument(
        "--start",
        action="store_true",
        help="start the prepared workflow immediately",
    )
    workflow_start_parser = workflow_subparsers.add_parser(
        "start", help="start from frozen request and authorization documents"
    )
    workflow_start_parser.add_argument("request")
    workflow_start_parser.add_argument("--authorization", required=True)
    workflow_status_parser = workflow_subparsers.add_parser(
        "status", help="read the current compact workflow summary"
    )
    workflow_status_parser.add_argument("workflow_id")
    workflow_resume_parser = workflow_subparsers.add_parser(
        "resume", help="resume with a new explicit authorization document"
    )
    workflow_resume_parser.add_argument("workflow_id")
    workflow_resume_parser.add_argument("--authorization", required=True)
    workflow_report_parser = workflow_subparsers.add_parser(
        "report", help="derive the compact summary and immutable Agent report"
    )
    workflow_report_parser.add_argument("workflow_id")
    for workflow_parser in (
        workflow_prepare_parser,
        workflow_start_parser,
        workflow_status_parser,
        workflow_resume_parser,
        workflow_report_parser,
    ):
        workflow_parser.add_argument(
            "--schema-version", type=int, choices=(1,), default=1
        )
        workflow_parser.add_argument(
            "--json", action="store_true", dest="json_output"
        )
        workflow_parser.add_argument(
            "--compact",
            action="store_true",
            help="return the bounded workflow digest instead of the full summary",
        )
    workflow_batch_parser = workflow_subparsers.add_parser(
        "batch", help="run an ordered manifest with one capability discovery"
    )
    workflow_batch_parser.add_argument("manifest")
    workflow_batch_parser.add_argument(
        "--authorized-through",
        choices=("review", "candidate", "verify", "measure"),
        required=True,
    )
    batch_basis = workflow_batch_parser.add_mutually_exclusive_group(required=True)
    batch_basis.add_argument("--prompt-file")
    batch_basis.add_argument("--proposal-file")
    workflow_batch_parser.add_argument("--confirmation-file")
    workflow_batch_parser.add_argument("--first-eligible", action="store_true")
    workflow_batch_parser.add_argument("--output-dir")
    workflow_batch_parser.add_argument("--jobs", type=int, choices=(1, 2, 3, 4), default=1)
    workflow_batch_parser.add_argument(
        "--schema-version", type=int, choices=(1,), default=1
    )
    workflow_batch_parser.add_argument(
        "--json", action="store_true", dest="json_output"
    )

    study = subparsers.add_parser(
        "study",
        help="validate, execute, and report immutable family evidence studies",
    )
    study_subparsers = study.add_subparsers(dest="study_command", required=True)
    study_validate = study_subparsers.add_parser(
        "validate",
        help="validate a frozen family-study manifest without running tools",
    )
    study_validate.add_argument("manifest")
    study_validate.add_argument("--json", action="store_true", dest="json_output")
    study_run = study_subparsers.add_parser(
        "run",
        help="run or resume one clean frozen family-study repeat",
    )
    study_run.add_argument("manifest")
    study_run.add_argument(
        "--repeat",
        choices=("repeat-1", "repeat-2"),
        required=True,
    )
    study_run.add_argument("--json", action="store_true", dest="json_output")
    study_report = study_subparsers.add_parser(
        "report",
        help="derive family and product claim gates from stored repeats",
    )
    study_report.add_argument("study_id")
    study_report.add_argument("--json", action="store_true", dest="json_output")

    model = subparsers.add_parser("model", help="train and inspect v2 gate models")
    model_subparsers = model.add_subparsers(dest="model_command", required=True)
    model_train = model_subparsers.add_parser(
        "train-v2",
        help="train the calibrated gate and random-forest challenger",
    )
    model_train.add_argument(
        "--suite",
        default="calibration-v2",
        choices=("calibration-v2",),
    )
    model_train.add_argument("--force-graph", action="store_true")
    model_train.add_argument("--json", action="store_true", dest="json_output")
    model_train_v21 = model_subparsers.add_parser(
        "train-v21",
        help="train the grouped-OOF V2.1 regressors, classifiers, OOD model, and policy",
    )
    model_train_v21.add_argument("--force-graph", action="store_true")
    model_train_v21.add_argument("--json", action="store_true", dest="json_output")
    model_train_v22 = model_subparsers.add_parser(
        "train-v22",
        help="train the frozen family-aware V2.2 eligibility policy",
    )
    model_train_v22.add_argument("--json", action="store_true", dest="json_output")

    benchmark = subparsers.add_parser(
        "benchmark",
        help="run and report blinded multi-arm benchmarks",
    )
    benchmark_subparsers = benchmark.add_subparsers(
        dest="benchmark_command",
        required=True,
    )
    benchmark_run = benchmark_subparsers.add_parser(
        "run",
        help="execute a stored smoke or pilot benchmark plan",
    )
    benchmark_run.add_argument(
        "--suite",
        choices=BENCHMARK_SUITES,
        required=True,
    )
    benchmark_run.add_argument(
        "--arm",
        choices=("all", *ARM_SPECS),
        default="all",
    )
    benchmark_run.add_argument("--force", action="store_true")
    benchmark_run.add_argument("--json", action="store_true", dest="json_output")
    benchmark_report = benchmark_subparsers.add_parser(
        "report",
        help="rebuild a report solely from stored raw run records",
    )
    benchmark_report.add_argument(
        "--suite",
        choices=BENCHMARK_SUITES,
        required=True,
    )
    benchmark_report.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
    )
    benchmark_lock_v2 = benchmark_subparsers.add_parser(
        "lock-v2",
        help="freeze v2 suites, models, profiles, prompt, and 264-call plan",
    )
    benchmark_lock_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_unseal_v2 = benchmark_subparsers.add_parser(
        "unseal-v2",
        help="verify the v2 lock and record the first blind evaluation",
    )
    benchmark_unseal_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_lock_v21 = benchmark_subparsers.add_parser(
        "lock-v21",
        help="freeze V2.1 suites, models, physical evidence, and 264-call plan",
    )
    benchmark_lock_v21.add_argument("--json", action="store_true", dest="json_output")
    benchmark_lock_v22 = benchmark_subparsers.add_parser(
        "lock-v22",
        help="freeze V2.2 only after family-risk calibration and formal gates pass",
    )
    benchmark_lock_v22.add_argument("--json", action="store_true", dest="json_output")
    benchmark_unseal_v21 = benchmark_subparsers.add_parser(
        "unseal-v21",
        help="verify the V2.1 lock and record the first blind evaluation",
    )
    benchmark_unseal_v21.add_argument("--json", action="store_true", dest="json_output")
    benchmark_run_v21 = benchmark_subparsers.add_parser(
        "run-v21",
        help="unseal and execute the exact locked 480-record V2.1 benchmark",
    )
    benchmark_run_v21.add_argument(
        "--synthesis-workers", type=int, choices=range(1, 9), default=4
    )
    benchmark_run_v21.add_argument("--force", action="store_true")
    benchmark_run_v21.add_argument("--json", action="store_true", dest="json_output")
    benchmark_report_v21 = benchmark_subparsers.add_parser(
        "report-v21",
        help="rebuild the V2.1 report solely from locked raw records",
    )
    benchmark_report_v21.add_argument("--json", action="store_true", dest="json_output")
    benchmark_run_v2 = benchmark_subparsers.add_parser(
        "run-v2",
        help="unseal and execute the exact locked 480-record v2 benchmark",
    )
    benchmark_run_v2.add_argument(
        "--synthesis-workers",
        type=int,
        choices=range(1, 9),
        default=4,
    )
    benchmark_run_v2.add_argument("--force", action="store_true")
    benchmark_run_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_report_v2 = benchmark_subparsers.add_parser(
        "report-v2",
        help="rebuild the v2 report solely from locked raw run records",
    )
    benchmark_report_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_diagnose_v2 = benchmark_subparsers.add_parser(
        "diagnose-v2",
        help="diagnose the immutable v2 result without changing its report",
    )
    benchmark_diagnose_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_diagnose_v22 = benchmark_subparsers.add_parser(
        "diagnose-v22",
        help="decompose the frozen V2.2 calibration failure without blind labels",
    )
    benchmark_diagnose_v22.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    benchmark_redundancy_v1 = benchmark_subparsers.add_parser(
        "synthesis-redundancy-v1",
        help="test which RTL candidate gains survive stronger Yosys synthesis",
    )
    benchmark_redundancy_v1.add_argument(
        "--workers", type=int, choices=range(1, 9), default=4
    )
    benchmark_redundancy_v1.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    benchmark_robustness_full_v1 = benchmark_subparsers.add_parser(
        "synthesis-robustness-full-v1",
        help="run the complete 936-case stronger-synthesis calibration sweep",
    )
    benchmark_robustness_full_v1.add_argument(
        "--workers", type=int, choices=range(1, 17), default=8
    )
    benchmark_robustness_full_v1.add_argument(
        "--json", action="store_true", dest="json_output"
    )
    benchmark_orfs_v2 = benchmark_subparsers.add_parser(
        "openroad-plan-v2",
        help="prepare the frozen 27-case, 108-run OpenROAD cross-check",
    )
    benchmark_orfs_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_orfs_lock_v2 = benchmark_subparsers.add_parser(
        "openroad-lock-v2",
        help="lock the OpenROAD plan, pinned ORFS source, and immutable image ID",
    )
    benchmark_orfs_lock_v2.add_argument("--image", default=DEFAULT_ORFS_IMAGE)
    benchmark_orfs_lock_v2.add_argument(
        "--orfs-root",
        help="clean host ORFS checkout at the pinned commit (otherwise use image checkout)",
    )
    benchmark_orfs_lock_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_orfs_run_v2 = benchmark_subparsers.add_parser(
        "openroad-run-v2",
        help="run or resume the locked 108-run OpenROAD cross-check",
    )
    benchmark_orfs_run_v2.add_argument("--workers", type=int, choices=range(1, 9), default=2)
    benchmark_orfs_run_v2.add_argument("--timeout-seconds", type=int, default=7200)
    benchmark_orfs_run_v2.add_argument(
        "--retry-failed",
        action="store_true",
        help="explicitly rerun prior failures and append a retry audit event",
    )
    benchmark_orfs_run_v2.add_argument("--json", action="store_true", dest="json_output")
    benchmark_orfs_report_v2 = benchmark_subparsers.add_parser(
        "openroad-report-v2",
        help="evaluate the physical-evidence gate from stored OpenROAD results",
    )
    benchmark_orfs_report_v2.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _tool_check(
    name: str,
    command: tuple[str, ...],
    *,
    timeout_seconds: int,
) -> CheckResult:
    try:
        result = run_command(command, timeout_seconds=timeout_seconds)
    except ToolExecutionError as exc:
        return CheckResult(name=name, status="error", detail=str(exc))

    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit code {result.returncode}"
        return CheckResult(name=name, status="error", detail=detail)
    return CheckResult(name=name, status="ok", version=first_output_line(result))


def _abc_check(config: ProjectConfig) -> CheckResult:
    try:
        result = run_command(
            (config.tools.yosys, "-Q", "-p", "help abc"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        return CheckResult(name="abc", status="error", detail=str(exc))

    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit code {result.returncode}"
        return CheckResult(name="abc", status="error", detail=detail)
    return CheckResult(
        name="abc",
        status="ok",
        version="available through Yosys",
    )


def _liberty_check(config: ProjectConfig, *, download: bool) -> CheckResult:
    liberty = config.liberty
    try:
        if liberty.path.is_file():
            actual_sha256 = sha256_file(liberty.path)
            if actual_sha256 != liberty.sha256:
                return CheckResult(
                    name="liberty",
                    status="error",
                    detail=(
                        f"checksum mismatch at {liberty.path}: expected "
                        f"{liberty.sha256}, got {actual_sha256}"
                    ),
                )
        elif download:
            actual_sha256 = download_verified(
                liberty.url,
                liberty.path,
                liberty.sha256,
            )
        else:
            return CheckResult(
                name="liberty",
                status="error",
                detail=f"missing: {liberty.path}",
            )

        if not liberty.license_path.is_file() and download:
            download_text(liberty.license_url, liberty.license_path)

        if not liberty.license_path.is_file():
            return CheckResult(
                name="liberty",
                status="error",
                detail=f"license missing: {liberty.license_path}",
            )

        return CheckResult(
            name="liberty",
            status="ok",
            version=liberty.name,
            detail=(
                f"sha256={actual_sha256}; source_commit={liberty.source_commit}; "
                f"path={liberty.path}"
            ),
        )
    except (DownloadError, OSError) as exc:
        return CheckResult(name="liberty", status="error", detail=str(exc))


def run_setup(config: ProjectConfig, *, download: bool) -> SetupReport:
    timeout = config.tools.timeout_seconds
    checks = (
        CheckResult(
            name="python",
            status="ok" if sys.version_info >= (3, 13) else "error",
            version=platform.python_version(),
            detail=None if sys.version_info >= (3, 13) else "Python 3.13+ is required",
        ),
        _tool_check(
            "verilator",
            (config.tools.verilator, "--version"),
            timeout_seconds=timeout,
        ),
        _tool_check(
            "yosys",
            (config.tools.yosys, "-V"),
            timeout_seconds=timeout,
        ),
        _abc_check(config),
        _tool_check(
            "codex",
            (config.tools.codex, "--version"),
            timeout_seconds=timeout,
        ),
        _liberty_check(config, download=download),
    )

    environment_file = config.artifacts_dir / "setup" / "environment.json"
    report = SetupReport(
        project_root=str(config.root),
        config_path=str(config.config_path),
        checks=checks,
        environment_file=str(environment_file),
    )
    environment_file.parent.mkdir(parents=True, exist_ok=True)
    environment_file.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _print_setup(report: SetupReport) -> None:
    print(f"RTL Advisor setup: {'ready' if report.ok else 'not ready'}")
    for check in report.checks:
        marker = "ok" if check.ok else "error"
        value = check.version or check.detail or ""
        print(f"  {check.name:<10} {marker:<5} {value}")
        if check.version and check.detail:
            print(f"  {'':<10} {'':<5} {check.detail}")
    print(f"  report           {report.environment_file}")


def _print_lint(results) -> None:
    ready = all(result.ok for result in results)
    print(f"RTL lint: {'passed' if ready else 'failed'}")
    for result in results:
        print(f"  {result.variant_id:<10} {result.status:<8} {result.log_path}")


def _print_equivalence(results) -> None:
    ready = all(result.expectation_met for result in results)
    print(f"RTL equivalence: {'passed' if ready else 'failed'}")
    for result in results:
        expected = "equivalent" if result.expected_equivalent else "inequivalent"
        marker = "ok" if result.expectation_met else "unexpected"
        print(
            f"  {result.candidate_id:<10} {result.status:<12} "
            f"expected={expected:<12} {marker}"
        )
        if result.counterexample_path:
            print(f"  {'':<10} counterexample={result.counterexample_path}")


def _print_synthesis(results, summary) -> None:
    print("RTL synthesis: passed")
    for result in results:
        metrics = result.metrics
        source = "cached" if result.cached else "fresh"
        print(
            f"  {result.variant_id:<10} {source:<6} "
            f"delay={metrics.critical_delay_ps:.2f} ps  "
            f"area={metrics.area_total:.3f}  cells={metrics.cell_count}"
        )
    for comparison in summary["comparisons"]:
        delay = comparison["critical_delay_ps"]["improvement_percent"]
        area = comparison["area_total"]["improvement_percent"]
        cells = comparison["cell_count"]["improvement_percent"]
        print(
            f"  {comparison['candidate_id']:<10} versus "
            f"{comparison['baseline_id']}: delay improvement={delay:+.2f}%  "
            f"area improvement={area:+.2f}%  cell improvement={cells:+.2f}%"
        )


def _print_graph(build) -> None:
    graph = build.graph
    modules = graph["modules"]
    print(f"RTL graph: {'cached' if build.cached else 'built'}")
    print(
        f"  {graph['variant_id']:<10} modules={len(modules)}  "
        f"nodes={sum(len(module['nodes']) for module in modules)}  "
        f"edges={sum(len(module['edges']) for module in modules)}  "
        f"instances={len(graph['hierarchy']['instances'])}"
    )
    print(f"  graph hash       {graph['graph_hash']}")
    print(f"  artifact         {build.graph_path}")


def _print_analysis(result, output_path: Path, *, cached: bool = False) -> None:
    source = "cached" if cached else "fresh"
    print(
        f"RTL analysis: {len(result['findings'])} finding(s)  "
        f"mode={result['mode']}  result={source}"
    )
    for finding in result["findings"]:
        evidence = finding["evidence"]
        if "rule_id" in finding:
            detail = (
                f"{finding['rule_id']}  module={finding['module']}  "
                f"operator={evidence.get('operator', evidence.get('operation', 'structure'))}"
            )
        else:
            detail = (
                f"rank={finding['rank']}  {finding['category']}  "
                f"transformation={finding['transformation_id']}"
            )
        print(f"  {detail}  confidence={finding['confidence']:.2f}")
        print(f"    {finding['recommendation']}")
    print(f"  artifact         {output_path}")


def _print_patch_validation(result: dict) -> None:
    print(f"RTL patch validation: {result['status']}")
    for name in ("lint", "equivalence", "synthesis"):
        stage = result["stages"][name]
        print(f"  {name:<12} {stage['status']}")
    print(f"  source unchanged {result['originals_unchanged']}")
    print(f"  patch             {result['patch_path']}")
    print(f"  result            {result['result_path']}")


def _normalized_agent_command(
    config: ProjectConfig,
    args: argparse.Namespace,
) -> tuple[str, ...]:
    command = [
        "rtl-advisor",
        "--config",
        str(config.config_path),
        "agent",
        args.agent_command,
    ]
    if args.agent_command == "review":
        input_path = Path(args.input).expanduser()
        if not input_path.is_absolute():
            input_path = config.root / input_path
        command.extend((str(input_path.resolve()), "--objective", args.objective))
        if args.top:
            command.extend(("--top", args.top))
        for include_dir in args.include_dirs:
            include_path = Path(include_dir).expanduser()
            if not include_path.is_absolute():
                include_path = config.root / include_path
            command.extend(("-I", str(include_path.resolve())))
        for definition in args.defines:
            command.extend(("-D", definition))
        if args.gate_model:
            model_path = Path(args.gate_model).expanduser()
            if not model_path.is_absolute():
                model_path = config.root / model_path
            command.extend(("--gate-model", str(model_path.resolve())))
        if args.force:
            command.append("--force")
    elif args.agent_command == "candidate":
        command.extend((args.run_id, "--finding", args.finding))
    elif args.agent_command == "verify":
        command.extend((args.run_id, "--candidate", args.candidate))
    elif args.agent_command == "measure":
        command.extend((args.run_id, "--candidate", args.candidate))
    elif args.agent_command == "report":
        command.append(args.run_id)
    elif args.agent_command == "evidence":
        command.append(args.evidence_command)
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
                command.extend((option, value))
        if args.output_dir:
            output_path = Path(args.output_dir).expanduser()
            if not output_path.is_absolute():
                output_path = config.root / output_path
            command.extend(("--output-dir", str(output_path.resolve())))
    elif args.agent_command == "corpus":
        command.append(args.agent_corpus_command)
        if args.agent_corpus_command == "coverage":
            for option, values in (
                ("--tier", args.tiers),
                ("--category", args.categories),
                ("--split", args.splits),
                ("--qualification-state", args.qualification_states),
                ("--qualification-status", args.qualification_statuses),
            ):
                for value in sorted(set(values)):
                    command.extend((option, value))
        elif args.agent_corpus_command == "qualify":
            for value in (args.lock, args.plan):
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = config.root / path
                command.append(str(path.resolve()))
        else:
            path = Path(args.lock).expanduser()
            if not path.is_absolute():
                path = config.root / path
            command.append(str(path.resolve()))
        if args.registry_dir:
            registry_path = Path(args.registry_dir).expanduser()
            if not registry_path.is_absolute():
                registry_path = config.root / registry_path
            command.extend(("--registry-dir", str(registry_path.resolve())))
    elif args.agent_command == "workflow":
        command.append(args.workflow_command)
        if args.workflow_command == "batch":
            manifest_path = Path(args.manifest).expanduser()
            if not manifest_path.is_absolute():
                manifest_path = config.root / manifest_path
            command.extend(
                (
                    str(manifest_path.resolve()),
                    "--authorized-through",
                    args.authorized_through,
                )
            )
            for option, value in (
                ("--prompt-file", args.prompt_file),
                ("--proposal-file", args.proposal_file),
                ("--confirmation-file", args.confirmation_file),
                ("--output-dir", args.output_dir),
            ):
                if value:
                    value_path = Path(value).expanduser()
                    if not value_path.is_absolute():
                        value_path = config.root / value_path
                    command.extend((option, str(value_path.resolve())))
            if args.first_eligible:
                command.append("--first-eligible")
            command.extend(("--jobs", str(args.jobs)))
        elif args.workflow_command == "prepare":
            input_path = Path(args.input).expanduser()
            if not input_path.is_absolute():
                input_path = config.root / input_path
            command.extend(
                (
                    str(input_path.resolve()),
                    "--input-kind",
                    args.input_kind,
                    "--objective",
                    args.objective,
                    "--authorized-through",
                    args.authorized_through,
                )
            )
            if args.top:
                command.extend(("--top", args.top))
            for include_dir in args.include_dirs:
                include_path = Path(include_dir).expanduser()
                if not include_path.is_absolute():
                    include_path = config.root / include_path
                command.extend(("-I", str(include_path.resolve())))
            for definition in args.defines:
                command.extend(("-D", definition))
            for option, value in (
                ("--prompt-file", args.prompt_file),
                ("--proposal-file", args.proposal_file),
                ("--confirmation-file", args.confirmation_file),
                ("--output-dir", args.output_dir),
            ):
                if value:
                    value_path = Path(value).expanduser()
                    if not value_path.is_absolute():
                        value_path = config.root / value_path
                    command.extend((option, str(value_path.resolve())))
            if args.first_eligible:
                command.append("--first-eligible")
            if args.finding_id:
                command.extend(("--finding-id", args.finding_id))
            if args.start:
                command.append("--start")
        elif args.workflow_command == "start":
            request_path = Path(args.request).expanduser()
            authorization_path = Path(args.authorization).expanduser()
            command.extend(
                (
                    str(request_path.resolve()),
                    "--authorization",
                    str(authorization_path.resolve()),
                )
            )
        elif args.workflow_command == "resume":
            authorization_path = Path(args.authorization).expanduser()
            command.extend(
                (
                    args.workflow_id,
                    "--authorization",
                    str(authorization_path.resolve()),
                )
            )
        else:
            command.append(args.workflow_id)
        if getattr(args, "compact", False):
            command.append("--compact")
    # Agent V1 predates the explicit schema selector.  Keep its normalized
    # command byte-for-byte compatible; only V2 clients opt into the new flag.
    if args.agent_command in {"workflow", "evidence", "corpus"}:
        command.extend(("--schema-version", "1"))
    elif args.schema_version == 2:
        command.extend(("--schema-version", "2"))
    command.append("--json")
    return tuple(command)


def _corpus_registry(config: ProjectConfig, registry_dir: str | None) -> CorpusRegistryV1:
    root = (
        Path(registry_dir).expanduser()
        if registry_dir is not None
        else config.corpus_dir / "registry-v1"
    )
    if not root.is_absolute():
        root = config.root / root
    return CorpusRegistryV1(root.resolve())


def _print_registry_listing(payload: dict[str, object]) -> None:
    print(f"Corpus Registry V1: {payload['count']} {payload['kind']}")
    for record in payload["records"]:
        assert isinstance(record, dict)
        if record["kind"] == "reference":
            print(
                f"  {record['record_id']:<36} reference  "
                f"tier={record['tier']}  state={record['qualification_state']}"
            )
        else:
            print(
                f"  {record['record_id']:<36} variant    "
                f"parent={record['parent_reference_id']}  state={record['state']}"
            )
    print(f"  registry          {payload['registry_root']}")


def _print_registry_summary(payload: dict[str, object]) -> None:
    print(
        "Corpus Registry V1: "
        f"references={payload['reference_count']}  variants={payload['variant_count']}"
    )
    tier_counts = payload["tier_reference_counts"]
    qualified_counts = payload["tier_qualified_counts"]
    assert isinstance(tier_counts, dict) and isinstance(qualified_counts, dict)
    for tier in ("A", "B", "C", "D"):
        print(
            f"  Tier {tier}            references={tier_counts[tier]}  "
            f"qualified={qualified_counts[tier]}"
        )
    print(f"  registry          {payload['registry_root']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))

    if args.command == "setup":
        report = run_setup(config, download=not args.no_download)
        if args.json_output:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            _print_setup(report)
        return 0 if report.ok else 1

    try:
        if args.command == "agent":
            normalized_command = _normalized_agent_command(config, args)
            if args.agent_command == "corpus":
                try:
                    registry = _corpus_registry(config, args.registry_dir)
                    if args.agent_corpus_command == "coverage":
                        payload = corpus_coverage(
                            config,
                            registry=registry,
                            tiers=tuple(args.tiers),
                            categories=tuple(args.categories),
                            splits=tuple(args.splits),
                            qualification_states=tuple(args.qualification_states),
                            qualification_statuses=tuple(args.qualification_statuses),
                            normalized_command=normalized_command,
                        )
                    elif args.agent_corpus_command == "qualify":
                        payload = qualify_corpus(
                            config,
                            tranche_lock_path=args.lock,
                            qualification_plan_path=args.plan,
                            registry=registry,
                            normalized_command=normalized_command,
                        )
                    elif args.agent_corpus_command == "validate":
                        payload = validate_corpus_tranche(
                            config,
                            tranche_lock_path=args.lock,
                            normalized_command=normalized_command,
                        )
                    else:
                        payload = register_corpus_tranche(
                            config,
                            tranche_lock_path=args.lock,
                            registry=registry,
                            normalized_command=normalized_command,
                        )
                except (
                    CorpusWorkflowError,
                    CorpusRegistryError,
                    CorpusQualificationError,
                    TrancheLockError,
                    MVPSchemaError,
                ) as exc:
                    payload = corpus_error_payload(
                        args.agent_corpus_command,
                        exc,
                        normalized_command=normalized_command,
                    )
                print(json.dumps(payload, indent=2, sort_keys=True))
                return corpus_exit_code(payload)
            if args.agent_command == "evidence":
                try:
                    payload = explore_evidence(
                        config,
                        workflow_ids=tuple(args.workflow_ids),
                        run_ids=tuple(args.run_ids),
                        profiles=tuple(args.profiles),
                        objectives=tuple(args.objectives),
                        classifications=tuple(args.classifications),
                        decisions=tuple(args.decisions),
                        transformations=tuple(args.transformations),
                        source_kinds=tuple(args.source_kinds),
                        output_dir=args.output_dir,
                        normalized_command=normalized_command,
                    )
                except (EvidenceExplorerError, MVPSchemaError) as exc:
                    payload = evidence_error_payload(
                        exc, normalized_command=normalized_command
                    )
                print(json.dumps(payload, indent=2, sort_keys=True))
                return evidence_exit_code(payload)
            if args.agent_command == "workflow":
                try:
                    if args.workflow_command == "batch":
                        payload = workflow_batch(
                            config,
                            args.manifest,
                            authorized_through=args.authorized_through,
                            prompt_file=args.prompt_file,
                            proposal_file=args.proposal_file,
                            confirmation_file=args.confirmation_file,
                            first_eligible=args.first_eligible,
                            output_dir=args.output_dir,
                            jobs=args.jobs,
                            normalized_command=normalized_command,
                        )
                    elif args.workflow_command == "prepare":
                        if args.first_eligible:
                            selection = {"mode": "first_eligible"}
                        elif args.finding_id:
                            selection = {
                                "mode": "finding_id",
                                "finding_id": args.finding_id,
                            }
                        else:
                            selection = None
                        payload = workflow_prepare(
                            config,
                            args.input,
                            input_kind=args.input_kind,
                            objective=args.objective,
                            authorized_through=args.authorized_through,
                            prompt_file=args.prompt_file,
                            proposal_file=args.proposal_file,
                            confirmation_file=args.confirmation_file,
                            top=args.top,
                            include_dirs=tuple(args.include_dirs),
                            defines=tuple(args.defines),
                            candidate_selection=selection,
                            output_dir=args.output_dir,
                            start=args.start,
                            compact=args.compact,
                            normalized_command=normalized_command,
                        )
                    elif args.workflow_command == "start":
                        payload = workflow_start(
                            config,
                            args.request,
                            args.authorization,
                            compact=args.compact,
                        )
                    elif args.workflow_command == "resume":
                        payload = workflow_resume(
                            config,
                            args.workflow_id,
                            args.authorization,
                            compact=args.compact,
                        )
                    elif args.workflow_command == "status":
                        payload = workflow_status(
                            config, args.workflow_id, compact=args.compact
                        )
                    else:
                        payload = run_workflow_report(
                            config, args.workflow_id, compact=args.compact
                        )
                except (
                    WorkflowRunnerError,
                    WorkflowContractError,
                    MVPSchemaError,
                ) as exc:
                    payload = workflow_error_payload(
                        args.workflow_command,
                        exc,
                        normalized_command=normalized_command,
                    )
                print(json.dumps(payload, indent=2, sort_keys=True))
                return workflow_exit_code(payload)
            if args.schema_version == 2:
                try:
                    if args.agent_command == "review" and (
                        args.gate_model is not None or args.force
                    ):
                        raise MVPAgentError(
                            "--gate-model and --force are Agent V1-only; "
                            "Agent V2 uses deterministic MVP rules",
                            code="unsupported_v2_option",
                        )
                    if args.agent_command == "capabilities":
                        payload = agent_v2_capabilities(
                            config,
                            normalized_command=normalized_command,
                        )
                    elif args.agent_command == "review":
                        payload = agent_v2_review(
                            config,
                            args.input,
                            objective=args.objective,
                            top=args.top,
                            include_dirs=tuple(args.include_dirs),
                            defines=tuple(args.defines),
                            normalized_command=normalized_command,
                        )
                    elif args.agent_command == "candidate":
                        payload = agent_v2_candidate(
                            config,
                            args.run_id,
                            finding_id=args.finding,
                            normalized_command=normalized_command,
                        )
                    elif args.agent_command == "verify":
                        payload = agent_v2_verify(
                            config,
                            args.run_id,
                            candidate_id=args.candidate,
                            normalized_command=normalized_command,
                        )
                    elif args.agent_command == "measure":
                        payload = agent_v2_measure(
                            config,
                            args.run_id,
                            candidate_id=args.candidate,
                            normalized_command=normalized_command,
                        )
                    else:
                        payload = agent_v2_report(
                            config,
                            args.run_id,
                            normalized_command=normalized_command,
                        )
                except (MVPAgentError, MVPSchemaError) as exc:
                    payload = agent_v2_error_payload(
                        args.agent_command,
                        exc,
                        normalized_command=normalized_command,
                    )
                print(json.dumps(payload, indent=2, sort_keys=True))
                return agent_v2_exit_code(payload)
            try:
                if args.agent_command == "capabilities":
                    payload = agent_capabilities(
                        config,
                        normalized_command=normalized_command,
                    )
                elif args.agent_command == "review":
                    payload = agent_review(
                        config,
                        args.input,
                        objective=args.objective,
                        top=args.top,
                        include_dirs=tuple(args.include_dirs),
                        defines=tuple(args.defines),
                        gate_model_path=args.gate_model,
                        force=args.force,
                        normalized_command=normalized_command,
                    )
                elif args.agent_command == "candidate":
                    payload = agent_candidate(
                        config,
                        args.run_id,
                        finding_id=args.finding,
                        normalized_command=normalized_command,
                    )
                else:
                    payload = agent_verify(
                        config,
                        args.run_id,
                        candidate_id=args.candidate,
                        normalized_command=normalized_command,
                    )
            except AgentAPIError as exc:
                payload = agent_error_payload(
                    args.agent_command,
                    exc,
                    normalized_command=normalized_command,
                )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return agent_exit_code(payload)

        if args.command == "study":
            if args.study_command == "validate":
                payload = read_family_study(args.manifest)
                result = {
                    "status": "valid",
                    "study_id": payload["study_id"],
                    "family_id": payload["family_id"],
                    "manifest_hash": payload["manifest_hash"],
                    "pair_count": len(payload["primary_cohort"]),
                    "reserve_count": len(payload["reserve_cohort"]),
                    "m2_sample_count": len(
                        payload["m2_selection"]["primary"]
                    ),
                }
            elif args.study_command == "run":
                result = run_family_study(
                    config,
                    args.manifest,
                    repeat_id=args.repeat,
                )
            else:
                result = report_family_study(config, args.study_id)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"Family study {result.get('study_id')}: "
                    f"{result.get('status')}"
                )
                if "manifest_hash" in result:
                    print(f"  manifest          {result['manifest_hash']}")
                if "summary" in result:
                    print(
                        "  executor coverage "
                        f"{result['summary']['executor_available_pair_count']}/"
                        f"{result['summary']['frozen_pair_count']}"
                    )
            return 0

        if args.command == "frontend":
            serve_frontend(config, host=args.host, port=args.port)
            return 0

        if args.command == "corpus" and args.corpus_command == "add":
            registry = _corpus_registry(config, args.registry_dir)
            result = registry.add(args.manifest)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"Corpus record added: {result['kind']} "
                    f"{result['record_id']}"
                )
                print(f"  hash              {result['semantic_hash']}")
                print(f"  record            {result['record_path']}")
            return 0

        if args.command == "corpus" and args.corpus_command == "advance":
            registry = _corpus_registry(config, args.registry_dir)
            result = registry.advance(args.manifest)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"Corpus record advanced: {result['kind']} "
                    f"{result['record_id']}"
                )
                print(f"  sequence          {result['sequence']}")
                print(f"  predecessor       {result['predecessor_hash']}")
                print(f"  hash              {result['semantic_hash']}")
                print(f"  record            {result['record_path']}")
            return 0

        if args.command == "corpus" and args.corpus_command == "validate":
            registry = _corpus_registry(config, args.registry_dir)
            if args.manifest is not None:
                manifest = registry.validate_manifest(args.manifest)
                record_id = (
                    manifest.reference_id
                    if hasattr(manifest, "reference_id")
                    else manifest.variant_id
                )
                result = {
                    "status": "passed",
                    "registry_schema": "rtl-advisor-corpus-v1",
                    "record_id": record_id,
                    "document_type": manifest.document_type,
                    "manifest_path": str(Path(args.manifest).expanduser().resolve()),
                }
            else:
                result = registry.validate()
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus registry validation: {result['status']}")
                if "record_id" in result:
                    print(f"  record            {result['record_id']}")
                else:
                    print(
                        f"  records           {result['record_count']}  "
                        f"references={result['reference_count']}  "
                        f"variants={result['variant_count']}"
                    )
            return 0

        if args.command == "corpus" and args.corpus_command == "list":
            result = _corpus_registry(config, args.registry_dir).listing(args.kind)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                _print_registry_listing(result)
            return 0

        if args.command == "corpus" and args.corpus_command == "summary":
            result = _corpus_registry(config, args.registry_dir).summary()
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                _print_registry_summary(result)
            return 0

        if args.command == "corpus" and args.corpus_command == "validate-tranche":
            result = tranche_summary(load_tranche_lock(args.lock), path=args.lock)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus tranche validation: {result['status']}")
                print(f"  tranche           {result['tranche_id']}")
                print(f"  references        {result['reference_count']}")
                print(f"  upstreams         {result['upstream_project_count']}")
                print(f"  categories        {result['category_count']}")
                print(f"  hash              {result['semantic_hash']}")
            return 0

        if args.command == "corpus" and args.corpus_command == "register-tranche":
            lock = load_tranche_lock(args.lock)
            registry = _corpus_registry(config, args.registry_dir)
            records = registry.add_many(materialize_tranche_references(lock))
            result = {
                **tranche_summary(lock, path=args.lock),
                "status": "registered",
                "registry_root": str(registry.root),
                "registered_count": len(records),
                "records": list(records),
            }
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus tranche registered: {result['tranche_id']}")
                print(f"  references        {result['registered_count']}")
                print(f"  registry          {result['registry_root']}")
            return 0

        if args.command == "corpus" and args.corpus_command == "verify-tranche-sources":
            source_roots: dict[str, str] = {}
            for assignment in args.source_root:
                upstream_id, separator, source_root = assignment.partition("=")
                if not separator or not upstream_id or not source_root:
                    raise TrancheLockError(
                        "--source-root must use UPSTREAM_ID=PATH"
                    )
                if upstream_id in source_roots:
                    raise TrancheLockError(
                        f"duplicate --source-root for {upstream_id!r}"
                    )
                source_roots[upstream_id] = source_root
            result = verify_tranche_sources(
                load_tranche_lock(args.lock),
                source_roots,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus source integrity: {result['status']}")
                print(f"  tranche           {result['tranche_id']}")
                print(f"  verified files    {result['verified_file_count']}")
                print(f"  failed files      {result['failed_file_count']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "corpus" and args.corpus_command == "qualify-tranche":
            result = qualify_tranche(
                config,
                tranche_lock_path=args.lock,
                qualification_plan_path=args.plan,
                registry=_corpus_registry(config, args.registry_dir),
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus qualification: {result['status']}")
                print(f"  references        {result['reference_count']}")
                print(f"  reproduced        {result['build_reproduced_count']}")
                print(f"  blocked           {result['blocked_count']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "corpus" and args.corpus_command == "prove-p2":
            result = run_p2_proof(
                config,
                plan_path=args.plan,
                formal_command=args.formal_command,
            )
            if args.record_registry:
                result = {
                    **result,
                    "registry": record_p2_proof(
                        config,
                        registry=_corpus_registry(config, args.registry_dir),
                        plan_path=args.plan,
                        result=result,
                    ),
                }
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"P2 sequential equivalence: {result['status']}")
                print(f"  proof             {result['proof_id']}")
                print(f"  expected          {result['expected_relation']}")
                print(f"  observed          {result['observed_relation']}")
                print(f"  expectation met   {result['expectation_met']}")
                print(f"  safe              {result['safe']}")
                print(f"  hash              {result['semantic_hash']}")
            return 0 if result["expectation_met"] else 1

        if args.command == "corpus" and args.corpus_command == "baseline-tranche":
            result = characterize_tranche_baselines(
                config,
                tranche_lock_path=args.lock,
                qualification_plan_path=args.plan,
                registry=_corpus_registry(config, args.registry_dir),
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus baseline synthesis: {result['status']}")
                print(f"  references        {result['reference_count']}")
                print(f"  candidate runs    {result['candidate_synthesis_enabled']}")
                print(f"  run hash          {result['run_hash']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "corpus" and args.corpus_command == "behavior-tranche":
            result = qualify_tranche_behavior(
                config,
                tranche_lock_path=args.lock,
                behavior_plan_path=args.plan,
                registry=_corpus_registry(config, args.registry_dir),
                record_registry=args.record_registry,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Corpus behavior gate: {result['status']}")
                print(f"  references        {result['reference_count']}")
                print(f"  qualified         {result['qualified_count']}")
                print(f"  blocked           {result['blocked_count']}")
                print(f"  run hash          {result['run_hash']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "corpus" and args.corpus_command == "prove-p3":
            result = run_p3_proof(
                config,
                plan_path=args.plan,
                formal_command=args.formal_command,
            )
            if args.record_registry:
                result = {
                    **result,
                    "registry": record_p3_proof(
                        config,
                        registry=_corpus_registry(config, args.registry_dir),
                        plan_path=args.plan,
                        result=result,
                    ),
                }
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"P3 transaction equivalence: {result['status']}")
                print(f"  proof             {result['proof_id']}")
                print(f"  expected          {result['expected_relation']}")
                print(f"  observed          {result['observed_relation']}")
                print(f"  expectation met   {result['expectation_met']}")
                print(f"  safe              {result['safe']}")
                print(f"  hash              {result['semantic_hash']}")
            return 0 if result["expectation_met"] else 1

        if args.command == "corpus" and args.corpus_command == "generate":
            default_width, default_seed = default_suite_parameters(args.suite)
            width = args.width if args.width is not None else default_width
            seed = args.seed if args.seed is not None else default_seed
            case_id = args.case_id or default_case_id(
                args.family,
                args.suite,
                width=width,
                seed=seed,
            )
            output_dir = (
                Path(args.output_dir).expanduser()
                if args.output_dir
                else config.corpus_dir / args.suite / case_id
            )
            if not output_dir.is_absolute():
                output_dir = config.root / output_dir
            manifest_path = generate_case(
                output_dir.resolve(),
                family=args.family,
                suite=args.suite,
                case_id=case_id,
                width=width,
                seed=seed,
                force=args.force,
            )
            print(f"Generated case manifest: {manifest_path}")
            return 0

        if args.command == "corpus" and args.corpus_command == "generate-suite":
            suite_path = generate_suite(
                config.corpus_dir,
                args.suite,
                force=args.force,
            )
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
            print(
                f"Generated {suite_payload['case_count']} {args.suite} cases: "
                f"{suite_path}"
            )
            return 0

        if args.command == "corpus" and args.corpus_command == "validate-suite":
            result = validate_suite(
                config,
                config.corpus_dir / args.suite / "suite.json",
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"RTL suite validation: {result['status']}  "
                    f"passed={result['passed_count']}/{result['case_count']}"
                )
                print(f"  result            {result['result_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "corpus" and args.corpus_command == "generate-suite-v2":
            suite_path = generate_v2_suite(
                config.corpus_dir,
                args.split,
                force=args.force,
            )
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
            print(
                f"Generated {suite_payload['case_count']} {args.split} cases: "
                f"{suite_path}"
            )
            return 0

        if args.command == "corpus" and args.corpus_command == "generate-suite-v21":
            suite_path = generate_v21_suite(
                config.corpus_dir,
                args.split,
                force=args.force,
            )
            suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
            print(
                f"Generated {suite_payload['case_count']} {args.split} cases: "
                f"{suite_path}"
            )
            return 0

        if args.command == "corpus" and args.corpus_command == "validate-suite-v21":
            result = validate_v21_suite(
                config,
                args.split,
                synthesize=args.synthesize,
                workers=args.workers,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.1 suite validation: {result['status']}  "
                    f"passed={result['passed_count']}/{result['case_count']}"
                )
                print(f"  summary           {result['summary_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "corpus" and args.corpus_command == "validate-suite-v2":
            result = validate_v2_suite(
                config,
                args.split,
                synthesize=args.synthesize,
                workers=args.workers,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2 suite validation: {result['status']}  "
                    f"passed={result['passed_count']}/{result['case_count']}"
                )
                print(f"  summary           {result['summary_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "lint":
            results = lint_case(config, args.case)
            payload = {
                "case_id": results[0].case_id if results else None,
                "ok": all(result.ok for result in results),
                "results": [result.to_dict() for result in results],
            }
            if args.json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_lint(results)
            return 0 if payload["ok"] else 1

        if args.command == "equivalence":
            results = prove_case_candidates(config, args.case, args.candidate)
            payload = {
                "case_id": results[0].case_id if results else None,
                "ok": all(result.expectation_met for result in results),
                "results": [result.to_dict() for result in results],
            }
            if args.json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                _print_equivalence(results)
            return 0 if payload["ok"] else 1

        if args.command == "synth":
            results, summary = synthesize_case(
                config,
                args.case,
                variant_id=args.variant,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(summary, indent=2, sort_keys=True))
            else:
                _print_synthesis(results, summary)
            return 0

        if args.command == "graph":
            build = build_graph(
                config,
                args.case,
                args.variant,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(build.graph, indent=2, sort_keys=True))
            else:
                _print_graph(build)
            return 0

        if args.command == "analyze":
            patch_result = None
            if args.emit_patch:
                manifest = load_manifest(args.case)
                if args.variant != manifest.baseline_id:
                    raise PatchValidationError(
                        "safe patch emission currently requires analyzing the baseline variant"
                    )
                patch_result = validate_candidate_patch(
                    config,
                    manifest,
                    args.patch_candidate,
                )
            build = build_graph(
                config,
                args.case,
                args.variant,
                force=args.force,
            )
            graph = build.graph
            rules_output_path = (
                config.artifacts_dir
                / "cases"
                / graph["case_id"]
                / "analysis"
                / "rules"
                / f"{graph['variant_id']}.json"
            )
            if args.mode in {"codex", "hybrid"}:
                rules_result = (
                    write_rule_analysis(graph, rules_output_path)
                    if args.mode == "hybrid"
                    else None
                )
                codex_build = analyze_with_codex(
                    config,
                    args.case,
                    args.variant,
                    mode=args.mode,
                    effort=args.effort,
                    rules_analysis=rules_result,
                    force=args.force,
                )
                output_result = dict(codex_build.result)
                if patch_result is not None:
                    output_result["patch_validation"] = patch_result
                if args.json_output:
                    print(json.dumps(output_result, indent=2, sort_keys=True))
                else:
                    _print_analysis(
                        codex_build.result,
                        codex_build.output_path,
                        cached=codex_build.cached,
                    )
                    if patch_result is not None:
                        _print_patch_validation(patch_result)
                return 0 if patch_result is None or patch_result["accepted"] else 1
            output_path = (
                rules_output_path
            )
            result = write_rule_analysis(graph, output_path)
            output_result = dict(result)
            if patch_result is not None:
                output_result["patch_validation"] = patch_result
            if args.json_output:
                print(json.dumps(output_result, indent=2, sort_keys=True))
            else:
                _print_analysis(result, output_path)
                if patch_result is not None:
                    _print_patch_validation(patch_result)
            return 0 if patch_result is None or patch_result["accepted"] else 1

        if args.command == "analyze-rtl":
            if args.candidate_source == "templates+codex" and not (
                args.emit_candidates and args.allow_model_source
            ):
                raise AdvisorV2Error(
                    "templates+codex requires --emit-candidates and "
                    "--allow-model-source"
                )
            result, output_path = analyze_live_rtl(
                config,
                top=args.top,
                files=tuple(args.file),
                filelist=args.filelist,
                include_dirs=tuple(args.include_dirs),
                defines=tuple(args.defines),
                profile_id=args.profile,
                mode=args.mode,
                output_dir=args.output,
                gate_model_path=args.gate_model,
                force=args.force,
            )
            if args.mode == "advisor":
                result["explanation"] = explain_gate_decision(
                    config,
                    result,
                    output_path,
                    allow_model_source=args.allow_model_source,
                    force=args.force,
                )
            if args.emit_candidates:
                result["candidate_emission"] = emit_selected_candidate(
                    config,
                    result,
                    output_path,
                    candidate_source=args.candidate_source,
                )
                output_path.write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            elif args.mode == "advisor":
                output_path.write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"RTL advisor v2: {result['decision']}  "
                    f"profile={result['profile']}  "
                    f"candidates={len(result['candidates'])}"
                )
                print(f"  gate              {result['gate']['status']}")
                print(f"  result            {output_path}")
            if args.emit_candidates and result.get("candidate_emission", {}).get(
                "status"
            ) != "accepted":
                return 4
            return 0

        if args.command == "model" and args.model_command == "train-v2":
            result = train_v2_models(
                config,
                config.corpus_dir / args.suite / "suite.json",
                force_graph=args.force_graph,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2 model training: {result['status']}  "
                    f"rows={result['row_count']}"
                )
                print(f"  gate              {result['gate_path']}")
                print(
                    f"  challenger        "
                    f"{result['challenger']['artifact_path']}"
                )
            return 0

        if args.command == "analyze-v21":
            manifest = load_manifest(args.case)
            output_dir = (
                Path(args.output).expanduser().resolve()
                if args.output
                else config.artifacts_dir
                / "cases"
                / manifest.case_id
                / "analysis/v21"
                / args.mode
            )
            analysis, analysis_path = write_case_analysis_v21(
                config,
                manifest,
                output_dir,
                mode=args.mode,
                profile_id=args.profile,
                force_graph=args.force_graph,
            )
            emission = None
            if args.emit_candidate:
                emission = emit_selected_candidate(
                    config,
                    analysis,
                    analysis_path,
                    candidate_source="templates",
                )
            result = {
                "analysis": analysis,
                "analysis_path": str(analysis_path),
                "candidate_emission": emission,
            }
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.1 advisor: {analysis['decision']}  "
                    f"mode={args.mode}  candidates={len(analysis['candidates'])}"
                )
                print(f"  analysis          {analysis_path}")
                if emission is not None:
                    print(f"  candidate         {emission['status']}")
            return 0

        if args.command == "analyze-v22":
            manifest = load_manifest(args.case)
            output_dir = (
                Path(args.output).expanduser().resolve()
                if args.output
                else config.artifacts_dir
                / "cases"
                / manifest.case_id
                / "analysis/v22"
                / args.mode
            )
            analysis, analysis_path = write_case_analysis_v22(
                config,
                manifest,
                output_dir,
                mode=args.mode,
                profile_id=args.profile,
                force_graph=args.force_graph,
            )
            emission = None
            if args.emit_candidate:
                emission = emit_selected_candidate(
                    config,
                    analysis,
                    analysis_path,
                    candidate_source="templates",
                )
            result = {
                "analysis": analysis,
                "analysis_path": str(analysis_path),
                "candidate_emission": emission,
            }
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.2 advisor: {analysis['decision']}  "
                    f"mode={args.mode}  candidates={len(analysis['candidates'])}"
                )
                print(f"  analysis          {analysis_path}")
                if emission is not None:
                    print(f"  candidate         {emission['status']}")
            return 0

        if args.command == "model" and args.model_command == "train-v21":
            result = train_v21_models(config, force_graph=args.force_graph)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.1 model training: {result['status']}  "
                    f"rows={result['row_count']}"
                )
                print(f"  metadata          {result['metadata_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "model" and args.model_command == "train-v22":
            result = train_v22_models(config)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.2 family-risk training: {result['status']}  "
                    f"rows={result['row_count']}"
                )
                print(f"  policy            {result['policy_hash']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "benchmark" and args.benchmark_command == "run":
            result = run_benchmark(
                config,
                args.suite,
                arm=args.arm,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"RTL benchmark: {result['status']}  "
                    f"completed={result['completed_run_count']}/"
                    f"{result['planned_run_count']}  "
                    f"failed={result['failed_run_count']}"
                )
                print(f"  result            {result['result_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "benchmark" and args.benchmark_command == "report":
            report = generate_benchmark_report(config, args.suite)
            if args.json_output:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"RTL benchmark report: {args.suite}")
                for arm, summary in report["arm_summaries"].items():
                    print(
                        f"  {arm:<14} actionable="
                        f"{summary['actionable_accuracy']}  direction="
                        f"{summary['direction_accuracy']}  regret="
                        f"{summary['mean_ranking_regret']}"
                    )
                print(f"  report            {report['markdown_path']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "lock-v2":
            lock_path = create_benchmark_lock(config)
            lock = verify_benchmark_lock(lock_path)
            if args.json_output:
                print(json.dumps(lock, indent=2, sort_keys=True))
            else:
                print(f"V2 benchmark locked: {lock['lock_hash']}")
                print(f"  lock              {lock_path}")
                print(f"  model calls       {lock['call_count']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "unseal-v2":
            lock_path = config.artifacts_dir / "benchmarks/v2/benchmark-lock.json"
            result = record_blind_unseal(config, lock_path)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"V2 blind status: {result['status']}")
                print(f"  fresh             {result['fresh']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "lock-v21":
            lock_path = create_benchmark_lock_v21(config)
            lock = verify_benchmark_lock_v21(lock_path)
            if args.json_output:
                print(json.dumps(lock, indent=2, sort_keys=True))
            else:
                print(f"V2.1 benchmark locked: {lock['lock_hash']}")
                print(f"  lock              {lock_path}")
                print(f"  runs              {lock['run_count']}")
                print(f"  model calls       {lock['call_count']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "lock-v22":
            lock_path = create_benchmark_lock_v22(config)
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            if args.json_output:
                print(json.dumps(lock, indent=2, sort_keys=True))
            else:
                print(f"V2.2 benchmark locked: {lock['lock_hash']}")
                print(f"  lock              {lock_path}")
                print(f"  runs              {lock['run_count']}")
                print(f"  model calls       {lock['call_count']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "unseal-v21":
            lock_path = config.artifacts_dir / "benchmarks/v21/benchmark-lock.json"
            result = record_blind_unseal_v21(config, lock_path)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"V2.1 blind status: {result['status']}")
                print(f"  fresh             {result['fresh']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "run-v21":
            lock_path = config.artifacts_dir / "benchmarks/v21/benchmark-lock.json"
            result = run_locked_v21_benchmark(
                config,
                lock_path,
                synthesis_workers=args.synthesis_workers,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.1 benchmark: {result['status']}  "
                    f"passed={result['passed_count']}/{result['run_count']}"
                )
                print(f"  summary           {result['summary_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "benchmark" and args.benchmark_command == "report-v21":
            lock_path = config.artifacts_dir / "benchmarks/v21/benchmark-lock.json"
            report = build_v21_benchmark_report(config, lock_path)
            if args.json_output:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(
                    f"V2.1 promotion: "
                    f"{'passed' if report['promotion']['passed'] else 'failed'}"
                )
                print(f"  report            {report['markdown_path']}")
            return 0 if report["promotion"]["passed"] else 1

        if args.command == "benchmark" and args.benchmark_command == "run-v2":
            lock_path = config.artifacts_dir / "benchmarks/v2/benchmark-lock.json"
            result = run_locked_v2_benchmark(
                config,
                lock_path,
                synthesis_workers=args.synthesis_workers,
                force=args.force,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"V2 benchmark: {result['status']}  "
                    f"passed={result['passed_count']}/{result['run_count']}  "
                    f"failed={result['failed_count']}"
                )
                print(f"  model calls       {result['model_call_count']}")
                print(f"  result            {result['summary_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "benchmark" and args.benchmark_command == "report-v2":
            lock_path = config.artifacts_dir / "benchmarks/v2/benchmark-lock.json"
            report = build_v2_benchmark_report(config, lock_path)
            if args.json_output:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(
                    f"V2 benchmark report: records={report['record_count']}/"
                    f"{report['expected_record_count']}"
                )
                print(f"  report            {report['report_path']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "diagnose-v2":
            postmortem = diagnose_v2(config)
            if args.json_output:
                print(json.dumps(postmortem, indent=2, sort_keys=True))
            else:
                rf = postmortem["shadow_counterfactuals"][
                    "random_forest_recorded"
                ]
                print(
                    "Frozen V2 postmortem: "
                    f"OOD={postmortem['rejection_diagnostics']['out_of_domain_candidate_count']}/"
                    f"{postmortem['rejection_diagnostics']['candidate_count']}  "
                    f"RF coverage={rf['opportunity_recall']:.1%}  "
                    f"RF harmful={rf['harmful_recommendation_rate']:.1%}"
                )
                print(f"  report            {postmortem['markdown_path']}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "diagnose-v22":
            diagnostic = diagnose_v22(config)
            if args.json_output:
                print(json.dumps(diagnostic, indent=2, sort_keys=True))
            else:
                aggregate = diagnostic["aggregate"]
                print(
                    "Frozen V2.2 diagnostic: "
                    f"covered={aggregate['covered_opportunity_count']}/"
                    f"{aggregate['opportunity_count']}  "
                    f"harmful={aggregate['harmful_count']}/"
                    f"{aggregate['recommendation_count']}"
                )
                print(f"  report            {diagnostic['markdown_path']}")
            return 0

        if (
            args.command == "benchmark"
            and args.benchmark_command == "synthesis-redundancy-v1"
        ):
            result = run_redundancy_benchmark(config, workers=args.workers)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"Synthesis redundancy V1: {result['status']}  "
                    f"passed={result['passed_count']}/{result['run_count']}  "
                    f"fresh={result['fresh_count']}  cached={result['cached_count']}"
                )
                print(f"  report            {result['report_path']}")
            return 0 if result["status"] == "passed" else 1

        if (
            args.command == "benchmark"
            and args.benchmark_command == "synthesis-robustness-full-v1"
        ):
            result = run_full_sweep(config, workers=args.workers)
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"Full synthesis robustness V1: {result['status']}  "
                    f"passed={result['passed_count']}/{result['run_count']}  "
                    f"fresh={result['fresh_count']}  cached={result['cached_count']}"
                )
                print(f"  training rows     {result['training_rows_path']}")
                print(f"  report            {result['report_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "benchmark" and args.benchmark_command == "openroad-plan-v2":
            plan_path = create_openroad_plan(config)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if args.json_output:
                print(json.dumps(plan, indent=2, sort_keys=True))
            else:
                print(
                    f"OpenROAD v2 plan: cases={plan['case_count']}  "
                    f"runs={plan['run_count']}"
                )
                print(f"  plan              {plan_path}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "openroad-lock-v2":
            lock_path = create_openroad_lock(
                config,
                image=args.image,
                orfs_root=args.orfs_root,
            )
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            if args.json_output:
                print(json.dumps(lock, indent=2, sort_keys=True))
            else:
                print(f"OpenROAD v2 locked: {lock['lock_hash']}")
                print(f"  image             {lock['image']['id']}")
                print(f"  lock              {lock_path}")
            return 0

        if args.command == "benchmark" and args.benchmark_command == "openroad-run-v2":
            result = run_openroad_v2(
                config,
                workers=args.workers,
                retry_failed=args.retry_failed,
                timeout_seconds=args.timeout_seconds,
            )
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"OpenROAD v2: {result['status']}  "
                    f"usable={result['usable_count']}/{result['run_count']}  "
                    f"fresh={result['fresh_count']}  cached={result['cached_count']}"
                )
                print(f"  summary           {result['summary_path']}")
            return 0 if result["status"] == "passed" else 1

        if args.command == "benchmark" and args.benchmark_command == "openroad-report-v2":
            report = build_openroad_report(config)
            gate = report["physical_evidence_gate"]
            if args.json_output:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(
                    f"OpenROAD physical gate: {'passed' if gate['passed'] else 'failed'}  "
                    f"complete={gate['complete_case_count']}/27  "
                    f"action={gate['candidate_action_agreement']:.1%}"
                )
                print(f"  report            {report['markdown_path']}")
            return 0 if gate["passed"] else 1
    except (
        CorpusError,
        CorpusRegistryError,
        CorpusBaselineError,
        CorpusBehaviorError,
        CorpusQualificationError,
        SequentialEquivalenceError,
        TransactionEquivalenceError,
        TrancheLockError,
        VerificationError,
        SynthesisError,
        GraphError,
        CodexAnalysisError,
        PatchValidationError,
        SuiteError,
        BenchmarkError,
        AdvisorV2Error,
        AdvisorV21Error,
        AdvisorV22Error,
        V2CorpusError,
        CalibrationError,
        CalibrationV21Error,
        CalibrationV22Error,
        CandidateV2Error,
        AdvisorExplanationError,
        BenchmarkV2Error,
        BenchmarkV21Error,
        BenchmarkV22Error,
        BenchmarkRunnerV2Error,
        BenchmarkRunnerV21Error,
        OpenROADV2Error,
        V2ValidationError,
        V21CorpusError,
        V21ValidationError,
        V2PostmortemError,
        DiagnosticV22Error,
        SynthesisRedundancyError,
        SynthesisRobustnessFullError,
        FrontendAPIError,
        FrontendServerError,
        FamilyStudyError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"unsupported command: {args.command}")
    return 2


def entrypoint() -> None:
    raise SystemExit(main())
