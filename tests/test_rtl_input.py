from __future__ import annotations

from pathlib import Path

import pytest

from rtl_advisor.rtl_input import RTLInputError, normalize_design_input


def test_normalize_design_input_supports_nested_filelists(tmp_path: Path) -> None:
    include = tmp_path / "include"
    include.mkdir()
    first = tmp_path / "first.sv"
    second = tmp_path / "second.sv"
    first.write_text("module first; endmodule\n", encoding="utf-8")
    second.write_text("module top; endmodule\n", encoding="utf-8")
    nested = tmp_path / "nested.f"
    nested.write_text("second.sv\n", encoding="utf-8")
    filelist = tmp_path / "sources.f"
    filelist.write_text(
        "+incdir+include +define+WIDTH=8 first.sv -f nested.f\n",
        encoding="utf-8",
    )

    design = normalize_design_input(
        top="top",
        filelist=filelist,
        base=tmp_path,
    )

    assert [Path(source.path).name for source in design.files] == [
        "first.sv",
        "second.sv",
    ]
    assert design.include_dirs == (str(include.resolve()),)
    assert design.defines == ("WIDTH=8",)
    assert len(design.filelists) == 2
    assert len(design.design_hash) == 64


def test_normalize_design_input_rejects_unknown_filelist_option(
    tmp_path: Path,
) -> None:
    filelist = tmp_path / "sources.f"
    filelist.write_text("-timescale=1ns/1ps\n", encoding="utf-8")

    with pytest.raises(RTLInputError, match="unsupported filelist option"):
        normalize_design_input(top="top", filelist=filelist, base=tmp_path)


def test_design_hash_changes_with_source_content(tmp_path: Path) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    first = normalize_design_input(top="top", files=(source,), base=tmp_path)
    source.write_text("module top; wire x; endmodule\n", encoding="utf-8")
    second = normalize_design_input(top="top", files=(source,), base=tmp_path)

    assert first.design_hash != second.design_hash
