import json
from pathlib import Path

import pytest

from rtl_advisor.codex_analysis import CodexAnalysisError, analyze_with_codex
from rtl_advisor.config import (
    CodexConfig,
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.corpus import generate_resource_sharing_case


VALID_RESPONSE = {
    "summary": "One resource-sharing opportunity was found.",
    "findings": [
        {
            "category": "arithmetic_resource_sharing",
            "source": {
                "file": "design.sv",
                "start_line": 17,
                "end_line": 17,
            },
            "evidence": [
                "Two additions feed the data inputs of the same result mux."
            ],
            "confidence": 0.88,
            "recommendation": "Select both operand pairs before one shared addition.",
            "transformation_id": "share_arithmetic_by_muxing_inputs",
            "predicted_effect": {
                "delay": "uncertain",
                "area": "improve",
                "cell_count": "improve",
            },
            "risks": ["Operand muxes may increase the arithmetic input delay."],
        }
    ],
}


def write_fake_codex(
    path: Path,
    response: object,
    *,
    sleep_seconds: int = 0,
    item_type: str = "agent_message",
) -> Path:
    response_text = response if isinstance(response, str) else json.dumps(response)
    script = f"""#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("codex-cli fake-1.0")
    raise SystemExit(0)

time.sleep({sleep_seconds})
args = sys.argv
output = Path(args[args.index("--output-last-message") + 1])
prompt = sys.stdin.read()
(output.parent / "captured-prompt.txt").write_text(prompt, encoding="utf-8")
response_text = {json.dumps(response_text)}
output.write_text(response_text, encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": "fake"}}))
print(json.dumps({{"type": "turn.started"}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": {json.dumps(item_type)}, "text": response_text}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_config(
    tmp_path: Path,
    fake_codex: Path,
    *,
    timeout_seconds: int = 5,
) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys="yosys",
            codex=str(fake_codex),
            timeout_seconds=5,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="unused",
            path=tmp_path / "unused.lib",
            url="unused",
            sha256="0" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="unused",
            source_commit="unused",
        ),
        codex=CodexConfig(
            model="gpt-5.6-sol",
            default_effort="xhigh",
            timeout_seconds=timeout_seconds,
        ),
    )


def generated_case(tmp_path: Path) -> Path:
    return generate_resource_sharing_case(
        tmp_path / "corpus/development/dev_rs_secret",
        case_id="dev_rs_secret",
        width=8,
        seed=42,
    )


def structural_rules() -> dict:
    return {
        "findings": [
            {
                "rule_id": "resource_sharing.output_mux.v1",
                "category": "arithmetic_resource_sharing",
                "source": {
                    "raw": "rtl/v0.sv:17.9-17.30",
                    "locations": [
                        {
                            "file": "rtl/v0.sv",
                            "start_line": 17,
                            "end_line": 17,
                        }
                    ],
                },
                "confidence": 0.9,
                "evidence": {
                    "selection_node": "dev_rs_secret_v0_kernel::mux",
                    "operator": "add",
                },
                "recommendation": "Share the arithmetic operator.",
                "transformation_id": "share_arithmetic_by_muxing_inputs",
                "predicted_effect": {
                    "delay": "uncertain",
                    "area": "improve",
                    "cell_count": "improve",
                },
                "risks": ["The input mux can lengthen the path."],
            }
        ]
    }


def test_codex_and_hybrid_inputs_are_blinded_and_cached(tmp_path: Path) -> None:
    fake = write_fake_codex(tmp_path / "fake-codex", VALID_RESPONSE)
    config = make_config(tmp_path, fake)
    manifest_path = generated_case(tmp_path)

    codex = analyze_with_codex(
        config,
        manifest_path,
        "v0",
        mode="codex",
    )
    cached = analyze_with_codex(
        config,
        manifest_path,
        "v0",
        mode="codex",
    )
    hybrid = analyze_with_codex(
        config,
        manifest_path,
        "v0",
        mode="hybrid",
        rules_analysis=structural_rules(),
    )

    codex_request_path = Path(codex.result["provenance"]["request_path"])
    hybrid_request_path = Path(hybrid.result["provenance"]["request_path"])
    codex_request = json.loads(codex_request_path.read_text(encoding="utf-8"))
    hybrid_request = json.loads(hybrid_request_path.read_text(encoding="utf-8"))
    model_visible = codex_request_path.read_text(encoding="utf-8")

    assert codex.result["mode"] == "codex"
    assert codex.result["model"] == "gpt-5.6-sol"
    assert codex.result["effort"] == "xhigh"
    assert codex.result["findings"][0]["source"]["file"] == "rtl/v0.sv"
    assert codex.result["provenance"]["audited_no_tool_use"] is True
    assert codex.result["provenance"]["synthesis_labels_visible"] is False
    assert codex.result["provenance"]["latency_seconds"] >= 0
    assert codex.result["provenance"]["model_usage"] == {
        "input_tokens": 1,
        "output_tokens": 1,
    }
    assert cached.cached is True
    assert "structural_findings" not in codex_request
    assert len(hybrid_request["structural_findings"]) == 1
    assert "dev_rs_secret" not in model_visible
    assert "case_id" not in model_visible
    assert "variant_id" not in model_visible
    assert "critical_delay_ps" not in model_visible
    assert "area_total" not in model_visible
    assert "dev_rs_secret" not in hybrid_request_path.read_text(encoding="utf-8")


def test_named_runs_use_separate_immutable_directories(tmp_path: Path) -> None:
    fake = write_fake_codex(tmp_path / "fake-codex", VALID_RESPONSE)
    config = make_config(tmp_path, fake)
    manifest_path = generated_case(tmp_path)

    first = analyze_with_codex(
        config,
        manifest_path,
        "v0",
        mode="codex",
        run_id="benchmark_r0",
    )
    second = analyze_with_codex(
        config,
        manifest_path,
        "v0",
        mode="codex",
        run_id="benchmark_r1",
    )

    assert first.output_path != second.output_path
    assert first.output_path.is_file()
    assert second.output_path.is_file()
    assert first.result["provenance"]["run_id"] == "benchmark_r0"
    assert second.result["provenance"]["run_id"] == "benchmark_r1"


def test_invalid_codex_response_is_recorded(tmp_path: Path) -> None:
    fake = write_fake_codex(
        tmp_path / "fake-codex",
        {"summary": "bad", "findings": [{"unexpected": True}]},
    )
    config = make_config(tmp_path, fake)
    manifest_path = generated_case(tmp_path)

    with pytest.raises(CodexAnalysisError, match="expected keys"):
        analyze_with_codex(config, manifest_path, "v0", mode="codex")

    failure = (
        tmp_path
        / "artifacts/cases/dev_rs_secret/analysis/codex/v0/xhigh/failure.json"
    )
    assert json.loads(failure.read_text(encoding="utf-8"))["kind"] == (
        "schema_or_audit"
    )


def test_codex_timeout_is_recorded(tmp_path: Path) -> None:
    fake = write_fake_codex(
        tmp_path / "fake-codex",
        VALID_RESPONSE,
        sleep_seconds=2,
    )
    config = make_config(tmp_path, fake, timeout_seconds=1)
    manifest_path = generated_case(tmp_path)

    with pytest.raises(CodexAnalysisError, match="timed out"):
        analyze_with_codex(config, manifest_path, "v0", mode="codex")

    failure = (
        tmp_path
        / "artifacts/cases/dev_rs_secret/analysis/codex/v0/xhigh/failure.json"
    )
    assert json.loads(failure.read_text(encoding="utf-8"))["kind"] == (
        "infrastructure"
    )


def test_codex_tool_activity_is_rejected(tmp_path: Path) -> None:
    fake = write_fake_codex(
        tmp_path / "fake-codex",
        VALID_RESPONSE,
        item_type="command_execution",
    )
    config = make_config(tmp_path, fake)
    manifest_path = generated_case(tmp_path)

    with pytest.raises(CodexAnalysisError, match="used item type"):
        analyze_with_codex(config, manifest_path, "v0", mode="codex")
