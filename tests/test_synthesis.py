import json
from pathlib import Path

from rtl_advisor.synthesis import _parse_abc_metrics, _parse_stat_metrics


def test_parse_abc_timing_summary() -> None:
    log = (
        'ABC: WireLoad = "none"  Gates = 213 ( 6.6 %)  Cap = 4.1 ff '
        "Area = 262.28 (85.4 %) Delay = 619.73 ps (18.8 %)"
    )

    gates, area, delay = _parse_abc_metrics(log)

    assert gates == 213
    assert area == 262.28
    assert delay == 619.73


def test_parse_yosys_stat_metrics_excludes_internal_cells(tmp_path: Path) -> None:
    stat_path = tmp_path / "stat.json"
    stat_path.write_text(
        json.dumps(
            {
                "modules": {
                    "\\test_top": {
                        "num_cells": 5,
                        "area": 12.5,
                        "sequential_area": 4.5,
                        "num_cells_by_type": {
                            "$scopeinfo": 1,
                            "DFF_X1": 1,
                            "NAND2_X1": 3,
                        },
                    }
                },
                "design": {
                    "num_cells": 5,
                    "area": 12.5,
                    "sequential_area": 4.5,
                    "num_cells_by_type": {
                        "$scopeinfo": 1,
                        "DFF_X1": 1,
                        "NAND2_X1": 3,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    area, sequential, cells, raw_cells, breakdown = _parse_stat_metrics(
        stat_path,
        "test_top",
    )

    assert area == 12.5
    assert sequential == 4.5
    assert cells == 4
    assert raw_cells == 5
    assert breakdown["NAND2_X1"] == 3
