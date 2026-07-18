from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from rtl_advisor.advisor_v2 import PROFILES, TRANSFORMATION_FAMILIES
from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest
from rtl_advisor.features_v21 import (
    FEATURE_ORDER_V21,
    FEATURE_SCHEMA_HASH_V21,
    candidate_features_v21,
    extract_case_kernel_features,
    score_family_ood,
)
from rtl_advisor.rules_v21 import RULESET_VERSION_V21, analyze_rules_v21
from rtl_advisor.rtl_input import normalize_design_input


ANALYSIS_SCHEMA_VERSION_V21 = 21
ADVISOR_FLOW_VERSION_V21 = "rtl-advisor-safe-decision-v21"


class AdvisorV21Error(RuntimeError):
    """Raised when the deterministic V2.1 advisor cannot reproduce its model."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorV21Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdvisorV21Error(f"expected JSON object for {description}: {path}")
    return payload


def load_model_bundle_v21(config: ProjectConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    root = config.artifacts_dir / "models/v21"
    metadata_path = root / "metadata.json"
    bundle_path = root / "model-bundle.joblib"
    ood_path = root / "ood.json"
    metadata = _load_json(metadata_path, "V2.1 model metadata")
    if metadata.get("feature_schema_hash") != FEATURE_SCHEMA_HASH_V21:
        raise AdvisorV21Error("V2.1 model feature schema mismatch")
    if not (
        metadata.get("risk_policy_feasible") is True
        and metadata.get("direction_policy_feasible") is True
    ):
        raise AdvisorV21Error(
            "V2.1 model is diagnostic-only because its calibration policy gates failed"
        )
    if not bundle_path.is_file():
        raise AdvisorV21Error(f"V2.1 model bundle missing: {bundle_path}")
    actual_hash = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    if actual_hash != metadata.get("bundle_sha256"):
        raise AdvisorV21Error("V2.1 model bundle hash mismatch")
    try:
        import joblib

        bundle = joblib.load(bundle_path)
    except Exception as exc:
        raise AdvisorV21Error(f"could not load V2.1 model bundle: {exc}") from exc
    if (
        bundle.get("feature_schema_hash") != FEATURE_SCHEMA_HASH_V21
        or tuple(bundle.get("feature_order") or ()) != FEATURE_ORDER_V21
    ):
        raise AdvisorV21Error("V2.1 model bundle runtime contract mismatch")
    ood = _load_json(ood_path, "V2.1 OOD model")
    if ood.get("model_hash") != metadata.get("ood_model_hash"):
        raise AdvisorV21Error("V2.1 OOD model hash mismatch")
    ood_core = {key: value for key, value in ood.items() if key != "model_hash"}
    if ood.get("model_hash") != _stable_hash(ood_core):
        raise AdvisorV21Error("V2.1 OOD model content hash mismatch")
    metadata_core = {
        key: value for key, value in metadata.items() if key != "metadata_hash"
    }
    if metadata.get("metadata_hash") != _stable_hash(metadata_core):
        raise AdvisorV21Error("V2.1 model metadata content hash mismatch")
    return bundle, ood


def _transformation(family: str) -> str:
    matches = [
        transformation
        for transformation, registered_family in TRANSFORMATION_FAMILIES.items()
        if registered_family == family
    ]
    if len(matches) != 1:
        raise AdvisorV21Error(f"family has no unique transformation: {family}")
    return matches[0]


def _probability(estimator: Any, matrix: list[list[float]], label: Any) -> list[float]:
    classes = list(estimator.classes_)
    if label not in classes:
        return [0.0] * len(matrix)
    index = classes.index(label)
    return [float(row[index]) for row in estimator.predict_proba(matrix)]


def analyze_case_v21(
    config: ProjectConfig,
    manifest: CaseManifest,
    *,
    mode: str = "safe",
    profile_id: str = "balanced",
    force_graph: bool = False,
) -> dict[str, Any]:
    if mode not in {"point", "risk", "safe"}:
        raise AdvisorV21Error(f"unsupported V2.1 decision mode: {mode}")
    try:
        profile = PROFILES[profile_id]
    except KeyError as exc:
        raise AdvisorV21Error(f"unknown PPA profile: {profile_id}") from exc
    bundle, ood_model = load_model_bundle_v21(config)
    extraction = extract_case_kernel_features(
        config, manifest, manifest.baseline_id, force_graph=force_graph
    )
    rule_analysis = analyze_rules_v21(extraction["graph"], extraction["syntax_facts"])
    transformation = _transformation(manifest.family)
    findings = [
        finding
        for finding in rule_analysis.get("findings") or []
        if finding.get("transformation_id") == transformation
    ]
    if not findings:
        core = {
            "schema_version": ANALYSIS_SCHEMA_VERSION_V21,
            "flow_version": ADVISOR_FLOW_VERSION_V21,
            "mode": mode,
            "profile": profile_id,
            "case_id": manifest.case_id,
            "family": manifest.family,
            "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
            "ruleset_version": RULESET_VERSION_V21,
            "feature_hash": extraction["feature_hash"],
            "rule_analysis_hash": rule_analysis["analysis_hash"],
            "decision": "abstain",
            "selected_candidate_id": None,
            "candidates": [],
            "ood": None,
            "rejection_reasons": ["registered V2.1 transformation was not detected"],
            "explanation_status": "not_requested",
        }
        return {**core, "analysis_hash": _stable_hash(core)}

    finding = findings[0]
    feature_rows = [
        candidate_features_v21(extraction["features"], finding, template_id)
        for template_id in ("v1", "v2", "v3")
    ]
    matrix = [
        [float(features.get(feature, 0.0)) for feature in FEATURE_ORDER_V21]
        for features in feature_rows
    ]
    regressions = {
        metric: [float(value) for value in bundle["regressors"][metric].predict(matrix)]
        for metric in ("delay", "area", "cell_count")
    }
    eligibility_probabilities = _probability(
        bundle["eligibility_classifier"], matrix, True
    )
    direction_predictions = {}
    for metric in ("delay", "area", "cell_count"):
        classifier = bundle["direction_classifiers"][metric]
        labels = [str(value) for value in classifier.predict(matrix)]
        probabilities = classifier.predict_proba(matrix)
        direction_predictions[metric] = [
            {
                "label": label,
                "confidence": float(probabilities[index][list(classifier.classes_).index(label)]),
            }
            for index, label in enumerate(labels)
        ]
    ood = score_family_ood(
        ood_model,
        family=manifest.family,
        features=extraction["features"],
    )
    candidates = []
    risk_threshold = float(bundle["risk_threshold"])
    for index, (template_id, features) in enumerate(
        zip(("v1", "v2", "v3"), feature_rows, strict=True)
    ):
        predictions = {}
        for metric in ("delay", "area", "cell_count"):
            direction = direction_predictions[metric][index]
            threshold = float(bundle["direction_thresholds"][metric])
            predictions[metric] = {
                "estimate": round(regressions[metric][index], 6),
                "direction": (
                    direction["label"]
                    if direction["confidence"] >= threshold
                    else "uncertain"
                ),
                "direction_confidence": round(direction["confidence"], 6),
                "direction_threshold": threshold,
            }
        point_eligible = profile.eligible(
            predictions["delay"]["estimate"], predictions["area"]["estimate"]
        )
        risk_eligible = eligibility_probabilities[index] >= risk_threshold
        eligible = {
            "point": point_eligible,
            "risk": risk_eligible,
            "safe": risk_eligible and not ood["out_of_domain"],
        }[mode]
        rejection_reasons = []
        if mode == "point" and not point_eligible:
            rejection_reasons.append("point prediction does not clear balanced profile")
        if mode in {"risk", "safe"} and not risk_eligible:
            rejection_reasons.append(
                f"eligibility probability {eligibility_probabilities[index]:.6f} below {risk_threshold:.6f}"
            )
        if mode == "safe" and ood["out_of_domain"]:
            rejection_reasons.append(
                f"nearest-neighbor OOD distance {ood['distance']:.6f} exceeds {ood['threshold']:.6f}"
            )
        identity = {
            "case_id": manifest.case_id,
            "finding_id": finding["finding_id"],
            "transformation_id": transformation,
            "template_id": template_id,
            "feature_hash": extraction["feature_hash"],
        }
        candidates.append(
            {
                "candidate_id": _stable_hash(identity)[:16],
                "template_id": template_id,
                "transformation_id": transformation,
                "finding_id": finding["finding_id"],
                "source": finding.get("source") or {"locations": []},
                "preconditions": finding.get("risks") or [],
                "features": features,
                "predicted_improvement_percent": predictions,
                "eligibility_probability": round(eligibility_probabilities[index], 6),
                "eligibility_threshold": risk_threshold,
                "eligible": eligible,
                "predicted_utility": round(
                    profile.utility(
                        predictions["delay"]["estimate"],
                        predictions["area"]["estimate"],
                        predictions["cell_count"]["estimate"],
                    ),
                    6,
                ),
                "ood": ood,
                "rejection_reasons": rejection_reasons,
                "rank": None,
                "generation": {"status": "not_requested"},
                "verification": {"status": "not_run"},
            }
        )
    eligible_candidates = [candidate for candidate in candidates if candidate["eligible"]]
    eligible_candidates.sort(
        key=lambda candidate: (-candidate["predicted_utility"], candidate["candidate_id"])
    )
    for rank, candidate in enumerate(eligible_candidates, 1):
        candidate["rank"] = rank
    selected = eligible_candidates[0] if eligible_candidates else None
    core = {
        "schema_version": ANALYSIS_SCHEMA_VERSION_V21,
        "flow_version": ADVISOR_FLOW_VERSION_V21,
        "mode": mode,
        "profile": profile_id,
        "case_id": manifest.case_id,
        "family": manifest.family,
        "feature_schema_hash": FEATURE_SCHEMA_HASH_V21,
        "ruleset_version": RULESET_VERSION_V21,
        "feature_hash": extraction["feature_hash"],
        "rule_analysis_hash": rule_analysis["analysis_hash"],
        "decision": "recommend" if selected else "abstain",
        "selected_candidate_id": selected["candidate_id"] if selected else None,
        "candidates": candidates,
        "ood": ood,
        "rejection_reasons": [] if selected else sorted(
            {reason for candidate in candidates for reason in candidate["rejection_reasons"]}
        ),
        "explanation_status": "not_requested",
    }
    return {**core, "analysis_hash": _stable_hash(core)}


def write_case_analysis_v21(
    config: ProjectConfig,
    manifest: CaseManifest,
    output_dir: Path,
    *,
    mode: str = "safe",
    profile_id: str = "balanced",
    force_graph: bool = False,
) -> tuple[dict[str, Any], Path]:
    variant = manifest.baseline
    design = normalize_design_input(
        top=variant.kernel_top,
        files=(manifest.variant_path(variant),),
        base=manifest.root,
    )
    analysis = analyze_case_v21(
        config,
        manifest,
        mode=mode,
        profile_id=profile_id,
        force_graph=force_graph,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / "input.json"
    analysis_path = output_dir / "analysis.json"
    input_path.write_text(
        json.dumps(design.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    analysis_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return analysis, analysis_path
