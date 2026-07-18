from pathlib import Path

from rtl_advisor.cli import build_parser, main


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


def test_frontend_parser_defaults_to_localhost() -> None:
    args = build_parser().parse_args(("frontend", "--port", "9001"))
    assert args.command == "frontend"
    assert args.host == "127.0.0.1"
    assert args.port == 9001
