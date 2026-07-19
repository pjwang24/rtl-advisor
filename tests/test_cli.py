from pathlib import Path
import json

from rtl_advisor.cli import _normalized_agent_command, build_parser, main
from rtl_advisor.config import load_config


def write_fake_tool(root: Path) -> Path:
    tool = root / "fake-tool"
    tool.write_text("#!/bin/sh\nprintf 'fake tool 1.0\\n'\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def write_test_config(
    root: Path,
    *,
    liberty_sha256: str,
    tool: Path,
) -> Path:
    config_path = root / "rtl-advisor.toml"
    config_path.write_text(
        f"""
[project]
artifacts_dir = "artifacts"
corpus_dir = "corpus"

[tools]
verilator = "{tool}"
yosys = "{tool}"
codex = "{tool}"
timeout_seconds = 5

[synthesis]
driving_cell = "BUF_X1"
output_load_ff = 10.0

[liberty]
name = "test library"
path = "cells.lib"
url = "https://example.invalid/cells.lib"
sha256 = "{liberty_sha256}"
license_path = "LICENSE"
license_url = "https://example.invalid/LICENSE"
source_commit = "test-commit"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def test_setup_reports_missing_liberty_without_download(
    tmp_path: Path,
    capsys,
) -> None:
    tool = write_fake_tool(tmp_path)
    config_path = write_test_config(
        tmp_path,
        liberty_sha256="a" * 64,
        tool=tool,
    )

    exit_code = main(
        ("--config", str(config_path), "setup", "--no-download", "--json")
    )

    assert exit_code == 1
    assert '"name": "liberty"' in capsys.readouterr().out
    assert (tmp_path / "artifacts/setup/environment.json").is_file()


def test_setup_succeeds_with_verified_local_assets(tmp_path: Path) -> None:
    tool = write_fake_tool(tmp_path)
    library = tmp_path / "cells.lib"
    library.write_bytes(b"abc")
    (tmp_path / "LICENSE").write_text("test license\n", encoding="utf-8")
    checksum = (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
    config_path = write_test_config(
        tmp_path,
        liberty_sha256=checksum,
        tool=tool,
    )

    exit_code = main(("--config", str(config_path), "setup", "--no-download"))

    assert exit_code == 0


def test_analyze_parser_requires_explicit_patch_flag() -> None:
    parser = build_parser()
    default_args = parser.parse_args(("analyze", "case"))
    patch_args = parser.parse_args(
        ("analyze", "case", "--emit-patch", "--patch-candidate", "v3")
    )

    assert default_args.emit_patch is False
    assert patch_args.emit_patch is True
    assert patch_args.patch_candidate == "v3"


def test_corpus_suite_parser_requires_explicit_suite() -> None:
    parser = build_parser()
    generate_args = parser.parse_args(
        ("corpus", "generate-suite", "--suite", "heldout")
    )
    validate_args = parser.parse_args(
        ("corpus", "validate-suite", "--suite", "development", "--json")
    )

    assert generate_args.suite == "heldout"
    assert validate_args.suite == "development"
    assert validate_args.json_output is True


def test_benchmark_parser_supports_smoke_pilot_and_arm_filters() -> None:
    parser = build_parser()
    run_args = parser.parse_args(
        ("benchmark", "run", "--suite", "smoke", "--arm", "hybrid-xhigh")
    )
    report_args = parser.parse_args(
        ("benchmark", "report", "--suite", "pilot", "--json")
    )

    assert run_args.suite == "smoke"
    assert run_args.arm == "hybrid-xhigh"
    assert report_args.suite == "pilot"
    assert report_args.json_output is True


def test_openroad_v2_parser_has_lock_run_and_report_commands() -> None:
    parser = build_parser()
    lock_args = parser.parse_args(
        ("benchmark", "openroad-lock-v2", "--image", "example/image@sha256:abc")
    )
    run_args = parser.parse_args(
        ("benchmark", "openroad-run-v2", "--workers", "3", "--retry-failed")
    )
    report_args = parser.parse_args(("benchmark", "openroad-report-v2", "--json"))

    assert lock_args.image == "example/image@sha256:abc"
    assert run_args.workers == 3
    assert run_args.retry_failed is True
    assert report_args.json_output is True


def test_v22_model_parser_is_versioned() -> None:
    args = build_parser().parse_args(("model", "train-v22", "--json"))
    assert args.model_command == "train-v22"
    assert args.json_output is True


def test_v22_analysis_parser_is_versioned() -> None:
    args = build_parser().parse_args(
        ("analyze-v22", "case/manifest.json", "--mode", "risk", "--json")
    )
    assert args.command == "analyze-v22"
    assert args.mode == "risk"
    assert args.json_output is True


def test_v22_lock_parser_is_versioned() -> None:
    args = build_parser().parse_args(("benchmark", "lock-v22", "--json"))
    assert args.benchmark_command == "lock-v22"
    assert args.json_output is True


def test_v22_diagnostic_parser_is_versioned() -> None:
    args = build_parser().parse_args(("benchmark", "diagnose-v22", "--json"))
    assert args.benchmark_command == "diagnose-v22"
    assert args.json_output is True


def test_synthesis_redundancy_parser_is_versioned() -> None:
    args = build_parser().parse_args(
        ("benchmark", "synthesis-redundancy-v1", "--workers", "3", "--json")
    )
    assert args.benchmark_command == "synthesis-redundancy-v1"
    assert args.workers == 3
    assert args.json_output is True


def test_full_synthesis_robustness_parser_is_versioned() -> None:
    args = build_parser().parse_args(
        (
            "benchmark",
            "synthesis-robustness-full-v1",
            "--workers",
            "12",
            "--json",
        )
    )
    assert args.benchmark_command == "synthesis-robustness-full-v1"
    assert args.workers == 12
    assert args.json_output is True


def test_frontend_parser_defaults_to_localhost() -> None:
    args = build_parser().parse_args(("frontend", "--port", "9001"))
    assert args.command == "frontend"
    assert args.host == "127.0.0.1"
    assert args.port == 9001


def test_agent_parser_exposes_versioned_automation_commands() -> None:
    parser = build_parser()
    capabilities = parser.parse_args(("agent", "capabilities", "--json"))
    review = parser.parse_args(
        (
            "agent",
            "review",
            "top.sv",
            "--top",
            "top",
            "--objective",
            "timing",
            "--json",
        )
    )
    candidate = parser.parse_args(
        ("agent", "candidate", "review-" + "a" * 20, "--finding", "finding01")
    )
    verify = parser.parse_args(
        ("agent", "verify", "review-" + "a" * 20, "--candidate", "candidate01")
    )

    assert capabilities.agent_command == "capabilities"
    assert review.top == "top"
    assert review.objective == "timing"
    assert candidate.finding == "finding01"
    assert verify.candidate == "candidate01"


def test_agent_capabilities_is_always_machine_readable(
    tmp_path: Path,
    capsys,
) -> None:
    tool = write_fake_tool(tmp_path)
    library = tmp_path / "cells.lib"
    library.write_bytes(b"abc")
    (tmp_path / "LICENSE").write_text("test license\n", encoding="utf-8")
    checksum = (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
    config_path = write_test_config(
        tmp_path,
        liberty_sha256=checksum,
        tool=tool,
    )

    exit_code = main(("--config", str(config_path), "agent", "capabilities"))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["document_type"] == "rtl-advisor.agent.capabilities"
    assert payload["analysis"]["live_recommendation_ready"] is False
    assert "--schema-version" not in payload["command"]
    assert payload["semantic_hash"]


def test_agent_v1_normalized_commands_match_pre_v2_golden(tmp_path: Path) -> None:
    tool = write_fake_tool(tmp_path)
    config_path = write_test_config(
        tmp_path,
        liberty_sha256="a" * 64,
        tool=tool,
    )
    config = load_config(config_path)
    parser = build_parser()
    source = tmp_path / "rtl/top.sv"
    include_dir = tmp_path / "include"
    gate_model = tmp_path / "models/gate.json"

    argument_sets = (
        (
            ("agent", "capabilities", "--schema-version", "1", "--json"),
            (
                "rtl-advisor",
                "--config",
                str(config_path),
                "agent",
                "capabilities",
                "--json",
            ),
        ),
        (
            (
                "agent",
                "review",
                "rtl/top.sv",
                "--objective",
                "timing",
                "--top",
                "top",
                "-I",
                "include",
                "-D",
                "WIDTH=8",
                "--gate-model",
                "models/gate.json",
                "--force",
                "--schema-version",
                "1",
                "--json",
            ),
            (
                "rtl-advisor",
                "--config",
                str(config_path),
                "agent",
                "review",
                str(source),
                "--objective",
                "timing",
                "--top",
                "top",
                "-I",
                str(include_dir),
                "-D",
                "WIDTH=8",
                "--gate-model",
                str(gate_model),
                "--force",
                "--json",
            ),
        ),
        (
            (
                "agent",
                "candidate",
                "review-" + "a" * 20,
                "--finding",
                "finding01",
                "--schema-version",
                "1",
                "--json",
            ),
            (
                "rtl-advisor",
                "--config",
                str(config_path),
                "agent",
                "candidate",
                "review-" + "a" * 20,
                "--finding",
                "finding01",
                "--json",
            ),
        ),
        (
            (
                "agent",
                "verify",
                "review-" + "a" * 20,
                "--candidate",
                "candidate01",
                "--schema-version",
                "1",
                "--json",
            ),
            (
                "rtl-advisor",
                "--config",
                str(config_path),
                "agent",
                "verify",
                "review-" + "a" * 20,
                "--candidate",
                "candidate01",
                "--json",
            ),
        ),
    )

    for arguments, golden in argument_sets:
        assert _normalized_agent_command(config, parser.parse_args(arguments)) == golden


def test_explicit_v1_selector_preserves_default_v1_payload(
    tmp_path: Path,
    capsys,
) -> None:
    tool = write_fake_tool(tmp_path)
    library = tmp_path / "cells.lib"
    library.write_bytes(b"abc")
    (tmp_path / "LICENSE").write_text("test license\n", encoding="utf-8")
    config_path = write_test_config(
        tmp_path,
        liberty_sha256=(
            "ba7816bf8f01cfea414140de5dae2223"
            "b00361a396177a9cb410ff61f20015ad"
        ),
        tool=tool,
    )

    default_exit = main(("--config", str(config_path), "agent", "capabilities"))
    default_payload = json.loads(capsys.readouterr().out)
    explicit_exit = main(
        (
            "--config",
            str(config_path),
            "agent",
            "capabilities",
            "--schema-version",
            "1",
        )
    )
    explicit_payload = json.loads(capsys.readouterr().out)

    assert default_exit == explicit_exit == 0
    assert explicit_payload == default_payload
    assert explicit_payload["flow_version"] == "rtl-advisor-agent-v1"
    assert "--schema-version" not in explicit_payload["command"]


def test_agent_v2_rejects_v1_model_controls(tmp_path: Path, capsys) -> None:
    tool = write_fake_tool(tmp_path)
    library = tmp_path / "cells.lib"
    library.write_bytes(b"abc")
    (tmp_path / "LICENSE").write_text("test license\n", encoding="utf-8")
    config_path = write_test_config(
        tmp_path,
        liberty_sha256=(
            "ba7816bf8f01cfea414140de5dae2223"
            "b00361a396177a9cb410ff61f20015ad"
        ),
        tool=tool,
    )
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")

    exit_code = main(
        (
            "--config",
            str(config_path),
            "agent",
            "review",
            str(source),
            "--top",
            "top",
            "--gate-model",
            "model.json",
            "--schema-version",
            "2",
            "--json",
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["error"]["code"] == "unsupported_v2_option"
    assert payload["flow_version"] == "rtl-advisor-agent-v2"
