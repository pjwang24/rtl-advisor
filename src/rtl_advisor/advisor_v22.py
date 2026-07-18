from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from rtl_advisor.advisor_v2 import PROFILES, TRANSFORMATION_FAMILIES
from rtl_advisor.calibration_v22 import (
    CALIBRATION_FLOW_VERSION_V22,
    frozen_input_paths_v22,
    verify_frozen_inputs_v22,
)
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


ANALYSIS_SCHEMA_VERSION_V22 = 22
ADVISOR_FLOW_VERSION_V22 = "rtl-advisor-safe-decision-v22"


class AdvisorV22Error(RuntimeError):
    """Raised when the deterministic V2.2 advisor contract is not deployable."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorV22Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdvisorV22Error(f"expected JSON object for {description}: {path}")
    return payload


def _verify_content_hash(payload: dict[str, Any], field: str, description: str) -> None:
    core = {key: value for key, value in payload.items() if key != field}
    if payload.get(field) != _stable_hash(core):
        raise AdvisorV22Error(f"{description} content hash mismatch")


def load_model_bundle_v22(
    config: ProjectConfig,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = config.artifacts_dir / "models/v22"
    summary = _load_json(root / "summary.json", "V2.2 summary")
    metadata = _load_json(root / "metadata.json", "V2.2 metadata")
    policy = _load_json(root / "policy.json", "V2.2 policy")
    input_lock = _load_json(root / "input-lock.json", "V2.2 input lock")
    _verify_content_hash(metadata, "metadata_hash", "V2.2 metadata")
    _verify_content_hash(policy, "policy_hash", "V2.2 policy")
    _verify_content_hash(input_lock, "input_lock_hash", "V2.2 input lock")
    if metadata.get("feature_schema_hash") != FEATURE_SCHEMA_HASH_V21:
        raise AdvisorV22Error("V2.2 feature schema mismatch")
    if not (
        summary.get("status") == "passed"
        and summary.get("risk_policy_feasible") is True
        and metadata.get("risk_policy_feasible") is True
        and metadata.get("direction_policy_feasible") is True
        and metadata.get("physical_evidence_feasible") is True
        and policy.get("feasible") is True
    ):
        raise AdvisorV22Error(
            "V2.2 model is diagnostic-only because its calibration policy gate failed"
        )
    try:
        current_lock = verify_frozen_inputs_v22(config)
    except Exception as exc:
        raise AdvisorV22Error(str(exc)) from exc
    if current_lock["input_lock_hash"] != input_lock.get("input_lock_hash"):
        raise AdvisorV22Error("V2.2 frozen-input lock mismatch")
    family_bundle_path = root / "family-model-bundle.joblib"
    if not family_bundle_path.is_file():
        raise AdvisorV22Error(f"V2.2 family bundle missing: {family_bundle_path}")
    if hashlib.sha256(family_bundle_path.read_bytes()).hexdigest() != metadata.get(
        "bundle_sha256"
    ):
        raise AdvisorV22Error("V2.2 family bundle hash mismatch")
    try:
        import joblib

        family_bundle = joblib.load(family_bundle_path)
        v21_bundle = joblib.load(frozen_input_paths_v22(config)["v21_bundle"])
    except Exception as exc:
        raise AdvisorV22Error(f"could not load V2.2 model bundles: {exc}") from exc
    if not (
        family_bundle.get("flow_version") == CALIBRATION_FLOW_VERSION_V22
        and family_bundle.get("feature_schema_hash") == FEATURE_SCHEMA_HASH_V21
        and tuple(family_bundle.get("feature_order") or ()) == FEATURE_ORDER_V21
        and family_bundle.get("policy_hash") == policy.get("policy_hash")
        and family_bundle.get("input_lock_hash") == input_lock.get("input_lock_hash")
    ):
        raise AdvisorV22Error("V2.2 family bundle runtime contract mismatch")
    if not (
        v21_bundle.get("feature_schema_hash") == FEATURE_SCHEMA_HASH_V21
        and tuple(v21_bundle.get("feature_order") or ()) == FEATURE_ORDER_V21
    ):
        raise AdvisorV22Error("reused V2.1 prediction bundle contract mismatch")
    ood = _load_json(frozen_input_paths_v22(config)["v21_ood"], "V2.1 OOD model")
    ood_core = {key: value for key, value in ood.items() if key != "model_hash"}
    if ood.get("model_hash") != _stable_hash(ood_core):
        raise AdvisorV22Error("reused V2.1 OOD model content hash mismatch")
    return v21_bundle, family_bundle, ood


def _transformation(family: str) -> str:
    matches = [
        transformation
        for transformation, registered_family in TRANSFORMATION_FAMILIES.items()
        if registered_family == family
    ]
    if len(matches) != 1:
        raise AdvisorV22Error(f"family has no unique transformation: {family}")
    return matches[0]


def _probability(estimator: Any, matrix: list[list[float]]) -> list[float]:
    classes = list(estimator.classes_)
    if True not in classes:
        return [0.0] * len(matrix)
    index = classes.index(True)
    return [float(row[index]) for row in estimator.predict_proba(matrix)]


def analyze_case_v22(
    config: ProjectConfig,
    manifest: CaseManifest,
    *,
    mode: str = "safe",
    profile_id: str = "balanced",
    force_graph: bool = False,
) -> dict[str, Any]:
    if mode not in {"point", "risk", "safe"}:
        raise AdvisorV22Error(f"unsupported V2.2 decision mode: {mode}")
    try:
        profile = PROFILES[profile_id]
    except KeyError as exc:
        raise AdvisorV22Error(f"unknown PPA profile: {profile_id}") from exc
    prediction_bundle, family_bundle, ood_model = load_model_bundle_v22(config)
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
            "schema_version": ANALYSIS_SCHEMA_VERSION_V22,
            "flow_version": ADVISOR_FLOW_VERSION_V22,
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
        metric: [
            float(value)
            for value in prediction_bundle["regressors"][metric].predict(matrix)
        ]
        for metric in ("delay", "area", "cell_count")
    }
    if manifest.family in set(family_bundle["unsupported_families"]):
        eligibility_probabilities = [0.0] * len(matrix)
    else:
        try:
            eligibility_classifier = family_bundle["family_classifiers"][manifest.family]
        except KeyError as exc:
            raise AdvisorV22Error(
                f"V2.2 family classifier missing for {manifest.family}"
            ) from exc
        eligibility_probabilities = _probability(eligibility_classifier, matrix)
    try:
        risk_threshold = float(family_bundle["family_thresholds"][manifest.family])
    except KeyError as exc:
        raise AdvisorV22Error(
            f"V2.2 family threshold missing for {manifest.family}"
        ) from exc
    direction_predictions = {}
    for metric in ("delay", "area", "cell_count"):
        classifier = prediction_bundle["direction_classifiers"][metric]
        labels = [str(value) for value in classifier.predict(matrix)]
        probabilities = classifier.predict_proba(matrix)
        direction_predictions[metric] = [
            {
                "label": label,
                "confidence": float(
                    probabilities[index][list(classifier.classes_).index(label)]
                ),
            }
            for index, label in enumerate(labels)
        ]
    ood = score_family_ood(
        ood_model,
        family=manifest.family,
        features=extraction["features"],
    )
    candidates = []
    for index, (template_id, features) in enumerate(
        zip(("v1", "v2", "v3"), feature_rows, strict=True)
    ):
        predictions = {}
        for metric in ("delay", "area", "cell_count"):
            direction = direction_predictions[metric][index]
            threshold = float(prediction_bundle["direction_thresholds"][metric])
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
                f"family eligibility probability {eligibility_probabilities[index]:.6f} "
                f"below {risk_threshold:.6f}"
            )
        if mode == "safe" and ood["out_of_domain"]:
            rejection_reasons.append(
                f"nearest-neighbor OOD distance {ood['distance']:.6f} exceeds "
                f"{ood['threshold']:.6f}"
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
                "eligibility_probability": round(
                    eligibility_probabilities[index], 6
                ),
                "eligibility_threshold": risk_threshold,
                "eligibility_family": manifest.family,
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
        "schema_version": ANALYSIS_SCHEMA_VERSION_V22,
        "flow_version": ADVISOR_FLOW_VERSION_V22,
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
            {
                reason
                for candidate in candidates
                for reason in candidate["rejection_reasons"]
            }
        ),
        "explanation_status": "not_requested",
    }
    return {**core, "analysis_hash": _stable_hash(core)}


def write_case_analysis_v22(
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
    analysis = analyze_case_v22(
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
