from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from rtl_advisor.mvp_schema import normalized_result_projection


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (
    ROOT
    / "plugins/rtl-advisor/skills/analyze-rtl/scripts/run_rtl_advisor.py"
)
LIBERTY = ROOT / "third_party/nangate45/NangateOpenCellLibrary_typical.lib"
LIBERTY_LICENSE = ROOT / "third_party/nangate45/LICENSE"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _toolchain_ready() -> bool:
    if not LIBERTY.is_file() or not LIBERTY_LICENSE.is_file():
        return False
    if shutil.which("yosys") is None or shutil.which("verilator") is None:
        return False
    result = subprocess.run(
        ["yosys", "-V"], text=True, capture_output=True, check=False
    )
    return result.returncode == 0 and result.stdout.startswith("Yosys 0.63")


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "rtl-advisor.toml"
    config.write_text(
        f"""[project]
artifacts_dir = {json.dumps(str(tmp_path / 'artifacts'))}
corpus_dir = {json.dumps(str(tmp_path / 'corpus'))}

[tools]
verilator = "verilator"
yosys = "yosys"
codex = "codex"
timeout_seconds = 30

[synthesis]
driving_cell = "BUF_X1"
output_load_ff = 10.0

[liberty]
name = "Nangate45 typical"
path = {json.dumps(str(LIBERTY))}
url = "https://example.invalid/not-used"
sha256 = "{_sha256(LIBERTY)}"
license_path = {json.dumps(str(LIBERTY_LICENSE))}
license_url = "https://example.invalid/not-used"
source_commit = "036d106273e66855cd5214d49518fd0f0df7de61"
""",
        encoding="utf-8",
    )
    return config


def _run(command: list[str], env: dict[str, str]) -> tuple[int, dict]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


@pytest.mark.skipif(not _toolchain_ready(), reason="pinned MVP toolchain unavailable")
def test_agent_v2_cli_plugin_and_dashboard_artifacts_match(tmp_path: Path) -> None:
    source = tmp_path / "adder_chain.sv"
    source.write_text(
        """module adder_chain(
  input logic [15:0] a,b,c,d,
  output logic [15:0] y
);
  assign y = a + b + c + d;
endmodule
""",
        encoding="utf-8",
    )
    original_hash = _sha256(source)
    config = _write_config(tmp_path)
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "RTL_ADVISOR_BIN": shlex.join((sys.executable, "-m", "rtl_advisor")),
    }

    def compare(operation: str, arguments: list[str], expected_exit: int = 0) -> dict:
        terminal_exit, terminal = _run(
            [
                sys.executable,
                "-m",
                "rtl_advisor",
                "--config",
                str(config),
                "agent",
                operation,
                *arguments,
                "--schema-version",
                "2",
                "--json",
            ],
            environment,
        )
        plugin_exit, plugin = _run(
            [
                sys.executable,
                str(RUNNER),
                "--config",
                str(config),
                operation,
                *arguments,
            ],
            environment,
        )
        assert terminal_exit == plugin_exit == expected_exit
        assert normalized_result_projection(terminal) == normalized_result_projection(plugin)
        return terminal

    capabilities = compare("capabilities", [])
    assert capabilities["model"]["affects_mvp_decision"] is False

    review = compare(
        "review",
        [str(source), "--top", "adder_chain", "--objective", "balanced"],
    )
    assert review["decision"] == "candidate_available"
    run_id = review["run_id"]
    finding_id = review["findings"][0]["finding_id"]

    candidate = compare("candidate", [run_id, "--finding", finding_id])
    candidate_id = candidate["candidate_id"]
    assert Path(candidate["artifacts"]["diff"]).is_file()

    verification = compare("verify", [run_id, "--candidate", candidate_id])
    assert verification["status"] == "formal_passed"
    assert verification["safe"] is True

    measurement = compare("measure", [run_id, "--candidate", candidate_id])
    assert measurement["decision"] == "synthesis_handles"
    assert {
        item["classification"] for item in measurement["measurements"].values()
    } == {"neutral"}

    report = compare("report", [run_id])
    assert report["decision"] == "synthesis_handles"
    assert Path(report["artifacts"]["report"]).is_file()
    assert Path(report["artifacts"]["html"]).is_file()
    assert _sha256(source) == original_hash
