from __future__ import annotations

import json
from pathlib import Path

from rtl_advisor.candidate_v2 import _design_from_artifact


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
