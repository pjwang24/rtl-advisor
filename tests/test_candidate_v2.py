from __future__ import annotations

import json
import hashlib
from pathlib import Path

import rtl_advisor.candidate_v2 as candidate_v2
from rtl_advisor.candidate_v2 import (
    _design_from_artifact,
    emit_selected_candidate,
    verify_emitted_candidate,
)
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.rtl_input import SlangLintResult


def test_design_artifact_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "top": "top",
                "files": [{"path": str(source), "sha256": "a" * 64}],
                "include_dirs": [],
                "defines": [],
                "filelists": [],
                "design_hash": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    design = _design_from_artifact(input_path)

    assert design.top == "top"
    assert design.files[0].path == str(source)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys="yosys",
            codex="codex",
            timeout_seconds=5,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="unused",
            path=tmp_path / "unused.lib",
            url="https://example.invalid/unused.lib",
            sha256="a" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="unused",
        ),
    )


def test_candidate_preparation_and_formal_verification_are_separate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    baseline = rtl / "v0.sv"
    baseline.write_text(
        "module demo_v0_kernel; wire y = 1'b0; endmodule\n"
        "module demo_v0_top; demo_v0_kernel kernel(); endmodule\n",
        encoding="utf-8",
    )
    sibling = rtl / "v1.sv"
    sibling.write_text(
        "module demo_v1_kernel; wire y = 1'b1; endmodule\n"
        "module demo_v1_top; demo_v1_kernel kernel(); endmodule\n",
        encoding="utf-8",
    )
    original_hash = _sha256(baseline)
    analysis_root = tmp_path / "analysis"
    analysis_root.mkdir()
    input_path = analysis_root / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "top": "demo_v0_top",
                "files": [{"path": str(baseline), "sha256": original_hash}],
                "include_dirs": [],
                "defines": [],
                "filelists": [],
                "design_hash": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    analysis_path = analysis_root / "analysis-v2.json"
    analysis = {
        "selected_candidate_id": "candidate01",
        "candidates": [
            {
                "candidate_id": "candidate01",
                "template_id": "v1",
                "transformation_id": "test_transform",
                "source": {"locations": [{"file": str(baseline)}]},
            }
        ],
    }
    analysis_path.write_text(json.dumps(analysis), encoding="utf-8")

    monkeypatch.setattr(
        candidate_v2,
        "lint_with_pyslang",
        lambda design: SlangLintResult(
            status="passed",
            version="test",
            diagnostics=(),
        ),
    )
    monkeypatch.setattr(
        candidate_v2,
        "_verilator_lint",
        lambda *args, **kwargs: {"status": "passed", "returncode": 0},
    )
    proof_calls = []

    def fake_proof(config, baseline_design, candidate_design, output_dir):
        proof_calls.append(candidate_design.design_hash)
        return {
            "status": "passed",
            "baseline_design_hash": baseline_design.design_hash,
            "candidate_design_hash": candidate_design.design_hash,
        }

    monkeypatch.setattr(candidate_v2, "_prove_equivalence", fake_proof)

    prepared = emit_selected_candidate(
        _config(tmp_path),
        analysis,
        analysis_path,
        verify_formal=False,
    )

    assert prepared["status"] == "prepared"
    assert prepared["formal"]["status"] == "not_run"
    assert proof_calls == []
    assert Path(prepared["diff_path"]).is_file()
    assert _sha256(baseline) == original_hash

    verified = verify_emitted_candidate(
        _config(tmp_path),
        analysis_path,
        "candidate01",
    )

    assert verified["status"] == "accepted"
    assert verified["safe"] is True
    assert verified["formal"]["status"] == "passed"
    assert proof_calls == [prepared["candidate_design_hash"]]
    assert verified["source_integrity"]["original"]["ok"] is True
    assert _sha256(baseline) == original_hash
