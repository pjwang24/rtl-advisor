from __future__ import annotations

from pathlib import Path

from rtl_advisor.config import load_config
from rtl_advisor.corpus import load_manifest
from rtl_advisor.features_v21 import (
    FEATURE_SCHEMA_HASH_V21,
    KERNEL_FEATURE_ORDER_V21,
    extract_case_kernel_features,
    fit_family_ood,
    score_family_ood,
    extract_syntax_facts,
)
from rtl_advisor.rules_v21 import analyze_rules_v21


MISS_MANIFEST = Path("corpus/heldout-v2/v2_dac48735a0bde7f3/manifest.json")


def test_v21_features_elaborate_kernel_and_preserve_zero_equality() -> None:
    config = load_config("rtl-advisor.toml")
    manifest = load_manifest(MISS_MANIFEST)
    extraction = extract_case_kernel_features(config, manifest)
    assert extraction["kernel_top"] == manifest.baseline.kernel_top
    assert extraction["features"]["module_count"] == 1.0
    assert extraction["features"]["register_count"] == 0.0
    assert extraction["features"]["syntax_equality_to_zero_count"] == 2.0
    assert extraction["feature_schema_hash"] == FEATURE_SCHEMA_HASH_V21


def test_v21_syntax_rule_recovers_frozen_comparator_miss() -> None:
    config = load_config("rtl-advisor.toml")
    manifest = load_manifest(MISS_MANIFEST)
    extraction = extract_case_kernel_features(config, manifest)
    analysis = analyze_rules_v21(extraction["graph"], extraction["syntax_facts"])
    findings = [
        finding
        for finding in analysis["findings"]
        if finding["transformation_id"] == "factor_comparator_selection"
    ]
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "comparator_selection.equality_to_zero_syntax.v21"


def test_v21_syntax_facts_recognize_signed_zero_casts() -> None:
    path = Path("corpus/calibration-v21/v21_2d5654156e2ca1fa/rtl/v0.sv")
    facts = extract_syntax_facts(
        (path,), top="v21_2d5654156e2ca1fa_v0_kernel"
    )
    assert facts["features"]["syntax_equality_to_zero_count"] == 2.0


def test_family_ood_records_nearest_topology_and_contributions() -> None:
    rows = []
    for index, value in enumerate((0.0, 1.0, 2.0, 3.0, 4.0)):
        features = {name: 0.0 for name in KERNEL_FEATURE_ORDER_V21}
        features["node_count"] = value
        features["edge_count"] = value * 2.0
        rows.append(
            {
                "family": "example",
                "topology_signature": f"topology-{index}",
                "features": features,
            }
        )
    model = fit_family_ood(rows)
    in_domain = {name: 0.0 for name in KERNEL_FEATURE_ORDER_V21}
    in_domain["node_count"] = 2.1
    in_domain["edge_count"] = 4.2
    score = score_family_ood(model, family="example", features=in_domain)
    assert score["nearest_calibration_topology"] == "topology-2"
    assert score["distance"] <= score["threshold"]
    assert score["contributing_features"]
