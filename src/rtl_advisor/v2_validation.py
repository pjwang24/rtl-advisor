from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import threading
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import load_manifest
from rtl_advisor.synthesis import synthesize_case
from rtl_advisor.v2_corpus import V2_SPLITS, V2_SUITE_SCHEMA_VERSION
from rtl_advisor.verification import lint_case, prove_case_candidates_v2


VALIDATION_FLOW_VERSION = "rtl-advisor-v2-validation-v1"


class V2ValidationError(RuntimeError):
    """Raised when a v2 suite cannot be validated."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_suite(path: Path, split: str) -> dict[str, Any]:
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V2ValidationError(f"invalid v2 suite {path}: {exc}") from exc
    if suite.get("schema_version") != V2_SUITE_SCHEMA_VERSION:
        raise V2ValidationError(f"unsupported suite schema in {path}")
    if suite.get("split") != split:
        raise V2ValidationError(f"expected split {split}, got {suite.get('split')}")
    return suite


def _validate_case(
    config: ProjectConfig,
    suite_root: Path,
    suite_hash: str,
    case: dict[str, Any],
    result_path: Path,
    *,
    synthesize: bool,
    force: bool,
) -> dict[str, Any]:
    if result_path.is_file() and not force:
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if (
            isinstance(cached, dict)
            and cached.get("suite_hash") == suite_hash
            and cached.get("synthesis_requested") == synthesize
            and cached.get("status") == "passed"
        ):
            return {**cached, "cached": True}
    manifest_path = suite_root / str(case["manifest"])
    actual_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if actual_manifest_hash != case["manifest_sha256"]:
        raise V2ValidationError(f"manifest hash mismatch: {manifest_path}")
    manifest = load_manifest(manifest_path)
    lint = lint_case(config, manifest)
    proofs = prove_case_candidates_v2(config, manifest)
    lint_ok = all(result.ok for result in lint)
    proof_ok = all(result.expectation_met for result in proofs)
    synthesis_summary = None
    if synthesize and lint_ok and proof_ok:
        _, synthesis_summary = synthesize_case(config, manifest, variant_id="all")
    status = "passed" if lint_ok and proof_ok and (
        not synthesize or synthesis_summary is not None
    ) else "failed"
    result = {
        "flow_version": VALIDATION_FLOW_VERSION,
        "suite_hash": suite_hash,
        "case_id": manifest.case_id,
        "family": manifest.family,
        "topology_signature": case["topology_signature"],
        "status": status,
        "cached": False,
        "synthesis_requested": synthesize,
        "lint_ok": lint_ok,
        "proof_ok": proof_ok,
        "proof_statuses": {
            proof.candidate_id: proof.status for proof in proofs
        },
        "synthesis_status": (
            synthesis_summary.get("status") if synthesis_summary is not None else None
        ),
    }
    _write_json(result_path, result)
    return result


def validate_v2_suite(
    config: ProjectConfig,
    split: str,
    *,
    synthesize: bool,
    workers: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    if split not in V2_SPLITS:
        raise V2ValidationError(f"unsupported split: {split}")
    if split == "heldout-v2" and synthesize:
        raise V2ValidationError(
            "heldout-v2 synthesis is forbidden before the benchmark lock and unseal"
        )
    if workers < 1 or workers > 16:
        raise V2ValidationError("workers must be between 1 and 16")
    suite_path = config.corpus_dir / split / "suite.json"
    suite = _load_suite(suite_path, split)
    validation_root = config.artifacts_dir / "validation/v2" / split
    case_root = validation_root / "cases"
    progress_path = validation_root / "progress.json"
    cases = list(suite.get("cases") or [])
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    lock = threading.Lock()

    def worker(case: dict[str, Any]) -> dict[str, Any]:
        result_path = case_root / f"{case['case_id']}.json"
        try:
            return _validate_case(
                config,
                suite_path.parent,
                suite["suite_hash"],
                case,
                result_path,
                synthesize=synthesize,
                force=force,
            )
        except Exception as exc:  # Preserve per-case evidence and continue the suite.
            result = {
                "flow_version": VALIDATION_FLOW_VERSION,
                "suite_hash": suite["suite_hash"],
                "case_id": case["case_id"],
                "family": case["family"],
                "status": "error",
                "synthesis_requested": synthesize,
                "detail": str(exc),
            }
            _write_json(result_path, result)
            return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {executor.submit(worker, case): case for case in cases}
        for future in as_completed(pending):
            result = future.result()
            with lock:
                completed.append(result)
                if result["status"] != "passed":
                    failures.append(result)
                _write_json(
                    progress_path,
                    {
                        "flow_version": VALIDATION_FLOW_VERSION,
                        "split": split,
                        "suite_hash": suite["suite_hash"],
                        "case_count": len(cases),
                        "completed_count": len(completed),
                        "passed_count": sum(
                            item["status"] == "passed" for item in completed
                        ),
                        "failed_count": len(failures),
                        "synthesis_requested": synthesize,
                    },
                )
    result = {
        "flow_version": VALIDATION_FLOW_VERSION,
        "split": split,
        "suite_hash": suite["suite_hash"],
        "status": "passed" if not failures else "failed",
        "case_count": len(cases),
        "passed_count": len(cases) - len(failures),
        "failed_count": len(failures),
        "synthesis_requested": synthesize,
        "workers": workers,
        "failures": sorted(failures, key=lambda item: item["case_id"]),
    }
    summary_path = validation_root / "summary.json"
    result["summary_path"] = str(summary_path)
    _write_json(summary_path, result)
    return result
