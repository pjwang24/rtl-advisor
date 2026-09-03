from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from rtl_advisor.mvp_schema import stable_hash


FAMILY_M01_REPRODUCIBILITY_DOCUMENT_TYPE = (
    "rtl-advisor.arbiter-family-m0-m1-reproducibility"
)


class FamilyMeasureReproducibilityError(RuntimeError):
    """Raised when repeat evidence cannot be compared safely."""


def _key(item: Mapping[str, Any]) -> tuple[str, str]:
    return str(item["reference_id"]), str(item["configuration_id"])


def _normalized_result(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reference_id": str(item["reference_id"]),
        "configuration_id": str(item["configuration_id"]),
        "candidate_id": str(item["candidate_id"]),
        "formal_semantic_hash": str(item["formal_semantic_hash"]),
        "decision": str(item["decision"]),
        "profiles": item["profiles"],
    }


def _normalized_failure(item: Mapping[str, Any]) -> dict[str, Any]:
    error = item.get("error") or {}
    return {
        "reference_id": str(item["reference_id"]),
        "configuration_id": str(item["configuration_id"]),
        "candidate_id": str(item["candidate_id"]),
        "error_code": str(error.get("code", "measurement_failed")),
    }


def _merge_results(
    evidence: Mapping[str, Any],
    supplemental: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    results = {
        _key(item): _normalized_result(item)
        for item in evidence.get("results") or []
    }
    failures = {
        _key(item): _normalized_failure(item)
        for item in evidence.get("failures") or []
    }
    preppa_hash = str(evidence.get("preppa_semantic_hash", ""))
    repeat_id = str(evidence.get("repeat_id", ""))
    for document in supplemental:
        if str(document.get("preppa_semantic_hash", "")) != preppa_hash:
            raise FamilyMeasureReproducibilityError(
                "supplemental evidence uses a different pre-PPA artifact"
            )
        if str(document.get("repeat_id", "")) != repeat_id:
            raise FamilyMeasureReproducibilityError(
                "supplemental evidence uses a different repeat ID"
            )
        for item in document.get("results") or []:
            key = _key(item)
            normalized = _normalized_result(item)
            existing = results.get(key)
            if existing is not None and existing != normalized:
                raise FamilyMeasureReproducibilityError(
                    f"supplemental result conflicts for {key[0]}/{key[1]}"
                )
            results[key] = normalized
            failures.pop(key, None)
        for item in document.get("failures") or []:
            key = _key(item)
            if key in results:
                continue
            normalized = _normalized_failure(item)
            existing = failures.get(key)
            if existing is not None and existing != normalized:
                raise FamilyMeasureReproducibilityError(
                    f"supplemental failure conflicts for {key[0]}/{key[1]}"
                )
            failures[key] = normalized
    if set(results) & set(failures):
        raise FamilyMeasureReproducibilityError(
            "a configuration cannot be both measured and failed"
        )
    return results, failures


def merge_family_m01_evidence(
    primary: Mapping[str, Any],
    supplemental: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Publish a canonical view while preserving immutable retry provenance."""

    full_results = {
        _key(item): deepcopy(dict(item))
        for item in primary.get("results") or []
    }
    full_failures = {
        _key(item): deepcopy(dict(item))
        for item in primary.get("failures") or []
    }
    supplemental_hashes: list[str] = []
    for document in supplemental:
        if document.get("study_id") != primary.get("study_id"):
            raise FamilyMeasureReproducibilityError(
                "supplemental evidence uses a different study ID"
            )
        if document.get("family_id") != primary.get("family_id"):
            raise FamilyMeasureReproducibilityError(
                "supplemental evidence uses a different family ID"
            )
        if document.get("repeat_id") != primary.get("repeat_id"):
            raise FamilyMeasureReproducibilityError(
                "supplemental evidence uses a different repeat ID"
            )
        if (
            document.get("preppa_semantic_hash")
            != primary.get("preppa_semantic_hash")
        ):
            raise FamilyMeasureReproducibilityError(
                "supplemental evidence uses a different pre-PPA artifact"
            )
        supplemental_hashes.append(str(document.get("semantic_hash", "")))
        for item in document.get("results") or []:
            key = _key(item)
            existing = full_results.get(key)
            if (
                existing is not None
                and _normalized_result(existing) != _normalized_result(item)
            ):
                raise FamilyMeasureReproducibilityError(
                    f"supplemental result conflicts for {key[0]}/{key[1]}"
                )
            if existing is None:
                full_results[key] = deepcopy(dict(item))
            full_failures.pop(key, None)
        for item in document.get("failures") or []:
            key = _key(item)
            if key in full_results:
                continue
            existing = full_failures.get(key)
            if (
                existing is not None
                and _normalized_failure(existing) != _normalized_failure(item)
            ):
                raise FamilyMeasureReproducibilityError(
                    f"supplemental failure conflicts for {key[0]}/{key[1]}"
                )
            if existing is None:
                full_failures[key] = deepcopy(dict(item))

    records = [full_results[key] for key in sorted(full_results)]
    failures = [full_failures[key] for key in sorted(full_failures)]
    selected_count = int(
        (primary.get("summary") or {}).get(
            "selected_configuration_count",
            len(records) + len(failures),
        )
    )
    if selected_count != len(records) + len(failures):
        raise FamilyMeasureReproducibilityError(
            "merged evidence does not cover the frozen selection"
        )
    core = {
        key: deepcopy(value)
        for key, value in primary.items()
        if key
        not in {
            "semantic_hash",
            "status",
            "summary",
            "results",
            "failures",
            "derivation",
        }
    }
    core.update(
        {
            "status": (
                "completed" if not failures else "completed_with_failures"
            ),
            "summary": {
                "selected_configuration_count": selected_count,
                "measured_configuration_count": len(records),
                "failure_count": len(failures),
                "decision_counts": {
                    decision: sum(
                        record["decision"] == decision for record in records
                    )
                    for decision in (
                        "measured_improvement",
                        "synthesis_handles",
                        "flow_dependent",
                        "regression",
                    )
                },
            },
            "results": records,
            "failures": failures,
            "derivation": {
                "primary_semantic_hash": primary.get("semantic_hash"),
                "supplemental_semantic_hashes": supplemental_hashes,
                "policy": (
                    "Hash-matched supplemental results replace only the "
                    "corresponding recorded failure; original artifacts remain "
                    "unchanged."
                ),
            },
        }
    )
    return {**core, "semantic_hash": stable_hash(core)}


def compare_family_m01_repeats(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    first_supplemental: Iterable[Mapping[str, Any]] = (),
    second_supplemental: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Compare immutable family M0/M1 evidence after approved retries.

    M0/M1 evidence reproduces only when its complete normalized result is
    byte-for-byte equivalent after canonical JSON serialization. Repeated
    measurement limitations count separately and must recur with the same
    candidate and error code.
    """

    if first.get("study_id") != second.get("study_id"):
        raise FamilyMeasureReproducibilityError("repeat study IDs differ")
    if first.get("family_id") != second.get("family_id"):
        raise FamilyMeasureReproducibilityError("repeat family IDs differ")
    if first.get("preppa_semantic_hash") != second.get("preppa_semantic_hash"):
        raise FamilyMeasureReproducibilityError(
            "repeat pre-PPA semantic hashes differ"
        )
    first_results, first_failures = _merge_results(first, first_supplemental)
    second_results, second_failures = _merge_results(
        second,
        second_supplemental,
    )
    keys = sorted(
        set(first_results)
        | set(second_results)
        | set(first_failures)
        | set(second_failures)
    )
    checks: list[dict[str, Any]] = []
    exact_results = 0
    repeated_limitations = 0
    for key in keys:
        left_result = first_results.get(key)
        right_result = second_results.get(key)
        left_failure = first_failures.get(key)
        right_failure = second_failures.get(key)
        if left_result is not None and right_result is not None:
            exact = left_result == right_result
            if exact:
                exact_results += 1
            checks.append(
                {
                    "reference_id": key[0],
                    "configuration_id": key[1],
                    "status": "exact_match" if exact else "result_mismatch",
                    "repeat_hashes": {
                        "repeat-1": stable_hash(left_result),
                        "repeat-2": stable_hash(right_result),
                    },
                }
            )
            continue
        if left_failure is not None and right_failure is not None:
            exact = left_failure == right_failure
            if exact:
                repeated_limitations += 1
            checks.append(
                {
                    "reference_id": key[0],
                    "configuration_id": key[1],
                    "status": (
                        "repeated_measurement_limitation"
                        if exact
                        else "failure_mismatch"
                    ),
                    "repeat_hashes": {
                        "repeat-1": stable_hash(left_failure),
                        "repeat-2": stable_hash(right_failure),
                    },
                    "error_code": (
                        left_failure["error_code"]
                        if exact
                        else None
                    ),
                }
            )
            continue
        checks.append(
            {
                "reference_id": key[0],
                "configuration_id": key[1],
                "status": "missing_or_changed_outcome",
            }
        )

    measured_denominator = sum(
        key in first_results and key in second_results for key in keys
    )
    measured_rate = (
        exact_results / measured_denominator
        if measured_denominator
        else 0.0
    )
    reproduced = exact_results + repeated_limitations
    cohort_rate = reproduced / len(keys) if keys else 0.0
    mismatches = [
        item
        for item in checks
        if item["status"]
        not in {"exact_match", "repeated_measurement_limitation"}
    ]
    core = {
        "schema_version": 1,
        "document_type": FAMILY_M01_REPRODUCIBILITY_DOCUMENT_TYPE,
        "study_id": first["study_id"],
        "family_id": first["family_id"],
        "preppa_semantic_hash": first["preppa_semantic_hash"],
        "status": (
            "passed"
            if not mismatches and cohort_rate >= 0.95
            else "failed"
        ),
        "summary": {
            "cohort_configuration_count": len(keys),
            "exact_measured_result_count": exact_results,
            "repeated_measurement_limitation_count": repeated_limitations,
            "mismatch_count": len(mismatches),
            "measured_exact_match_rate": measured_rate,
            "cohort_reproducibility_rate": cohort_rate,
        },
        "checks": checks,
        "limitations": [
            (
                "Repeated measurement limitations are tracked separately from "
                "exactly reproduced M0/M1 netlists and metrics."
            ),
            (
                "This result applies only to the pinned Yosys/ABC recipes and "
                "Nangate45 Liberty data."
            ),
        ],
    }
    return {**core, "semantic_hash": stable_hash(core)}
