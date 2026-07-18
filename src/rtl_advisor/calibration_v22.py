from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from rtl_advisor.calibration_v21 import _case_policy_metrics
from rtl_advisor.config import ProjectConfig
from rtl_advisor.features_v21 import FEATURE_ORDER_V21, FEATURE_SCHEMA_HASH_V21


CALIBRATION_FLOW_VERSION_V22 = "rtl-advisor-calibration-v22"
MODEL_RANDOM_SEED_V22 = 20260716
RF_ESTIMATORS_V22 = 500
RF_MAX_DEPTH_V22 = 12
RF_MIN_LEAF_V22 = 3
GROUP_FOLDS_V22 = 5
MINIMUM_FAMILY_OPPORTUNITIES_V22 = 10
MAXIMUM_HARMFUL_RATE_V22 = 0.05
MINIMUM_SPECIFICITY_V22 = 0.90
MINIMUM_BALANCED_ACCURACY_V22 = 0.70
MAXIMUM_FAMILY_HARMFUL_RATE_V22 = 0.10
MINIMUM_FAMILY_SPECIFICITY_V22 = 0.80
MINIMUM_FAMILY_RECOMMENDATIONS_FOR_HARM_GATE_V22 = 10
PHYSICAL_REPORT_HASH_V22 = (
    "6dcb060959df8e41f1c919fe794b5631f6d3b055759febab88e1fe16692f0643"
)

FROZEN_INPUT_SHA256_V22 = {
    "calibration_rows": "18e45b5e172d28812236fb686fdc8aa391ad3bb7a2d832cc7966a40996def625",
    "grouped_oof": "24f15494dfea0b11e0a79d547970f25fce4105e5c5866e9dfa4fc6a0f015d5e6",
    "v21_bundle": "4ffb492234f3fa44e676ff81d4c66c98c716bac0c8b9028df9600181ff008c2a",
    "v21_metadata": "1f200cbada5c09da71d706b011e23dc367021d3e8450c3641ea5ee3ee5961c2a",
    "v21_ood": "c3fc69e41df575d6a32286b9384d73c0a623949409db6e8432e4beda2ffc4bf0",
    "v21_policy": "24cd6a34569dcfc8773092cbd74ed1b3cad517c7cc2f665c552415d8d38b2e76",
    "physical_report": "3b1c65cd3aad92adffbc369df608902316608b2487619e8744b45e69897c6d34",
}


class CalibrationV22Error(RuntimeError):
    """Raised when V2.2 family-risk calibration violates its frozen contract."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationV22Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CalibrationV22Error(f"expected object in {description} {path}")
    return value


def frozen_input_paths_v22(config: ProjectConfig) -> dict[str, Path]:
    v21 = config.artifacts_dir / "models/v21"
    return {
        "calibration_rows": v21 / "calibration-rows.json",
        "grouped_oof": v21 / "grouped-oof.json",
        "v21_bundle": v21 / "model-bundle.joblib",
        "v21_metadata": v21 / "metadata.json",
        "v21_ood": v21 / "ood.json",
        "v21_policy": v21 / "policy.json",
        "physical_report": config.artifacts_dir / "openroad/v2/report.json",
    }


def verify_frozen_inputs_v22(config: ProjectConfig) -> dict[str, Any]:
    paths = frozen_input_paths_v22(config)
    artifacts = {}
    for name, path in paths.items():
        if not path.is_file():
            raise CalibrationV22Error(f"frozen V2.2 input missing ({name}): {path}")
        actual = _file_hash(path)
        expected = FROZEN_INPUT_SHA256_V22[name]
        if actual != expected:
            raise CalibrationV22Error(
                f"frozen V2.2 input changed ({name}): expected {expected}, got {actual}"
            )
        artifacts[name] = {
            "path": str(path.resolve()),
            "sha256": actual,
        }
    metadata = _load_json(paths["v21_metadata"], "V2.1 metadata")
    if metadata.get("feature_schema_hash") != FEATURE_SCHEMA_HASH_V21:
        raise CalibrationV22Error("V2.1 feature schema changed before V2.2 calibration")
    if metadata.get("direction_policy_feasible") is not True:
        raise CalibrationV22Error("reused V2.1 direction policy is not feasible")
    physical = _load_json(paths["physical_report"], "OpenROAD physical report")
    if physical.get("report_hash") != PHYSICAL_REPORT_HASH_V22:
        raise CalibrationV22Error("OpenROAD semantic report hash changed")
    if not (physical.get("physical_evidence_gate") or {}).get("passed"):
        raise CalibrationV22Error("OpenROAD physical-evidence gate is not passing")
    core = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V22,
        "blind_labels_used": False,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "physical_report_hash": PHYSICAL_REPORT_HASH_V22,
        "artifacts": artifacts,
    }
    return {**core, "input_lock_hash": _stable_hash(core)}


def _sklearn() -> tuple[Any, Any]:
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import GroupKFold
    except ImportError as exc:
        raise CalibrationV22Error(
            "scikit-learn is required for V2.2 family calibration"
        ) from exc
    return RandomForestClassifier, GroupKFold


def _scipy_milp() -> tuple[Any, Any, Any, Any]:
    try:
        import numpy
        from scipy.optimize import Bounds, LinearConstraint, milp
    except ImportError as exc:
        raise CalibrationV22Error(
            "SciPy MILP support is required for V2.2 joint calibration"
        ) from exc
    return numpy, Bounds, LinearConstraint, milp


def _matrix(rows: Iterable[dict[str, Any]]) -> list[list[float]]:
    return [
        [float(row["features"].get(feature, 0.0)) for feature in FEATURE_ORDER_V21]
        for row in rows
    ]


def family_opportunity_count_v22(rows: list[dict[str, Any]]) -> int:
    cases: dict[str, bool] = {}
    for row in rows:
        case_id = str(row["case_id"])
        cases[case_id] = cases.get(case_id, False) or bool(row["eligible"])
    return sum(cases.values())


def _classifier_probability(estimator: Any, values: Any) -> list[float]:
    classes = list(estimator.classes_)
    if True not in classes:
        return [0.0] * len(values)
    index = classes.index(True)
    return [float(row[index]) for row in estimator.predict_proba(values)]


def grouped_family_oof_v22(
    rows: list[dict[str, Any]],
) -> tuple[list[float], dict[str, Any], Any | None]:
    opportunity_count = family_opportunity_count_v22(rows)
    supported = opportunity_count >= MINIMUM_FAMILY_OPPORTUNITIES_V22
    evidence = {
        "candidate_row_count": len(rows),
        "case_count": len({str(row["case_id"]) for row in rows}),
        "topology_group_count": len(
            {str(row["topology_signature"]) for row in rows}
        ),
        "opportunity_count": opportunity_count,
        "minimum_opportunity_count": MINIMUM_FAMILY_OPPORTUNITIES_V22,
        "supported": supported,
    }
    if not supported:
        return [0.0] * len(rows), evidence, None
    RandomForestClassifier, GroupKFold = _sklearn()
    x_values = _matrix(rows)
    labels = [bool(row["eligible"]) for row in rows]
    groups = [str(row["topology_signature"]) for row in rows]
    if len(set(groups)) < GROUP_FOLDS_V22:
        raise CalibrationV22Error("supported family has fewer than five topology groups")

    def classifier() -> Any:
        return RandomForestClassifier(
            n_estimators=RF_ESTIMATORS_V22,
            max_depth=RF_MAX_DEPTH_V22,
            min_samples_leaf=RF_MIN_LEAF_V22,
            max_features="sqrt",
            bootstrap=True,
            class_weight="balanced_subsample",
            random_state=MODEL_RANDOM_SEED_V22,
            n_jobs=-1,
        )

    probabilities = [0.0] * len(rows)
    splitter = GroupKFold(n_splits=GROUP_FOLDS_V22)
    for train_indices, test_indices in splitter.split(x_values, labels, groups):
        train_x = [x_values[int(index)] for index in train_indices]
        train_y = [labels[int(index)] for index in train_indices]
        model = classifier()
        model.fit(train_x, train_y)
        fold = _classifier_probability(
            model, [x_values[int(index)] for index in test_indices]
        )
        for row_index, probability in zip(test_indices, fold, strict=True):
            probabilities[int(row_index)] = probability
    final_model = classifier()
    final_model.fit(x_values, labels)
    return probabilities, evidence, final_model


def _threshold_metrics(
    rows: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    metrics = _case_policy_metrics(rows, predictions, threshold)
    decisions = metrics.pop("decisions")
    correct = sum(
        bool(item["opportunity"] and item["correct"]) for item in decisions
    )
    abstained_nonopportunity = sum(
        bool(not item["opportunity"] and item["selected_template"] is None)
        for item in decisions
    )
    harmful = sum(bool(item["harmful"]) for item in decisions)
    if metrics["recommendation_count"] != correct + harmful:
        raise CalibrationV22Error("case policy produced an unclassified recommendation")
    family_constraints = {
        "minimum_abstention_specificity": (
            metrics["abstention_specificity"] >= MINIMUM_FAMILY_SPECIFICITY_V22
        ),
        "maximum_harmful_rate_when_supported": (
            metrics["recommendation_count"]
            < MINIMUM_FAMILY_RECOMMENDATIONS_FOR_HARM_GATE_V22
            or metrics["harmful_recommendation_rate"]
            <= MAXIMUM_FAMILY_HARMFUL_RATE_V22
        ),
    }
    core = {
        **metrics,
        "correct_opportunity_count": correct,
        "abstained_nonopportunity_count": abstained_nonopportunity,
        "harmful_count": harmful,
        "family_constraints": family_constraints,
        "family_constraints_passed": all(family_constraints.values()),
        "decision_hash": _stable_hash(decisions),
    }
    return core


def build_family_frontier_v22(
    rows: list[dict[str, Any]],
    base_predictions: list[dict[str, Any]],
    probabilities: list[float],
    *,
    supported: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(rows) != len(base_predictions) or len(rows) != len(probabilities):
        raise CalibrationV22Error("family rows and prediction inputs do not align")
    predictions = []
    for prediction, probability in zip(base_predictions, probabilities, strict=True):
        updated = dict(prediction)
        updated["eligibility_probability"] = float(probability)
        predictions.append(updated)
    thresholds = sorted({0.0, 1.0, *map(float, probabilities)}) if supported else [1.0]
    frontier = []
    for threshold in thresholds:
        frontier.append(
            _threshold_metrics(rows, predictions, threshold)
        )
    eligible = [item for item in frontier if item["family_constraints_passed"]]
    if not eligible:
        raise CalibrationV22Error("family threshold frontier has no safe option")
    # Equal count outcomes are interchangeable to the joint optimizer. Preserve
    # the higher threshold as the frozen conservative/lexicographic tie-break.
    deduplicated: dict[tuple[int, int, int], dict[str, Any]] = {}
    for item in eligible:
        key = (
            int(item["correct_opportunity_count"]),
            int(item["abstained_nonopportunity_count"]),
            int(item["harmful_count"]),
        )
        previous = deduplicated.get(key)
        if previous is None or float(item["threshold"]) > float(previous["threshold"]):
            deduplicated[key] = item
    options = sorted(
        deduplicated.values(),
        key=lambda item: (
            float(item["threshold"]),
            int(item["correct_opportunity_count"]),
            int(item["abstained_nonopportunity_count"]),
            -int(item["harmful_count"]),
        ),
    )
    return frontier, options


def _aggregate_selection(
    selected: dict[str, dict[str, Any]],
    *,
    opportunity_count: int,
    nonopportunity_count: int,
) -> dict[str, Any]:
    correct = sum(int(item["correct_opportunity_count"]) for item in selected.values())
    abstained = sum(
        int(item["abstained_nonopportunity_count"]) for item in selected.values()
    )
    harmful = sum(int(item["harmful_count"]) for item in selected.values())
    recommendations = correct + harmful
    recall = correct / opportunity_count if opportunity_count else 1.0
    specificity = (
        abstained / nonopportunity_count if nonopportunity_count else 1.0
    )
    harmful_rate = harmful / recommendations if recommendations else 0.0
    balanced = (recall + specificity) / 2.0
    checks = {
        "maximum_harmful_recommendation_rate": (
            harmful_rate <= MAXIMUM_HARMFUL_RATE_V22
        ),
        "minimum_abstention_specificity": specificity >= MINIMUM_SPECIFICITY_V22,
        "minimum_balanced_actionable_accuracy": (
            balanced >= MINIMUM_BALANCED_ACCURACY_V22
        ),
        "family_constraints": all(
            item["family_constraints_passed"] for item in selected.values()
        ),
    }
    return {
        "opportunity_count": opportunity_count,
        "nonopportunity_count": nonopportunity_count,
        "correct_opportunity_count": correct,
        "abstained_nonopportunity_count": abstained,
        "recommendation_count": recommendations,
        "harmful_count": harmful,
        "opportunity_recall": recall,
        "abstention_specificity": specificity,
        "harmful_recommendation_rate": harmful_rate,
        "balanced_actionable_accuracy": balanced,
        "checks": checks,
        "passed": all(checks.values()),
    }


def select_joint_family_policy_v22(
    family_options: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if not family_options or any(not options for options in family_options.values()):
        raise CalibrationV22Error("joint optimizer requires options for every family")
    numpy, Bounds, LinearConstraint, milp = _scipy_milp()
    families = sorted(family_options)
    flat = [
        (family, option_index, option)
        for family in families
        for option_index, option in enumerate(family_options[family])
    ]
    count = len(flat)
    correct = numpy.asarray(
        [float(item[2]["correct_opportunity_count"]) for item in flat]
    )
    abstained = numpy.asarray(
        [float(item[2]["abstained_nonopportunity_count"]) for item in flat]
    )
    harmful = numpy.asarray([float(item[2]["harmful_count"]) for item in flat])
    opportunity_count = sum(
        int(family_options[family][0]["opportunity_count"]) for family in families
    )
    nonopportunity_count = sum(
        int(family_options[family][0]["non_opportunity_count"])
        for family in families
    )
    base_constraints = []
    for family in families:
        row = numpy.zeros(count)
        row[[index for index, item in enumerate(flat) if item[0] == family]] = 1.0
        base_constraints.append(LinearConstraint(row, 1.0, 1.0))
    safety_constraints = [
        # H / (C + H) <= 0.05 is equivalent to C - 19H >= 0.
        LinearConstraint(correct - 19.0 * harmful, 0.0, numpy.inf),
        LinearConstraint(
            abstained,
            math.ceil(MINIMUM_SPECIFICITY_V22 * nonopportunity_count - 1e-12),
            numpy.inf,
        ),
    ]
    balanced_row = 5.0 * (
        nonopportunity_count * correct + opportunity_count * abstained
    )
    balanced_minimum = 7.0 * opportunity_count * nonopportunity_count
    full_constraints = [
        *base_constraints,
        *safety_constraints,
        LinearConstraint(balanced_row, balanced_minimum, numpy.inf),
    ]
    bounds = Bounds(numpy.zeros(count), numpy.ones(count))
    integrality = numpy.ones(count)

    def solve(objective: Any, constraints: list[Any]) -> Any:
        return milp(
            c=objective,
            integrality=integrality,
            bounds=bounds,
            constraints=constraints,
            options={"mip_rel_gap": 0.0, "presolve": True, "time_limit": 120.0},
        )

    initial = solve(-correct, full_constraints)
    feasible = bool(initial.success)
    active = list(full_constraints if feasible else [*base_constraints, *safety_constraints])
    result = initial if feasible else solve(-correct, active)
    if not result.success:
        raise CalibrationV22Error(f"joint safety optimizer failed: {result.message}")

    # Hierarchical objective: recall, balanced/specificity, harmful rate, then
    # lexicographically higher thresholds by sorted family name.
    maximum_correct = int(round(float(correct @ result.x)))
    active.append(LinearConstraint(correct, maximum_correct, maximum_correct))
    result = solve(-abstained, active)
    maximum_abstained = int(round(float(abstained @ result.x)))
    active.append(
        LinearConstraint(abstained, maximum_abstained, maximum_abstained)
    )
    result = solve(harmful, active)
    minimum_harmful = int(round(float(harmful @ result.x)))
    active.append(LinearConstraint(harmful, minimum_harmful, minimum_harmful))
    for family in families:
        threshold_objective = numpy.zeros(count)
        for index, item in enumerate(flat):
            if item[0] == family:
                threshold_objective[index] = -float(item[2]["threshold"])
        result = solve(threshold_objective, active)
        if not result.success:
            raise CalibrationV22Error(
                f"joint threshold tie-break failed for {family}: {result.message}"
            )
        chosen = [
            index
            for index, value in enumerate(result.x)
            if value > 0.5 and flat[index][0] == family
        ]
        if len(chosen) != 1:
            raise CalibrationV22Error("joint optimizer returned an ambiguous family choice")
        selector = numpy.zeros(count)
        selector[chosen[0]] = 1.0
        active.append(LinearConstraint(selector, 1.0, 1.0))
    result = solve(numpy.zeros(count), active)
    if not result.success:
        raise CalibrationV22Error(f"joint optimizer finalization failed: {result.message}")
    selected = {}
    for index, value in enumerate(result.x):
        if value > 0.5:
            family, _, option = flat[index]
            selected[family] = option
    if set(selected) != set(families):
        raise CalibrationV22Error("joint optimizer did not select every family")
    aggregate = _aggregate_selection(
        selected,
        opportunity_count=opportunity_count,
        nonopportunity_count=nonopportunity_count,
    )
    return {
        "feasible": feasible and aggregate["passed"],
        "selection_kind": "full_policy" if feasible else "safety_frontier",
        "constraints": {
            "maximum_harmful_recommendation_rate": MAXIMUM_HARMFUL_RATE_V22,
            "minimum_abstention_specificity": MINIMUM_SPECIFICITY_V22,
            "minimum_balanced_actionable_accuracy": MINIMUM_BALANCED_ACCURACY_V22,
            "maximum_supported_family_harmful_rate": (
                MAXIMUM_FAMILY_HARMFUL_RATE_V22
            ),
            "minimum_supported_family_specificity": MINIMUM_FAMILY_SPECIFICITY_V22,
            "minimum_recommendations_for_family_harm_gate": (
                MINIMUM_FAMILY_RECOMMENDATIONS_FOR_HARM_GATE_V22
            ),
        },
        "optimizer": {
            "kind": "scipy_milp_highs_hierarchical",
            "family_order": families,
            "option_count": count,
            "objectives": [
                "maximum_global_opportunity_recall",
                "maximum_balanced_accuracy_and_specificity",
                "minimum_harmful_rate",
                "lexicographically_higher_family_thresholds",
            ],
        },
        "aggregate": aggregate,
        "selected": {
            family: {
                "threshold": float(item["threshold"]),
                "metrics": item,
            }
            for family, item in sorted(selected.items())
        },
    }


def _validate_rows_and_predictions(
    rows: list[dict[str, Any]], predictions: list[dict[str, Any]]
) -> None:
    if len(rows) != 2808 or len(predictions) != len(rows):
        raise CalibrationV22Error("V2.2 requires the frozen 2,808 aligned rows")
    for row, prediction in zip(rows, predictions, strict=True):
        identity = (
            row.get("case_id"),
            row.get("topology_signature"),
            row.get("template_id"),
        )
        predicted_identity = (
            prediction.get("case_id"),
            prediction.get("topology_signature"),
            prediction.get("template_id"),
        )
        if identity != predicted_identity:
            raise CalibrationV22Error("V2.1 row and grouped-OOF identity mismatch")
    if len({str(row["topology_signature"]) for row in rows}) != 936:
        raise CalibrationV22Error("V2.2 requires 936 frozen topology groups")


def train_v22_models(config: ProjectConfig) -> dict[str, Any]:
    try:
        import joblib
    except ImportError as exc:
        raise CalibrationV22Error("joblib is required for V2.2 model storage") from exc
    input_lock = verify_frozen_inputs_v22(config)
    input_paths = frozen_input_paths_v22(config)
    rows_payload = _load_json(input_paths["calibration_rows"], "calibration rows")
    oof_payload = _load_json(input_paths["grouped_oof"], "grouped OOF")
    rows = list(rows_payload.get("rows") or [])
    base_predictions = list(oof_payload.get("predictions") or [])
    _validate_rows_and_predictions(rows, base_predictions)

    families = sorted({str(row["family"]) for row in rows})
    if len(families) != 9:
        raise CalibrationV22Error(f"expected nine V2.2 families, got {len(families)}")
    family_models = {}
    family_support = {}
    family_probabilities = [0.0] * len(rows)
    frontiers = {}
    optimizer_options = {}
    for family in families:
        indices = [index for index, row in enumerate(rows) if row["family"] == family]
        family_rows = [rows[index] for index in indices]
        family_base = [base_predictions[index] for index in indices]
        probabilities, support, model = grouped_family_oof_v22(family_rows)
        family_support[family] = support
        if model is not None:
            family_models[family] = model
        for index, probability in zip(indices, probabilities, strict=True):
            family_probabilities[index] = probability
        frontier, options = build_family_frontier_v22(
            family_rows,
            family_base,
            probabilities,
            supported=bool(support["supported"]),
        )
        frontiers[family] = frontier
        optimizer_options[family] = options

    policy = select_joint_family_policy_v22(optimizer_options)
    policy_core = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V22,
        "selection_source": "family_grouped_out_of_fold_calibration_only",
        "blind_labels_used": False,
        "support_floor": MINIMUM_FAMILY_OPPORTUNITIES_V22,
        "family_support": family_support,
        **policy,
    }
    policy_payload = {**policy_core, "policy_hash": _stable_hash(policy_core)}

    root = config.artifacts_dir / "models/v22"
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "input-lock.json", input_lock)
    _write_json(
        root / "family-grouped-oof.json",
        {
            "schema_version": 1,
            "flow_version": CALIBRATION_FLOW_VERSION_V22,
            "row_count": len(rows),
            "predictions": [
                {
                    "case_id": row["case_id"],
                    "topology_signature": row["topology_signature"],
                    "family": row["family"],
                    "template_id": row["template_id"],
                    "eligible": row["eligible"],
                    "eligibility_probability": probability,
                }
                for row, probability in zip(rows, family_probabilities, strict=True)
            ],
        },
    )
    _write_json(
        root / "family-threshold-frontier.json",
        {
            "schema_version": 1,
            "flow_version": CALIBRATION_FLOW_VERSION_V22,
            "families": frontiers,
            "frontier_hash": _stable_hash(frontiers),
        },
    )
    _write_json(root / "policy.json", policy_payload)
    bundle = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V22,
        "feature_order": FEATURE_ORDER_V21,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "family_classifiers": family_models,
        "family_thresholds": {
            family: float(item["threshold"])
            for family, item in (
                (family, policy_payload["selected"][family]) for family in families
            )
        },
        "unsupported_families": [
            family for family in families if not family_support[family]["supported"]
        ],
        "input_lock_hash": input_lock["input_lock_hash"],
        "policy_hash": policy_payload["policy_hash"],
    }
    bundle_path = root / "family-model-bundle.joblib"
    joblib.dump(bundle, bundle_path)
    metadata_core = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V22,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "random_seed": MODEL_RANDOM_SEED_V22,
        "row_count": len(rows),
        "topology_group_count": 936,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "input_lock_hash": input_lock["input_lock_hash"],
        "policy_hash": policy_payload["policy_hash"],
        "bundle_path": str(bundle_path.resolve()),
        "bundle_sha256": _file_hash(bundle_path),
        "supported_families": sorted(family_models),
        "unsupported_families": bundle["unsupported_families"],
        "hyperparameters": {
            "n_estimators": RF_ESTIMATORS_V22,
            "max_depth": RF_MAX_DEPTH_V22,
            "min_samples_leaf": RF_MIN_LEAF_V22,
            "max_features": "sqrt",
            "bootstrap": True,
            "classifier_class_weight": "balanced_subsample",
            "group_folds": GROUP_FOLDS_V22,
            "minimum_family_opportunities": MINIMUM_FAMILY_OPPORTUNITIES_V22,
        },
        "risk_policy_feasible": policy_payload["feasible"],
        "direction_policy_feasible": True,
        "physical_evidence_feasible": True,
        "blind_labels_used": False,
    }
    metadata = {**metadata_core, "metadata_hash": _stable_hash(metadata_core)}
    _write_json(root / "metadata.json", metadata)
    summary = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V22,
        "status": "passed" if policy_payload["feasible"] else "calibration_gate_failed",
        "row_count": len(rows),
        "risk_policy_feasible": policy_payload["feasible"],
        "direction_policy_feasible": True,
        "physical_evidence_feasible": True,
        "metadata_hash": metadata["metadata_hash"],
        "policy_hash": policy_payload["policy_hash"],
        "input_lock_hash": input_lock["input_lock_hash"],
    }
    _write_json(root / "summary.json", summary)
    build_v22_calibration_report(config)
    return summary


def build_v22_calibration_report(config: ProjectConfig) -> dict[str, Any]:
    root = config.artifacts_dir / "models/v22"
    summary = _load_json(root / "summary.json", "V2.2 summary")
    metadata = _load_json(root / "metadata.json", "V2.2 metadata")
    policy = _load_json(root / "policy.json", "V2.2 policy")
    input_lock = _load_json(root / "input-lock.json", "V2.2 input lock")
    core = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V22,
        "status": summary["status"],
        "blind_labels_used": False,
        "input_lock_hash": input_lock["input_lock_hash"],
        "metadata_hash": metadata["metadata_hash"],
        "policy_hash": policy["policy_hash"],
        "selection_kind": policy["selection_kind"],
        "family_support": policy["family_support"],
        "aggregate": policy["aggregate"],
        "selected": policy["selected"],
        "artifacts": {
            name: {
                "path": str((root / name).resolve()),
                "sha256": _file_hash(root / name),
            }
            for name in (
                "family-grouped-oof.json",
                "family-model-bundle.joblib",
                "family-threshold-frontier.json",
                "input-lock.json",
                "metadata.json",
                "policy.json",
                "summary.json",
            )
        },
    }
    report = {**core, "report_hash": _stable_hash(core)}
    report["json_path"] = str((root / "calibration-report.json").resolve())
    report["markdown_path"] = str((root / "calibration-report.md").resolve())
    _write_json(root / "calibration-report.json", report)
    aggregate = policy["aggregate"]
    lines = [
        "# RTL Advisor V2.2 Calibration Report",
        "",
        f"Calibration policy: **{'PASS' if policy['feasible'] else 'FAIL'}**",
        "",
        "> Family-grouped out-of-fold calibration evidence only. No blind labels were used.",
        "",
        f"- Selection: `{policy['selection_kind']}`",
        f"- Training rows: {metadata['row_count']}",
        f"- Topology groups: {metadata['topology_group_count']}",
        f"- Opportunity recall: {aggregate['opportunity_recall']:.1%}",
        f"- Abstention specificity: {aggregate['abstention_specificity']:.1%}",
        f"- Harmful recommendation rate: {aggregate['harmful_recommendation_rate']:.1%}",
        f"- Balanced actionable accuracy: {aggregate['balanced_actionable_accuracy']:.1%}",
        "",
        "## Family policy",
        "",
        "| Family | Supported | Opportunities | Threshold | Recall | Specificity | Harmful | Balanced |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for family in sorted(policy["selected"]):
        support = policy["family_support"][family]
        item = policy["selected"][family]
        metrics = item["metrics"]
        lines.append(
            f"| {family} | {'yes' if support['supported'] else 'no'} | "
            f"{support['opportunity_count']} | {item['threshold']:.6f} | "
            f"{metrics['opportunity_recall']:.1%} | "
            f"{metrics['abstention_specificity']:.1%} | "
            f"{metrics['harmful_recommendation_rate']:.1%} | "
            f"{metrics['balanced_actionable_accuracy']:.1%} |"
        )
    lines.extend(
        (
            "",
            "V2.2 remains diagnostic-only unless the calibration policy passes.",
            "",
            f"Report hash: `{report['report_hash']}`",
            "",
        )
    )
    (root / "calibration-report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
