from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import (
    DEFAULT_HELDOUT_SEED,
    DEFAULT_SEED,
    DEFAULT_WIDTH,
    FAMILY_REGISTRY,
    GENERATOR_VERSION,
    SUPPORTED_SUITES,
    default_case_id,
    generate_case,
    load_manifest,
)
from rtl_advisor.synthesis import SynthesisError, synthesize_case
from rtl_advisor.verification import lint_case, prove_case_candidates


SUITE_SCHEMA_VERSION = 1
SUITE_FLOW_VERSION = "rtl-advisor-suite-v1"
DEVELOPMENT_WIDTHS = (DEFAULT_WIDTH, 8, 12, 20)
HELDOUT_WIDTHS = (9, 13, 17, 21)


class SuiteError(RuntimeError):
    """Raised when a generated suite cannot be built or validated."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def suite_case_specs(suite: str) -> tuple[dict[str, Any], ...]:
    if suite not in SUPPORTED_SUITES:
        raise SuiteError(f"unsupported corpus suite: {suite}")
    specs = []
    definitions = tuple(FAMILY_REGISTRY.values())
    for family_index, definition in enumerate(definitions):
        count = 4 if suite == "heldout" or family_index < 5 else 3
        widths = HELDOUT_WIDTHS if suite == "heldout" else DEVELOPMENT_WIDTHS
        for case_index in range(count):
            width = widths[case_index]
            if suite == "development":
                seed = (
                    DEFAULT_SEED
                    if case_index == 0
                    else DEFAULT_SEED + family_index * 100 + case_index
                )
                case_id = (
                    definition.default_development_case_id
                    if case_index == 0
                    else f"dev_{definition.short_code}_{case_index + 1:04d}"
                )
            else:
                seed = DEFAULT_HELDOUT_SEED + family_index * 100 + case_index
                case_id = default_case_id(
                    definition.family_id,
                    suite,
                    width=width,
                    seed=seed,
                )
            specs.append(
                {
                    "index": len(specs),
                    "family": definition.family_id,
                    "case_id": case_id,
                    "width": width,
                    "seed": seed,
                }
            )
    expected = 32 if suite == "development" else 36
    if len(specs) != expected:
        raise SuiteError(
            f"internal suite allocation error: expected {expected}, got {len(specs)}"
        )
    return tuple(specs)


def generate_suite(
    corpus_dir: Path,
    suite: str,
    *,
    force: bool = False,
) -> Path:
    suite_root = corpus_dir / suite
    cases = []
    for spec in suite_case_specs(suite):
        case_root = suite_root / spec["case_id"]
        manifest_path = generate_case(
            case_root,
            family=spec["family"],
            suite=suite,
            case_id=spec["case_id"],
            width=spec["width"],
            seed=spec["seed"],
            force=force,
        )
        manifest = load_manifest(manifest_path)
        cases.append(
            {
                **spec,
                "manifest": str(manifest_path.relative_to(suite_root)),
                "variant_count": len(manifest.variants),
            }
        )
    payload = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "flow_version": SUITE_FLOW_VERSION,
        "generator_version": GENERATOR_VERSION,
        "suite": suite,
        "case_count": len(cases),
        "cases": cases,
    }
    manifest_path = suite_root / "suite.json"
    _write_json(manifest_path, payload)
    return manifest_path


def load_suite_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise SuiteError(f"suite manifest not found: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"could not read suite manifest {manifest_path}: {exc}") from exc
    if payload.get("schema_version") != SUITE_SCHEMA_VERSION:
        raise SuiteError("unsupported suite manifest schema")
    if payload.get("suite") not in SUPPORTED_SUITES:
        raise SuiteError("invalid suite name in manifest")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != payload.get("case_count"):
        raise SuiteError("suite case list does not match case_count")
    payload["manifest_path"] = str(manifest_path)
    return payload


def validate_suite(
    config: ProjectConfig,
    suite_manifest: str | Path,
) -> dict[str, Any]:
    suite_payload = load_suite_manifest(suite_manifest)
    manifest_path = Path(suite_payload["manifest_path"])
    suite_root = manifest_path.parent
    case_results = []
    for case in suite_payload["cases"]:
        case_manifest_path = (suite_root / case["manifest"]).resolve()
        record: dict[str, Any] = {
            "index": case["index"],
            "case_id": case["case_id"],
            "family": case["family"],
            "manifest": str(case_manifest_path),
            "lint": "not_run",
            "equivalence": "not_run",
            "synthesis": "not_run",
            "status": "failed",
        }
        try:
            lint_results = lint_case(config, case_manifest_path)
            record["lint"] = (
                "passed" if all(result.ok for result in lint_results) else "failed"
            )
            if record["lint"] != "passed":
                case_results.append(record)
                continue
            proof_results = prove_case_candidates(config, case_manifest_path)
            record["equivalence"] = (
                "passed"
                if all(result.expectation_met for result in proof_results)
                else "failed"
            )
            if record["equivalence"] != "passed":
                case_results.append(record)
                continue
            synthesis_results, summary = synthesize_case(config, case_manifest_path)
            record["synthesis"] = (
                "passed"
                if all(result.status == "passed" for result in synthesis_results)
                else "failed"
            )
            record["comparison_count"] = len(summary["comparisons"])
            record["status"] = (
                "passed" if record["synthesis"] == "passed" else "failed"
            )
        except (OSError, SynthesisError, RuntimeError) as exc:
            record["error"] = str(exc)
        case_results.append(record)

    passed = sum(record["status"] == "passed" for record in case_results)
    result = {
        "schema_version": SUITE_SCHEMA_VERSION,
        "flow_version": SUITE_FLOW_VERSION,
        "suite": suite_payload["suite"],
        "case_count": len(case_results),
        "passed_count": passed,
        "failed_count": len(case_results) - passed,
        "status": "passed" if passed == len(case_results) else "failed",
        "cases": case_results,
    }
    output_path = (
        config.artifacts_dir
        / "suites"
        / suite_payload["suite"]
        / "validation.json"
    )
    result["result_path"] = str(output_path)
    _write_json(output_path, result)
    return result
