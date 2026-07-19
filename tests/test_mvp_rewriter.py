from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import shutil

import pytest

import rtl_advisor.mvp_rewriter as mvp_rewriter
import rtl_advisor.mvp_measure as mvp_measure

from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.mvp_rewriter import (
    MVPRewriteError,
    _prove,
    candidate_design_from_record,
    prepare_addition_candidate,
    scan_addition_analysis,
    scan_addition_sites,
    verify_addition_candidate,
)
from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.rtl_input import DesignInputV2, SourceFileV2, normalize_design_input
from rtl_advisor.tools import CommandResult, ToolExecutionError


def _write_design(tmp_path: Path, source: str, *, name: str = "top.sv") -> DesignInputV2:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return normalize_design_input(top="top", files=(path,), base=tmp_path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_single_file(path: Path) -> DesignInputV2:
    source = SourceFileV2(path=str(path), sha256=_sha256(path))
    core = {
        "schema_version": 2,
        "top": "top",
        "files": [{"path": source.path, "sha256": source.sha256}],
        "include_dirs": [],
        "defines": [],
        "filelists": [],
    }
    return DesignInputV2(
        schema_version=2,
        top="top",
        files=(source,),
        include_dirs=(),
        defines=(),
        filelists=(),
        design_hash=stable_hash(core),
    )


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
            timeout_seconds=30,
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


def test_scan_finds_stable_source_linked_sites_and_balances_them(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        """\
module top(
  input logic [7:0] a, b, c, d,
  output logic [7:0] y, z
);
  assign y = a + b + c + d;
  assign z = ((a + b) + c) + d;
endmodule
""",
    )

    first = scan_addition_sites(design)
    second = scan_addition_sites(design)

    assert first == second
    assert len(first) == 2
    assert [item["target"]["name"] for item in first] == ["y", "z"]
    assert first[0]["status"] == "candidate_available"
    assert first[0]["finding_id"].startswith("addsite_")
    assert first[0]["source"]["line"] == 5
    assert first[0]["target"]["width"] == 8
    assert [item["name"] for item in first[0]["operands"]] == ["a", "b", "c", "d"]
    assert first[0]["replacement_expression"] == "((a + b) + (c + d))"


def test_scan_skips_an_already_balanced_expression(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        """\
module top(
  input logic [7:0] a, b, c, d,
  output logic [7:0] y
);
  assign y = (a + b) + (c + d);
endmodule
""",
    )

    assert scan_addition_sites(design) == []


@pytest.mark.parametrize(
    "source",
    [
        # Fixed-width only: parameterized ranges are deliberately unsupported.
        """module top #(parameter W=8) (
             input logic [W-1:0] a,b,c, output logic [W-1:0] y);
             assign y=a+b+c; endmodule""",
        # Mixed signedness.
        """module top(input logic signed [7:0] a,
             input logic [7:0] b,c, output logic [7:0] y);
             assign y=a+b+c; endmodule""",
        # Destination/operand mismatch would rely on implicit sizing/truncation.
        """module top(input logic [7:0] a,b,c,
             output logic [6:0] y); assign y=a+b+c; endmodule""",
        # Procedural and potentially sequential sources are out of MVP scope.
        """module top(input logic [7:0] a,b,c,
             output logic [7:0] y); always_comb y=a+b+c; endmodule""",
        """module top(input logic clk, input logic [7:0] a,b,c,
             output logic [7:0] y); always_ff @(posedge clk) y<=a+b+c; endmodule""",
        # Macro-expanded expressions do not have an unambiguous rewrite span.
        """module top(input logic [7:0] a,b,c,
             output logic [7:0] y); `define ADD3 a+b+c
             assign y=`ADD3; endmodule""",
        # Functions and generate blocks are intentionally rejected wholesale.
        """module top(input logic [7:0] a,b,c,
             output logic [7:0] y); function automatic [7:0] f(input [7:0] x);
             f=x; endfunction assign y=f(a)+b+c; endmodule""",
        """module top(input logic [7:0] a,b,c,
             output logic [7:0] y); generate if (1) assign y=a+b+c;
             endgenerate endmodule""",
        # Multiple drivers make the target span ambiguous.
        """module top(input logic [7:0] a,b,c,d,
             output logic [7:0] y); assign y=a+b+c; assign y=a+b+d; endmodule""",
    ],
)
def test_scan_fails_closed_for_unsupported_or_ambiguous_rtl(
    tmp_path: Path,
    source: str,
) -> None:
    design = _write_design(tmp_path, source)

    assert scan_addition_sites(design) == []


def test_prepare_copies_complete_input_and_never_edits_original(tmp_path: Path) -> None:
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    helper = rtl / "helper.sv"
    helper.write_text("module helper; endmodule\n", encoding="utf-8")
    top = rtl / "top.sv"
    top.write_text(
        """\
module top(input logic [15:0] a,b,c,d, output logic [15:0] y);
  assign y = a + b + c + d;
endmodule
""",
        encoding="utf-8",
    )
    design = normalize_design_input(
        top="top",
        files=(helper, top),
        base=tmp_path,
    )
    original_hashes = {source.path: source.sha256 for source in design.files}
    finding = scan_addition_sites(design)[0]

    candidate = prepare_addition_candidate(
        design,
        finding["finding_id"],
        tmp_path / "artifacts",
    )

    assert candidate["status"] == "candidate_prepared"
    assert candidate["formal"] == {"status": "not_run", "safe": False}
    assert Path(candidate["artifact_dir"]).parent == (tmp_path / "artifacts").resolve()
    assert Path(candidate["diff_path"]).read_text(encoding="utf-8").count("+") > 0
    isolated = candidate_design_from_record(candidate)
    assert len(isolated.files) == 2
    assert all(Path(source.path).is_relative_to(Path(candidate["artifact_dir"])) for source in isolated.files)
    rewritten_top = next(Path(source.path) for source in isolated.files if Path(source.path).name == "top.sv")
    assert "((a + b) + (c + d))" in rewritten_top.read_text(encoding="utf-8")
    assert {source.path: _sha256(Path(source.path)) for source in design.files} == original_hashes
    assert candidate["source_integrity"]["original"]["ok"] is True
    assert candidate["source_integrity"]["candidate"]["ok"] is True

    # Deterministic preparation returns the same append-only record.
    assert prepare_addition_candidate(
        design,
        finding["finding_id"],
        tmp_path / "artifacts",
    ) == candidate


def test_stale_source_cannot_be_scanned_or_verified(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
    )
    finding = scan_addition_sites(design)[0]
    candidate = prepare_addition_candidate(
        design,
        finding["finding_id"],
        tmp_path / "artifacts",
    )
    Path(design.files[0].path).write_text("module top; endmodule\n", encoding="utf-8")

    with pytest.raises(MVPRewriteError) as scan_error:
        scan_addition_sites(design)
    assert scan_error.value.code == "stale_source"
    with pytest.raises(MVPRewriteError) as verify_error:
        verify_addition_candidate(_config(tmp_path), candidate, tmp_path / "artifacts")
    assert verify_error.value.code == "stale_candidate"


@pytest.mark.skipif(
    shutil.which("yosys") is None or shutil.which("verilator") is None,
    reason="Yosys and Verilator are required for the formal integration check",
)
def test_formal_accepts_rewrite_and_rejects_intentional_logic_error(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        """\
module top(input logic [7:0] a,b,c,d, output logic [7:0] y);
  assign y = a + b + c + d;
endmodule
""",
    )
    finding = scan_addition_sites(design)[0]
    candidate_record = prepare_addition_candidate(
        design,
        finding["finding_id"],
        tmp_path / "artifacts",
    )

    verified = verify_addition_candidate(
        _config(tmp_path),
        candidate_record,
        tmp_path / "artifacts",
    )

    assert verified["status"] == "formal_passed"
    assert verified["safe"] is True
    assert verified["formal"]["backend"] == "yosys-combinational-equiv-v1"
    assert verified["source_integrity"]["original"]["ok"] is True

    # Exercise the same backend on four hash-current negative controls. Pure
    # reassociation is mathematically equivalent here, so the final control
    # represents an incorrect tree by duplicating one leaf.
    wrong_sources = {
        "operand_removed": "assign y = a + b + c;",
        "bit_flipped": "assign y = (a ^ 8'h01) + b + c + d;",
        "width_changed": "logic [6:0] ab; assign ab = a + b; assign y = ab + c + d;",
        "incorrect_tree_grouping_with_duplicated_leaf": "assign y = (a + b) + (b + d);",
    }
    for control, statement in wrong_sources.items():
        wrong_path = tmp_path / f"{control}.sv"
        wrong_path.write_text(
            "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);\n"
            f"  {statement}\n"
            "endmodule\n",
            encoding="utf-8",
        )
        negative = _prove(
            _config(tmp_path),
            design,
            _normalized_single_file(wrong_path),
            tmp_path / "negative" / control,
        )

        assert negative["status"] == "failed", control


def test_formal_rejects_zero_exit_without_yosys_identity_or_success_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
    )
    true_path = shutil.which("true")
    assert true_path is not None
    config = replace(
        _config(tmp_path),
        tools=replace(_config(tmp_path).tools, yosys=true_path),
    )

    identity_rejected = _prove(config, design, design, tmp_path / "identity")

    assert identity_rejected["status"] == "inconclusive"
    assert identity_rejected["tool_identity"] is None

    trusted_identity = {
        "yosys_version": "Yosys 0.63 (test)",
        "yosys_path": true_path,
        "yosys_sha256": _sha256(Path(true_path)),
    }
    monkeypatch.setattr(mvp_measure, "_yosys_identity", lambda _config: trusted_identity)
    monkeypatch.setattr(
        mvp_rewriter,
        "run_command",
        lambda *args, **kwargs: CommandResult(
            command=(true_path,), returncode=0, stdout="", stderr=""
        ),
    )

    transcript_rejected = _prove(config, design, design, tmp_path / "transcript")

    assert transcript_rejected["status"] == "inconclusive"
    assert transcript_rejected["success_marker_seen"] is False


def test_formal_tool_timeout_is_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
    )
    identity = {
        "yosys_version": "Yosys 0.63 (test)",
        "yosys_path": "/test/yosys",
        "yosys_sha256": "a" * 64,
    }
    monkeypatch.setattr(mvp_measure, "_yosys_identity", lambda _config: identity)

    def timeout(*args, **kwargs):
        raise ToolExecutionError("command timed out after 1s")

    monkeypatch.setattr(mvp_rewriter, "run_command", timeout)

    result = _prove(_config(tmp_path), design, design, tmp_path / "timeout")

    assert result["status"] == "inconclusive"
    assert result["returncode"] is None
    assert "timed out" in result["detail"]


def test_candidate_record_semantic_hash_is_enforced(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
    )
    finding = scan_addition_sites(design)[0]
    candidate = prepare_addition_candidate(
        design,
        finding["finding_id"],
        tmp_path / "artifacts",
    )
    tampered = {**candidate, "finding_id": "changed"}

    with pytest.raises(MVPRewriteError) as error:
        candidate_design_from_record(tampered)

    assert error.value.code == "artifact_hash_mismatch"


def test_candidate_rewrite_preserves_crlf_bytes_outside_the_expression(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.sv"
    original = (
        b"module top(input logic [7:0] a,b,c,d, output logic [7:0] y);\r\n"
        b"  assign y = a + b + c + d;\r\n"
        b"endmodule\r\n"
    )
    source.write_bytes(original)
    design = normalize_design_input(top="top", files=(source,), base=tmp_path)
    finding = scan_addition_sites(design)[0]

    prepared = prepare_addition_candidate(
        design, finding["finding_id"], tmp_path / "artifacts"
    )
    candidate_path = Path(prepared["candidate_design"]["files"][0]["path"])
    rewritten = candidate_path.read_bytes()

    assert rewritten.count(b"\r\n") == original.count(b"\r\n")
    start = finding["source"]["start_offset"]
    end = finding["source"]["end_offset"]
    replacement = finding["replacement_expression"].encode("utf-8")
    assert rewritten == original[:start] + replacement + original[end:]


def _filelist_design(tmp_path: Path) -> tuple[DesignInputV2, Path, Path]:
    include = tmp_path / "include"
    include.mkdir()
    header = include / "context.svh"
    header.write_text("`define UNUSED_CONTEXT 1\n", encoding="utf-8")
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    source = rtl / "top.sv"
    source.write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);\n"
        "  assign y = a + b + c + d;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    filelist = tmp_path / "sources.f"
    filelist.write_text(
        "-I include\n-D WIDTH=8\nrtl/top.sv\n",
        encoding="utf-8",
    )
    return (
        normalize_design_input(top="top", filelist=filelist, base=tmp_path),
        header,
        filelist,
    )


@pytest.mark.skipif(
    shutil.which("yosys") is None or shutil.which("verilator") is None,
    reason="Yosys and Verilator are required for the formal integration check",
)
def test_filelist_include_define_context_is_isolated_and_formally_proven(
    tmp_path: Path,
) -> None:
    design, _, _ = _filelist_design(tmp_path)
    finding = scan_addition_sites(design)[0]
    prepared = prepare_addition_candidate(
        design, finding["finding_id"], tmp_path / "artifacts"
    )
    candidate = candidate_design_from_record(prepared)

    assert candidate.defines == ("WIDTH=8",)
    assert len(candidate.include_dirs) == 1
    assert len(candidate.filelists) == 1
    assert all(
        Path(path).is_relative_to(Path(prepared["artifact_dir"]))
        for path in (*candidate.include_dirs, *candidate.filelists)
    )
    verified = verify_addition_candidate(
        _config(tmp_path), prepared, tmp_path / "artifacts"
    )

    assert verified["status"] == "formal_passed"
    assert verified["safe"] is True
    assert verified["compile_context"]["baseline"] == prepared[
        "baseline_compile_context"
    ]
    assert verified["compile_context"]["candidate"] == prepared[
        "candidate_compile_context"
    ]


@pytest.mark.parametrize("changed_input", ("header", "filelist"))
def test_changed_header_or_filelist_invalidates_candidate_proof(
    tmp_path: Path, changed_input: str
) -> None:
    design, header, filelist = _filelist_design(tmp_path)
    finding = scan_addition_sites(design)[0]
    prepared = prepare_addition_candidate(
        design, finding["finding_id"], tmp_path / "artifacts"
    )
    changed_path = header if changed_input == "header" else filelist
    changed_path.write_text(changed_path.read_text(encoding="utf-8") + "# changed\n")

    with pytest.raises(MVPRewriteError) as error:
        verify_addition_candidate(_config(tmp_path), prepared, tmp_path / "artifacts")

    assert error.value.code == "stale_candidate"


def test_structured_exclusions_explain_near_miss_and_multiple_driver(
    tmp_path: Path,
) -> None:
    near_miss = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [6:0] y);"
        " assign y=a+b+c; endmodule\n",
        name="near.sv",
    )
    analysis = scan_addition_analysis(near_miss)
    assert analysis["findings"] == []
    assert analysis["exclusions"][0]["reason_code"] == "width_or_truncation_risk"
    assert analysis["exclusions"][0]["source"]["file"].endswith("near.sv")

    multiple = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c; assign y=a+b+d; endmodule\n",
        name="multiple.sv",
    )
    analysis = scan_addition_analysis(multiple)
    assert analysis["findings"] == []
    assert {item["reason_code"] for item in analysis["exclusions"]} == {
        "multiple_driver"
    }


def test_compile_lint_failure_is_inconclusive_not_formal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);"
        " assign y=a+b+c+d; endmodule\n",
    )
    finding = scan_addition_sites(design)[0]
    prepared = prepare_addition_candidate(
        design, finding["finding_id"], tmp_path / "artifacts"
    )
    monkeypatch.setattr(
        mvp_rewriter,
        "_verilator_lint",
        lambda *args, **kwargs: {
            "status": "failed",
            "returncode": 0,
            "command": [],
            "log_path": "lint.log",
            "blocking_warnings": ["%Warning-WIDTH"],
            "detail": "%Warning-WIDTH",
        },
    )
    prove_called = False

    def unexpected_prove(*args, **kwargs):
        nonlocal prove_called
        prove_called = True
        raise AssertionError("formal backend must not run after blocking lint")

    monkeypatch.setattr(mvp_rewriter, "_prove", unexpected_prove)

    result = verify_addition_candidate(
        _config(tmp_path), prepared, tmp_path / "artifacts"
    )

    assert result["status"] == "formal_inconclusive"
    assert result["safe"] is False
    assert result["formal"]["status"] == "inconclusive"
    assert prove_called is False


def test_zero_exit_non_verilator_is_inconclusive(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
    )
    true_path = shutil.which("true")
    assert true_path is not None
    config = _config(tmp_path)
    config = replace(
        config,
        tools=replace(config.tools, verilator=true_path),
    )

    result = mvp_rewriter._verilator_lint(
        config, design, tmp_path / "lint", "baseline"
    )

    assert result["status"] == "inconclusive"
    assert result["identity"] is None
    assert result["identity_error"]["code"] == "verilator_identity_mismatch"


def test_yosys_script_arguments_reject_newline_paths(tmp_path: Path) -> None:
    design = _write_design(
        tmp_path,
        "module top(input logic [7:0] a,b,c, output logic [7:0] y);"
        " assign y=a+b+c; endmodule\n",
    )
    unsafe = DesignInputV2(
        schema_version=design.schema_version,
        top=design.top,
        files=design.files,
        include_dirs=("unsafe\nread_verilog attacker.sv",),
        defines=design.defines,
        filelists=design.filelists,
        design_hash=design.design_hash,
    )

    with pytest.raises(MVPRewriteError) as error:
        mvp_rewriter._yosys_read(unsafe)

    assert error.value.code == "unsafe_compile_context"
