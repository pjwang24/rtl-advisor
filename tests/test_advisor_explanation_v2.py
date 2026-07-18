from __future__ import annotations

import pytest

from rtl_advisor.advisor_explanation_v2 import (
    AdvisorExplanationError,
    _validate_response,
)


def _analysis() -> dict:
    return {
        "decision": "recommend",
        "selected_candidate_id": "candidate-1",
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "transformation_id": "reassociate_arithmetic_tree",
            }
        ],
    }


def _response() -> dict:
    return {
        "summary": "A balanced tree is recommended.",
        "decision": "recommend",
        "selected_candidate_id": "candidate-1",
        "transformation_id": "reassociate_arithmetic_tree",
        "predicted_directions": {
            "delay": "improve",
            "area": "uncertain",
            "cell_count": "neutral",
        },
        "recommendation": "Generate and formally prove the isolated candidate.",
        "risks": ["Preserve widths."],
        "verification": ["Run whole-design equivalence."],
    }


def test_explanation_cannot_override_gate_decision() -> None:
    response = _response()
    response["decision"] = "abstain"
    with pytest.raises(AdvisorExplanationError, match="override the gate"):
        _validate_response(response, _analysis())


def test_explanation_cannot_override_candidate() -> None:
    response = _response()
    response["selected_candidate_id"] = "candidate-2"
    with pytest.raises(AdvisorExplanationError, match="selected candidate"):
        _validate_response(response, _analysis())


def test_valid_explanation_is_preserved() -> None:
    response = _response()
    assert _validate_response(response, _analysis()) == response
