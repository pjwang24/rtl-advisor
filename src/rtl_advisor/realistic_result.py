from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from rtl_advisor.mvp_schema import write_hashed_json
from rtl_advisor.realistic_study import STUDY_ID


RESULT_DOCUMENT_TYPE = "rtl-advisor.realistic-evidence-result"


class RealisticResultError(RuntimeError):
    """Raised when the realistic study records cannot form one result."""


def _by_configuration(
    record: Mapping[str, Any],
    *,
    path: tuple[str, ...],
) -> dict[str, Mapping[str, Any]]:
    value: Any = record
    for key in path:
        value = value.get(key) if isinstance(value, Mapping) else None
    if not isinstance(value, list):
        raise RealisticResultError(
            f"record lacks configuration list at {'.'.join(path)}"
        )
    result: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping) or not item.get("configuration_id"):
            raise RealisticResultError("configuration record is invalid")
        configuration_id = str(item["configuration_id"])
        if configuration_id in result:
            raise RealisticResultError(
                f"duplicate configuration {configuration_id}"
            )
        result[configuration_id] = item
    return result


def _conclusion(
    configuration: Mapping[str, Any],
    second: Mapping[str, Any],
    m2: Mapping[str, Any] | None,
    second_m2: Mapping[str, Any] | None,
    m2_reproduction: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    formal = configuration.get("formal") or {}
    second_formal = second.get("formal") or {}
    if not (
        formal.get("status") == "formal_passed"
        and formal.get("safe") is True
        and second_formal.get("status") == "formal_passed"
        and second_formal.get("safe") is True
    ):
        return "formal_not_proven", None
    decision = str((configuration.get("measurement") or {}).get("decision"))
    second_decision = str((second.get("measurement") or {}).get("decision"))
    if decision != second_decision:
        return "measurement_not_reproduced", None
    if decision == "regression":
        return "rejected_regression", None
    if decision == "synthesis_handles":
        return "synthesis_handles", None
    if decision == "flow_dependent":
        return "yosys_recipe_disagreement", None
    if decision != "measured_improvement":
        return "evidence_incomplete", None
    if m2 is None or second_m2 is None or m2_reproduction is None:
        return (
            "repeatable_yosys_abc_improvement",
            "repeatable Yosys/ABC improvement for this configuration",
        )
    m2_repeat_ok = bool(
        m2_reproduction.get("available")
        and m2_reproduction.get("repeat_direction_agreement")
        and m2_reproduction.get("within_2_percent")
    )
    cross_flow_ok = bool(
        m2.get("direction_agreement")
        and second_m2.get("direction_agreement")
        and m2.get("classification") == "improved"
        and second_m2.get("classification") == "improved"
    )
    if m2_repeat_ok and cross_flow_ok:
        return (
            "openroad_confirmed_improvement",
            "confirmed by the pinned OpenROAD cross-check",
        )
    if m2_repeat_ok:
        return "cross_flow_disagreement", None
    return "m2_not_reproduced", None


def publish_realistic_result(
    first_study: Mapping[str, Any],
    second_study: Mapping[str, Any],
    study_reproducibility: Mapping[str, Any],
    first_m2: Mapping[str, Any],
    second_m2: Mapping[str, Any],
    m2_reproducibility: Mapping[str, Any],
    *,
    output_path: str | Path,
) -> dict[str, Any]:
    """Publish the complete frozen matrix without selecting favorable points."""

    for record in (
        first_study,
        second_study,
        study_reproducibility,
        first_m2,
        second_m2,
        m2_reproducibility,
    ):
        if record.get("study_id") != STUDY_ID:
            raise RealisticResultError("record belongs to a different study")
    first = _by_configuration(
        first_study,
        path=("normalized", "configurations"),
    )
    second = _by_configuration(
        second_study,
        path=("normalized", "configurations"),
    )
    first_physical = _by_configuration(
        first_m2,
        path=("configurations",),
    )
    second_physical = _by_configuration(
        second_m2,
        path=("configurations",),
    )
    physical_reproduction = _by_configuration(
        m2_reproducibility,
        path=("configurations",),
    )
    if set(first) != set(second):
        raise RealisticResultError("study repeats use different matrices")

    configurations: list[dict[str, Any]] = []
    for configuration_id in sorted(first):
        item = first[configuration_id]
        other = second[configuration_id]
        m2 = first_physical.get(configuration_id)
        other_m2 = second_physical.get(configuration_id)
        m2_repeat = physical_reproduction.get(configuration_id)
        conclusion, permitted_claim = _conclusion(
            item,
            other,
            m2,
            other_m2,
            m2_repeat,
        )
        configurations.append(
            {
                "configuration_id": configuration_id,
                "parameters": item.get("parameters"),
                "candidate_origin": item.get("candidate_origin"),
                "reference_id": item.get("reference_id"),
                "transformation": item.get("transformation"),
                "proof_contract_hash": item.get("proof_contract_hash"),
                "formal": item.get("formal"),
                "m0_m1": item.get("measurement"),
                "m2": (
                    {
                        "repeat_1": {
                            "classification": m2.get("classification"),
                            "comparison": m2.get("comparison"),
                            "direction_agreement": m2.get(
                                "direction_agreement"
                            ),
                        },
                        "repeat_2": {
                            "classification": other_m2.get("classification"),
                            "comparison": other_m2.get("comparison"),
                            "direction_agreement": other_m2.get(
                                "direction_agreement"
                            ),
                        },
                        "reproducibility": m2_repeat,
                    }
                    if m2 is not None
                    and other_m2 is not None
                    and m2_repeat is not None
                    else None
                ),
                "conclusion": conclusion,
                "permitted_claim": permitted_claim,
                "recommend_candidate": conclusion
                == "openroad_confirmed_improvement",
            }
        )

    study_reproduced = study_reproducibility.get("status") == "passed"
    m2_reproduced = m2_reproducibility.get("status") == "passed"
    core = {
        "schema_version": 1,
        "document_type": RESULT_DOCUMENT_TYPE,
        "study_id": STUDY_ID,
        "status": (
            "completed"
            if study_reproduced
            and m2_reproduced
            and first_m2.get("status") == "completed"
            and second_m2.get("status") == "completed"
            else "incomplete"
        ),
        "reference_id": (
            (first_study.get("normalized") or {}).get("reference_id")
        ),
        "reference_manifest_hash": (
            (first_study.get("normalized") or {}).get(
                "reference_manifest_hash"
            )
        ),
        "transformation_registry_hash": (
            (first_study.get("normalized") or {}).get(
                "transformation_registry_hash"
            )
        ),
        "controls": (first_study.get("normalized") or {}).get("controls"),
        "reproducibility": {
            "m0_m1_p2": study_reproducibility,
            "m2": m2_reproducibility,
        },
        "configurations": configurations,
        "claim_scope": [
            "Formal safety applies only under the recorded P2 contract.",
            "M0/M1 claims apply only to the pinned Yosys/ABC recipes and library.",
            "M2 claims apply only to the pinned Nangate45 OpenROAD cross-check.",
            "No result is a Genus, target-flow, production-PPA, or general unseen-RTL claim.",
        ],
        "family_gate": {
            "passed": False,
            "reason": (
                "The study contains one reference/alternative pair from one "
                "upstream lineage; the ten-pair, three-lineage family gate "
                "remains open."
            ),
        },
    }
    return write_hashed_json(Path(output_path), core, exclusive=True)
