#!/usr/bin/env python3
"""Merge hash-matched audited retries into a canonical M0/M1 view."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_advisor.family_measure_reproducibility import (  # noqa: E402
    merge_family_m01_evidence,
)
from rtl_advisor.mvp_schema import read_hashed_json, write_hashed_json  # noqa: E402


DOCUMENT_TYPE = "rtl-advisor.arbiter-family-m0-m1-evidence"


def _read(path: Path) -> dict[str, object]:
    return read_hashed_json(
        path,
        document_type=DOCUMENT_TYPE,
        schema_version=1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--supplemental", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    result = merge_family_m01_evidence(
        _read(arguments.primary),
        [_read(path) for path in arguments.supplemental],
    )
    output = arguments.output.resolve()
    core = {
        key: value
        for key, value in result.items()
        if key != "semantic_hash"
    }
    if output.is_file():
        stored = _read(output)
        if stored != result:
            raise RuntimeError(f"append-only merge conflict: {output}")
    else:
        stored = write_hashed_json(output, core, exclusive=True)
    print(json.dumps(stored["summary"], indent=2, sort_keys=True))
    return 0 if stored["status"] == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
