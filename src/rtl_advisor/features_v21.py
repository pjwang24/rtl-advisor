from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable

from rtl_advisor.advisor_v2 import (
    FEATURE_ORDER as V2_FEATURE_ORDER,
    candidate_features,
    extract_design_features,
)
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest
from rtl_advisor.rtl_input import build_live_graph, normalize_design_input


FEATURE_SCHEMA_VERSION_V21 = "rtl-advisor-kernel-syntax-features-v21"
SYNTAX_FEATURE_ORDER = (
    "syntax_equality_count",
    "syntax_equality_to_zero_count",
    "syntax_inequality_count",
    "syntax_inequality_to_zero_count",
    "syntax_relational_count",
    "syntax_conditional_count",
    "syntax_if_count",
    "syntax_case_count",
)
DESIGN_FEATURE_ORDER_V21 = V2_FEATURE_ORDER[
    : V2_FEATURE_ORDER.index("shift_excess_bits") + 1
]
KERNEL_FEATURE_ORDER_V21 = DESIGN_FEATURE_ORDER_V21 + SYNTAX_FEATURE_ORDER
FEATURE_ORDER_V21 = V2_FEATURE_ORDER + SYNTAX_FEATURE_ORDER
FEATURE_TYPES_V21 = {feature: "float64" for feature in FEATURE_ORDER_V21}
FEATURE_SCHEMA_HASH_V21 = hashlib.sha256(
    json.dumps(
        {
            "version": FEATURE_SCHEMA_VERSION_V21,
            "feature_order": FEATURE_ORDER_V21,
            "feature_types": FEATURE_TYPES_V21,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
OOD_FLOW_VERSION_V21 = "rtl-advisor-family-nearest-neighbor-ood-v21"


class FeatureV21Error(RuntimeError):
    """Raised when V2.1 pre-synthesis features cannot be reproduced."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _is_zero_literal(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    while True:
        signed_cast = re.fullmatch(r"\$(?:signed|unsigned)\((.*)\)", compact)
        parenthesized = re.fullmatch(r"\((.*)\)", compact)
        match = signed_cast or parenthesized
        if match is None:
            break
        compact = match.group(1)
    return bool(
        compact in {"0", "'0", "1'b0", "1'h0"}
        or re.fullmatch(r"\d+'[s]?[bdho]0+", compact)
    )


def extract_syntax_facts(
    files: Iterable[str | Path],
    *,
    top: str,
) -> dict[str, Any]:
    """Extract source-level facts from only the requested module with PySlang."""
    try:
        import pyslang  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FeatureV21Error("PySlang is required for V2.1 syntax facts") from exc

    modules = []
    source_paths = [Path(path).expanduser().resolve() for path in files]
    try:
        trees = [pyslang.SyntaxTree.fromFile(str(path)) for path in source_paths]
        for tree in trees:
            def collect(node: Any) -> Any:
                if node.kind == pyslang.SyntaxKind.ModuleDeclaration:
                    modules.append(node)
                    return pyslang.VisitAction.Skip
                return pyslang.VisitAction.Advance

            tree.root.visit(collect)
    except Exception as exc:  # native binding exceptions do not share one base class
        raise FeatureV21Error(f"PySlang syntax extraction failed: {exc}") from exc
    selected = [
        module
        for module in modules
        if str(module.header.name).strip() == top
    ]
    if len(selected) != 1:
        raise FeatureV21Error(
            f"expected exactly one PySlang module named {top}, found {len(selected)}"
        )

    counts = {feature: 0 for feature in SYNTAX_FEATURE_ORDER}
    comparisons = []

    def visit(node: Any) -> Any:
        kind = node.kind
        if kind == pyslang.SyntaxKind.EqualityExpression:
            counts["syntax_equality_count"] += 1
            left, right = str(node.left).strip(), str(node.right).strip()
            to_zero = _is_zero_literal(left) or _is_zero_literal(right)
            counts["syntax_equality_to_zero_count"] += int(to_zero)
            comparisons.append(
                {
                    "kind": "equality",
                    "text": str(node).strip(),
                    "to_zero": to_zero,
                    "file": str(source_paths[0]) if source_paths else None,
                }
            )
        elif kind == pyslang.SyntaxKind.InequalityExpression:
            counts["syntax_inequality_count"] += 1
            left, right = str(node.left).strip(), str(node.right).strip()
            to_zero = _is_zero_literal(left) or _is_zero_literal(right)
            counts["syntax_inequality_to_zero_count"] += int(to_zero)
            comparisons.append(
                {
                    "kind": "inequality",
                    "text": str(node).strip(),
                    "to_zero": to_zero,
                    "file": str(source_paths[0]) if source_paths else None,
                }
            )
        elif kind in {
            pyslang.SyntaxKind.LessThanExpression,
            pyslang.SyntaxKind.LessThanEqualExpression,
            pyslang.SyntaxKind.GreaterThanExpression,
            pyslang.SyntaxKind.GreaterThanEqualExpression,
        }:
            counts["syntax_relational_count"] += 1
        elif kind == pyslang.SyntaxKind.ConditionalExpression:
            counts["syntax_conditional_count"] += 1
        elif kind == pyslang.SyntaxKind.ConditionalStatement:
            counts["syntax_if_count"] += 1
        elif kind in {
            pyslang.SyntaxKind.CaseStatement,
            pyslang.SyntaxKind.RandCaseStatement,
        }:
            counts["syntax_case_count"] += 1
        return pyslang.VisitAction.Advance

    try:
        selected[0].visit(visit)
    except AttributeError as exc:
        raise FeatureV21Error(f"unsupported PySlang syntax API: {exc}") from exc
    core = {
        "schema_version": 1,
        "flow_version": FEATURE_SCHEMA_VERSION_V21,
        "pyslang_version": getattr(pyslang, "__version__", "unknown"),
        "top": top,
        "files": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in source_paths
        ],
        "features": {name: float(counts[name]) for name in SYNTAX_FEATURE_ORDER},
        "comparisons": comparisons,
    }
    return {**core, "syntax_hash": _stable_hash(core)}


def extract_case_kernel_features(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant_id: str = "v0",
    *,
    force_graph: bool = False,
) -> dict[str, Any]:
    variant = manifest.variant(variant_id)
    source = manifest.variant_path(variant)
    design = normalize_design_input(
        top=variant.kernel_top,
        files=(source,),
        base=manifest.root,
    )
    graph_build = build_live_graph(config, design, force=force_graph)
    if graph_build.graph.get("top") != variant.kernel_top:
        raise FeatureV21Error("kernel-only graph elaborated the wrong top")
    syntax = extract_syntax_facts((source,), top=variant.kernel_top)
    design_features = extract_design_features(graph_build.graph)
    design_features.update(syntax["features"])
    missing = sorted(set(KERNEL_FEATURE_ORDER_V21) - set(design_features))
    if missing:
        raise FeatureV21Error(f"kernel feature extraction omitted: {missing}")
    core = {
        "schema_version": 1,
        "feature_schema_version": FEATURE_SCHEMA_VERSION_V21,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "case_id": manifest.case_id,
        "variant_id": variant_id,
        "kernel_top": variant.kernel_top,
        "wrapper_top": variant.wrapper_top,
        "source_sha256": variant.sha256,
        "graph_hash": graph_build.graph["graph_hash"],
        "syntax_hash": syntax["syntax_hash"],
        "features": {
            feature: float(design_features[feature])
            for feature in KERNEL_FEATURE_ORDER_V21
        },
        "syntax_facts": syntax,
    }
    return {
        **core,
        "feature_hash": _stable_hash(core),
        "graph": graph_build.graph,
        "graph_path": str(graph_build.graph_path),
        "graph_cached": graph_build.cached,
    }


def candidate_features_v21(
    kernel_features: dict[str, float],
    finding: dict[str, Any],
    template_id: str,
) -> dict[str, float]:
    # Reuse the stable V2 evidence encoding, replacing its design features with
    # the kernel-only vector and then appending the syntax dimensions.
    values = candidate_features(kernel_features, finding, template_id)
    values.update(
        {
            feature: float(kernel_features.get(feature, 0.0))
            for feature in SYNTAX_FEATURE_ORDER
        }
    )
    return {feature: float(values.get(feature, 0.0)) for feature in FEATURE_ORDER_V21}


def _percentile_nearest_rank(values: list[float], fraction: float) -> float:
    if not values:
        raise FeatureV21Error("cannot take a percentile of an empty distance set")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def _distance(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise FeatureV21Error("OOD vector dimensions do not match")
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)) / len(left))


def fit_family_ood(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        signature = str(row["topology_signature"])
        by_family[str(row["family"])].setdefault(signature, row)
    families = {}
    for family, topology_rows in sorted(by_family.items()):
        if len(topology_rows) < 2:
            raise FeatureV21Error(f"{family} requires at least two OOD topologies")
        features = [
            feature
            for feature in KERNEL_FEATURE_ORDER_V21
            if max(float(row["features"].get(feature, 0.0)) for row in topology_rows.values())
            != min(float(row["features"].get(feature, 0.0)) for row in topology_rows.values())
        ]
        if not features:
            raise FeatureV21Error(f"{family} has no varying kernel OOD features")
        normalization = {}
        for feature in features:
            values = sorted(
                float(row["features"].get(feature, 0.0))
                for row in topology_rows.values()
            )
            median = float(statistics.median(values))
            quartiles = statistics.quantiles(values, n=4, method="inclusive")
            iqr = float(quartiles[2] - quartiles[0])
            normalization[feature] = {"median": median, "iqr": iqr or 1.0}

        def vector(row: dict[str, Any]) -> list[float]:
            return [
                (
                    float(row["features"].get(feature, 0.0))
                    - normalization[feature]["median"]
                )
                / normalization[feature]["iqr"]
                for feature in features
            ]

        vectors = {
            signature: vector(row) for signature, row in sorted(topology_rows.items())
        }
        leave_one_out = {}
        for signature, current in vectors.items():
            nearest_signature, nearest_distance = min(
                (
                    (other_signature, _distance(current, other))
                    for other_signature, other in vectors.items()
                    if other_signature != signature
                ),
                key=lambda item: (item[1], item[0]),
            )
            leave_one_out[signature] = {
                "nearest_topology_signature": nearest_signature,
                "distance": nearest_distance,
            }
        threshold = _percentile_nearest_rank(
            [item["distance"] for item in leave_one_out.values()], 0.95
        )
        families[family] = {
            "feature_order": features,
            "normalization": normalization,
            "calibration_vectors": vectors,
            "leave_one_topology_out": leave_one_out,
            "threshold_percentile": 0.95,
            "threshold": threshold,
        }
    core = {
        "schema_version": 1,
        "flow_version": OOD_FLOW_VERSION_V21,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "families": families,
    }
    return {**core, "model_hash": _stable_hash(core)}


def score_family_ood(
    model: dict[str, Any],
    *,
    family: str,
    features: dict[str, float],
) -> dict[str, Any]:
    if model.get("feature_schema_hash") != FEATURE_SCHEMA_HASH_V21:
        raise FeatureV21Error("OOD model feature schema mismatch")
    try:
        spec = model["families"][family]
    except KeyError as exc:
        raise FeatureV21Error(f"OOD model has no family {family}") from exc
    order = spec["feature_order"]
    vector = [
        (float(features.get(feature, 0.0)) - spec["normalization"][feature]["median"])
        / spec["normalization"][feature]["iqr"]
        for feature in order
    ]
    nearest_signature, nearest_vector, distance = min(
        (
            (signature, calibration, _distance(vector, calibration))
            for signature, calibration in spec["calibration_vectors"].items()
        ),
        key=lambda item: (item[2], item[0]),
    )
    contributions = sorted(
        (
            {
                "feature": feature,
                "normalized_absolute_difference": abs(value - nearest),
            }
            for feature, value, nearest in zip(
                order, vector, nearest_vector, strict=True
            )
        ),
        key=lambda item: (-item["normalized_absolute_difference"], item["feature"]),
    )
    threshold = float(spec["threshold"])
    return {
        "flow_version": OOD_FLOW_VERSION_V21,
        "family": family,
        "out_of_domain": distance > threshold,
        "distance": distance,
        "threshold": threshold,
        "nearest_calibration_topology": nearest_signature,
        "contributing_features": contributions,
    }
