from __future__ import annotations

from rtl_advisor.advisor_v2 import (
    FEATURE_SCHEMA_HASH,
    PROFILES,
    RESOURCE_SHARING_FAMILY,
    candidate_features,
    gate_model_payload,
    score_rule_candidates,
)


def _graph() -> dict:
    return {
        "modules": [
            {
                "ports": [{"width": 16}],
                "nodes": [
                    {"kind": "operator", "operation": "add"},
                    {"kind": "operator", "operation": "add"},
                    {"kind": "operator", "operation": "mux"},
                ],
                "edges": [{}, {}],
            }
        ]
    }


def _finding() -> dict:
    return {
        "finding_id": "finding-1",
        "transformation_id": "share_arithmetic_by_muxing_inputs",
        "source": {"locations": [{"file": "top.sv", "start_line": 4}]},
        "evidence": {
            "serial_depth": 1,
            "duplicate_count": 2,
            "result_width": 17,
        },
        "risks": ["preserve mutual exclusivity"],
    }


def _leaf(value: float) -> dict:
    return {"nodes": [{"value": value}]}


def test_profile_boundaries_are_frozen() -> None:
    assert PROFILES["balanced"].eligible(3.0, -10.0)
    assert PROFILES["balanced"].eligible(-2.0, 5.0)
    assert not PROFILES["balanced"].eligible(2.99, 4.99)
    assert PROFILES["timing-first"].eligible(3.0, -20.0)
    assert not PROFILES["timing-first"].eligible(2.99, 100.0)
    assert PROFILES["area-first"].eligible(-10.0, 5.0)


def test_gate_selects_candidate_from_conservative_bounds() -> None:
    transformation = "share_arithmetic_by_muxing_inputs"
    core = {
        "model_version": "test",
        "estimators": {
            RESOURCE_SHARING_FAMILY: {
                "delay": _leaf(6.0),
                "area": _leaf(2.0),
                "cell_count": _leaf(5.0),
            }
        },
        "intervals": {
            RESOURCE_SHARING_FAMILY: {
                transformation: {
                    "delay": 1.0,
                    "area": 1.0,
                    "cell_count": 1.0,
                }
            }
        },
        "envelopes": {},
    }
    model = gate_model_payload(core)

    candidates, _ = score_rule_candidates(
        _graph(),
        [_finding()],
        profile_id="balanced",
        model=model,
    )

    assert model["feature_schema_hash"] == FEATURE_SCHEMA_HASH
    assert candidates[0]["eligible"] is True
    assert candidates[0]["predicted_improvement_percent"]["delay"]["lower"] == 5.0
    assert candidates[0]["conservative_utility"] == 6.4


def test_gate_abstains_without_a_calibrated_model() -> None:
    candidates, _ = score_rule_candidates(
        _graph(),
        [_finding()],
        profile_id="balanced",
        model=None,
    )

    assert candidates[0]["eligible"] is False
    assert "calibrated gate model is not installed" in candidates[0][
        "rejection_reasons"
    ]


def test_candidate_feature_vector_is_fixed() -> None:
    features = candidate_features({"node_count": 3.0}, _finding())
    assert features["node_count"] == 3.0
    assert features["duplicate_count"] == 2.0
    assert features["result_width"] == 17.0
