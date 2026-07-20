from __future__ import annotations

from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser
from rtl_advisor.corpus_baseline import CorpusBaselineError, _synthesis_script


def test_baseline_script_uses_slang_parameters_and_fixed_profile(tmp_path: Path) -> None:
    script = _synthesis_script(
        top="demo_top",
        sources=[Path("/workspace/src/demo.sv")],
        include_dirs=[Path("/workspace/include")],
        defines=["SYNTHESIS"],
        parameters={"N": 4},
        profile="stronger",
        slang_plugin=Path("/opt/slang.so"),
        liberty=Path("/workspace/lib.lib"),
        abc=Path("/opt/yosys-abc"),
        constraints=tmp_path / "abc.constr",
        stat=tmp_path / "stat.json",
        netlist=tmp_path / "mapped.v",
    )

    assert "read_slang --top demo_top -G N=4 -D SYNTHESIS" in script
    assert "share -aggressive" in script
    assert "abc -exe" in script
    assert "write_verilog -noattr -noexpr" in script


def test_baseline_script_rejects_whitespace_in_slang_source_path(tmp_path: Path) -> None:
    with pytest.raises(CorpusBaselineError, match="without whitespace"):
        _synthesis_script(
            top="demo_top",
            sources=[Path("/workspace/bad source.sv")],
            include_dirs=[],
            defines=[],
            parameters={},
            profile="standard",
            slang_plugin=Path("/opt/slang.so"),
            liberty=Path("/workspace/lib.lib"),
            abc=Path("/opt/yosys-abc"),
            constraints=tmp_path / "abc.constr",
            stat=tmp_path / "stat.json",
            netlist=tmp_path / "mapped.v",
        )


def test_baseline_tranche_cli_is_explicitly_baseline_only() -> None:
    args = build_parser().parse_args(
        (
            "corpus",
            "baseline-tranche",
            "tranche.lock.json",
            "qualification.plan.json",
            "--json",
        )
    )

    assert args.corpus_command == "baseline-tranche"
    assert not hasattr(args, "candidate")
    assert args.json_output is True
