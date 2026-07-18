from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser
from rtl_advisor.config import load_config
from rtl_advisor.postmortem_v2 import (
    corrected_metrics,
    diagnose_v2,
    normalize_rejection_reason,
)


def test_rejection_reason_normalizes_ood_feature() -> None:
    assert normalize_rejection_reason(
        "feature module_count=1 outside [2.0, 2.0]"
    ) == "out_of_domain:module_count"


def test_corrected_metrics_are_balanced_and_tie_aware() -> None:
    rows = [
        {
            "recommended": True,
            "opportunity": True,
            "opportunity_covered": True,
            "harmful": False,
            "selected_template_id": "v1",
            "utilities": {"v1": 5.0, "v2": 5.0, "v3": 0.0},
        },
        {
            "recommended": False,
            "opportunity": True,
            "opportunity_covered": False,
            "harmful": False,
            "selected_template_id": None,
            "utilities": {"v1": 3.0, "v2": 0.0, "v3": 0.0},
        },
        {
            "recommended": False,
            "opportunity": False,
            "opportunity_covered": False,
            "harmful": False,
            "selected_template_id": None,
            "utilities": {"v1": 0.0, "v2": 0.0, "v3": 0.0},
        },
    ]

    metrics = corrected_metrics(rows)

    assert metrics["opportunity_recall"] == 0.5
    assert metrics["abstention_specificity"] == 1.0
    assert metrics["balanced_actionable_accuracy"] == 0.75
    assert metrics["tie_aware_exact_best_accuracy"] == 1.0
    assert metrics["conditional_normalized_ranking_regret"] == 0.0


def test_diagnose_v2_preserves_frozen_report() -> None:
    root = Path(__file__).resolve().parents[1]
    report_path = root / "artifacts/benchmarks/v2/report.json"
    if not report_path.is_file():
        pytest.skip("frozen V2 benchmark artifacts are not present")
    before = hashlib.sha256(report_path.read_bytes()).hexdigest()

    result = diagnose_v2(load_config(root / "rtl-advisor.toml"))

    after = hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert before == after
    assert result["integrity"]["run_record_count"] == 480
    assert result["rejection_diagnostics"]["out_of_domain_candidate_count"] == 204
    assert result["rule_diagnostics"]["missed_opportunity_count"] == 1
    rf = result["shadow_counterfactuals"]["random_forest_recorded"]
    assert rf["recommendation_count"] == 9
    assert rf["tie_aware_exact_best_accuracy"] == pytest.approx(7 / 9)


def test_cli_parser_has_frozen_v2_diagnosis() -> None:
    args = build_parser().parse_args(("benchmark", "diagnose-v2", "--json"))
    assert args.benchmark_command == "diagnose-v2"
    assert args.json_output is True
