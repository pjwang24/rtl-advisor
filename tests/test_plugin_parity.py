from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rtl_advisor.plugin_parity import (
    ExpectedValue,
    ParityScenario,
    _json_hash,
    _review_source_paths,
    compare_scenario,
    render_markdown,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (
    ROOT
    / "plugins/rtl-advisor/skills/analyze-rtl/scripts/run_rtl_advisor.py"
)
CONFIG = ROOT / "rtl-advisor.toml"
FIXTURE = ROOT / "tests/fixtures/plugin_parity/minimal.sv"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_error_result_is_identical_through_cli_and_plugin_runner() -> None:
    missing = ROOT / "tests/fixtures/plugin_parity/absent.sv"
    scenario = ParityScenario(
        scenario_id="missing_input",
        description="missing input",
        operation="review",
        arguments=(str(missing), "--objective", "timing"),
        config_path=CONFIG,
        expected_exit_code=2,
        expected_document_type="rtl-advisor.agent.error",
        expected_values=(ExpectedValue("error.code", "input_not_found"),),
    )

    result = compare_scenario(
        scenario,
        repo_root=ROOT,
        runner_path=RUNNER,
        timeout_seconds=30,
    )

    assert result["status"] == "passed"
    assert result["comparison"]["payload_equal"] is True
    assert result["comparison"]["semantic_hash_equal"] is True


def test_source_hash_is_preserved_through_both_paths() -> None:
    original_hash = _sha256(FIXTURE)
    scenario = ParityScenario(
        scenario_id="top_required",
        description="top required",
        operation="review",
        arguments=(str(FIXTURE), "--objective", "balanced"),
        config_path=CONFIG,
        expected_exit_code=2,
        expected_document_type="rtl-advisor.agent.error",
        expected_values=(ExpectedValue("error.code", "top_required"),),
        source_paths=(FIXTURE,),
    )

    result = compare_scenario(
        scenario,
        repo_root=ROOT,
        runner_path=RUNNER,
        timeout_seconds=30,
    )

    assert result["status"] == "passed"
    assert result["comparison"]["sources_unchanged"] is True
    assert _sha256(FIXTURE) == original_hash


def test_comparison_fails_when_expected_field_differs() -> None:
    scenario = ParityScenario(
        scenario_id="wrong_expectation",
        description="wrong expectation",
        operation="capabilities",
        arguments=(),
        config_path=CONFIG,
        expected_exit_code=0,
        expected_document_type="rtl-advisor.agent.capabilities",
        expected_values=(ExpectedValue("analysis.live_recommendation_ready", True),),
    )

    result = compare_scenario(
        scenario,
        repo_root=ROOT,
        runner_path=RUNNER,
        timeout_seconds=30,
    )

    assert result["status"] == "failed"
    assert result["comparison"]["payload_equal"] is True
    assert any("live_recommendation_ready" in item for item in result["errors"])


def test_markdown_summary_carries_report_status_and_evidence_path() -> None:
    report = {
        "status": "passed",
        "scenarios": [
            {
                "scenario_id": "missing_input",
                "status": "passed",
                "terminal": {
                    "exit_code": 2,
                    "payload": {
                        "document_type": "rtl-advisor.agent.error",
                        "error": {"code": "input_not_found"},
                    },
                },
                "plugin_runner": {"exit_code": 2},
                "comparison": {
                    "semantic_hash_equal": True,
                    "sources_unchanged": True,
                },
            }
        ],
        "artifacts": {"json": "/tmp/parity.json"},
    }

    markdown = render_markdown(report)

    assert "Overall status: **passed**" in markdown
    assert "| missing_input | input_not_found | 2 | 2 | yes | yes | **passed** |" in markdown
    assert "`/tmp/parity.json`" in markdown


def test_report_hash_is_stable_for_equivalent_content() -> None:
    first = {"status": "passed", "scenarios": []}
    second = json.loads(json.dumps(first))

    assert _json_hash(first) == _json_hash(second)


def test_generated_manifest_tracks_manifest_and_baseline_sources(
    tmp_path: Path,
) -> None:
    rtl_dir = tmp_path / "rtl"
    rtl_dir.mkdir()
    source = rtl_dir / "v0.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": "parity_case",
                "family": "width_signedness",
                "width": 8,
                "seed": 1,
                "baseline_id": "v0",
                "variants": [
                    {
                        "id": "v0",
                        "role": "baseline",
                        "file": "rtl/v0.sv",
                        "kernel_top": "top",
                        "wrapper_top": "top",
                        "expected_equivalent": True,
                        "sha256": _sha256(source),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    paths = _review_source_paths(manifest)

    assert paths[0] == manifest.resolve()
    assert paths[1] == source.resolve()
