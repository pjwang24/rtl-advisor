from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from rtl_advisor.advisor_v2 import PROFILES, TRANSFORMATION_FAMILIES
from rtl_advisor.calibration import registered_topology_context
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import load_manifest
from rtl_advisor.features_v21 import (
    FEATURE_ORDER_V21,
    FEATURE_SCHEMA_HASH_V21,
    FEATURE_SCHEMA_VERSION_V21,
    FEATURE_TYPES_V21,
    candidate_features_v21,
    extract_case_kernel_features,
    fit_family_ood,
)
from rtl_advisor.rules_v21 import analyze_rules_v21
from rtl_advisor.v2_corpus import V2_SUITE_SCHEMA_VERSION
from rtl_advisor.v21_corpus import V21_SUITE_SCHEMA_VERSION


CALIBRATION_FLOW_VERSION_V21 = "rtl-advisor-calibration-v21"
MODEL_RANDOM_SEED_V21 = 20260715
RF_ESTIMATORS_V21 = 500
RF_MAX_DEPTH_V21 = 12
RF_MIN_LEAF_V21 = 3
GROUP_FOLDS_V21 = 5
METRICS = ("delay", "area", "cell_count")
DIRECTIONS = ("improve", "neutral", "degrade")


class CalibrationV21Error(RuntimeError):
    """Raised when V2.1 training evidence violates its frozen contract."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_suite(path: Path, expected_split: str) -> dict[str, Any]:
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationV21Error(f"invalid training suite {path}: {exc}") from exc
    expected_schema = (
        V2_SUITE_SCHEMA_VERSION
        if expected_split == "calibration-v2"
        else V21_SUITE_SCHEMA_VERSION
    )
    if suite.get("schema_version") != expected_schema:
        raise CalibrationV21Error(f"wrong suite schema for {expected_split}")
    if suite.get("split") != expected_split:
        raise CalibrationV21Error(f"expected {expected_split} at {path}")
    return suite


def _transformation(family: str) -> str:
    matches = [
        transformation
        for transformation, registered_family in TRANSFORMATION_FAMILIES.items()
        if registered_family == family
    ]
    if len(matches) != 1:
        raise CalibrationV21Error(f"family has no unique transformation: {family}")
    return matches[0]


def _synthesis_summary(config: ProjectConfig, case_id: str) -> dict[str, Any]:
    path = config.artifacts_dir / "cases" / case_id / "synthesis/summary.json"
    if not path.is_file():
        raise CalibrationV21Error(f"synthesis ground truth missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationV21Error(f"invalid synthesis summary {path}: {exc}") from exc


def collect_v21_rows(
    config: ProjectConfig,
    *,
    v2_suite_path: str | Path,
    v21_suite_path: str | Path,
    force_graph: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    suite_specs = (
        (Path(v2_suite_path).expanduser().resolve(), "calibration-v2"),
        (Path(v21_suite_path).expanduser().resolve(), "calibration-v21"),
    )
    suites = [(path, _load_suite(path, split), split) for path, split in suite_specs]
    rows = []
    source_counts = {}
    for suite_path, suite, split in suites:
        source_counts[split] = 0
        for case in suite.get("cases") or []:
            manifest = load_manifest(suite_path.parent / str(case["manifest"]))
            extraction = extract_case_kernel_features(
                config,
                manifest,
                manifest.baseline_id,
                force_graph=force_graph,
            )
            analysis = analyze_rules_v21(extraction["graph"], extraction["syntax_facts"])
            transformation = _transformation(manifest.family)
            findings = [
                finding
                for finding in analysis.get("findings") or []
                if finding.get("transformation_id") == transformation
            ]
            finding = (
                findings[0]
                if findings
                else registered_topology_context(manifest.family, case["topology"])
            )
            summary = _synthesis_summary(config, manifest.case_id)
            comparisons = {
                comparison["candidate_id"]: comparison
                for comparison in summary.get("comparisons") or []
            }
            for template_id in ("v1", "v2", "v3"):
                try:
                    comparison = comparisons[template_id]
                except KeyError as exc:
                    raise CalibrationV21Error(
                        f"missing {template_id} synthesis comparison for {manifest.case_id}"
                    ) from exc
                targets = {
                    "delay": float(
                        comparison["critical_delay_ps"]["improvement_percent"]
                    ),
                    "area": float(comparison["area_total"]["improvement_percent"]),
                    "cell_count": float(
                        comparison["cell_count"]["improvement_percent"]
                    ),
                }
                rows.append(
                    {
                        "case_id": manifest.case_id,
                        "training_split": split,
                        "topology_signature": case["topology_signature"],
                        "family": manifest.family,
                        "transformation_id": transformation,
                        "template_id": template_id,
                        "detection": (
                            "registered_rule_v21"
                            if findings
                            else "registered_topology_context"
                        ),
                        "kernel_top": extraction["kernel_top"],
                        "kernel_feature_hash": extraction["feature_hash"],
                        "syntax_hash": extraction["syntax_hash"],
                        "features": candidate_features_v21(
                            extraction["features"], finding, template_id
                        ),
                        "targets": targets,
                        "direction_labels": {
                            metric: _direction_label(value)
                            for metric, value in targets.items()
                        },
                        "eligible": PROFILES["balanced"].eligible(
                            targets["delay"], targets["area"]
                        ),
                    }
                )
                source_counts[split] += 1
    expected = (360 + 576) * 3
    if len(rows) != expected:
        raise CalibrationV21Error(f"expected {expected} combined rows, got {len(rows)}")
    evidence = {
        "suite_hashes": {
            split: suite["suite_hash"] for _, suite, split in suites
        },
        "source_row_counts": source_counts,
        "row_count": len(rows),
        "combined_training_hash": _stable_hash(
            {
                "suite_hashes": {
                    split: suite["suite_hash"] for _, suite, split in suites
                },
                "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
                "row_identities": [
                    [row["case_id"], row["template_id"], row["kernel_feature_hash"]]
                    for row in rows
                ],
            }
        ),
    }
    return rows, evidence


def _direction_label(value: float) -> str:
    if value > 1.0:
        return "improve"
    if value < -1.0:
        return "degrade"
    return "neutral"


def _sklearn() -> tuple[Any, Any, Any]:
    try:
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        from sklearn.model_selection import GroupKFold
    except ImportError as exc:
        raise CalibrationV21Error("scikit-learn is required for V2.1 training") from exc
    return RandomForestClassifier, RandomForestRegressor, GroupKFold


def _matrix(rows: Iterable[dict[str, Any]]) -> list[list[float]]:
    return [
        [float(row["features"].get(feature, 0.0)) for feature in FEATURE_ORDER_V21]
        for row in rows
    ]


def _classifier_probability(estimator: Any, values: Any, label: Any) -> list[float]:
    classes = list(estimator.classes_)
    if label not in classes:
        return [0.0] * len(values)
    index = classes.index(label)
    return [float(row[index]) for row in estimator.predict_proba(values)]


def _case_policy_metrics(
    rows: list[dict[str, Any]],
    oof: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    by_case: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_case[str(row["case_id"])].append(index)
    opportunity_count = 0
    correct_opportunity = 0
    non_opportunity_count = 0
    abstained_non_opportunity = 0
    recommendation_count = 0
    harmful_count = 0
    decisions = []
    for case_id, indices in sorted(by_case.items()):
        opportunity = any(bool(rows[index]["eligible"]) for index in indices)
        opportunity_count += int(opportunity)
        non_opportunity_count += int(not opportunity)
        qualified = [
            index for index in indices if float(oof[index]["eligibility_probability"]) >= threshold
        ]
        selected = (
            max(
                qualified,
                key=lambda index: (
                    PROFILES["balanced"].utility(
                        float(oof[index]["regression"]["delay"]),
                        float(oof[index]["regression"]["area"]),
                        float(oof[index]["regression"]["cell_count"]),
                    ),
                    rows[index]["template_id"],
                ),
            )
            if qualified
            else None
        )
        recommended = selected is not None
        correct = bool(recommended and rows[selected]["eligible"])
        harmful = bool(recommended and not rows[selected]["eligible"])
        recommendation_count += int(recommended)
        harmful_count += int(harmful)
        correct_opportunity += int(opportunity and correct)
        abstained_non_opportunity += int(not opportunity and not recommended)
        decisions.append(
            {
                "case_id": case_id,
                "opportunity": opportunity,
                "selected_template": rows[selected]["template_id"] if selected is not None else None,
                "correct": correct,
                "harmful": harmful,
            }
        )
    opportunity_recall = (
        correct_opportunity / opportunity_count if opportunity_count else 1.0
    )
    specificity = (
        abstained_non_opportunity / non_opportunity_count
        if non_opportunity_count
        else 1.0
    )
    harmful_rate = harmful_count / recommendation_count if recommendation_count else 0.0
    return {
        "threshold": threshold,
        "case_count": len(by_case),
        "opportunity_count": opportunity_count,
        "non_opportunity_count": non_opportunity_count,
        "recommendation_count": recommendation_count,
        "opportunity_recall": opportunity_recall,
        "abstention_specificity": specificity,
        "harmful_recommendation_rate": harmful_rate,
        "balanced_actionable_accuracy": (opportunity_recall + specificity) / 2.0,
        "decisions": decisions,
    }


def select_risk_threshold(
    rows: list[dict[str, Any]], oof: list[dict[str, Any]]
) -> dict[str, Any]:
    thresholds = sorted(
        {0.0, 1.0, *(float(item["eligibility_probability"]) for item in oof)}
    )
    evaluations = [_case_policy_metrics(rows, oof, threshold) for threshold in thresholds]
    feasible = [
        item
        for item in evaluations
        if item["harmful_recommendation_rate"] <= 0.05
        and item["abstention_specificity"] >= 0.90
        and item["balanced_actionable_accuracy"] >= 0.70
    ]
    safety_feasible = [
        item
        for item in evaluations
        if item["harmful_recommendation_rate"] <= 0.05
        and item["abstention_specificity"] >= 0.90
    ]
    # If the full policy is infeasible, persist the safest useful frontier
    # rather than an unsafe high-coverage threshold. The model remains marked
    # non-deployable either way.
    pool = feasible or safety_feasible or evaluations
    selected = max(
        pool,
        key=lambda item: (
            item["opportunity_recall"],
            item["balanced_actionable_accuracy"],
            item["abstention_specificity"],
            -item["harmful_recommendation_rate"],
            -item["threshold"],
        ),
    )
    return {
        "feasible": bool(feasible),
        "safety_constraints_feasible": bool(safety_feasible),
        "constraints": {
            "maximum_harmful_recommendation_rate": 0.05,
            "minimum_abstention_specificity": 0.90,
            "minimum_balanced_actionable_accuracy": 0.70,
        },
        "selected": selected,
        "evaluated_threshold_count": len(evaluations),
    }


def select_direction_threshold(
    true_labels: list[str],
    predicted_labels: list[str],
    confidence: list[float],
) -> dict[str, Any]:
    thresholds = sorted({0.0, 1.0, *confidence})
    evaluations = []
    for threshold in thresholds:
        covered = [index for index, value in enumerate(confidence) if value >= threshold]
        accuracy = (
            sum(predicted_labels[index] == true_labels[index] for index in covered)
            / len(covered)
            if covered
            else 0.0
        )
        evaluations.append(
            {
                "threshold": threshold,
                "coverage": len(covered) / len(true_labels) if true_labels else 0.0,
                "accuracy": accuracy,
                "covered_count": len(covered),
            }
        )
    feasible = [
        item
        for item in evaluations
        if item["coverage"] >= 0.90 and item["accuracy"] >= 0.70
    ]
    pool = feasible or evaluations
    selected = max(
        pool,
        key=lambda item: (item["coverage"], item["accuracy"], -item["threshold"]),
    )
    return {
        "feasible": bool(feasible),
        "minimum_coverage": 0.90,
        "minimum_accuracy": 0.70,
        "selected": selected,
    }


def train_v21_from_rows(
    rows: list[dict[str, Any]],
    *,
    training_evidence: dict[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    RandomForestClassifier, RandomForestRegressor, GroupKFold = _sklearn()
    try:
        import joblib
    except ImportError as exc:
        raise CalibrationV21Error("joblib is required for V2.1 model storage") from exc
    if len({row["topology_signature"] for row in rows}) < GROUP_FOLDS_V21:
        raise CalibrationV21Error("V2.1 training requires at least five topology groups")
    x_values = _matrix(rows)
    groups = [row["topology_signature"] for row in rows]
    splitter = GroupKFold(n_splits=GROUP_FOLDS_V21)
    splits = list(splitter.split(x_values, groups=groups))
    oof = [
        {
            "regression": {},
            "directions": {},
            "eligibility_probability": 0.0,
        }
        for _ in rows
    ]

    def regressor() -> Any:
        return RandomForestRegressor(
            n_estimators=RF_ESTIMATORS_V21,
            max_depth=RF_MAX_DEPTH_V21,
            min_samples_leaf=RF_MIN_LEAF_V21,
            max_features="sqrt",
            bootstrap=True,
            random_state=MODEL_RANDOM_SEED_V21,
            n_jobs=-1,
        )

    def classifier() -> Any:
        return RandomForestClassifier(
            n_estimators=RF_ESTIMATORS_V21,
            max_depth=RF_MAX_DEPTH_V21,
            min_samples_leaf=RF_MIN_LEAF_V21,
            max_features="sqrt",
            bootstrap=True,
            class_weight="balanced_subsample",
            random_state=MODEL_RANDOM_SEED_V21,
            n_jobs=-1,
        )

    for train_indices, test_indices in splits:
        train_x = [x_values[int(index)] for index in train_indices]
        test_x = [x_values[int(index)] for index in test_indices]
        for metric in METRICS:
            reg = regressor()
            reg.fit(train_x, [float(rows[int(index)]["targets"][metric]) for index in train_indices])
            reg_predictions = reg.predict(test_x)
            direction = classifier()
            direction.fit(
                train_x,
                [rows[int(index)]["direction_labels"][metric] for index in train_indices],
            )
            direction_predictions = direction.predict(test_x)
            direction_probabilities = direction.predict_proba(test_x)
            for offset, row_index in enumerate(test_indices):
                index = int(row_index)
                oof[index]["regression"][metric] = float(reg_predictions[offset])
                label = str(direction_predictions[offset])
                class_index = list(direction.classes_).index(label)
                oof[index]["directions"][metric] = {
                    "label": label,
                    "confidence": float(direction_probabilities[offset][class_index]),
                }
        eligibility = classifier()
        eligibility.fit(
            train_x,
            [bool(rows[int(index)]["eligible"]) for index in train_indices],
        )
        probabilities = _classifier_probability(eligibility, test_x, True)
        for row_index, probability in zip(test_indices, probabilities, strict=True):
            oof[int(row_index)]["eligibility_probability"] = probability

    risk_policy = select_risk_threshold(rows, oof)
    direction_policy = {
        metric: select_direction_threshold(
            [row["direction_labels"][metric] for row in rows],
            [item["directions"][metric]["label"] for item in oof],
            [float(item["directions"][metric]["confidence"]) for item in oof],
        )
        for metric in METRICS
    }
    final_regressors = {}
    final_direction_classifiers = {}
    for metric in METRICS:
        final_regressors[metric] = regressor()
        final_regressors[metric].fit(
            x_values, [float(row["targets"][metric]) for row in rows]
        )
        final_direction_classifiers[metric] = classifier()
        final_direction_classifiers[metric].fit(
            x_values, [row["direction_labels"][metric] for row in rows]
        )
    final_eligibility = classifier()
    final_eligibility.fit(x_values, [bool(row["eligible"]) for row in rows])
    ood_model = fit_family_ood(rows)

    output_root.mkdir(parents=True, exist_ok=True)
    bundle_path = output_root / "model-bundle.joblib"
    bundle = {
        "flow_version": CALIBRATION_FLOW_VERSION_V21,
        "feature_order": FEATURE_ORDER_V21,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "regressors": final_regressors,
        "direction_classifiers": final_direction_classifiers,
        "eligibility_classifier": final_eligibility,
        "risk_threshold": risk_policy["selected"]["threshold"],
        "direction_thresholds": {
            metric: policy["selected"]["threshold"]
            for metric, policy in direction_policy.items()
        },
        "training_evidence": training_evidence,
    }
    joblib.dump(bundle, bundle_path)
    bundle_hash = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    oof_path = output_root / "grouped-oof.json"
    _write_json(
        oof_path,
        {
            "flow_version": CALIBRATION_FLOW_VERSION_V21,
            "row_count": len(rows),
            "predictions": [
                {
                    "case_id": row["case_id"],
                    "topology_signature": row["topology_signature"],
                    "template_id": row["template_id"],
                    "targets": row["targets"],
                    "eligible": row["eligible"],
                    **prediction,
                }
                for row, prediction in zip(rows, oof, strict=True)
            ],
        },
    )
    policy = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V21,
        "selection_source": "grouped_out_of_fold_only",
        "risk": risk_policy,
        "direction": direction_policy,
    }
    policy["policy_hash"] = _stable_hash(policy)
    _write_json(output_root / "policy.json", policy)
    _write_json(output_root / "ood.json", ood_model)
    metadata = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V21,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "random_seed": MODEL_RANDOM_SEED_V21,
        "row_count": len(rows),
        "topology_group_count": len(set(groups)),
        "feature_schema_version": FEATURE_SCHEMA_VERSION_V21,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "feature_order": list(FEATURE_ORDER_V21),
        "feature_types": FEATURE_TYPES_V21,
        "training_evidence": training_evidence,
        "bundle_path": str(bundle_path),
        "bundle_sha256": bundle_hash,
        "ood_model_hash": ood_model["model_hash"],
        "policy_hash": policy["policy_hash"],
        "hyperparameters": {
            "n_estimators": RF_ESTIMATORS_V21,
            "max_depth": RF_MAX_DEPTH_V21,
            "min_samples_leaf": RF_MIN_LEAF_V21,
            "max_features": "sqrt",
            "bootstrap": True,
            "classifier_class_weight": "balanced_subsample",
            "group_folds": GROUP_FOLDS_V21,
        },
        "model_count": 7,
        "risk_policy_feasible": risk_policy["feasible"],
        "direction_policy_feasible": all(
            item["feasible"] for item in direction_policy.values()
        ),
    }
    metadata["metadata_hash"] = _stable_hash(metadata)
    _write_json(output_root / "metadata.json", metadata)
    return metadata


def train_v21_models(
    config: ProjectConfig,
    *,
    force_graph: bool = False,
) -> dict[str, Any]:
    rows, evidence = collect_v21_rows(
        config,
        v2_suite_path=config.corpus_dir / "calibration-v2/suite.json",
        v21_suite_path=config.corpus_dir / "calibration-v21/suite.json",
        force_graph=force_graph,
    )
    root = config.artifacts_dir / "models/v21"
    _write_json(
        root / "calibration-rows.json",
        {
            "flow_version": CALIBRATION_FLOW_VERSION_V21,
            "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
            "training_evidence": evidence,
            "row_count": len(rows),
            "rows": rows,
        },
    )
    metadata = train_v21_from_rows(
        rows,
        training_evidence=evidence,
        output_root=root,
    )
    result = {
        "status": (
            "passed"
            if metadata["risk_policy_feasible"]
            and metadata["direction_policy_feasible"]
            else "calibration_gate_failed"
        ),
        "flow_version": CALIBRATION_FLOW_VERSION_V21,
        "row_count": len(rows),
        "metadata_path": str(root / "metadata.json"),
        "metadata_hash": metadata["metadata_hash"],
        "rows_path": str(root / "calibration-rows.json"),
    }
    _write_json(root / "summary.json", result)
    build_v21_calibration_report(config)
    return result


def build_v21_calibration_report(config: ProjectConfig) -> dict[str, Any]:
    root = config.artifacts_dir / "models/v21"

    def load(name: str) -> dict[str, Any]:
        path = root / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CalibrationV21Error(f"invalid V2.1 model artifact {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise CalibrationV21Error(f"expected object in {path}")
        return value

    summary = load("summary.json")
    metadata = load("metadata.json")
    policy = load("policy.json")
    rows = load("calibration-rows.json")
    risk = policy["risk"]
    directions = policy["direction"]
    core = {
        "schema_version": 1,
        "flow_version": CALIBRATION_FLOW_VERSION_V21,
        "status": summary["status"],
        "blind_labels_used": False,
        "row_count": rows["row_count"],
        "topology_group_count": metadata["topology_group_count"],
        "training_evidence": metadata["training_evidence"],
        "feature_schema_hash": metadata["feature_schema_hash"],
        "risk_policy": risk,
        "direction_policy": directions,
        "artifacts": {
            name: {
                "path": str((root / name).resolve()),
                "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            }
            for name in (
                "calibration-rows.json",
                "grouped-oof.json",
                "model-bundle.joblib",
                "metadata.json",
                "ood.json",
                "policy.json",
                "summary.json",
            )
        },
    }
    report = {**core, "report_hash": _stable_hash(core)}
    report["json_path"] = str((root / "calibration-report.json").resolve())
    report["markdown_path"] = str((root / "calibration-report.md").resolve())
    _write_json(root / "calibration-report.json", report)
    selected = risk["selected"]
    lines = [
        "# RTL Advisor V2.1 Calibration Report",
        "",
        f"Calibration policy: **{'PASS' if risk['feasible'] and all(item['feasible'] for item in directions.values()) else 'FAIL'}**",
        "",
        "> Calibration evidence only. No V2 or V2.1 blind synthesis labels were used.",
        "",
        f"- Training rows: {rows['row_count']}",
        f"- Topology groups: {metadata['topology_group_count']}",
        f"- Risk opportunity recall: {selected['opportunity_recall']:.1%}",
        f"- Risk abstention specificity: {selected['abstention_specificity']:.1%}",
        f"- Risk harmful recommendation rate: {selected['harmful_recommendation_rate']:.1%}",
        f"- Risk balanced actionable accuracy: {selected['balanced_actionable_accuracy']:.1%}",
        "",
        "## Direction grouped-OOF results",
        "",
        "| Metric | Feasible | Accuracy | Coverage |",
        "|---|---:|---:|---:|",
    ]
    for metric in METRICS:
        item = directions[metric]
        lines.append(
            f"| {metric} | {'yes' if item['feasible'] else 'no'} | "
            f"{item['selected']['accuracy']:.1%} | {item['selected']['coverage']:.1%} |"
        )
    lines.extend(("", f"Report hash: `{report['report_hash']}`", ""))
    (root / "calibration-report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def diagnose_v21_calibration_risk(config: ProjectConfig) -> dict[str, Any]:
    root = config.artifacts_dir / "models/v21"
    try:
        rows_payload = json.loads((root / "calibration-rows.json").read_text())
        oof_payload = json.loads((root / "grouped-oof.json").read_text())
        policy = json.loads((root / "policy.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationV21Error(f"invalid V2.1 calibration diagnostic input: {exc}") from exc
    rows = rows_payload.get("rows") or []
    predictions = oof_payload.get("predictions") or []
    if len(rows) != len(predictions) or not rows:
        raise CalibrationV21Error("V2.1 calibration rows/OOF predictions do not align")
    per_family = {}
    for family in sorted({str(row["family"]) for row in rows}):
        indices = [index for index, row in enumerate(rows) if row["family"] == family]
        family_rows = [rows[index] for index in indices]
        family_predictions = [predictions[index] for index in indices]
        risk = select_risk_threshold(family_rows, family_predictions)
        directions = {}
        for metric in METRICS:
            correct = sum(
                row["direction_labels"][metric]
                == prediction["directions"][metric]["label"]
                for row, prediction in zip(
                    family_rows, family_predictions, strict=True
                )
            )
            directions[metric] = correct / len(family_rows)
        topology_context_cases = sorted(
            {
                row["case_id"]
                for row in family_rows
                if row["detection"] == "registered_topology_context"
            }
        )
        per_family[family] = {
            "candidate_row_count": len(family_rows),
            "case_count": len({row["case_id"] for row in family_rows}),
            "eligible_candidate_fraction": sum(row["eligible"] for row in family_rows)
            / len(family_rows),
            "risk_policy": risk,
            "direction_accuracy": directions,
            "training_context_case_count": len(topology_context_cases),
            "training_context_cases": topology_context_cases,
        }
    bins = []
    for lower in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        upper = lower + 0.1
        indices = [
            index
            for index, prediction in enumerate(predictions)
            if lower <= float(prediction["eligibility_probability"])
            < (upper if upper < 1.0 else 1.000000001)
        ]
        bins.append(
            {
                "lower": lower,
                "upper": min(1.0, upper),
                "candidate_count": len(indices),
                "measured_eligible_fraction": (
                    sum(rows[index]["eligible"] for index in indices) / len(indices)
                    if indices
                    else None
                ),
            }
        )
    core = {
        "schema_version": 1,
        "flow_version": "rtl-advisor-calibration-risk-diagnostic-v21",
        "source": "calibration grouped-OOF predictions only",
        "blind_labels_used": False,
        "row_count": len(rows),
        "global_risk_policy": policy["risk"],
        "probability_reliability_bins": bins,
        "per_family": per_family,
    }
    report = {**core, "diagnostic_hash": _stable_hash(core)}
    report["json_path"] = str((root / "risk-diagnostics.json").resolve())
    report["markdown_path"] = str((root / "risk-diagnostics.md").resolve())
    _write_json(root / "risk-diagnostics.json", report)
    lines = [
        "# RTL Advisor V2.1 Calibration Risk Diagnostic",
        "",
        "> Grouped-OOF calibration evidence only; no blind labels were used.",
        "",
        "The direction models are viable, but the global eligibility classifier "
        "does not achieve the required precision/coverage frontier.",
        "",
        "| Family | Opportunities | Safe recall | Specificity | Harmful | Balanced |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for family, item in per_family.items():
        selected = item["risk_policy"]["selected"]
        lines.append(
            f"| {family} | {selected['opportunity_count']} | "
            f"{selected['opportunity_recall']:.1%} | "
            f"{selected['abstention_specificity']:.1%} | "
            f"{selected['harmful_recommendation_rate']:.1%} | "
            f"{selected['balanced_actionable_accuracy']:.1%} |"
        )
    lines.extend(
        (
            "",
            "## Next-version implication",
            "",
            "Keep the direction stack. Replace the monolithic eligibility policy "
            "with preregistered family-aware selective-risk calibration, then generate "
            "a new disjoint blind suite before evaluating it.",
            "",
            f"Diagnostic hash: `{report['diagnostic_hash']}`",
            "",
        )
    )
    (root / "risk-diagnostics.md").write_text("\n".join(lines), encoding="utf-8")
    return report
