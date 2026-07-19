from __future__ import annotations

from pathlib import Path

import pytest

from rtl_advisor.cli import main
from rtl_advisor.config import LibertyConfig, ProjectConfig, SynthesisConfig, ToolConfig
from rtl_advisor.mvp_agent import agent_v2_capabilities


def _missing_prerequisite_config(tmp_path: Path) -> ProjectConfig:
    missing_root = tmp_path / "missing-tools"
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator=str(missing_root / "verilator"),
            yosys=str(missing_root / "yosys"),
            codex=str(missing_root / "codex"),
            timeout_seconds=5,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="missing",
            path=tmp_path / "missing.lib",
            url="https://example.invalid/missing.lib",
            sha256="0" * 64,
            license_path=tmp_path / "missing-license",
            license_url="https://example.invalid/missing-license",
            source_commit="missing",
        ),
    )


def test_v2_capabilities_report_each_missing_tool_prerequisite(tmp_path: Path) -> None:
    result = agent_v2_capabilities(_missing_prerequisite_config(tmp_path))

    assert result["tools"]["yosys"]["status"] == "missing"
    assert result["tools"]["verilator"]["status"] == "missing"
    assert result["tools"]["liberty"]["status"] == "missing"
    assert result["operations"]["review"]["available"] is True
    assert result["operations"]["candidate"]["available"] is True
    assert result["operations"]["verify"]["available"] is False
    assert result["operations"]["measure"]["available"] is False


def test_cli_reports_a_missing_configuration_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist.toml"

    with pytest.raises(SystemExit) as error:
        main(
            [
                "--config",
                str(missing),
                "agent",
                "capabilities",
                "--schema-version",
                "2",
                "--json",
            ]
        )

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "configuration file not found" in stderr
    assert str(missing) in stderr
