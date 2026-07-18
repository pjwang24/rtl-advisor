from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Iterable

from rtl_advisor.config import ProjectConfig


FRONTEND_API_VERSION = "v1"
FRONTEND_API_SCHEMA_VERSION = 1
FRONTEND_SOURCE_VERSION = "v22"
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
METRICS = ("delay", "area", "cell_count")


class FrontendAPIError(RuntimeError):
    """Raised when stored evidence cannot satisfy the frontend contract."""


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrontendAPIError(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrontendAPIError(f"expected JSON object for {description}: {path}")
    return payload


def _percent(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value) * 100.0, 3)


def _display_name(identifier: str) -> str:
    return identifier.replace("_", " ").title()


def _case_sort_key(case: dict[str, Any]) -> tuple[str, str]:
    return (str(case.get("family", "")), str(case.get("case_id", "")))


class FrontendDataStore:
    """Read-only adapter from frozen model evidence to frontend API v1."""

    def __init__(self, config: ProjectConfig):
        self.config = config
        model_root = config.artifacts_dir / "models/v22"
        self._summary_path = model_root / "summary.json"
        self._report_path = model_root / "calibration-report.json"
        self._diagnostic_path = model_root / "failure-diagnostics.json"
        self._summary: dict[str, Any] | None = None
        self._report: dict[str, Any] | None = None
        self._diagnostic: dict[str, Any] | None = None

    def _load(self) -> None:
        if self._summary is not None:
            return
        summary = _load_json(self._summary_path, "V2.2 summary")
        report = _load_json(self._report_path, "V2.2 calibration report")
        diagnostic = _load_json(self._diagnostic_path, "V2.2 failure diagnostic")
        if report.get("blind_labels_used") is not False:
            raise FrontendAPIError("frontend source report is not calibration-only")
        if diagnostic.get("blind_labels_used") is not False:
            raise FrontendAPIError("frontend source diagnostic is not calibration-only")
        cases = diagnostic.get("cases")
        if not isinstance(cases, list):
            raise FrontendAPIError("V2.2 diagnostic cases are missing")
        self._summary = summary
        self._report = report
        self._diagnostic = diagnostic

    @property
    def summary(self) -> dict[str, Any]:
        self._load()
        assert self._summary is not None
        return self._summary

    @property
    def report(self) -> dict[str, Any]:
        self._load()
        assert self._report is not None
        return self._report

    @property
    def diagnostic(self) -> dict[str, Any]:
        self._load()
        assert self._diagnostic is not None
        return self._diagnostic

    def health(self) -> dict[str, Any]:
        self._load()
        return {
            "api_version": FRONTEND_API_VERSION,
            "schema_version": FRONTEND_API_SCHEMA_VERSION,
            "status": "ready",
            "source_version": FRONTEND_SOURCE_VERSION,
            "source_status": self.summary.get("status"),
            "read_only": True,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "api_version": FRONTEND_API_VERSION,
            "schema_version": FRONTEND_API_SCHEMA_VERSION,
            "read_only": True,
            "routes": [
                {"method": "GET", "path": "/api/v1/health"},
                {"method": "GET", "path": "/api/v1/overview"},
                {
                    "method": "GET",
                    "path": "/api/v1/cases",
                    "query": ["family", "category", "q", "limit", "offset"],
                },
                {"method": "GET", "path": "/api/v1/cases/{case_id}"},
                {"method": "GET", "path": "/api/v1/contract"},
            ],
            "analysis_contract": {
                "decision": ["recommend", "abstain"],
                "candidate_metrics": list(METRICS),
                "candidate_stages": ["generation", "lint", "formal", "physical"],
                "live_analysis_available": False,
                "next_source_version": "v23",
            },
        }

    def overview(self) -> dict[str, Any]:
        aggregate = self.report.get("aggregate") or {}
        diagnostic = self.diagnostic
        score_separation = diagnostic.get("score_separation") or {}
        family_categories = diagnostic.get("per_family_category_counts") or {}
        families = []
        for family in sorted(family_categories):
            categories = family_categories[family]
            separation = score_separation.get(family) or {}
            case_metrics = separation.get("case_opportunity") or {}
            opportunity_count = sum(
                int(categories.get(name, 0))
                for name in (
                    "covered_best",
                    "covered_suboptimal",
                    "no_candidate_clears_threshold",
                    "ranking_selected_ineligible",
                    "qualified_only_ineligible",
                    "unsupported_family",
                )
            )
            covered_count = int(categories.get("covered_best", 0)) + int(
                categories.get("covered_suboptimal", 0)
            )
            case_count = sum(int(value) for value in categories.values())
            families.append(
                {
                    "id": family,
                    "name": _display_name(family),
                    "case_count": case_count,
                    "opportunity_count": opportunity_count,
                    "covered_count": covered_count,
                    "opportunity_recall_percent": (
                        round(covered_count / opportunity_count * 100.0, 3)
                        if opportunity_count
                        else None
                    ),
                    "case_roc_auc": case_metrics.get("roc_auc"),
                    "case_average_precision": case_metrics.get("average_precision"),
                    "categories": categories,
                    "support": (
                        "unsupported"
                        if int(categories.get("unsupported_family", 0))
                        else "supported"
                    ),
                }
            )

        balanced = float(aggregate.get("balanced_actionable_accuracy", 0.0))
        return {
            "api_version": FRONTEND_API_VERSION,
            "schema_version": FRONTEND_API_SCHEMA_VERSION,
            "project": {
                "name": "RTL Advisor",
                "tagline": "Pre-synthesis RTL review",
                "source_version": FRONTEND_SOURCE_VERSION,
                "source_status": self.summary.get("status"),
                "live_analysis": {
                    "available": False,
                    "reason": (
                        "V2.2 is available for evaluation only because it did not "
                        "reach the required 70% overall decision score."
                    ),
                    "next_version": "V2.3",
                },
            },
            "evidence": {
                "kind": "calibration",
                "blind_labels_used": False,
                "case_count": len(diagnostic.get("cases") or []),
                "candidate_count": int(self.summary.get("row_count", 0)),
                "diagnostic_hash": diagnostic.get("diagnostic_hash"),
                "policy_hash": self.summary.get("policy_hash"),
            },
            "metrics": {
                "balanced_actionable_accuracy_percent": _percent(balanced),
                "balanced_actionable_target_percent": 70.0,
                "balanced_actionable_gap_points": round(max(0.0, 0.70 - balanced) * 100, 3),
                "opportunity_recall_percent": _percent(
                    aggregate.get("opportunity_recall")
                ),
                "abstention_specificity_percent": _percent(
                    aggregate.get("abstention_specificity")
                ),
                "harmful_recommendation_rate_percent": _percent(
                    aggregate.get("harmful_recommendation_rate")
                ),
                "opportunity_count": int(aggregate.get("opportunity_count", 0)),
                "covered_opportunity_count": int(
                    aggregate.get("correct_opportunity_count", 0)
                ),
                "recommendation_count": int(aggregate.get("recommendation_count", 0)),
                "harmful_count": int(aggregate.get("harmful_count", 0)),
                "no_change_case_count": int(
                    aggregate.get("nonopportunity_count", 0)
                ),
                "correct_no_change_count": int(
                    aggregate.get("abstained_nonopportunity_count", 0)
                ),
            },
            "gates": [
                {
                    "id": "balanced_actionable_accuracy",
                    "label": "Overall decision score",
                    "passed": bool(
                        (aggregate.get("checks") or {}).get(
                            "minimum_balanced_actionable_accuracy"
                        )
                    ),
                    "actual_percent": _percent(balanced),
                    "target": "Required for live use: at least 70%",
                },
                {
                    "id": "abstention_specificity",
                    "label": "Correct no-change decisions",
                    "passed": bool(
                        (aggregate.get("checks") or {}).get(
                            "minimum_abstention_specificity"
                        )
                    ),
                    "actual_percent": _percent(
                        aggregate.get("abstention_specificity")
                    ),
                    "target": "Required: at least 90%",
                },
                {
                    "id": "harmful_recommendations",
                    "label": "Incorrect recommendations",
                    "passed": bool(
                        (aggregate.get("checks") or {}).get(
                            "maximum_harmful_recommendation_rate"
                        )
                    ),
                    "actual_percent": _percent(
                        aggregate.get("harmful_recommendation_rate")
                    ),
                    "target": "Required: at most 5%",
                },
                {
                    "id": "physical_evidence",
                    "label": "OpenROAD validation",
                    "passed": bool(self.summary.get("physical_evidence_feasible")),
                    "actual_percent": None,
                    "target": "Required: passing place-and-route check",
                },
            ],
            "failure_categories": diagnostic.get("category_counts") or {},
            "near_threshold_misses": (
                diagnostic.get("near_threshold_missed_opportunities") or {}
            ),
            "families": families,
        }

    def _summary_case(self, case: dict[str, Any]) -> dict[str, Any]:
        classification = case.get("classification") or {}
        candidates = case.get("candidates") or []
        max_probability = max(
            (float(item.get("eligibility_probability", 0.0)) for item in candidates),
            default=0.0,
        )
        selected_template = classification.get("selected_template")
        selected = next(
            (
                item
                for item in candidates
                if item.get("template_id") == selected_template
            ),
            None,
        )
        return {
            "case_id": case.get("case_id"),
            "family": case.get("family"),
            "family_name": _display_name(str(case.get("family", ""))),
            "category": classification.get("category"),
            "category_name": _display_name(str(classification.get("category", ""))),
            "decision": (
                "recommend" if classification.get("recommended") else "abstain"
            ),
            "opportunity": bool(classification.get("opportunity")),
            "covered": bool(classification.get("covered")),
            "supported": bool(classification.get("supported")),
            "selected_template": selected_template,
            "selected_eligible": bool(classification.get("selected_eligible")),
            "max_eligibility_probability": round(max_probability, 6),
            "threshold": classification.get("threshold"),
            "predicted_utility": (
                selected.get("predicted_utility") if selected is not None else None
            ),
            "out_of_domain": bool(
                (case.get("ood_leave_one_topology_out") or {}).get("out_of_domain")
            ),
        }

    def cases(
        self,
        *,
        family: str | None = None,
        category: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            raise FrontendAPIError("case limit must be between 1 and 200")
        if offset < 0:
            raise FrontendAPIError("case offset cannot be negative")
        source_cases = sorted(self.diagnostic.get("cases") or [], key=_case_sort_key)
        normalized_query = (query or "").strip().lower()
        filtered = []
        for case in source_cases:
            classification = case.get("classification") or {}
            if family and case.get("family") != family:
                continue
            if category and classification.get("category") != category:
                continue
            if normalized_query and normalized_query not in " ".join(
                (
                    str(case.get("case_id", "")),
                    str(case.get("family", "")),
                    str(classification.get("category", "")),
                )
            ).lower():
                continue
            filtered.append(self._summary_case(case))
        page = filtered[offset : offset + limit]
        return {
            "api_version": FRONTEND_API_VERSION,
            "schema_version": FRONTEND_API_SCHEMA_VERSION,
            "items": page,
            "pagination": {
                "total": len(filtered),
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page) < len(filtered),
            },
            "filters": {
                "family": family,
                "category": category,
                "q": query,
            },
        }

    def _find_case(self, case_id: str) -> dict[str, Any]:
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise FrontendAPIError("invalid case identifier")
        matches = [
            case
            for case in self.diagnostic.get("cases") or []
            if case.get("case_id") == case_id
        ]
        if len(matches) != 1:
            raise FrontendAPIError(f"unknown diagnostic case: {case_id}")
        return matches[0]

    def _case_rtl(self, case_id: str) -> dict[str, Any]:
        if case_id.startswith("v21_"):
            split = "calibration-v21"
            metadata_key = "v21"
        elif case_id.startswith("v2_"):
            split = "calibration-v2"
            metadata_key = "v2"
        else:
            raise FrontendAPIError(f"unsupported generated case identifier: {case_id}")
        root = self.config.corpus_dir / split / case_id
        manifest = _load_json(root / "manifest.json", "case manifest")
        variants = manifest.get("variants") or []
        baseline_id = manifest.get("baseline_id")
        baseline = next(
            (item for item in variants if item.get("id") == baseline_id),
            None,
        )
        if baseline is None:
            raise FrontendAPIError(f"baseline variant missing for {case_id}")
        relative_file = Path(str(baseline.get("file", "")))
        if relative_file.is_absolute() or ".." in relative_file.parts:
            raise FrontendAPIError(f"unsafe RTL path for {case_id}")
        source_path = root / relative_file
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FrontendAPIError(f"could not read generated RTL for {case_id}: {exc}") from exc
        topology = (
            ((manifest.get("metadata") or {}).get(metadata_key) or {}).get("topology")
            or {}
        )
        return {
            "split": split,
            "file": str(relative_file),
            "top": baseline.get("kernel_top"),
            "language": "systemverilog",
            "source": source,
            "topology": topology,
        }

    def case_detail(self, case_id: str) -> dict[str, Any]:
        case = self._find_case(case_id)
        classification = case.get("classification") or {}
        selected_template = classification.get("selected_template")
        best_templates = set(classification.get("best_candidate_ids") or [])
        candidates = []
        for candidate in case.get("candidates") or []:
            predictions = candidate.get("predicted_improvement_percent") or {}
            measured = candidate.get("measured_improvement_percent") or {}
            probability = float(candidate.get("eligibility_probability", 0.0))
            threshold = classification.get("threshold")
            candidates.append(
                {
                    "candidate_id": (
                        f"{case_id}:{candidate.get('template_id', 'unknown')}"
                    ),
                    "template_id": candidate.get("template_id"),
                    "selected": candidate.get("template_id") == selected_template,
                    "measured_best": candidate.get("template_id") in best_templates,
                    "measured_eligible": bool(candidate.get("eligible")),
                    "safe_best": bool(candidate.get("safe_best")),
                    "eligibility": {
                        "probability": round(probability, 6),
                        "threshold": threshold,
                        "margin": (
                            round(probability - float(threshold), 6)
                            if threshold is not None
                            else None
                        ),
                    },
                    "predicted": {
                        metric: round(float(predictions.get(metric, 0.0)), 6)
                        for metric in METRICS
                    },
                    "measured": {
                        metric: round(float(measured.get(metric, 0.0)), 6)
                        for metric in METRICS
                    },
                    "predicted_utility": candidate.get("predicted_utility"),
                    "measured_utility": candidate.get("measured_utility"),
                    "stages": {
                        "generation": "available",
                        "lint": "passed",
                        "formal": "passed",
                        "physical": "not_run",
                    },
                }
            )
        return {
            "api_version": FRONTEND_API_VERSION,
            "schema_version": FRONTEND_API_SCHEMA_VERSION,
            "case": self._summary_case(case),
            "classification": classification,
            "ood": case.get("ood_leave_one_topology_out"),
            "rtl": self._case_rtl(case_id),
            "candidates": candidates,
            "provenance": {
                "evidence_kind": "calibration",
                "blind_labels_used": False,
                "topology_signature": case.get("topology_signature"),
                "diagnostic_hash": self.diagnostic.get("diagnostic_hash"),
            },
        }


def category_options(cases: Iterable[dict[str, Any]]) -> list[str]:
    """Return stable unique categories; useful to clients and tests."""
    counts = Counter(
        str((case.get("classification") or {}).get("category", ""))
        for case in cases
    )
    return sorted(category for category in counts if category)
