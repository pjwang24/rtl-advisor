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
from rtl_advisor.v21_corpus import V21_SPLITS, V21_SUITE_SCHEMA_VERSION
from rtl_advisor.verification import lint_case, prove_case_candidates_v2


VALIDATION_FLOW_VERSION_V21 = "rtl-advisor-v21-validation-v1"


class V21ValidationError(RuntimeError):
    """Raised when V2.1 lint, formal, or calibration synthesis fails."""


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
        raise V21ValidationError(f"invalid V2.1 suite {path}: {exc}") from exc
    if suite.get("schema_version") != V21_SUITE_SCHEMA_VERSION:
        raise V21ValidationError(f"unsupported V2.1 suite schema in {path}")
    if suite.get("split") != split:
        raise V21ValidationError(f"expected split {split}, got {suite.get('split')}")
    if not suite.get("v2_disjoint"):
        raise V21ValidationError("V2.1 suite does not attest V2 topology disjointness")
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
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != case["manifest_sha256"]:
        raise V21ValidationError(f"manifest hash mismatch: {manifest_path}")
    manifest = load_manifest(manifest_path)
    actual_rtl_hashes = {
        variant.variant_id: hashlib.sha256(
            manifest.variant_path(variant).read_bytes()
        ).hexdigest()
        for variant in manifest.variants
    }
    if actual_rtl_hashes != case.get("rtl_sha256"):
        raise V21ValidationError(f"RTL hash mismatch: {manifest.case_id}")
    lint = lint_case(config, manifest)
    proofs = prove_case_candidates_v2(config, manifest)
    lint_ok = all(result.ok for result in lint)
    proof_ok = all(result.expectation_met for result in proofs)
    positive_proofs = {
        proof.candidate_id: proof.status
        for proof in proofs
        if proof.expected_equivalent
    }
    negative_proofs = {
        proof.candidate_id: proof.status
        for proof in proofs
        if not proof.expected_equivalent
    }
    formal_contract_ok = (
        set(positive_proofs) == {"v1", "v2", "v3"}
        and all(status == "equivalent" for status in positive_proofs.values())
        and negative_proofs == {"n0": "inequivalent"}
    )
    synthesis_summary = None
    if synthesize and lint_ok and proof_ok and formal_contract_ok:
        _, synthesis_summary = synthesize_case(
            config,
            manifest,
            variant_id="all",
            force=force,
        )
    status = "passed" if (
        lint_ok
        and proof_ok
        and formal_contract_ok
        and (not synthesize or synthesis_summary is not None)
    ) else "failed"
    result = {
        "flow_version": VALIDATION_FLOW_VERSION_V21,
        "suite_hash": suite_hash,
        "case_id": manifest.case_id,
        "family": manifest.family,
        "topology_signature": case["topology_signature"],
        "status": status,
        "cached": False,
        "synthesis_requested": synthesize,
        "lint_ok": lint_ok,
        "proof_ok": proof_ok,
        "formal_contract_ok": formal_contract_ok,
        "positive_proof_statuses": positive_proofs,
        "negative_proof_statuses": negative_proofs,
        "synthesis_status": (
            synthesis_summary.get("status") if synthesis_summary is not None else None
        ),
    }
    _write_json(result_path, result)
    return result


def validate_v21_suite(
    config: ProjectConfig,
    split: str,
    *,
    synthesize: bool,
    workers: int = 4,
    force: bool = False,
) -> dict[str, Any]:
    if split not in V21_SPLITS:
        raise V21ValidationError(f"unsupported V2.1 split: {split}")
    if split == "heldout-v21" and synthesize:
        raise V21ValidationError(
            "heldout-v21 synthesis is forbidden before the benchmark lock and unseal"
        )
    if workers < 1 or workers > 16:
        raise V21ValidationError("workers must be between 1 and 16")
    suite_path = config.corpus_dir / split / "suite.json"
    suite = _load_suite(suite_path, split)
    validation_root = config.artifacts_dir / "validation/v21" / split
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
        except Exception as exc:  # preserve per-case evidence and continue
            result = {
                "flow_version": VALIDATION_FLOW_VERSION_V21,
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
                        "flow_version": VALIDATION_FLOW_VERSION_V21,
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
        "flow_version": VALIDATION_FLOW_VERSION_V21,
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
