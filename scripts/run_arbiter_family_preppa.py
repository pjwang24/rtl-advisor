#!/usr/bin/env python3
"""Run frozen arbiter candidate lint/formal qualification without PPA."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_advisor.arbiter_family_execution import (  # noqa: E402
    family_compile_spec,
    family_configurations,
    prepare_family_candidate,
    run_family_negative_controls,
    verify_family_candidate,
)
from rtl_advisor.config import load_config  # noqa: E402
from rtl_advisor.mvp_schema import (  # noqa: E402
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.transformation_executor import (  # noqa: E402
    DEFAULT_EXECUTOR_REGISTRY,
)
from rtl_advisor.transformation_registry import (  # noqa: E402
    DEFAULT_TRANSFORMATION_REGISTRY,
)


FREEZE_VERSION = "arbiter-family-preppa-v10"
FAMILY_REFERENCES = tuple(
    reference_id
    for executor in DEFAULT_EXECUTOR_REGISTRY.executors()
    for reference_id in executor.spec.reference_ids
    if reference_id != "opentitan-prim-arbiter-ppc"
)


def _reference_input(
    reference_id: str,
    artifact_root: Path,
) -> dict[str, Any]:
    executor = DEFAULT_EXECUTOR_REGISTRY.for_reference(reference_id)
    compile_spec = family_compile_spec(reference_id)
    source_root = (ROOT / "corpus" / executor.spec.source_root).resolve()
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.run.reference-input",
        "kind": "qualified_reference",
        "reference_id": reference_id,
        "manifest_path": str(
            artifact_root / "reference-inputs" / reference_id / "derived-manifest.json"
        ),
        "manifest_semantic_hash": stable_hash(
            {
                "freeze_version": FREEZE_VERSION,
                "reference_id": reference_id,
                "state": "reference_qualified",
            }
        ),
        "source_root": str(source_root),
        "source_hashes": {
            source: file_sha256(source_root / source)
            for source in compile_spec["sources"]
        },
        "compile_context": {
            "include_dirs": list(compile_spec["include_dirs"]),
            "defines": list(compile_spec["defines"]),
        },
        "provenance": {
            "project": executor.spec.upstream_project_id,
            "revision": "family-study-pinned",
            "license_expression": "open-source",
        },
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_id": executor.spec.executor_id,
        "executor_version": executor.spec.version,
        "executor_registry_hash": DEFAULT_EXECUTOR_REGISTRY.registry_hash,
    }
    path = artifact_root / "reference-inputs" / reference_id / "input.json"
    expected = {**payload, "semantic_hash": stable_hash(payload)}
    if path.is_file():
        current = read_hashed_json(path)
        if current != expected:
            raise RuntimeError(f"stale pre-PPA reference input: {reference_id}")
        return current
    return write_hashed_json(path, payload, exclusive=True)


def _run_reference(
    reference_id: str,
    configuration_id: str | None,
    *,
    controls: str,
) -> list[dict[str, Any]]:
    config = load_config(ROOT / "rtl-advisor.toml")
    artifact_root = config.artifacts_dir / FREEZE_VERSION
    executor = DEFAULT_EXECUTOR_REGISTRY.for_reference(reference_id)
    reference_input = _reference_input(reference_id, artifact_root)
    configurations = family_configurations(reference_id)
    if configuration_id is not None:
        configurations = tuple(
            item
            for item in configurations
            if item["configuration_id"] == configuration_id
        )
        if not configurations:
            raise RuntimeError(
                f"unknown configuration {configuration_id} for {reference_id}"
            )
    widest_id = family_configurations(reference_id)[-1]["configuration_id"]
    records = []
    for configuration in configurations:
        finding = {
            "finding_id": (
                f"{FREEZE_VERSION}-{reference_id}-"
                f"{configuration['configuration_id']}"
            ),
            "transformation_id": "same_cycle_arbiter_topology",
            "executor_id": executor.spec.executor_id,
            "executor_version": executor.spec.version,
            "reference_id": reference_id,
            "variant_id": f"{reference_id}-candidate",
            "configuration": configuration,
            "measurement_levels": ["M0", "M1"],
        }
        candidate = prepare_family_candidate(
            config,
            reference_input,
            finding,
            artifact_root / "candidates",
            executor.spec,
            executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        )
        verification = verify_family_candidate(
            config,
            candidate,
            artifact_root / "candidates",
            executor.spec,
        )
        run_controls = controls == "all" or (
            controls == "representative"
            and configuration["configuration_id"] == widest_id
        )
        control_result: dict[str, Any] | None = None
        if verification["safe"] is True and run_controls:
            control_result = run_family_negative_controls(
                config,
                candidate,
                executor.spec,
            )
        records.append(
            {
                "reference_id": reference_id,
                "configuration_id": configuration["configuration_id"],
                "candidate_id": candidate["candidate_id"],
                "formal_status": verification["status"],
                "safe": verification["safe"],
                "formal_semantic_hash": verification["semantic_hash"],
                "controls": (
                    None
                    if control_result is None
                    else {
                        "status": control_result["status"],
                        "relations": {
                            key: value["observed_relation"]
                            for key, value in control_result["results"].items()
                        },
                    }
                ),
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reference",
        action="append",
        choices=FAMILY_REFERENCES,
        help="family reference to qualify; defaults to every new reference",
    )
    parser.add_argument("--configuration")
    parser.add_argument(
        "--controls",
        choices=("none", "representative", "all"),
        default="representative",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write one immutable hash-linked pre-PPA evidence summary",
    )
    arguments = parser.parse_args()
    references = tuple(arguments.reference or FAMILY_REFERENCES)
    results = [
        result
        for reference_id in references
        for result in _run_reference(
            reference_id,
            arguments.configuration,
            controls=arguments.controls,
        )
    ]
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.arbiter-family-preppa-evidence",
        "freeze_version": FREEZE_VERSION,
        "ppa_inspected": False,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "executor_registry_hash": DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        "summary": {
            "reference_count": len(references),
            "configuration_count": len(results),
            "formal_pass_count": sum(
                item["safe"] is True for item in results
            ),
            "control_set_count": sum(
                item["controls"] is not None for item in results
            ),
            "control_set_pass_count": sum(
                item["controls"] is not None
                and item["controls"]["status"] == "passed"
                for item in results
            ),
        },
        "results": results,
    }
    if arguments.output is not None:
        output_path = arguments.output.expanduser().resolve()
        if output_path.is_file():
            stored = read_hashed_json(
                output_path,
                document_type=payload["document_type"],
                schema_version=1,
            )
            expected = {**payload, "semantic_hash": stable_hash(payload)}
            if stored != expected:
                raise RuntimeError(
                    f"append-only pre-PPA evidence conflict: {output_path}"
                )
        else:
            stored = write_hashed_json(
                output_path,
                payload,
                exclusive=True,
            )
        payload = stored
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(
        item["safe"] is True
        and (
            item["controls"] is None
            or item["controls"]["status"] == "passed"
        )
        for item in results
    ) else 4


if __name__ == "__main__":
    raise SystemExit(main())
