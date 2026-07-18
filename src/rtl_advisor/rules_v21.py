from __future__ import annotations

import hashlib
import json
from typing import Any

from rtl_advisor.rules import analyze_rules


RULESET_VERSION_V21 = "rtl-advisor-structural-rules-v21"


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _zero_equality_finding(
    graph: dict[str, Any], syntax_facts: dict[str, Any]
) -> dict[str, Any] | None:
    zero_equalities = [
        comparison
        for comparison in syntax_facts.get("comparisons") or []
        if comparison.get("kind") == "equality" and comparison.get("to_zero")
    ]
    # One equality is not evidence of a factorable selection structure. Two or
    # more independent source comparisons recover the demonstrated V2 miss
    # while keeping the rule conservative.
    if len(zero_equalities) < 2:
        return None
    top = str(graph["top"])
    modules = [module for module in graph["modules"] if module["name"] == top]
    if len(modules) != 1:
        return None
    module = modules[0]
    output_width = max(
        (
            int(port.get("width", 0))
            for port in module.get("ports") or []
            if port.get("direction") == "output"
        ),
        default=0,
    )
    identity = {
        "rule_id": "comparator_selection.equality_to_zero_syntax.v21",
        "module": top,
        "syntax_hash": syntax_facts["syntax_hash"],
        "comparisons": [item["text"] for item in zero_equalities],
    }
    return {
        "finding_id": _stable_hash(identity)[:16],
        "rule_id": identity["rule_id"],
        "category": "comparator_selection",
        "severity": "advisory",
        "module": top,
        "module_display_name": module.get("display_name", top),
        "source": {
            "locations": [
                {
                    "file": zero_equalities[0]["file"],
                    "start_line": 1,
                    "end_line": 1,
                }
            ]
            if zero_equalities[0].get("file")
            else []
        },
        "confidence": 0.9,
        "evidence": {
            "operator": "equality_to_zero",
            "branch_count": len(zero_equalities),
            "duplicate_count": len(zero_equalities),
            "fanout": max(1, int(syntax_facts["features"]["syntax_conditional_count"])),
            "result_width": output_width,
            "syntax_comparisons": [item["text"] for item in zero_equalities],
            "syntax_hash": syntax_facts["syntax_hash"],
        },
        "recommendation": (
            "Consider selecting the compared operand before one equality-to-zero "
            "comparison when the source conditions are mutually exclusive."
        ),
        "transformation_id": "factor_comparator_selection",
        "predicted_effect": {
            "area": "improve",
            "cell_count": "improve",
            "delay": "uncertain",
        },
        "risks": [
            "The selected source expression and comparison width must remain identical.",
            "Four-state equality behavior and condition priority must be preserved.",
        ],
        "syntax_fact_source": True,
    }


def analyze_rules_v21(
    graph: dict[str, Any], syntax_facts: dict[str, Any]
) -> dict[str, Any]:
    base = analyze_rules(graph)
    findings = list(base.get("findings") or [])
    transformations = {finding.get("transformation_id") for finding in findings}
    syntax_finding = _zero_equality_finding(graph, syntax_facts)
    if (
        syntax_finding is not None
        and syntax_finding["transformation_id"] not in transformations
    ):
        findings.append(syntax_finding)
    findings.sort(key=lambda finding: finding["finding_id"])
    core = {
        "schema_version": 21,
        "ruleset_version": RULESET_VERSION_V21,
        "base_ruleset_version": base["ruleset_version"],
        "case_id": graph["case_id"],
        "variant_id": graph["variant_id"],
        "graph_hash": graph["graph_hash"],
        "syntax_hash": syntax_facts["syntax_hash"],
        "mode": "rules-v21",
        "findings": findings,
    }
    return {**core, "analysis_hash": _stable_hash(core)}
