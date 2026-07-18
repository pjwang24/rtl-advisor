from __future__ import annotations

from dataclasses import replace
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, VariantSpec, load_manifest
from rtl_advisor.synthesis import SynthesisError, synthesize_case
from rtl_advisor.verification import lint_case, prove_equivalence


PATCH_SCHEMA_VERSION = 1
PATCH_FLOW_VERSION = "rtl-advisor-safe-patch-v1"


class PatchValidationError(RuntimeError):
    """Raised when a requested patch cannot be prepared safely."""


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _normalize_candidate_source(
    source: str,
    baseline: VariantSpec,
    candidate: VariantSpec,
) -> str:
    replacements = (
        (candidate.wrapper_top, baseline.wrapper_top),
        (candidate.kernel_top, baseline.kernel_top),
    )
    normalized = source
    for old, new in replacements:
        normalized = re.sub(rf"\b{re.escape(old)}\b", new, normalized)
    return normalized


def _patch_manifest(
    *,
    workspace: Path,
    patch_case_id: str,
    source_case: CaseManifest,
    baseline_source: str,
    patched_source: str,
) -> Path:
    baseline = source_case.baseline
    payload = {
        "schema_version": 1,
        "case_id": patch_case_id,
        "family": source_case.family,
        "width": source_case.width,
        "seed": source_case.seed,
        "baseline_id": "v0",
        "variants": [
            {
                "id": "v0",
                "role": "baseline",
                "file": "rtl/v0.sv",
                "kernel_top": baseline.kernel_top,
                "wrapper_top": baseline.wrapper_top,
                "expected_equivalent": True,
                "sha256": _sha256_text(baseline_source),
            },
            {
                "id": "p1",
                "role": "candidate",
                "file": "rtl/p1.sv",
                "kernel_top": baseline.kernel_top,
                "wrapper_top": baseline.wrapper_top,
                "expected_equivalent": True,
                "sha256": _sha256_text(patched_source),
            },
        ],
    }
    baseline_path = workspace / "rtl/v0.sv"
    patched_path = workspace / "rtl/p1.sv"
    manifest_path = workspace / "manifest.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(baseline_source, encoding="utf-8")
    patched_path.write_text(patched_source, encoding="utf-8")
    _write_json(manifest_path, payload)
    return manifest_path


def validate_candidate_patch(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    candidate_id: str = "v1",
) -> dict[str, Any]:
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    baseline = manifest.baseline
    candidate = manifest.variant(candidate_id)
    if candidate.variant_id == baseline.variant_id:
        raise PatchValidationError("the patch candidate cannot be the baseline")

    baseline_path = manifest.variant_path(baseline)
    candidate_path = manifest.variant_path(candidate)
    baseline_before = baseline_path.read_bytes()
    candidate_before = candidate_path.read_bytes()
    baseline_source = baseline_before.decode("utf-8")
    candidate_source = candidate_before.decode("utf-8")
    patched_source = _normalize_candidate_source(
        candidate_source,
        baseline,
        candidate,
    )
    patch_key = hashlib.sha256(
        (
            PATCH_FLOW_VERSION
            + baseline.sha256
            + candidate.sha256
            + _sha256_text(patched_source)
        ).encode()
    ).hexdigest()
    patch_id = patch_key[:16]
    patch_case_id = f"patch_{patch_id}"
    output_dir = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "patches"
        / f"{baseline.variant_id}_to_{candidate.variant_id}"
        / patch_id
    )
    workspace = output_dir / "workspace"
    manifest_path = _patch_manifest(
        workspace=workspace,
        patch_case_id=patch_case_id,
        source_case=manifest,
        baseline_source=baseline_source,
        patched_source=patched_source,
    )
    patch_text = "".join(
        difflib.unified_diff(
            baseline_source.splitlines(keepends=True),
            patched_source.splitlines(keepends=True),
            fromfile=f"a/{baseline.file}",
            tofile=f"b/{baseline.file}",
        )
    )
    patch_path = output_dir / "candidate.patch"
    patch_path.write_text(patch_text, encoding="utf-8")

    isolated_config = replace(
        config,
        artifacts_dir=output_dir / "validation",
    )
    patch_manifest = load_manifest(manifest_path)
    lint_results = lint_case(isolated_config, patch_manifest)
    patched_lint = next(
        result for result in lint_results if result.variant_id == "p1"
    )
    stages: dict[str, Any] = {
        "lint": {
            "status": patched_lint.status,
            "ok": patched_lint.ok,
            "log_path": patched_lint.log_path,
        },
        "equivalence": {"status": "skipped", "ok": False},
        "synthesis": {"status": "skipped", "ok": False},
    }
    comparison = None
    if patched_lint.ok:
        proof = prove_equivalence(isolated_config, patch_manifest, "p1")
        proof_ok = proof.status == "equivalent" and proof.expectation_met
        stages["equivalence"] = {
            "status": proof.status,
            "ok": proof_ok,
            "log_path": proof.log_path,
            "counterexample_path": proof.counterexample_path,
        }
        if proof_ok:
            try:
                _, synthesis_summary = synthesize_case(
                    isolated_config,
                    patch_manifest,
                )
                comparison = synthesis_summary["comparisons"][0]
                stages["synthesis"] = {
                    "status": "passed",
                    "ok": True,
                    "summary_path": str(
                        isolated_config.artifacts_dir
                        / "cases"
                        / patch_case_id
                        / "synthesis/summary.json"
                    ),
                }
            except SynthesisError as exc:
                stages["synthesis"] = {
                    "status": "error",
                    "ok": False,
                    "detail": str(exc),
                }

    originals_unchanged = (
        baseline_path.read_bytes() == baseline_before
        and candidate_path.read_bytes() == candidate_before
    )
    accepted = all(stage["ok"] for stage in stages.values()) and originals_unchanged
    result = {
        "schema_version": PATCH_SCHEMA_VERSION,
        "flow_version": PATCH_FLOW_VERSION,
        "patch_id": patch_id,
        "case_id": manifest.case_id,
        "source_variant_id": baseline.variant_id,
        "candidate_variant_id": candidate.variant_id,
        "status": "accepted" if accepted else "rejected",
        "accepted": accepted,
        "originals_unchanged": originals_unchanged,
        "source_sha256": baseline.sha256,
        "candidate_sha256": candidate.sha256,
        "patched_sha256": _sha256_text(patched_source),
        "patch_path": str(patch_path),
        "workspace_path": str(workspace),
        "validation_artifacts": str(isolated_config.artifacts_dir),
        "stages": stages,
        "synthesis_comparison": comparison,
    }
    result_path = output_dir / "validation.json"
    result["result_path"] = str(result_path)
    _write_json(result_path, result)
    return result
