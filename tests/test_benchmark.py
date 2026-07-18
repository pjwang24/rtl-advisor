import json
from pathlib import Path

from rtl_advisor.benchmark import (
    generate_benchmark_report,
    planned_runs,
    score_analysis,
)
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)


def fake_cases() -> list[dict]:
    return [
        {
            "index": index,
            "case_id": f"h_{index:012x}",
            "family": "variable_shift",
            "manifest": f"h_{index:012x}/manifest.json",
        }
        for index in range(36)
    ]


def comparison(candidate: str, delay: float, area: float, cells: float) -> dict:
    def metric(value: float) -> dict:
        return {"improvement_percent": value}

    return {
        "baseline_id": "v0",
        "candidate_id": candidate,
        "critical_delay_ps": metric(delay),
        "area_total": metric(area),
        "cell_count": metric(cells),
    }


def test_planned_run_counts_match_v1_campaign() -> None:
    cases = fake_cases()
    smoke = planned_runs("smoke", cases)
    pilot = planned_runs("pilot", cases)
    rules = planned_runs("pilot", cases, arm="rules")
    codex_xhigh = planned_runs("pilot", cases, arm="codex-xhigh")

    assert len(smoke) == 20
    assert sum(run["arm"] != "rules" for run in smoke) == 16
    assert len(pilot) == 276
    assert sum(run["arm"] != "rules" for run in pilot) == 240
    assert len(rules) == 36
    assert len(codex_xhigh) == 60


def test_scoring_distinguishes_actionable_and_harmful_advice() -> None:
    variable_truth = {
        "comparisons": [
            comparison("v1", 16.0, 8.0, 8.0),
            comparison("v2", 14.0, -38.0, -47.0),
            comparison("v3", 16.1, 8.0, 8.0),
        ]
    }
    correct_analysis = {
        "findings": [
            {
                "transformation_id": "bound_variable_shift",
                "predicted_effect": {
                    "delay": "improve",
                    "area": "improve",
                    "cell_count": "improve",
                },
            }
        ]
    }
    correct = score_analysis(
        "variable_shift",
        correct_analysis,
        variable_truth,
    )

    comparator_truth = {
        "comparisons": [
            comparison("v1", -6.3, -18.3, -1.0),
            comparison("v2", -0.9, -12.5, 5.9),
            comparison("v3", -0.9, -11.8, 5.9),
        ]
    }
    harmful_analysis = {
        "findings": [
            {
                "transformation_id": "factor_comparator_selection",
                "predicted_effect": {
                    "delay": "uncertain",
                    "area": "improve",
                    "cell_count": "improve",
                },
            }
        ]
    }
    harmful = score_analysis(
        "comparator_selection",
        harmful_analysis,
        comparator_truth,
    )

    assert correct["actionable_correct"] is True
    assert correct["direction_accuracy"] == 1.0
    assert correct["beneficial_candidate_available"] is True
    assert harmful["actionable_correct"] is False
    assert harmful["beneficial_candidate_available"] is False
    assert harmful["recommended_action"] is True


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig("verilator", "yosys", "codex", 30),
        synthesis=SynthesisConfig("BUF_X1", 10.0),
        liberty=LibertyConfig(
            "unused",
            tmp_path / "unused.lib",
            "unused",
            "0" * 64,
            tmp_path / "LICENSE",
            "unused",
            "unused",
        ),
    )


def test_report_rebuilds_from_raw_records_only(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    run_root = config.artifacts_dir / "benchmarks/smoke/runs"
    run_root.mkdir(parents=True)
    for arm, correct in (("rules", False), ("hybrid-xhigh", True)):
        record = {
            "status": "passed",
            "case_id": "h_test",
            "family": "variable_shift",
            "arm": arm,
            "repeat_index": 0,
            "latency_seconds": 1.0,
            "model_usage": {},
            "patch_validation": None,
            "score": {
                "actionable_correct": correct,
                "direction_matches": 2,
                "direction_total": 3,
                "ranking_regret": 1.5,
                "recommended_action": correct,
                "recommended_transformation_id": "bound_variable_shift",
            },
        }
        (run_root / f"h_test__{arm}__r0.json").write_text(
            json.dumps(record),
            encoding="utf-8",
        )
    repeated = {
        "status": "passed",
        "case_id": "h_test",
        "family": "variable_shift",
        "arm": "hybrid-xhigh",
        "repeat_index": 1,
        "latency_seconds": 2.0,
        "model_usage": {"input_tokens": 10},
        "patch_validation": None,
        "score": {
            "actionable_correct": False,
            "direction_matches": 0,
            "direction_total": 3,
            "ranking_regret": 99.0,
            "recommended_action": False,
            "recommended_transformation_id": None,
        },
    }
    (run_root / "h_test__hybrid-xhigh__r1.json").write_text(
        json.dumps(repeated),
        encoding="utf-8",
    )

    report = generate_benchmark_report(config, "smoke")

    assert report["record_count"] == 3
    assert report["arm_summaries"]["rules"]["actionable_accuracy"] == 0.0
    assert report["arm_summaries"]["hybrid-xhigh"]["actionable_accuracy"] == 1.0
    assert report["arm_summaries"]["hybrid-xhigh"]["evaluation_case_count"] == 1
    assert report["arm_summaries"]["hybrid-xhigh"]["run_count"] == 2
    assert report["arm_summaries"]["hybrid-xhigh"]["direction_matches"] == 2
    assert report["arm_summaries"]["hybrid-xhigh"]["direction_coverage"] == 1.0
    assert report["arm_summaries"]["hybrid-xhigh"]["mean_ranking_regret"] == 1.5
    assert report["arm_summaries"]["hybrid-xhigh"]["run_to_run_agreement"] == 0.0
    assert report["family_summaries"]["variable_shift"]["hybrid-xhigh"][
        "actionable_accuracy"
    ] == 1.0
    assert Path(report["json_path"]).is_file()
    assert Path(report["markdown_path"]).is_file()
    assert report["source"] == "rebuilt solely from stored benchmark run records"
