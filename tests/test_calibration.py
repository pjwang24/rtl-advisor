from __future__ import annotations

from rtl_advisor.advisor_v2 import FEATURE_ORDER, RESOURCE_SHARING_FAMILY
from rtl_advisor.calibration import registered_topology_context, train_gate_from_rows


def _rows() -> list[dict]:
    rows = []
    for case_index in range(10):
        for template_index, template_id in enumerate(("v1", "v2", "v3"), start=1):
            features = {feature: float(case_index + 1) for feature in FEATURE_ORDER}
            features["template_code"] = float(template_index)
            rows.append(
                {
                    "case_id": f"case-{case_index}",
                    "topology_signature": f"topology-{case_index}",
                    "family": RESOURCE_SHARING_FAMILY,
                    "transformation_id": "share_arithmetic_by_muxing_inputs",
                    "template_id": template_id,
                    "features": features,
                    "targets": {
                        "delay": float(case_index + template_index),
                        "area": float(template_index),
                        "cell_count": float(template_index * 2),
                    },
                }
            )
    return rows


def test_gate_training_is_deterministic_and_exports_intervals() -> None:
    first = train_gate_from_rows(_rows(), training_suite_hash="suite-hash")
    second = train_gate_from_rows(_rows(), training_suite_hash="suite-hash")

    assert first == second
    assert len(first["model_hash"]) == 64
    family_intervals = first["intervals"][RESOURCE_SHARING_FAMILY]
    assert set(family_intervals) == {
        "share_arithmetic_by_muxing_inputs:v1",
        "share_arithmetic_by_muxing_inputs:v2",
        "share_arithmetic_by_muxing_inputs:v3",
    }
    assert set(first["estimators"][RESOURCE_SHARING_FAMILY]) == {
        "delay",
        "area",
        "cell_count",
    }


def test_registered_topology_context_marks_training_only_boundary() -> None:
    finding = registered_topology_context(
        "variable_shift",
        {
            "direction": "left",
            "width": 16,
            "amount_excess": 0,
            "guarded": False,
            "signed": False,
        },
    )

    assert finding["transformation_id"] == "bound_variable_shift"
    assert finding["training_context_only"] is True
    assert finding["evidence"]["excess_amount_bits"] == 0
    assert finding["source"] == {"locations": []}
