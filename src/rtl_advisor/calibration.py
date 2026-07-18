from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from rtl_advisor.advisor_v2 import (
    FEATURE_ORDER,
    TRANSFORMATION_FAMILIES,
    candidate_features,
    extract_design_features,
    gate_model_payload,
)
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import load_manifest
from rtl_advisor.graph import build_graph
from rtl_advisor.rules import analyze_rules
from rtl_advisor.v2_corpus import V2_SUITE_SCHEMA_VERSION


CALIBRATION_FLOW_VERSION = "rtl-advisor-calibration-v2"
TREE_RANDOM_SEED = 20260714
TREE_MAX_DEPTH = 3
TREE_MIN_LEAF = 8
RF_ESTIMATORS = 500
RF_MAX_DEPTH = 8
RF_MIN_LEAF = 3


class CalibrationError(RuntimeError):
    """Raised when training data or a model artifact violates the v2 contract."""


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_suite(path: Path) -> dict[str, Any]:
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"invalid calibration suite {path}: {exc}") from exc
    if not isinstance(suite, dict) or suite.get("schema_version") != V2_SUITE_SCHEMA_VERSION:
        raise CalibrationError(f"unsupported v2 suite schema in {path}")
    if suite.get("split") != "calibration-v2":
        raise CalibrationError("model training accepts calibration-v2 only")
    return suite


def _family_transformation(family: str) -> str:
    matches = [
        transformation
        for transformation, registered_family in TRANSFORMATION_FAMILIES.items()
        if registered_family == family
    ]
    if len(matches) != 1:
        raise CalibrationError(f"family has no unique transformation: {family}")
    return matches[0]


def registered_topology_context(
    family: str,
    topology: dict[str, Any],
) -> dict[str, Any]:
    """Build training-only evidence for a registered corpus topology.

    Some legal corpus points intentionally sit on the boundary where a live
    rule should not fire (for example, a variable shift with zero excess amount
    bits).  They are still useful calibration examples.  This function never
    participates in live analysis; it only supplies the fixed feature schema
    while collecting ground-truth rows from the generated corpus.
    """
    transformation = _family_transformation(family)
    width = int(topology.get("width", topology.get("opcode_width", 1)))
    evidence: dict[str, Any] = {"result_width": width}
    if family == "arithmetic_resource_sharing":
        branches = int(topology["branch_count"])
        evidence.update(branch_count=branches, duplicate_count=branches)
    elif family == "adder_reduction_association":
        operands = int(topology["operand_count"])
        evidence.update(
            serial_depth=max(0, operands - 1),
            operand_count_estimate=operands,
        )
    elif family == "priority_selection":
        requests = int(topology["request_count"])
        evidence.update(mux_depth=max(0, requests - 1), branch_count=requests)
    elif family == "mux_placement":
        fan_in = int(topology["fan_in"])
        evidence.update(branch_count=fan_in, duplicate_count=fan_in)
    elif family == "decode_factoring":
        evidence.update(
            branch_count=int(topology["match_count"]),
            reuse_count=int(topology["reuse_count"]),
        )
    elif family == "comparator_selection":
        evidence.update(
            branch_count=2,
            duplicate_count=2,
            fanout=int(topology["fanout"]),
        )
    elif family == "variable_shift":
        evidence.update(excess_amount_bits=int(topology["amount_excess"]))
    elif family == "width_signedness":
        evidence.update(maximum_width=width + int(topology["extension"]))
    elif family == "popcount_saturation":
        evidence.update(
            serial_depth=max(0, width - 1),
            operand_count_estimate=width,
            result_width=max(1, math.ceil(math.log2(width + 1))),
        )
    else:
        raise CalibrationError(f"unsupported registered topology family: {family}")
    return {
        "transformation_id": transformation,
        "evidence": evidence,
        "source": {"locations": []},
        "confidence": 1.0,
        "training_context_only": True,
    }


def _synthesis_summary(config: ProjectConfig, case_id: str) -> dict[str, Any]:
    path = config.artifacts_dir / "cases" / case_id / "synthesis/summary.json"
    if not path.is_file():
        raise CalibrationError(f"synthesis ground truth missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"invalid synthesis summary {path}: {exc}") from exc


def collect_calibration_rows(
    config: ProjectConfig,
    suite_path: str | Path,
    *,
    force_graph: bool = False,
) -> list[dict[str, Any]]:
    path = Path(suite_path).expanduser().resolve()
    suite = _load_suite(path)
    rows: list[dict[str, Any]] = []
    for case in suite.get("cases") or []:
        manifest_path = path.parent / str(case["manifest"])
        manifest = load_manifest(manifest_path)
        graph = build_graph(
            config,
            manifest,
            manifest.baseline_id,
            force=force_graph,
        ).graph
        rules = analyze_rules(graph)
        transformation = _family_transformation(manifest.family)
        findings = [
            finding
            for finding in rules.get("findings") or []
            if finding.get("transformation_id") == transformation
        ]
        detected_by_rule = bool(findings)
        finding = (
            findings[0]
            if findings
            else registered_topology_context(manifest.family, case["topology"])
        )
        design_features = extract_design_features(graph)
        summary = _synthesis_summary(config, manifest.case_id)
        comparisons = {
            comparison["candidate_id"]: comparison
            for comparison in summary.get("comparisons") or []
        }
        for template_id in ("v1", "v2", "v3"):
            comparison = comparisons.get(template_id)
            if comparison is None:
                raise CalibrationError(
                    f"missing {template_id} synthesis comparison for {manifest.case_id}"
                )
            rows.append(
                {
                    "case_id": manifest.case_id,
                    "topology_signature": case["topology_signature"],
                    "family": manifest.family,
                    "transformation_id": transformation,
                    "template_id": template_id,
                    "detection": (
                        "registered_rule"
                        if detected_by_rule
                        else "registered_topology_context"
                    ),
                    "features": candidate_features(
                        design_features, finding, template_id
                    ),
                    "targets": {
                        "delay": float(
                            comparison["critical_delay_ps"]["improvement_percent"]
                        ),
                        "area": float(
                            comparison["area_total"]["improvement_percent"]
                        ),
                        "cell_count": float(
                            comparison["cell_count"]["improvement_percent"]
                        ),
                    },
                }
            )
    expected = int(suite.get("case_count", 0)) * 3
    if len(rows) != expected:
        raise CalibrationError(f"expected {expected} calibration rows, got {len(rows)}")
    return rows


def _sklearn() -> tuple[Any, Any, Any]:
    try:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.model_selection import GroupKFold
        from sklearn.tree import DecisionTreeRegressor
    except ImportError as exc:
        raise CalibrationError(
            "scikit-learn 1.9.0 is required; install rtl-advisor[train]"
        ) from exc
    return DecisionTreeRegressor, RandomForestRegressor, GroupKFold


def _matrix(rows: Iterable[dict[str, Any]]) -> list[list[float]]:
    return [
        [float(row["features"].get(feature, 0.0)) for feature in FEATURE_ORDER]
        for row in rows
    ]


def _tree_payload(estimator: Any) -> dict[str, Any]:
    tree = estimator.tree_
    nodes = []
    for index in range(tree.node_count):
        left = int(tree.children_left[index])
        right = int(tree.children_right[index])
        if left == right:
            nodes.append({"value": round(float(tree.value[index][0][0]), 9)})
        else:
            nodes.append(
                {
                    "feature": FEATURE_ORDER[int(tree.feature[index])],
                    "threshold": round(float(tree.threshold[index]), 9),
                    "left": left,
                    "right": right,
                }
            )
    return {"nodes": nodes}


def _conformal_radius(residuals: list[float], coverage: float = 0.9) -> float:
    if not residuals:
        raise CalibrationError("cannot calibrate an empty residual set")
    ordered = sorted(abs(value) for value in residuals)
    rank = min(len(ordered), math.ceil((len(ordered) + 1) * coverage))
    return round(float(ordered[rank - 1]), 9)


def train_gate_from_rows(
    rows: list[dict[str, Any]],
    *,
    training_suite_hash: str,
) -> dict[str, Any]:
    DecisionTreeRegressor, _, GroupKFold = _sklearn()
    families = sorted({str(row["family"]) for row in rows})
    estimators: dict[str, Any] = {}
    intervals: dict[str, Any] = {}
    envelopes: dict[str, Any] = {}
    cv_metrics: dict[str, Any] = {}
    for family in families:
        family_rows = [row for row in rows if row["family"] == family]
        groups = [row["topology_signature"] for row in family_rows]
        if len(set(groups)) < 5:
            raise CalibrationError(f"{family} requires at least five topology groups")
        x_values = _matrix(family_rows)
        estimators[family] = {}
        intervals[family] = {}
        envelopes[family] = {
            feature: [
                min(float(row["features"][feature]) for row in family_rows),
                max(float(row["features"][feature]) for row in family_rows),
            ]
            for feature in FEATURE_ORDER
        }
        residuals_by_metric: dict[str, list[float]] = {}
        predictions_by_metric: dict[str, list[float]] = {}
        splitter = GroupKFold(n_splits=5)
        splits = list(splitter.split(x_values, groups=groups))
        for metric in ("delay", "area", "cell_count"):
            targets = [float(row["targets"][metric]) for row in family_rows]
            oof = [0.0] * len(family_rows)
            for train_indices, test_indices in splits:
                estimator = DecisionTreeRegressor(
                    max_depth=TREE_MAX_DEPTH,
                    min_samples_leaf=TREE_MIN_LEAF,
                    random_state=TREE_RANDOM_SEED,
                )
                estimator.fit(
                    [x_values[index] for index in train_indices],
                    [targets[index] for index in train_indices],
                )
                predicted = estimator.predict(
                    [x_values[index] for index in test_indices]
                )
                for index, value in zip(test_indices, predicted, strict=True):
                    oof[int(index)] = float(value)
            residuals = [target - prediction for target, prediction in zip(targets, oof)]
            residuals_by_metric[metric] = residuals
            predictions_by_metric[metric] = oof
            final = DecisionTreeRegressor(
                max_depth=TREE_MAX_DEPTH,
                min_samples_leaf=TREE_MIN_LEAF,
                random_state=TREE_RANDOM_SEED,
            )
            final.fit(x_values, targets)
            estimators[family][metric] = _tree_payload(final)
            cv_metrics.setdefault(family, {})[metric] = {
                "mean_absolute_error": round(
                    sum(abs(value) for value in residuals) / len(residuals), 9
                ),
                "residual_count": len(residuals),
            }
        transformation = _family_transformation(family)
        for template_id in ("v1", "v2", "v3"):
            indices = [
                index
                for index, row in enumerate(family_rows)
                if row["template_id"] == template_id
            ]
            intervals[family][f"{transformation}:{template_id}"] = {
                metric: _conformal_radius(
                    [residuals_by_metric[metric][index] for index in indices]
                )
                for metric in ("delay", "area", "cell_count")
            }

    return gate_model_payload(
        {
            "model_version": CALIBRATION_FLOW_VERSION,
            "training_suite_hash": training_suite_hash,
            "random_seed": TREE_RANDOM_SEED,
            "feature_order": list(FEATURE_ORDER),
            "hyperparameters": {
                "max_depth": TREE_MAX_DEPTH,
                "min_samples_leaf": TREE_MIN_LEAF,
                "folds": 5,
                "conformal_coverage": 0.9,
            },
            "estimators": estimators,
            "intervals": intervals,
            "envelopes": envelopes,
            "cross_validation": cv_metrics,
        }
    )


def train_challenger_from_rows(
    rows: list[dict[str, Any]],
    output_path: Path,
    *,
    training_suite_hash: str,
) -> dict[str, Any]:
    _, RandomForestRegressor, _ = _sklearn()
    try:
        import joblib
    except ImportError as exc:
        raise CalibrationError("joblib is required to store the challenger") from exc
    family_codes = {
        family: float(index)
        for index, family in enumerate(sorted({row["family"] for row in rows}))
    }
    x_values = [
        [*values, family_codes[row["family"]]]
        for row, values in zip(rows, _matrix(rows), strict=True)
    ]
    targets = [
        [
            float(row["targets"]["delay"]),
            float(row["targets"]["area"]),
            float(row["targets"]["cell_count"]),
        ]
        for row in rows
    ]
    estimator = RandomForestRegressor(
        n_estimators=RF_ESTIMATORS,
        max_depth=RF_MAX_DEPTH,
        min_samples_leaf=RF_MIN_LEAF,
        max_features="sqrt",
        bootstrap=True,
        random_state=TREE_RANDOM_SEED,
        n_jobs=-1,
    )
    estimator.fit(x_values, targets)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "estimator": estimator,
            "feature_order": [*FEATURE_ORDER, "family_code"],
            "family_codes": family_codes,
            "training_suite_hash": training_suite_hash,
        },
        output_path,
    )
    artifact_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
    metadata = {
        "flow_version": CALIBRATION_FLOW_VERSION,
        "artifact_path": str(output_path),
        "artifact_sha256": artifact_hash,
        "training_suite_hash": training_suite_hash,
        "row_count": len(rows),
        "family_codes": family_codes,
        "hyperparameters": {
            "n_estimators": RF_ESTIMATORS,
            "max_depth": RF_MAX_DEPTH,
            "min_samples_leaf": RF_MIN_LEAF,
            "max_features": "sqrt",
            "bootstrap": True,
            "random_seed": TREE_RANDOM_SEED,
        },
    }
    _write_json(output_path.with_suffix(".json"), metadata)
    return metadata


def train_v2_models(
    config: ProjectConfig,
    suite_path: str | Path,
    *,
    force_graph: bool = False,
) -> dict[str, Any]:
    path = Path(suite_path).expanduser().resolve()
    suite = _load_suite(path)
    rows = collect_calibration_rows(config, path, force_graph=force_graph)
    model_root = config.artifacts_dir / "models/v2"
    rows_path = model_root / "calibration-rows.json"
    _write_json(
        rows_path,
        {
            "flow_version": CALIBRATION_FLOW_VERSION,
            "suite_hash": suite["suite_hash"],
            "row_count": len(rows),
            "rows": rows,
        },
    )
    gate = train_gate_from_rows(rows, training_suite_hash=suite["suite_hash"])
    gate_path = model_root / "gate.json"
    _write_json(gate_path, gate)
    challenger = train_challenger_from_rows(
        rows,
        model_root / "challenger.joblib",
        training_suite_hash=suite["suite_hash"],
    )
    result = {
        "status": "passed",
        "flow_version": CALIBRATION_FLOW_VERSION,
        "suite_hash": suite["suite_hash"],
        "row_count": len(rows),
        "gate_path": str(gate_path),
        "gate_hash": gate["model_hash"],
        "challenger": challenger,
        "rows_path": str(rows_path),
    }
    _write_json(model_root / "summary.json", result)
    return result
