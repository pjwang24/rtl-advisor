from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from rtl_advisor.advisor_v2 import PROFILES
from rtl_advisor.benchmark_v2 import BenchmarkV2Error, verify_benchmark_lock
from rtl_advisor.config import ProjectConfig


POSTMORTEM_SCHEMA_VERSION = 1
POSTMORTEM_FLOW_VERSION = "rtl-advisor-v2-frozen-postmortem-v1"
_OOD_REASON = re.compile(r"^feature ([A-Za-z0-9_]+)=.* outside \[")
_METRICS = ("delay", "area", "cell_count")
_TIE_EPSILON = 1e-6


class V2PostmortemError(RuntimeError):
    """Raised when the frozen V2 evidence is incomplete or inconsistent."""


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V2PostmortemError(f"invalid frozen artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise V2PostmortemError(f"expected a JSON object in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def normalize_rejection_reason(reason: str) -> str:
    match = _OOD_REASON.match(reason)
    if match:
        return f"out_of_domain:{match.group(1)}"
    if reason.startswith("does not clear "):
        if "point predictions" in reason:
            return "point_prediction_ineligible"
        return "conservative_interval_ineligible"
    if "source" in reason.lower() and "ambiguous" in reason.lower():
        return "ambiguous_source_mapping"
    return re.sub(r"\s+", "_", reason.strip().lower()) or "unspecified"


def _direction(value: float) -> str:
    if value > 1.0:
        return "improve"
    if value < -1.0:
        return "degrade"
    return "neutral"


def _select_counterfactual(
    record: dict[str, Any],
    *,
    bound: str,
) -> dict[str, Any] | None:
    profile = PROFILES["balanced"]
    eligible: list[tuple[float, str, dict[str, Any]]] = []
    for candidate in record.get("candidates") or []:
        prediction = candidate.get("predicted_improvement_percent") or {}
        if any(metric not in prediction for metric in _METRICS):
            continue
        delay = float(prediction["delay"][bound])
        area = float(prediction["area"][bound])
        cells = float(prediction["cell_count"][bound])
        if profile.eligible(delay, area):
            eligible.append(
                (
                    profile.utility(delay, area, cells),
                    str(candidate.get("candidate_id", "")),
                    candidate,
                )
            )
    if not eligible:
        return None
    eligible.sort(key=lambda item: (-item[0], item[1]))
    return eligible[0][2]


def _shadow_rows(
    gate_records: Iterable[dict[str, Any]],
    *,
    bound: str,
) -> list[dict[str, Any]]:
    rows = []
    for record in gate_records:
        selected = _select_counterfactual(record, bound=bound)
        score = record["score"]
        selected_template = selected.get("template_id") if selected else None
        opportunity = bool(score["opportunity"])
        eligible = set(score.get("eligible_templates") or [])
        recommended = selected is not None
        selected_eligible = selected_template in eligible
        rows.append(
            {
                "case_id": record["case_id"],
                "family": record["family"],
                "recommended": recommended,
                "selected_template_id": selected_template,
                "opportunity": opportunity,
                "selected_eligible": selected_eligible,
                "opportunity_covered": recommended and selected_eligible,
                "harmful": recommended and not selected_eligible,
                "utilities": score.get("utilities") or {},
            }
        )
    return rows


def corrected_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(rows)
    opportunities = [row for row in values if row["opportunity"]]
    negatives = [row for row in values if not row["opportunity"]]
    recommendations = [row for row in values if row["recommended"]]
    covered = [row for row in opportunities if row["opportunity_covered"]]
    true_abstentions = [row for row in negatives if not row["recommended"]]
    opportunity_recall = len(covered) / len(opportunities) if opportunities else 0.0
    specificity = len(true_abstentions) / len(negatives) if negatives else 0.0
    harmful_rate = (
        sum(bool(row["harmful"]) for row in recommendations) / len(recommendations)
        if recommendations
        else 0.0
    )
    exact_tie = 0
    regrets = []
    for row in covered:
        utilities = {key: float(value) for key, value in row["utilities"].items()}
        if not utilities:
            continue
        best = max(utilities.values())
        best_ids = {
            key for key, value in utilities.items() if abs(value - best) <= _TIE_EPSILON
        }
        if row["selected_template_id"] in best_ids:
            exact_tie += 1
        selected_utility = utilities.get(row["selected_template_id"], 0.0)
        regrets.append(0.0 if best <= 0 else min(1.0, max(0.0, best - selected_utility) / abs(best)))
    return {
        "case_count": len(values),
        "opportunity_count": len(opportunities),
        "recommendation_count": len(recommendations),
        "covered_opportunity_count": len(covered),
        "opportunity_recall": opportunity_recall,
        "abstention_specificity": specificity,
        "balanced_actionable_accuracy": (opportunity_recall + specificity) / 2.0,
        "harmful_recommendation_rate": harmful_rate,
        "tie_aware_exact_best_accuracy": exact_tie / len(covered) if covered else 0.0,
        "conditional_normalized_ranking_regret": (
            sum(regrets) / len(regrets) if regrets else 0.0
        ),
        "missed_opportunity_count": len(opportunities) - len(covered),
    }


def _record_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        score = record["score"]
        selected_template = score.get("selected_template_id")
        selected_eligible = selected_template in set(score.get("eligible_templates") or [])
        rows.append(
            {
                "case_id": record["case_id"],
                "family": record["family"],
                "recommended": bool(score["recommended"]),
                "selected_template_id": selected_template,
                "opportunity": bool(score["opportunity"]),
                "selected_eligible": selected_eligible,
                "opportunity_covered": bool(score["opportunity_covered"]),
                "harmful": bool(score["harmful_recommendation"]),
                "utilities": score.get("utilities") or {},
            }
        )
    return rows


def _direction_diagnostics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    confusion: dict[str, Counter[tuple[str, str]]] = {
        metric: Counter() for metric in _METRICS
    }
    recommendation_count = 0
    for record in records:
        if record["score"]["recommended"]:
            recommendation_count += 1
        for pair in record["score"].get("direction_pairs") or []:
            metric = str(pair["metric"])
            confusion[metric][(str(pair["observed"]), str(pair["predicted"]))] += 1
    per_metric = {}
    total_pairs = 0
    total_correct = 0
    for metric in _METRICS:
        counts = confusion[metric]
        pair_count = sum(counts.values())
        correct = sum(count for (observed, predicted), count in counts.items() if observed == predicted)
        total_pairs += pair_count
        total_correct += correct
        per_metric[metric] = {
            "pair_count": pair_count,
            "accuracy": correct / pair_count if pair_count else 0.0,
            "confusion": {
                f"observed={observed},predicted={predicted}": count
                for (observed, predicted), count in sorted(counts.items())
            },
        }
    possible_slots = recommendation_count * len(_METRICS)
    return {
        "recommendation_count": recommendation_count,
        "pair_count": total_pairs,
        "conditional_coverage": total_pairs / possible_slots if possible_slots else 0.0,
        "accuracy": total_correct / total_pairs if total_pairs else 0.0,
        "per_metric": per_metric,
    }


def _feature_drift(
    calibration_rows: list[dict[str, Any]],
    gate_records: list[dict[str, Any]],
) -> dict[str, Any]:
    calibration: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in calibration_rows:
        for feature, value in (row.get("features") or {}).items():
            calibration[str(row["family"])][feature].append(float(value))
    live: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for record in gate_records:
        candidates = record.get("candidates") or []
        if not candidates:
            continue
        # Design features are repeated across templates except template_code.
        for feature, value in (candidates[0].get("features") or {}).items():
            if feature == "template_code":
                continue
            live[str(record["family"])][feature].append(float(value))
    by_family: dict[str, Any] = {}
    global_outside: Counter[str] = Counter()
    for family in sorted(calibration):
        feature_rows = {}
        for feature in sorted(calibration[family]):
            cal_values = calibration[family][feature]
            live_values = live[family].get(feature, [])
            if not live_values:
                continue
            cal_min, cal_max = min(cal_values), max(cal_values)
            outside = sum(value < cal_min or value > cal_max for value in live_values)
            if outside:
                global_outside[feature] += outside
                feature_rows[feature] = {
                    "calibration_min": cal_min,
                    "calibration_max": cal_max,
                    "live_min": min(live_values),
                    "live_max": max(live_values),
                    "live_case_count": len(live_values),
                    "outside_case_count": outside,
                    "outside_fraction": outside / len(live_values),
                }
        by_family[family] = feature_rows
    return {
        "method": "V2 min/max comparison using calibration rows and one kernel feature vector per live case",
        "top_outside_features": [
            {"feature": feature, "outside_case_count": count}
            for feature, count in global_outside.most_common()
        ],
        "by_family": by_family,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    rejection = report["rejection_diagnostics"]
    shadows = report["shadow_counterfactuals"]
    rf = shadows["random_forest_recorded"]
    lines = [
        "# Frozen V2 Postmortem",
        "",
        "> Diagnostic only. This report does not change the frozen V2 report or promotion result.",
        "",
        "## Executive finding",
        "",
        (
            f"The calibrated gate abstained on all {report['case_count']} blind cases. "
            f"All {rejection['candidate_count']} detected candidates had at least one "
            "out-of-domain rejection because calibration used wrapper-level graph features "
            "while live inference used kernel-level graph features."
        ),
        "",
        "## Rejection causes",
        "",
        "| Cause | Candidate count |",
        "|---|---:|",
    ]
    for item in rejection["normalized_counts"]:
        lines.append(f"| `{item['reason']}` | {item['count']} |")
    lines.extend(
        [
            "",
            "## Shadow counterfactuals",
            "",
            "| Policy | Recommendations | Opportunity coverage | Harmful rate | Balanced actionable | Tie-aware best | Conditional regret |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for key in (
        "frozen_calibrated_gate",
        "interval_without_ood",
        "point_without_ood",
        "random_forest_recorded",
    ):
        metric = shadows[key]
        lines.append(
            f"| {metric['label']} | {metric['recommendation_count']} | "
            f"{metric['opportunity_recall']:.1%} | "
            f"{metric['harmful_recommendation_rate']:.1%} | "
            f"{metric['balanced_actionable_accuracy']:.1%} | "
            f"{metric['tie_aware_exact_best_accuracy']:.1%} | "
            f"{metric['conditional_normalized_ranking_regret']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Random-forest direction diagnosis",
            "",
            f"Conditional direction coverage was {rf['direction']['conditional_coverage']:.1%}; "
            f"accuracy was {rf['direction']['accuracy']:.1%}.",
            "",
            "| Metric | Accuracy | Pairs |",
            "|---|---:|---:|",
        ]
    )
    for metric in _METRICS:
        item = rf["direction"]["per_metric"][metric]
        lines.append(f"| {metric} | {item['accuracy']:.1%} | {item['pair_count']} |")
    feasibility = report["direction_metric_feasibility"]
    lines.extend(
        [
            "",
            "## Metric-contract defect",
            "",
            (
                f"The old direction-coverage denominator was all {report['case_count'] * 3} "
                f"case-metric slots. Recommending every measured safe opportunity could cover "
                f"only {feasibility['safe_opportunity_ceiling_old_definition']:.1%}; allowing one "
                f"harmful recommendation raises that only to "
                f"{feasibility['one_harmful_ceiling_old_definition']:.1%}. The 90% gate was "
                "therefore incompatible with safety-oriented abstention. V2.1 measures direction "
                "coverage only over recommended candidate metric slots."
            ),
            "",
            "## Rule misses",
            "",
        ]
    )
    misses = report["rule_diagnostics"]["missed_opportunities"]
    if misses:
        for miss in misses:
            lines.append(f"- `{miss['case_id']}` ({miss['family']}), best template `{miss['best_template_id']}`.")
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Integrity",
            "",
            f"- Benchmark lock: `{report['lock_hash']}`",
            f"- Frozen report SHA-256 before/after: `{report['integrity']['report_sha256_before']}`",
            f"- Run records verified: {report['integrity']['run_record_count']}",
            f"- Postmortem hash: `{report['postmortem_hash']}`",
            "",
        ]
    )
    return "\n".join(lines)


def diagnose_v2(config: ProjectConfig) -> dict[str, Any]:
    root = config.artifacts_dir / "benchmarks/v2"
    lock_path = root / "benchmark-lock.json"
    report_path = root / "report.json"
    report_hash_before = _file_hash(report_path)
    try:
        lock = verify_benchmark_lock(lock_path)
    except BenchmarkV2Error as exc:
        raise V2PostmortemError(str(exc)) from exc
    frozen_report = _load_json(report_path)
    if frozen_report.get("lock_hash") != lock["lock_hash"]:
        raise V2PostmortemError("frozen report does not match the benchmark lock")
    run_paths = sorted((root / "runs").glob("*.json"))
    records = [_load_json(path) for path in run_paths]
    if len(records) != int(frozen_report.get("record_count", -1)):
        raise V2PostmortemError("run-record count does not match the frozen report")
    if any(record.get("lock_hash") != lock["lock_hash"] for record in records):
        raise V2PostmortemError("one or more run records do not match the benchmark lock")
    primary = [record for record in records if int(record.get("repeat_index", -1)) == 0]
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in primary:
        by_arm[str(record["arm"])].append(record)
    gate_records = sorted(by_arm["v2_calibrated_gate"], key=lambda row: row["case_id"])
    rf_records = sorted(
        by_arm["v2_random_forest_challenger"], key=lambda row: row["case_id"]
    )
    if len(gate_records) != 72 or len(rf_records) != 72:
        raise V2PostmortemError("expected 72 primary gate and challenger records")

    rejection_counts: Counter[str] = Counter()
    candidate_count = 0
    out_of_domain_candidates = 0
    for record in gate_records:
        for candidate in record.get("candidates") or []:
            candidate_count += 1
            normalized = {
                normalize_rejection_reason(str(reason))
                for reason in candidate.get("rejection_reasons") or []
            }
            rejection_counts.update(normalized)
            if any(reason.startswith("out_of_domain:") for reason in normalized):
                out_of_domain_candidates += 1

    calibration_path = Path(lock["model_artifacts"]["calibration_rows"]["path"])
    calibration_payload = _load_json(calibration_path)
    calibration_rows = calibration_payload.get("rows") or []
    if not isinstance(calibration_rows, list):
        raise V2PostmortemError("calibration rows are malformed")

    rule_misses = [
        {
            "case_id": record["case_id"],
            "family": record["family"],
            "best_template_id": record["score"].get("best_template_id"),
        }
        for record in gate_records
        if not record.get("candidates") and record["score"]["opportunity"]
    ]
    no_candidate_negatives = [
        record["case_id"]
        for record in gate_records
        if not record.get("candidates") and not record["score"]["opportunity"]
    ]

    frozen_metrics = corrected_metrics(_record_rows(gate_records))
    interval_metrics = corrected_metrics(_shadow_rows(gate_records, bound="lower"))
    point_metrics = corrected_metrics(_shadow_rows(gate_records, bound="estimate"))
    rf_metrics = corrected_metrics(_record_rows(rf_records))
    for label, metric in (
        ("Frozen V2 calibrated gate", frozen_metrics),
        ("Intervals, ignoring OOD", interval_metrics),
        ("Point estimates, ignoring intervals and OOD", point_metrics),
        ("Recorded V2 random-forest challenger", rf_metrics),
    ):
        metric["label"] = label
        metric["authoritative"] = label == "Frozen V2 calibrated gate"
    rf_metrics["direction"] = _direction_diagnostics(rf_records)

    opportunity_count = frozen_metrics["opportunity_count"]
    case_count = frozen_metrics["case_count"]
    core = {
        "schema_version": POSTMORTEM_SCHEMA_VERSION,
        "flow_version": POSTMORTEM_FLOW_VERSION,
        "source": "frozen V2 stored records",
        "authoritative": False,
        "lock_hash": lock["lock_hash"],
        "case_count": case_count,
        "rejection_diagnostics": {
            "candidate_count": candidate_count,
            "out_of_domain_candidate_count": out_of_domain_candidates,
            "out_of_domain_candidate_fraction": (
                out_of_domain_candidates / candidate_count if candidate_count else 0.0
            ),
            "normalized_counts": [
                {"reason": reason, "count": count}
                for reason, count in rejection_counts.most_common()
            ],
        },
        "feature_drift": _feature_drift(calibration_rows, gate_records),
        "rule_diagnostics": {
            "no_candidate_case_count": len(rule_misses) + len(no_candidate_negatives),
            "missed_opportunity_count": len(rule_misses),
            "missed_opportunities": rule_misses,
            "correct_no_candidate_abstentions": no_candidate_negatives,
        },
        "shadow_counterfactuals": {
            "frozen_calibrated_gate": frozen_metrics,
            "interval_without_ood": interval_metrics,
            "point_without_ood": point_metrics,
            "random_forest_recorded": rf_metrics,
        },
        "direction_metric_feasibility": {
            "old_denominator_case_metric_slots": case_count * len(_METRICS),
            "safe_opportunity_count": opportunity_count,
            "safe_opportunity_ceiling_old_definition": opportunity_count / case_count,
            "one_harmful_ceiling_old_definition": (opportunity_count + 1) / case_count,
            "old_required_coverage": 0.90,
            "minimum_recommended_cases_for_old_gate": 65,
            "v21_denominator": "known direction slots on recommended candidates",
        },
        "integrity": {
            "report_path": str(report_path.resolve()),
            "report_sha256_before": report_hash_before,
            "report_sha256_after": report_hash_before,
            "lock_path": str(lock_path.resolve()),
            "lock_file_sha256": _file_hash(lock_path),
            "calibration_rows_sha256": _file_hash(calibration_path),
            "run_record_count": len(records),
            "run_set_sha256": _json_hash(
                [{"path": path.name, "sha256": _file_hash(path)} for path in run_paths]
            ),
        },
    }
    result = {**core, "postmortem_hash": _json_hash(core)}
    json_path = root / "postmortem.json"
    markdown_path = root / "postmortem.md"
    result["json_path"] = str(json_path.resolve())
    result["markdown_path"] = str(markdown_path.resolve())
    _write_json(json_path, result)
    markdown_path.write_text(_render_markdown(result), encoding="utf-8")
    report_hash_after = _file_hash(report_path)
    if report_hash_after != report_hash_before:
        raise V2PostmortemError("frozen V2 report changed while writing the postmortem")
    return result
