#!/usr/bin/env python3
"""Create immutable M0/M1 reproducibility evidence for the arbiter family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_advisor.family_measure_reproducibility import (  # noqa: E402
    FAMILY_M01_REPRODUCIBILITY_DOCUMENT_TYPE,
    compare_family_m01_repeats,
)
from rtl_advisor.mvp_schema import read_hashed_json, write_hashed_json  # noqa: E402


EVIDENCE_DOCUMENT_TYPE = "rtl-advisor.arbiter-family-m0-m1-evidence"


def _read(path: Path) -> dict[str, object]:
    return read_hashed_json(
        path,
        document_type=EVIDENCE_DOCUMENT_TYPE,
        schema_version=1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat-1", type=Path, required=True)
    parser.add_argument("--repeat-2", type=Path, required=True)
    parser.add_argument("--repeat-1-supplemental", type=Path, action="append")
    parser.add_argument("--repeat-2-supplemental", type=Path, action="append")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    result = compare_family_m01_repeats(
        _read(arguments.repeat_1),
        _read(arguments.repeat_2),
        first_supplemental=[
            _read(path)
            for path in arguments.repeat_1_supplemental or []
        ],
        second_supplemental=[
            _read(path)
            for path in arguments.repeat_2_supplemental or []
        ],
    )
    output = arguments.output.expanduser().resolve()
    core = {
        key: value
        for key, value in result.items()
        if key != "semantic_hash"
    }
    if output.is_file():
        stored = read_hashed_json(
            output,
            document_type=FAMILY_M01_REPRODUCIBILITY_DOCUMENT_TYPE,
            schema_version=1,
        )
        if stored != result:
            raise RuntimeError(f"append-only comparison conflict: {output}")
    else:
        stored = write_hashed_json(output, core, exclusive=True)
    print(json.dumps(stored["summary"], indent=2, sort_keys=True))
    return 0 if stored["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
