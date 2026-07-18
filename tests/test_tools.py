from pathlib import Path
import sys

import pytest

from rtl_advisor.tools import ToolExecutionError, run_command, sha256_file


def test_run_command_captures_output() -> None:
    result = run_command(
        (sys.executable, "-c", "print('rtl-advisor')"),
        timeout_seconds=5,
    )

    assert result.returncode == 0
    assert result.stdout == "rtl-advisor"


def test_run_command_reports_missing_executable() -> None:
    with pytest.raises(ToolExecutionError, match="command not found"):
        run_command(("definitely-not-a-real-command",), timeout_seconds=5)


def test_sha256_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"abc")

    assert sha256_file(source) == (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
