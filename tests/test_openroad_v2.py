from __future__ import annotations

import json
from pathlib import Path

from rtl_advisor.openroad_v2 import (
    build_openroad_report,
    evaluate_physical_gate,
    fixed_die_side,
    parse_openroad_metrics,
)


def test_fixed_die_is_rounded_and_has_a_minimum() -> None:
    assert fixed_die_side(1.0) == 100.0
    assert fixed_die_side(10_000.0) % 10.0 == 0.0
    assert fixed_die_side(10_000.0) > 100.0


def test_openroad_metric_aliases_are_normalized(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text(
        json.dumps(
            {
                "finish__timing__setup__ws": -0.1,
                "finish__design__instance__area": 42.0,
                "finish__design__instance__count": 12,
                "finish__route__wirelength": 100.0,
                "finish__route__drc_errors": 0,
            }
        ),
        encoding="utf-8",
    )

    metrics = parse_openroad_metrics(path)

    assert metrics["worst_slack_ns"] == -0.1
    assert metrics["cell_area_um2"] == 42.0
    assert metrics["drc_count"] == 0


def test_physical_gate_enforces_completeness_and_each_direction() -> None:
    families = [f"family_{index}" for index in range(9)]
    rows = []
    for index in range(27):
        rows.append(
            {
                "family": families[index % len(families)],
                "complete": index < 24,
                "action_agreement": index < 20,
                "direction_pairs": [
                    {
                        "metric": metric,
                        "agreement": not (metric == "area" and index >= 18),
                    }
                    for metric in ("delay", "area", "cell_count")
                ],
            }
        )

    gate = evaluate_physical_gate(rows)

    assert gate["complete_case_count"] == 24
    assert gate["candidate_action_agreement"] == 20 / 24
    assert gate["direction_agreement"]["delay"]["agreement"] == 1.0
    assert gate["direction_agreement"]["area"]["agreement"] == 18 / 24
    assert gate["passed"] is True


def test_physical_gate_fails_below_action_threshold() -> None:
    rows = [
        {
            "family": f"family_{index % 9}",
            "complete": True,
            "action_agreement": index < 19,
            "direction_pairs": [
                {"metric": metric, "agreement": True}
                for metric in ("delay", "area", "cell_count")
            ],
        }
        for index in range(24)
    ]

    gate = evaluate_physical_gate(rows)

    assert gate["candidate_action_agreement"] == 19 / 24
    assert gate["checks"]["candidate_action_agreement"] is False
    assert gate["passed"] is False


def test_openroad_report_builds_direction_pairs(tmp_path: Path, monkeypatch) -> None:
    artifacts = tmp_path / "artifacts"
    root = artifacts / "openroad/v2"
    plan_path = root / "plan.json"
    runs = [
        {
            "run_id": f"case_0__{variant}",
            "case_id": "case_0",
            "family": "mux",
            "crosscheck_source": "calibration_lowest",
            "variant_id": variant,
        }
        for variant in ("v0", "v1", "v2", "v3")
    ]
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    result_metrics = {
        "v0": {"worst_slack_ns": 0.0, "cell_area_um2": 100.0, "cell_count": 100},
        "v1": {"worst_slack_ns": 0.2, "cell_area_um2": 98.0, "cell_count": 98},
        "v2": {"worst_slack_ns": 0.0, "cell_area_um2": 100.0, "cell_count": 100},
        "v3": {"worst_slack_ns": -0.2, "cell_area_um2": 102.0, "cell_count": 102},
    }
    results = root / "results"
    results.mkdir()
    for run in runs:
        (results / f"{run['run_id']}.json").write_text(
            json.dumps(
                {
                    "lock_hash": "test-lock",
                    "usable": True,
                    "metrics": result_metrics[run["variant_id"]],
                }
            ),
            encoding="utf-8",
        )

    synthesis = {
        "comparisons": [
            {
                "candidate_id": variant,
                "critical_delay_ps": {"improvement_percent": value},
                "area_total": {"improvement_percent": value},
                "cell_count": {"improvement_percent": value},
            }
            for variant, value in (("v1", 2.0), ("v2", 0.0), ("v3", -2.0))
        ]
    }
    monkeypatch.setattr(
        "rtl_advisor.openroad_v2.verify_openroad_lock",
        lambda _config: {
            "lock_hash": "test-lock",
            "plan": {"path": str(plan_path)},
        },
    )
    monkeypatch.setattr(
        "rtl_advisor.openroad_v2._summary", lambda _config, _case_id: synthesis
    )

    report = build_openroad_report(type("Config", (), {"artifacts_dir": artifacts})())

    pairs = report["cases"][0]["direction_pairs"]
    assert [pair["synthesis"] for pair in pairs[0:3]] == ["improve"] * 3
    assert [pair["physical"] for pair in pairs[0:3]] == ["improve"] * 3
    assert (root / "report.json").is_file()
