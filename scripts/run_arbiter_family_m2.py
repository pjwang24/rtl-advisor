#!/usr/bin/env python3
"""Prepare, run, and compare frozen arbiter-family OpenROAD samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_advisor.config import load_config  # noqa: E402
from rtl_advisor.family_openroad import (  # noqa: E402
    FAMILY_M2_DOCUMENT_TYPE,
    FAMILY_M2_REPRODUCIBILITY_DOCUMENT_TYPE,
    FAMILY_M2_SOURCES_DOCUMENT_TYPE,
    M01_DOCUMENT_TYPE,
    PREPPA_DOCUMENT_TYPE,
    compare_family_m2_repeats,
    collect_family_m2_runs,
    create_family_m2_lock,
    family_m2_run_commands,
    prepare_family_m2_sources,
    run_family_m2,
)
from rtl_advisor.family_study import read_family_study  # noqa: E402
from rtl_advisor.mvp_schema import read_hashed_json, write_hashed_json  # noqa: E402


def _source(path: Path) -> dict[str, object]:
    return read_hashed_json(
        path,
        document_type=FAMILY_M2_SOURCES_DOCUMENT_TYPE,
        schema_version=1,
    )


def _m2(path: Path) -> dict[str, object]:
    return read_hashed_json(
        path,
        document_type=FAMILY_M2_DOCUMENT_TYPE,
        schema_version=1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--study", type=Path, required=True)
    prepare.add_argument("--preppa", type=Path, required=True)
    prepare.add_argument("--measurements", type=Path, required=True)
    prepare.add_argument("--repeat-root", type=Path, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--sources", type=Path, required=True)
    run.add_argument("--repeat-root", type=Path, required=True)
    run.add_argument("--base-lock", type=Path)
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--timeout-seconds", type=int, default=7200)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--sources", type=Path, required=True)
    plan.add_argument("--repeat-root", type=Path, required=True)
    plan.add_argument("--base-lock", type=Path)
    plan.add_argument("--defer-runtime-image-validation", action="store_true")

    commands = subparsers.add_parser("commands")
    commands.add_argument("--repeat-root", type=Path, required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--sources", type=Path, required=True)
    collect.add_argument("--repeat-root", type=Path, required=True)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--repeat-1", type=Path, required=True)
    compare.add_argument("--repeat-2", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)

    arguments = parser.parse_args()
    config = load_config(ROOT / "rtl-advisor.toml")
    if arguments.operation == "prepare":
        result = prepare_family_m2_sources(
            config,
            read_family_study(arguments.study),
            read_hashed_json(
                arguments.preppa,
                document_type=PREPPA_DOCUMENT_TYPE,
                schema_version=1,
            ),
            read_hashed_json(
                arguments.measurements,
                document_type=M01_DOCUMENT_TYPE,
                schema_version=1,
            ),
            repeat_root=arguments.repeat_root.resolve(),
        )
    elif arguments.operation == "run":
        result = run_family_m2(
            config,
            _source(arguments.sources),
            repeat_root=arguments.repeat_root.resolve(),
            base_lock_path=(
                arguments.base_lock.resolve()
                if arguments.base_lock
                else None
            ),
            workers=arguments.workers,
            timeout_seconds=arguments.timeout_seconds,
        )
    elif arguments.operation == "plan":
        path = create_family_m2_lock(
            config,
            _source(arguments.sources),
            repeat_root=arguments.repeat_root.resolve(),
            base_lock_path=(
                arguments.base_lock.resolve()
                if arguments.base_lock
                else None
            ),
            defer_runtime_image_validation=(
                arguments.defer_runtime_image_validation
            ),
        )
        result = {
            "status": "planned",
            "lock_path": str(path),
            "commands": family_m2_run_commands(
                config,
                repeat_root=arguments.repeat_root.resolve(),
            ),
        }
    elif arguments.operation == "commands":
        result = {
            "status": "planned",
            "commands": family_m2_run_commands(
                config,
                repeat_root=arguments.repeat_root.resolve(),
            ),
        }
    elif arguments.operation == "collect":
        collect_family_m2_runs(
            config,
            repeat_root=arguments.repeat_root.resolve(),
        )
        result = run_family_m2(
            config,
            _source(arguments.sources),
            repeat_root=arguments.repeat_root.resolve(),
        )
    else:
        result = compare_family_m2_repeats(
            _m2(arguments.repeat_1),
            _m2(arguments.repeat_2),
        )
        output = arguments.output.resolve()
        core = {
            key: value
            for key, value in result.items()
            if key != "semantic_hash"
        }
        if output.is_file():
            stored = read_hashed_json(
                output,
                document_type=FAMILY_M2_REPRODUCIBILITY_DOCUMENT_TYPE,
                schema_version=1,
            )
            if stored != result:
                raise RuntimeError(
                    f"append-only M2 comparison conflict: {output}"
                )
            result = stored
        else:
            result = write_hashed_json(output, core, exclusive=True)
    if arguments.operation == "commands":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        summary = {
            "status": result.get("status"),
            "repeat_id": result.get("repeat_id"),
            "configuration_count": len(result.get("configurations") or []),
            "semantic_hash": result.get("semantic_hash"),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return (
        0
        if result.get("status")
        in {None, "planned", "completed", "passed"}
        else 4
    )


if __name__ == "__main__":
    raise SystemExit(main())
