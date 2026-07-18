from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any

from rtl_advisor.advisor_v2 import PROFILES
from rtl_advisor.calibration_v22 import (
    CALIBRATION_FLOW_VERSION_V22,
    frozen_input_paths_v22,
    verify_frozen_inputs_v22,
)
from rtl_advisor.config import ProjectConfig


DIAGNOSTIC_FLOW_VERSION_V22 = "rtl-advisor-failure-diagnostic-v22"
TIE_EPSILON_V22 = 1e-6


class DiagnosticV22Error(RuntimeError):
    """Raised when the frozen V2.2 failure evidence cannot be reproduced."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagnosticV22Error(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DiagnosticV22Error(f"expected object in {description} {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _verify_content_hash(
    payload: dict[str, Any],
    *,
    hash_field: str,
    ignored_fields: tuple[str, ...] = (),
    description: str,
) -> None:
    core = {
        key: value
        for key, value in payload.items()
        if key != hash_field and key not in ignored_fields
    }
    if payload.get(hash_field) != _stable_hash(core):
        raise DiagnosticV22Error(f"{description} content hash mismatch")


def classify_case_v22(
    candidates: list[dict[str, Any]],
    *,
    supported: bool,
    threshold: float,
) -> dict[str, Any]:
    if not candidates:
        raise DiagnosticV22Error("case diagnostic requires candidates")
    opportunity = any(bool(candidate["eligible"]) for candidate in candidates)
    qualified = [
        candidate
        for candidate in candidates
        if float(candidate["eligibility_probability"]) >= threshold
    ]
    selected = (
        max(
            qualified,
            key=lambda candidate: (
                float(candidate["predicted_utility"]),
                str(candidate["template_id"]),
            ),
        )
        if qualified
        else None
    )
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    if eligible:
        best_utility = max(float(candidate["measured_utility"]) for candidate in eligible)
        best_ids = sorted(
            str(candidate["template_id"])
            for candidate in eligible
            if abs(float(candidate["measured_utility"]) - best_utility)
            <= TIE_EPSILON_V22
        )
    else:
        best_utility = None
        best_ids = []
    selected_eligible = bool(selected and selected["eligible"])
    if opportunity:
        if not supported:
            category = "unsupported_family"
        elif selected is None:
            category = "no_candidate_clears_threshold"
        elif not selected_eligible and any(candidate["eligible"] for candidate in qualified):
            category = "ranking_selected_ineligible"
        elif not selected_eligible:
            category = "qualified_only_ineligible"
        elif str(selected["template_id"]) in best_ids:
            category = "covered_best"
        else:
            category = "covered_suboptimal"
    else:
        category = "true_abstention" if selected is None else "harmful_nonopportunity"
    max_eligible_probability = (
        max(float(candidate["eligibility_probability"]) for candidate in eligible)
        if eligible
        else None
    )
    return {
        "opportunity": opportunity,
        "supported": supported,
        "threshold": threshold,
        "category": category,
        "recommended": selected is not None,
        "selected_template": str(selected["template_id"]) if selected else None,
        "selected_eligible": selected_eligible,
        "qualified_template_ids": sorted(
            str(candidate["template_id"]) for candidate in qualified
        ),
        "qualified_eligible_template_ids": sorted(
            str(candidate["template_id"])
            for candidate in qualified
            if candidate["eligible"]
        ),
        "best_candidate_ids": best_ids,
        "best_measured_utility": best_utility,
        "max_eligible_probability": max_eligible_probability,
        "eligible_probability_margin_to_threshold": (
            threshold - max_eligible_probability
            if max_eligible_probability is not None
            else None
        ),
        "covered": bool(opportunity and selected_eligible),
        "oracle_rank_with_frozen_thresholds_covered": bool(
            opportunity and any(candidate["eligible"] for candidate in qualified)
        ),
        "oracle_threshold_supported_covered": bool(opportunity and supported),
    }


def _binary_ranking_metrics(labels: list[bool], scores: list[float]) -> dict[str, Any]:
    if len(labels) != len(scores) or not labels:
        raise DiagnosticV22Error("ranking metric inputs do not align")
    positive_count = sum(labels)
    negative_count = len(labels) - positive_count
    if not positive_count or not negative_count:
        return {
            "row_count": len(labels),
            "positive_count": positive_count,
            "roc_auc": None,
            "average_precision": None,
        }
    positive_scores = [score for label, score in zip(labels, scores, strict=True) if label]
    negative_scores = [score for label, score in zip(labels, scores, strict=True) if not label]
    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in positive_scores
        for negative in negative_scores
    )
    roc_auc = wins / (positive_count * negative_count)
    grouped: dict[float, list[bool]] = defaultdict(list)
    for label, score in zip(labels, scores, strict=True):
        grouped[float(score)].append(bool(label))
    true_positive = 0
    false_positive = 0
    previous_recall = 0.0
    average_precision = 0.0
    for score in sorted(grouped, reverse=True):
        group = grouped[score]
        true_positive += sum(group)
        false_positive += len(group) - sum(group)
        recall = true_positive / positive_count
        precision = true_positive / (true_positive + false_positive)
        average_precision += (recall - previous_recall) * precision
        previous_recall = recall
    return {
        "row_count": len(labels),
        "positive_count": positive_count,
        "roc_auc": roc_auc,
        "average_precision": average_precision,
    }


def _load_verified_inputs(config: ProjectConfig) -> dict[str, Any]:
    try:
        frozen = verify_frozen_inputs_v22(config)
    except Exception as exc:
        raise DiagnosticV22Error(str(exc)) from exc
    v21_paths = frozen_input_paths_v22(config)
    root = config.artifacts_dir / "models/v22"
    paths = {
        "v22_summary": root / "summary.json",
        "v22_metadata": root / "metadata.json",
        "v22_policy": root / "policy.json",
        "v22_report": root / "calibration-report.json",
        "v22_oof": root / "family-grouped-oof.json",
        "v21_rows": v21_paths["calibration_rows"],
        "v21_oof": v21_paths["grouped_oof"],
        "v21_ood": v21_paths["v21_ood"],
    }
    for name, path in paths.items():
        if not path.is_file():
            raise DiagnosticV22Error(f"diagnostic dependency missing ({name}): {path}")
    summary = _load_json(paths["v22_summary"], "V2.2 summary")
    metadata = _load_json(paths["v22_metadata"], "V2.2 metadata")
    policy = _load_json(paths["v22_policy"], "V2.2 policy")
    report = _load_json(paths["v22_report"], "V2.2 calibration report")
    _verify_content_hash(
        metadata, hash_field="metadata_hash", description="V2.2 metadata"
    )
    _verify_content_hash(policy, hash_field="policy_hash", description="V2.2 policy")
    _verify_content_hash(
        report,
        hash_field="report_hash",
        ignored_fields=("json_path", "markdown_path"),
        description="V2.2 calibration report",
    )
    if not (
        summary.get("status") == "calibration_gate_failed"
        and summary.get("risk_policy_feasible") is False
        and metadata.get("blind_labels_used") is False
        and policy.get("blind_labels_used") is False
        and report.get("blind_labels_used") is False
    ):
        raise DiagnosticV22Error("V2.2 diagnostic source is not the frozen failed calibration")
    for artifact_name in (
        "family-grouped-oof.json",
        "family-threshold-frontier.json",
        "input-lock.json",
        "metadata.json",
        "policy.json",
        "summary.json",
    ):
        artifact = report["artifacts"][artifact_name]
        artifact_path = Path(artifact["path"])
        if not artifact_path.is_file() or _file_hash(artifact_path) != artifact["sha256"]:
            raise DiagnosticV22Error(
                f"V2.2 calibration report dependency changed: {artifact_path}"
            )
    return {
        "frozen": frozen,
        "paths": paths,
        "summary": summary,
        "metadata": metadata,
        "policy": policy,
        "report": report,
    }


def diagnose_v22(config: ProjectConfig) -> dict[str, Any]:
    verified = _load_verified_inputs(config)
    paths = verified["paths"]
    rows = list(_load_json(paths["v21_rows"], "V2.1 rows").get("rows") or [])
    v21_predictions = list(
        _load_json(paths["v21_oof"], "V2.1 grouped OOF").get("predictions") or []
    )
    v22_predictions = list(
        _load_json(paths["v22_oof"], "V2.2 family grouped OOF").get("predictions")
        or []
    )
    if not (len(rows) == len(v21_predictions) == len(v22_predictions) == 2808):
        raise DiagnosticV22Error("V2.2 diagnostic inputs are not 2,808 aligned rows")
    for row, old, new in zip(rows, v21_predictions, v22_predictions, strict=True):
        identity = (
            row.get("case_id"),
            row.get("topology_signature"),
            row.get("template_id"),
        )
        if identity != (
            old.get("case_id"),
            old.get("topology_signature"),
            old.get("template_id"),
        ) or identity != (
            new.get("case_id"),
            new.get("topology_signature"),
            new.get("template_id"),
        ):
            raise DiagnosticV22Error("V2.2 diagnostic row identity mismatch")
        if row.get("training_split") not in {"calibration-v2", "calibration-v21"}:
            raise DiagnosticV22Error("non-calibration row reached V2.2 diagnostic")
    ood_model = _load_json(paths["v21_ood"], "V2.1 OOD model")
    by_case: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_case[str(row["case_id"])].append(index)
    profile = PROFILES["balanced"]
    cases = []
    categories: Counter[str] = Counter()
    per_family: dict[str, Counter[str]] = defaultdict(Counter)
    near_threshold = Counter({"within_0.01": 0, "within_0.05": 0, "within_0.10": 0})
    safe_best_candidate_count = 0
    eligible_candidate_count = 0
    ood_case_count = 0
    covered_ood_case_count = 0
    separation_data: dict[str, dict[str, list[Any]]] = defaultdict(
        lambda: {
            "candidate_eligible_labels": [],
            "candidate_safe_best_labels": [],
            "candidate_scores": [],
            "case_opportunity_labels": [],
            "case_scores": [],
        }
    )
    for case_id, indices in sorted(by_case.items()):
        case_rows = [rows[index] for index in indices]
        family = str(case_rows[0]["family"])
        if len(indices) != 3 or len({row["family"] for row in case_rows}) != 1:
            raise DiagnosticV22Error(f"invalid candidate group for {case_id}")
        support = verified["policy"]["family_support"][family]
        threshold = float(verified["policy"]["selected"][family]["threshold"])
        candidate_rows = []
        measured_eligible = [row for row in case_rows if row["eligible"]]
        if measured_eligible:
            measured_best = max(
                profile.utility(
                    float(row["targets"]["delay"]),
                    float(row["targets"]["area"]),
                    float(row["targets"]["cell_count"]),
                )
                for row in measured_eligible
            )
        else:
            measured_best = None
        for index in indices:
            row = rows[index]
            old = v21_predictions[index]
            new = v22_predictions[index]
            measured_utility = profile.utility(
                float(row["targets"]["delay"]),
                float(row["targets"]["area"]),
                float(row["targets"]["cell_count"]),
            )
            predicted_utility = profile.utility(
                float(old["regression"]["delay"]),
                float(old["regression"]["area"]),
                float(old["regression"]["cell_count"]),
            )
            safe_best = bool(
                row["eligible"]
                and measured_best is not None
                and abs(measured_utility - measured_best) <= TIE_EPSILON_V22
            )
            eligible_candidate_count += int(bool(row["eligible"]))
            safe_best_candidate_count += int(safe_best)
            separation_data[family]["candidate_eligible_labels"].append(
                bool(row["eligible"])
            )
            separation_data[family]["candidate_safe_best_labels"].append(safe_best)
            separation_data[family]["candidate_scores"].append(
                float(new["eligibility_probability"])
            )
            candidate_rows.append(
                {
                    "template_id": row["template_id"],
                    "eligible": bool(row["eligible"]),
                    "safe_best": safe_best,
                    "eligibility_probability": float(
                        new["eligibility_probability"]
                    ),
                    "predicted_utility": predicted_utility,
                    "measured_utility": measured_utility,
                    "predicted_improvement_percent": old["regression"],
                    "measured_improvement_percent": row["targets"],
                }
            )
        classification = classify_case_v22(
            candidate_rows,
            supported=bool(support["supported"]),
            threshold=threshold,
        )
        separation_data[family]["case_opportunity_labels"].append(
            bool(classification["opportunity"])
        )
        separation_data[family]["case_scores"].append(
            max(float(candidate["eligibility_probability"]) for candidate in candidate_rows)
        )
        topology_signature = str(case_rows[0]["topology_signature"])
        try:
            ood_spec = ood_model["families"][family]
            loo = ood_spec["leave_one_topology_out"][topology_signature]
        except KeyError as exc:
            raise DiagnosticV22Error(
                f"missing leave-one-topology-out OOD evidence for {case_id}"
            ) from exc
        ood = {
            "distance": float(loo["distance"]),
            "threshold": float(ood_spec["threshold"]),
            "out_of_domain": float(loo["distance"]) > float(ood_spec["threshold"]),
            "nearest_topology_signature": loo["nearest_topology_signature"],
        }
        ood_case_count += int(ood["out_of_domain"])
        covered_ood_case_count += int(
            bool(classification["covered"] and ood["out_of_domain"])
        )
        margin = classification["eligible_probability_margin_to_threshold"]
        if (
            classification["category"] == "no_candidate_clears_threshold"
            and margin is not None
        ):
            near_threshold["within_0.01"] += int(0.0 < margin <= 0.01)
            near_threshold["within_0.05"] += int(0.0 < margin <= 0.05)
            near_threshold["within_0.10"] += int(0.0 < margin <= 0.10)
        categories[classification["category"]] += 1
        per_family[family][classification["category"]] += 1
        cases.append(
            {
                "case_id": case_id,
                "family": family,
                "topology_signature": topology_signature,
                "classification": classification,
                "ood_leave_one_topology_out": ood,
                "candidates": candidate_rows,
            }
        )
    opportunity_cases = [case for case in cases if case["classification"]["opportunity"]]
    covered_cases = [case for case in opportunity_cases if case["classification"]["covered"]]
    recommendations = [case for case in cases if case["classification"]["recommended"]]
    harmful = [
        case
        for case in recommendations
        if not case["classification"]["selected_eligible"]
    ]
    nonopportunities = [
        case for case in cases if not case["classification"]["opportunity"]
    ]
    true_abstentions = [
        case
        for case in nonopportunities
        if case["classification"]["category"] == "true_abstention"
    ]
    aggregate = {
        "case_count": len(cases),
        "opportunity_count": len(opportunity_cases),
        "nonopportunity_count": len(nonopportunities),
        "covered_opportunity_count": len(covered_cases),
        "missed_opportunity_count": len(opportunity_cases) - len(covered_cases),
        "recommendation_count": len(recommendations),
        "harmful_count": len(harmful),
        "true_abstention_count": len(true_abstentions),
        "opportunity_recall": len(covered_cases) / len(opportunity_cases),
        "abstention_specificity": len(true_abstentions) / len(nonopportunities),
        "harmful_recommendation_rate": len(harmful) / len(recommendations),
    }
    aggregate["balanced_actionable_accuracy"] = (
        aggregate["opportunity_recall"] + aggregate["abstention_specificity"]
    ) / 2.0
    expected = verified["policy"]["aggregate"]
    for key in (
        "opportunity_recall",
        "abstention_specificity",
        "harmful_recommendation_rate",
        "balanced_actionable_accuracy",
    ):
        if abs(float(aggregate[key]) - float(expected[key])) > 1e-12:
            raise DiagnosticV22Error(f"diagnostic does not reproduce V2.2 {key}")
    threshold_oracle_count = sum(
        case["classification"]["oracle_threshold_supported_covered"]
        for case in opportunity_cases
    )
    rank_oracle_count = sum(
        case["classification"]["oracle_rank_with_frozen_thresholds_covered"]
        for case in opportunity_cases
    )
    safe_covered_count = sum(
        case["classification"]["covered"]
        and not case["ood_leave_one_topology_out"]["out_of_domain"]
        for case in opportunity_cases
    )
    score_separation = {}
    for family, data in sorted(separation_data.items()):
        score_separation[family] = {
            "candidate_eligibility": _binary_ranking_metrics(
                data["candidate_eligible_labels"], data["candidate_scores"]
            ),
            "candidate_safe_best": _binary_ranking_metrics(
                data["candidate_safe_best_labels"], data["candidate_scores"]
            ),
            "case_opportunity": _binary_ranking_metrics(
                data["case_opportunity_labels"], data["case_scores"]
            ),
        }
    core = {
        "schema_version": 1,
        "flow_version": DIAGNOSTIC_FLOW_VERSION_V22,
        "source_flow_version": CALIBRATION_FLOW_VERSION_V22,
        "source": "frozen calibration rows and grouped-OOF predictions only",
        "blind_labels_used": False,
        "input_lock_hash": verified["frozen"]["input_lock_hash"],
        "v22_policy_hash": verified["policy"]["policy_hash"],
        "v22_report_hash": verified["report"]["report_hash"],
        "aggregate": aggregate,
        "category_counts": dict(sorted(categories.items())),
        "per_family_category_counts": {
            family: dict(sorted(counts.items()))
            for family, counts in sorted(per_family.items())
        },
        "near_threshold_missed_opportunities": dict(near_threshold),
        "counterfactual_bounds": {
            "covered_with_measured_oracle_ranking_at_frozen_thresholds": rank_oracle_count,
            "additional_covered_from_oracle_ranking": (
                rank_oracle_count - len(covered_cases)
            ),
            "covered_with_oracle_threshold_for_supported_families": (
                threshold_oracle_count
            ),
            "unsupported_opportunity_count": (
                len(opportunity_cases) - threshold_oracle_count
            ),
        },
        "ood_leave_one_topology_out": {
            "out_of_domain_case_count": ood_case_count,
            "covered_recommendation_case_count": len(covered_cases),
            "covered_recommendations_rejected_by_ood": covered_ood_case_count,
            "safe_covered_opportunity_count": safe_covered_count,
            "safe_opportunity_recall": safe_covered_count / len(opportunity_cases),
        },
        "safe_best_target": {
            "candidate_row_count": len(rows),
            "measured_eligible_candidate_count": eligible_candidate_count,
            "safe_best_candidate_count": safe_best_candidate_count,
            "safe_best_positive_fraction": safe_best_candidate_count / len(rows),
        },
        "score_separation": score_separation,
        "cases": cases,
    }
    report = {**core, "diagnostic_hash": _stable_hash(core)}
    root = config.artifacts_dir / "models/v22"
    report["json_path"] = str((root / "failure-diagnostics.json").resolve())
    report["markdown_path"] = str((root / "failure-diagnostics.md").resolve())
    _write_json(root / "failure-diagnostics.json", report)
    missed_categories = {
        category: count
        for category, count in categories.items()
        if category
        in {
            "unsupported_family",
            "no_candidate_clears_threshold",
            "ranking_selected_ineligible",
            "qualified_only_ineligible",
        }
    }
    lines = [
        "# RTL Advisor V2.2 Failure Diagnostic",
        "",
        "> Frozen calibration and grouped-OOF evidence only. No blind labels were used.",
        "",
        f"- Opportunity cases: {len(opportunity_cases)}",
        f"- Covered opportunities: {len(covered_cases)}",
        f"- Missed opportunities: {len(opportunity_cases) - len(covered_cases)}",
        f"- Recommendations: {len(recommendations)}",
        f"- Harmful recommendations: {len(harmful)}",
        "",
        "## Miss decomposition",
        "",
        "| Cause | Cases |",
        "|---|---:|",
    ]
    for category, count in sorted(missed_categories.items()):
        lines.append(f"| {category} | {count} |")
    lines.extend(
        (
            "",
            "## Score separation",
            "",
            "| Family | Case ROC AUC | Case AP | Candidate ROC AUC | Candidate AP |",
            "|---|---:|---:|---:|---:|",
        )
    )
    for family, metrics in score_separation.items():
        case = metrics["case_opportunity"]
        candidate = metrics["candidate_eligibility"]

        def display(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.3f}"

        lines.append(
            f"| {family} | {display(case['roc_auc'])} | "
            f"{display(case['average_precision'])} | "
            f"{display(candidate['roc_auc'])} | "
            f"{display(candidate['average_precision'])} |"
        )
    lines.extend(
        (
            "",
            "## Counterfactual bounds",
            "",
            f"- Frozen-threshold measured-oracle ranking covers {rank_oracle_count} opportunities ({rank_oracle_count - len(covered_cases):+d} versus V2.2).",
            f"- Oracle thresholding for supported families can cover at most {threshold_oracle_count} opportunities; {len(opportunity_cases) - threshold_oracle_count} opportunities belong to unsupported families.",
            f"- Leave-one-topology-out OOD would reject {covered_ood_case_count} of the {len(covered_cases)} covered recommendations.",
            f"- Safe-best candidate labels: {safe_best_candidate_count}/{len(rows)} ({safe_best_candidate_count / len(rows):.1%}).",
            "",
            "## V2.3 implication",
            "",
            "Use the miss decomposition to decide between targeted calibration expansion and a case-level safe-best selector. Do not loosen the frozen V2.2 safety constraints.",
            "",
            f"Diagnostic hash: `{report['diagnostic_hash']}`",
            "",
        )
    )
    (root / "failure-diagnostics.md").write_text("\n".join(lines), encoding="utf-8")
    return report
