from pathlib import Path

import pytest

from rtl_advisor.config import ConfigError, load_config


def write_config(path: Path, *, sha256: str = "a" * 64) -> None:
    path.write_text(
        f"""
[project]
artifacts_dir = "artifacts"
corpus_dir = "corpus"

[tools]
verilator = "verilator"
yosys = "yosys"
codex = "codex"
timeout_seconds = 10

[synthesis]
driving_cell = "BUF_X1"
output_load_ff = 10.0

[liberty]
name = "test"
path = "cells.lib"
url = "https://example.invalid/cells.lib"
sha256 = "{sha256}"
license_path = "LICENSE"
license_url = "https://example.invalid/LICENSE"
source_commit = "test-commit"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_load_config_resolves_paths_from_config_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "rtl-advisor.toml"
    write_config(config_path)

    config = load_config(config_path)

    assert config.root == tmp_path
    assert config.artifacts_dir == tmp_path / "artifacts"
    assert config.liberty.path == tmp_path / "cells.lib"
    assert config.tools.timeout_seconds == 10
    assert config.synthesis.driving_cell == "BUF_X1"
    assert config.synthesis.output_load_ff == 10.0
    assert config.codex.model == "gpt-5.6-sol"
    assert config.codex.default_effort == "xhigh"
    assert config.codex.timeout_seconds == 600


def test_load_config_rejects_invalid_checksum(tmp_path: Path) -> None:
    config_path = tmp_path / "rtl-advisor.toml"
    write_config(config_path, sha256="not-a-checksum")

    with pytest.raises(ConfigError, match="sha256"):
        load_config(config_path)
