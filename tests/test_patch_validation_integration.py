from pathlib import Path
import shutil

import pytest

from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.corpus import generate_resource_sharing_case
from rtl_advisor.patch_validation import validate_candidate_patch
from rtl_advisor.tools import sha256_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBERTY = (
    PROJECT_ROOT
    / "third_party"
    / "nangate45"
    / "NangateOpenCellLibrary_typical.lib"
)
YOSYS = shutil.which("yosys")
VERILATOR = shutil.which("verilator")


def make_config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator=VERILATOR or "verilator",
            yosys=YOSYS or "yosys",
            codex="codex",
            timeout_seconds=30,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="Nangate45 typical",
            path=LIBERTY,
            url="unused",
            sha256=sha256_file(LIBERTY),
            license_path=LIBERTY.parent / "LICENSE",
            license_url="unused",
            source_commit="test",
        ),
    )


@pytest.mark.skipif(
    YOSYS is None or VERILATOR is None or not LIBERTY.is_file(),
    reason="Yosys, Verilator, and the pinned Nangate45 library are required",
)
def test_safe_patch_accepts_equivalent_candidate_without_touching_sources(
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "corpus/development/dev_patch_test"
    manifest_path = generate_resource_sharing_case(
        case_dir,
        case_id="dev_patch_test",
        width=8,
        seed=41,
    )
    baseline_before = (case_dir / "rtl/v0.sv").read_bytes()
    candidate_before = (case_dir / "rtl/v1.sv").read_bytes()

    result = validate_candidate_patch(make_config(tmp_path), manifest_path, "v1")

    assert result["status"] == "accepted"
    assert result["accepted"] is True
    assert result["originals_unchanged"] is True
    assert all(stage["ok"] for stage in result["stages"].values())
    assert result["synthesis_comparison"] is not None
    assert Path(result["patch_path"]).is_file()
    assert Path(result["result_path"]).is_file()
    assert Path(result["workspace_path"]).is_dir()
    assert (case_dir / "rtl/v0.sv").read_bytes() == baseline_before
    assert (case_dir / "rtl/v1.sv").read_bytes() == candidate_before


@pytest.mark.skipif(
    YOSYS is None or VERILATOR is None or not LIBERTY.is_file(),
    reason="Yosys, Verilator, and the pinned Nangate45 library are required",
)
def test_safe_patch_rejects_inequivalent_candidate_before_synthesis(
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "corpus/development/dev_patch_bad"
    manifest_path = generate_resource_sharing_case(
        case_dir,
        case_id="dev_patch_bad",
        width=8,
        seed=41,
    )
    baseline_before = (case_dir / "rtl/v0.sv").read_bytes()
    candidate_before = (case_dir / "rtl/n0.sv").read_bytes()

    result = validate_candidate_patch(make_config(tmp_path), manifest_path, "n0")

    assert result["status"] == "rejected"
    assert result["accepted"] is False
    assert result["originals_unchanged"] is True
    assert result["stages"]["lint"]["ok"] is True
    assert result["stages"]["equivalence"]["status"] == "inequivalent"
    assert result["stages"]["synthesis"]["status"] == "skipped"
    assert (case_dir / "rtl/v0.sv").read_bytes() == baseline_before
    assert (case_dir / "rtl/n0.sv").read_bytes() == candidate_before
