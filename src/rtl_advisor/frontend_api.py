from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
from typing import Any, Iterable

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_schema import (
    AGENT_V2_SCHEMA_VERSION,
    RUN_SCHEMA_ID,
    MVPSchemaError,
    compile_context_snapshot,
    source_integrity,
    stable_hash,
    read_hashed_json,
)
from rtl_advisor.mvp_measure import MVPMeasurementError, classify_recipe
from rtl_advisor.rtl_input import DesignInputV2, SourceFileV2


FRONTEND_API_VERSION = "v1"
FRONTEND_API_SCHEMA_VERSION = 1
FRONTEND_SOURCE_VERSION = "v22"
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
RUN_ID_PATTERN = re.compile(r"^mvp-[0-9a-f]{20}$")
CANDIDATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
METRICS = ("delay", "area", "cell_count")
RUNS_API_VERSION = "v1"
RUNS_API_SCHEMA_VERSION = 1
RUN_DOCUMENT_TYPES = {
    "review": "rtl-advisor.agent.v2.review",
    "candidate": "rtl-advisor.agent.v2.candidate",
    "verification": "rtl-advisor.agent.v2.verification",
    "measurement": "rtl-advisor.agent.v2.measurement",
    "measurement_failure": "rtl-advisor.agent.v2.measurement-failure",
    "report": "rtl-advisor.agent.v2.report",
}
RUN_OUTCOMES = {
    "measured_improvement": {
        "label": "Measured improvement",
        "tone": "positive",
        "summary": "Both Yosys/ABC recipes measured an improvement.",
        "detail": "This result applies only to the recorded recipes and Liberty file, not a target implementation flow.",
    },
    "synthesis_handles": {
        "label": "Synthesis already handles it",
        "tone": "neutral",
        "summary": "Both Yosys/ABC recipes produced a neutral result.",
        "detail": "No RTL change is advised from this evidence.",
    },
    "flow_dependent": {
        "label": "Result depends on synthesis recipe",
        "tone": "warning",
        "summary": "The standard and stronger recipes did not agree.",
        "detail": "Keep the original RTL and evaluate in the target flow before acting.",
    },
    "regression": {
        "label": "Candidate regressed",
        "tone": "negative",
        "summary": "At least one Yosys/ABC recipe measured a regression.",
        "detail": "Do not use this candidate.",
    },
    "formal_passed": {
        "label": "Formal proof passed",
        "tone": "progress",
        "summary": "The candidate is logically equivalent under the recorded two-state RTL proof.",
        "detail": "Synthesis measurement has not been recorded yet.",
    },
    "formal_failed": {
        "label": "Formal proof failed",
        "tone": "negative",
        "summary": "The candidate was not proven equivalent to the original RTL.",
        "detail": "The candidate is blocked and synthesis measurement must not run.",
    },
    "formal_inconclusive": {
        "label": "Formal proof inconclusive",
        "tone": "warning",
        "summary": "The proof did not establish equivalence.",
        "detail": "Treat the candidate as unverified and keep the original RTL.",
    },
    "candidate_prepared": {
        "label": "Candidate prepared",
        "tone": "progress",
        "summary": "An isolated candidate and source diff are available.",
        "detail": "Formal verification is required before any synthesis comparison.",
    },
    "candidate_available": {
        "label": "Candidate available",
        "tone": "progress",
        "summary": "A supported adder-chain pattern was found.",
        "detail": "No source was changed; candidate generation must be requested from the CLI or Codex.",
    },
    "unsupported": {
        "label": "No supported pattern",
        "tone": "muted",
        "summary": "This run contains no adder chain that the MVP can safely rewrite.",
        "detail": "The tool made no change and did not force a recommendation.",
    },
    "incomplete": {
        "label": "Evidence incomplete",
        "tone": "warning",
        "summary": "At least one eligible site is missing a required candidate, proof, or synthesis result.",
        "detail": "No positive final conclusion is supported until the missing stages are recorded.",
    },
}


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _run_state(decision: str) -> str:
    if decision in {
        "measured_improvement",
        "synthesis_handles",
        "flow_dependent",
        "regression",
    }:
        return "completed"
    if decision in {"formal_failed", "formal_inconclusive"}:
        return "failed"
    if decision == "unsupported":
        return "unsupported"
    if decision == "incomplete":
        return "incomplete"
    return "in_progress"


def _outcome(decision: str) -> dict[str, str]:
    default = {
        "label": _display_name(decision or "in progress"),
        "tone": "progress",
        "summary": "This run has not reached a final synthesis result.",
        "detail": "Use the recorded CLI command to continue the workflow.",
    }
    return {"id": decision, **RUN_OUTCOMES.get(decision, default)}


def _command_text(command: Any) -> str | None:
    if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
        return None
    return shlex.join(command)


def _measurement_decision(profiles: Any, *, objective: str) -> str:
    if not isinstance(profiles, dict) or set(profiles) != {"standard", "stronger"}:
        raise FrontendAPIError(
            "measurement does not contain both frozen synthesis profiles"
        )
    classifications: list[str] = []
    for name in ("standard", "stronger"):
        profile = profiles[name]
        if not isinstance(profile, dict):
            raise FrontendAPIError(f"measurement profile {name!r} is invalid")
        baseline = profile.get("baseline")
        candidate = profile.get("candidate")
        baseline_metrics = baseline.get("metrics") if isinstance(baseline, dict) else None
        candidate_metrics = candidate.get("metrics") if isinstance(candidate, dict) else None
        if not isinstance(baseline_metrics, dict) or not isinstance(
            candidate_metrics, dict
        ):
            raise FrontendAPIError(
                f"measurement profile {name!r} is missing baseline or candidate metrics"
            )
        try:
            expected = classify_recipe(objective, baseline_metrics, candidate_metrics)
        except (MVPMeasurementError, TypeError, ValueError) as exc:
            raise FrontendAPIError(
                f"measurement profile {name!r} cannot be classified: {exc}"
            ) from exc
        recorded = str(profile.get("classification", ""))
        if recorded != expected:
            raise FrontendAPIError(
                f"measurement profile {name!r} classification does not match its metrics"
            )
        classifications.append(expected)
    standard, stronger = classifications
    if standard not in {"improved", "neutral", "regressed"} or stronger not in {
        "improved",
        "neutral",
        "regressed",
    }:
        raise FrontendAPIError("measurement has an invalid recipe classification")
    if standard == stronger == "improved":
        return "measured_improvement"
    if standard == stronger == "neutral":
        return "synthesis_handles"
    if "regressed" in {standard, stronger}:
        return "regression"
    return "flow_dependent"


class FrontendDataStore:
    """Read-only adapter from frozen model evidence to frontend API v1."""

    def __init__(self, config: ProjectConfig):
        self.config = config
        model_root = config.artifacts_dir / "models/v22"
        self._runs_root = config.artifacts_dir / "agent-v2/runs"
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
                {"method": "GET", "path": "/api/runs/v1"},
                {"method": "GET", "path": "/api/runs/v1/{run_id}"},
                {"method": "GET", "path": "/api/runs/v1/{run_id}/diff"},
                {"method": "GET", "path": "/api/runs/v1/{run_id}/artifacts"},
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
        split_root = (self.config.corpus_dir / split).resolve()
        root = self.config.corpus_dir / split / case_id
        if root.is_symlink() or not _is_within(root.resolve(), split_root):
            raise FrontendAPIError(f"unsafe generated case path for {case_id}")
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink():
            raise FrontendAPIError(f"unsafe case manifest path for {case_id}")
        manifest = _load_json(manifest_path, "case manifest")
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
        recorded_source_path = root / relative_file
        source_path = recorded_source_path.resolve()
        if (
            recorded_source_path.is_symlink()
            or not _is_within(source_path, root.resolve())
            or not source_path.is_file()
        ):
            raise FrontendAPIError(f"unsafe RTL path for {case_id}")
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

    def _validate_run_id(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise FrontendAPIError("invalid MVP run identifier")
        root = (self._runs_root / run_id).resolve()
        runs_root = self._runs_root.resolve()
        if not _is_within(root, runs_root):
            raise FrontendAPIError("invalid MVP run path")
        if not root.is_dir():
            raise FrontendAPIError(f"unknown MVP run: {run_id}")
        return root

    def _read_agent_record(
        self,
        path: Path,
        *,
        kind: str,
        run_id: str,
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = read_hashed_json(
                path,
                document_type=RUN_DOCUMENT_TYPES[kind],
                schema_version=AGENT_V2_SCHEMA_VERSION,
            )
        except MVPSchemaError as exc:
            raise FrontendAPIError(f"invalid {kind} record for {run_id}: {exc}") from exc
        if payload.get("run_schema") != RUN_SCHEMA_ID:
            raise FrontendAPIError(
                f"invalid {kind} record for {run_id}: expected {RUN_SCHEMA_ID}"
            )
        if payload.get("run_id") != run_id:
            raise FrontendAPIError(
                f"invalid {kind} record for {run_id}: run identifier mismatch"
            )
        if candidate_id is not None and payload.get("candidate_id") != candidate_id:
            raise FrontendAPIError(
                f"invalid {kind} record for {run_id}: candidate identifier mismatch"
            )
        return payload

    @staticmethod
    def _current_input_context(
        input_record: dict[str, Any], run_id: str
    ) -> tuple[DesignInputV2, dict[str, Any]]:
        try:
            files = tuple(
                SourceFileV2(path=str(item["path"]), sha256=str(item["sha256"]))
                for item in input_record["files"]
            )
            design = DesignInputV2(
                schema_version=int(input_record["design_input_schema_version"]),
                top=str(input_record["top"]),
                files=files,
                include_dirs=tuple(
                    str(item) for item in input_record.get("include_dirs", [])
                ),
                defines=tuple(str(item) for item in input_record.get("defines", [])),
                filelists=tuple(
                    str(item) for item in input_record.get("filelists", [])
                ),
                design_hash=str(input_record["design_hash"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FrontendAPIError(
                f"invalid input record for {run_id}: malformed design input: {exc}"
            ) from exc

        integrity = source_integrity(input_record["files"])
        if not integrity["ok"]:
            raise FrontendAPIError(
                f"stale MVP run {run_id}: baseline source hashes changed"
            )
        expected_context = input_record.get("compile_context")
        if not isinstance(expected_context, dict):
            raise FrontendAPIError(
                f"invalid input record for {run_id}: compile-context snapshot is missing"
            )
        try:
            current_context = compile_context_snapshot(design)
        except MVPSchemaError as exc:
            raise FrontendAPIError(
                f"stale MVP run {run_id}: compile context is unavailable: {exc}"
            ) from exc
        if current_context != expected_context:
            raise FrontendAPIError(
                f"stale MVP run {run_id}: baseline compile context changed"
            )
        return design, integrity

    def _validate_candidate_artifacts(
        self,
        root: Path,
        candidate_root: Path,
        candidate: dict[str, Any],
        review: dict[str, Any],
    ) -> None:
        prepared = candidate.get("candidate")
        finding = candidate.get("finding")
        if not isinstance(prepared, dict) or not isinstance(finding, dict):
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: rewrite evidence is missing"
            )
        expected_nested_hash = prepared.get("semantic_hash")
        nested_core = {
            key: value for key, value in prepared.items() if key != "semantic_hash"
        }
        if expected_nested_hash != stable_hash(nested_core):
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: nested rewrite hash mismatch"
            )
        candidate_id = str(candidate.get("candidate_id", ""))
        finding_id = str(finding.get("finding_id", ""))
        review_findings = {
            str(item.get("finding_id")): item
            for item in review.get("findings", [])
            if isinstance(item, dict)
        }
        if (
            finding_id not in review_findings
            or finding != review_findings[finding_id]
            or prepared.get("candidate_id") != candidate_id
            or prepared.get("finding_id") != finding_id
        ):
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: eligible finding mismatch"
            )
        design = prepared.get("candidate_design")
        files = design.get("files") if isinstance(design, dict) else None
        if not isinstance(files, list) or not files:
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: candidate design is missing"
            )
        for item in files:
            if not isinstance(item, dict):
                raise FrontendAPIError(
                    f"invalid candidate record for {root.name}: candidate source entry is invalid"
                )
            recorded_source_path = Path(str(item.get("path", ""))).expanduser()
            source_path = recorded_source_path.resolve()
            if (
                not _is_within(source_path, candidate_root.resolve())
                or recorded_source_path.is_symlink()
            ):
                raise FrontendAPIError(
                    f"invalid candidate record for {root.name}: candidate source escapes its workspace"
                )
        if not source_integrity(files)["ok"]:
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: candidate source hash mismatch"
            )
        diff_path = self._safe_recorded_path(prepared.get("diff_path"), root)
        if diff_path is None or not _is_within(diff_path, candidate_root.resolve()):
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: candidate diff is missing"
            )
        if _sha256(diff_path) != prepared.get("diff_sha256"):
            raise FrontendAPIError(
                f"invalid candidate record for {root.name}: candidate diff hash mismatch"
            )

    def _validate_report_chain(
        self,
        root: Path,
        records: dict[str, Any],
        report: dict[str, Any],
    ) -> None:
        expected_parents = {
            "review_semantic_hash": records["review"]["semantic_hash"],
            "candidates": {
                entry["candidate_id"]: {
                    "candidate_semantic_hash": entry["candidate"]["semantic_hash"],
                    "verification_semantic_hash": (
                        entry["verification"]["semantic_hash"]
                        if isinstance(entry.get("verification"), dict)
                        else None
                    ),
                    "measurement_semantic_hash": (
                        entry["measurement"]["semantic_hash"]
                        if isinstance(entry.get("measurement"), dict)
                        else None
                    ),
                }
                for entry in records["candidates"]
            },
        }
        if report.get("parents") != expected_parents:
            raise FrontendAPIError(
                f"invalid report record for {root.name}: stage parent hashes mismatch"
            )
        embedded_review = report.get("review")
        if embedded_review != records["review"]:
            raise FrontendAPIError(
                f"invalid report record for {root.name}: embedded review mismatch"
            )
        embedded_candidates = report.get("candidates")
        if not isinstance(embedded_candidates, list):
            raise FrontendAPIError(
                f"invalid report record for {root.name}: embedded candidates are missing"
            )
        if embedded_candidates != records["candidates"]:
            raise FrontendAPIError(
                f"invalid report record for {root.name}: embedded stage records mismatch"
            )
        embedded_hashes = {
            str(entry.get("candidate_id")): {
                "candidate": (entry.get("candidate") or {}).get("semantic_hash"),
                "verification": (entry.get("verification") or {}).get(
                    "semantic_hash"
                ),
                "measurement": (entry.get("measurement") or {}).get(
                    "semantic_hash"
                ),
            }
            for entry in embedded_candidates
            if isinstance(entry, dict)
        }
        current_hashes = {
            entry["candidate_id"]: {
                "candidate": entry["candidate"].get("semantic_hash"),
                "verification": (entry.get("verification") or {}).get(
                    "semantic_hash"
                ),
                "measurement": (entry.get("measurement") or {}).get(
                    "semantic_hash"
                ),
            }
            for entry in records["candidates"]
        }
        if embedded_hashes != current_hashes:
            raise FrontendAPIError(
                f"invalid report record for {root.name}: embedded stage records mismatch"
            )

        snapshot_path = root / "reports" / f"{report['semantic_hash']}.json"
        try:
            snapshot = read_hashed_json(
                snapshot_path,
                document_type=RUN_DOCUMENT_TYPES["report"],
                schema_version=AGENT_V2_SCHEMA_VERSION,
            )
            latest = read_hashed_json(
                root / "reports" / "latest.json",
                document_type="rtl-advisor.run.report-latest",
                schema_version=1,
            )
        except MVPSchemaError as exc:
            raise FrontendAPIError(
                f"invalid report snapshot for {root.name}: {exc}"
            ) from exc
        if snapshot != report:
            raise FrontendAPIError(
                f"invalid report snapshot for {root.name}: compatibility report mismatch"
            )
        recorded_snapshot = self._safe_recorded_path(
            latest.get("report_snapshot"), root
        )
        recorded_html = self._safe_recorded_path(latest.get("html_snapshot"), root)
        expected_html_path = (
            root / "reports" / f"{report['semantic_hash']}.html"
        ).resolve()
        if (
            latest.get("run_id") != root.name
            or latest.get("report_semantic_hash") != report.get("semantic_hash")
            or recorded_snapshot != snapshot_path.resolve()
            or recorded_html != expected_html_path
        ):
            raise FrontendAPIError(
                f"invalid report snapshot for {root.name}: latest pointer mismatch"
            )
        try:
            from rtl_advisor.mvp_agent import _render_report_html

            expected_html = _render_report_html(report)
            actual_html = recorded_html.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FrontendAPIError(
                f"invalid report snapshot for {root.name}: could not read HTML: {exc}"
            ) from exc
        if actual_html != expected_html:
            raise FrontendAPIError(
                f"invalid report snapshot for {root.name}: HTML content mismatch"
            )

    def _load_run_records(self, run_id: str) -> dict[str, Any]:
        root = self._validate_run_id(run_id)
        review_path = root / "review.json"
        if not review_path.is_file():
            raise FrontendAPIError(f"invalid MVP run {run_id}: review record is missing")
        review = self._read_agent_record(
            review_path,
            kind="review",
            run_id=run_id,
        )
        findings = review.get("findings")
        if not isinstance(findings, list) or any(
            not isinstance(item, dict)
            or not CANDIDATE_ID_PATTERN.fullmatch(str(item.get("finding_id", "")))
            for item in findings
        ):
            raise FrontendAPIError(
                f"invalid review record for {run_id}: findings are invalid"
            )
        finding_ids = [str(item["finding_id"]) for item in findings]
        if len(set(finding_ids)) != len(finding_ids):
            raise FrontendAPIError(
                f"invalid review record for {run_id}: finding IDs are duplicated"
            )
        expected_review_decision = "candidate_available" if findings else "unsupported"
        if review.get("decision") != expected_review_decision:
            raise FrontendAPIError(
                f"invalid review record for {run_id}: decision does not match findings"
            )

        input_record: dict[str, Any] | None = None
        input_path = root / "input.json"
        if not input_path.is_file() or input_path.is_symlink():
            raise FrontendAPIError(f"invalid MVP run {run_id}: input record is missing")
        try:
            input_record = read_hashed_json(
                input_path,
                document_type="rtl-advisor.run.design-input",
                schema_version=1,
            )
        except MVPSchemaError as exc:
            raise FrontendAPIError(
                f"invalid input record for {run_id}: {exc}"
            ) from exc
        recorded_input_hash = (review.get("evidence") or {}).get(
            "input_semantic_hash"
        )
        if recorded_input_hash != input_record.get("semantic_hash"):
            raise FrontendAPIError(
                f"invalid review record for {run_id}: input parent hash mismatch"
            )
        _, current_input_integrity = self._current_input_context(input_record, run_id)

        candidates: list[dict[str, Any]] = []
        candidates_root = root / "candidates"
        if candidates_root.is_dir():
            for candidate_root in sorted(
                candidates_root.iterdir(), key=lambda path: path.name
            ):
                if candidate_root.is_symlink() or not candidate_root.is_dir():
                    raise FrontendAPIError(
                        f"invalid candidate artifact entry in {run_id}: {candidate_root.name!r}"
                    )
                candidate_id = candidate_root.name
                if not CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
                    raise FrontendAPIError(
                        f"invalid candidate directory in {run_id}: {candidate_id!r}"
                    )
                candidate_path = candidate_root / "candidate.json"
                if not candidate_path.is_file() or candidate_path.is_symlink():
                    raise FrontendAPIError(
                        f"invalid candidate record for {run_id}: evidence is missing for {candidate_id}"
                    )
                candidate = self._read_agent_record(
                    candidate_path,
                    kind="candidate",
                    run_id=run_id,
                    candidate_id=candidate_id,
                )
                if (candidate.get("parents") or {}).get(
                    "review_semantic_hash"
                ) != review.get("semantic_hash"):
                    raise FrontendAPIError(
                        f"invalid candidate record for {run_id}: review parent hash mismatch"
                    )
                self._validate_candidate_artifacts(
                    root, candidate_root, candidate, review
                )
                entry: dict[str, Any] = {
                    "candidate_id": candidate_id,
                    "candidate": candidate,
                }
                verification_path = candidate_root / "verification.json"
                if verification_path.is_file():
                    verification = self._read_agent_record(
                        verification_path,
                        kind="verification",
                        run_id=run_id,
                        candidate_id=candidate_id,
                    )
                    if (verification.get("parents") or {}).get(
                        "candidate_semantic_hash"
                    ) != candidate.get("semantic_hash"):
                        raise FrontendAPIError(
                            f"invalid verification record for {run_id}: candidate parent hash mismatch"
                        )
                    status = str(verification.get("status", ""))
                    expected_formal = {
                        "formal_passed": "passed",
                        "formal_failed": "failed",
                        "formal_inconclusive": "inconclusive",
                    }.get(status)
                    if expected_formal != str(
                        (verification.get("formal") or {}).get("status", "")
                    ) or (verification.get("safe") is True) != (
                        status == "formal_passed"
                    ):
                        raise FrontendAPIError(
                            f"invalid verification record for {run_id}: formal status or safety flag mismatch"
                        )
                    entry["verification"] = verification
                measurement_path = candidate_root / "measurement.json"
                if measurement_path.is_file():
                    if "verification" not in entry:
                        raise FrontendAPIError(
                            f"invalid measurement record for {run_id}: verification is missing"
                        )
                    measurement = self._read_agent_record(
                        measurement_path,
                        kind="measurement",
                        run_id=run_id,
                        candidate_id=candidate_id,
                    )
                    if (measurement.get("parents") or {}).get(
                        "verification_semantic_hash"
                    ) != entry["verification"].get("semantic_hash"):
                        raise FrontendAPIError(
                            f"invalid measurement record for {run_id}: verification parent hash mismatch"
                        )
                    if entry["verification"].get("status") != "formal_passed" or entry[
                        "verification"
                    ].get("safe") is not True:
                        raise FrontendAPIError(
                            f"invalid measurement record for {run_id}: passing formal proof is missing"
                        )
                    if (measurement.get("formal") or {}).get(
                        "semantic_hash"
                    ) != entry["verification"].get("semantic_hash"):
                        raise FrontendAPIError(
                            f"invalid measurement record for {run_id}: formal evidence hash mismatch"
                        )
                    if measurement.get("objective") != review.get("objective"):
                        raise FrontendAPIError(
                            f"invalid measurement record for {run_id}: objective does not match review"
                        )
                    expected_measurement = _measurement_decision(
                        measurement.get("measurements"),
                        objective=str(review.get("objective", "")),
                    )
                    if measurement.get("decision") != expected_measurement:
                        raise FrontendAPIError(
                            f"invalid measurement record for {run_id}: decision does not match recipe evidence"
                        )
                    entry["measurement"] = measurement
                failure_root = candidate_root / "measurement-failures"
                if failure_root.is_symlink() or failure_root.exists():
                    if failure_root.is_symlink() or not failure_root.is_dir():
                        raise FrontendAPIError(
                            f"invalid measurement-failure artifacts for {run_id}: "
                            f"expected a directory for {candidate_id}"
                        )
                    if "verification" not in entry:
                        raise FrontendAPIError(
                            f"invalid measurement-failure record for {run_id}: "
                            "verification is missing"
                        )
                    verification = entry["verification"]
                    if (
                        verification.get("status") != "formal_passed"
                        or verification.get("safe") is not True
                    ):
                        raise FrontendAPIError(
                            f"invalid measurement-failure record for {run_id}: "
                            "passing formal proof is missing"
                        )
                    failures: list[dict[str, Any]] = []
                    for failure_path in sorted(
                        failure_root.iterdir(), key=lambda path: path.name
                    ):
                        if (
                            failure_path.is_symlink()
                            or not failure_path.is_file()
                            or failure_path.suffix != ".json"
                        ):
                            raise FrontendAPIError(
                                f"invalid measurement-failure artifact for {run_id}: "
                                f"{failure_path.name!r}"
                            )
                        failure = self._read_agent_record(
                            failure_path,
                            kind="measurement_failure",
                            run_id=run_id,
                            candidate_id=candidate_id,
                        )
                        if (failure.get("parents") or {}).get(
                            "verification_semantic_hash"
                        ) != verification.get("semantic_hash"):
                            raise FrontendAPIError(
                                f"invalid measurement-failure record for {run_id}: "
                                "verification parent hash mismatch"
                            )
                        error = failure.get("error")
                        if (
                            failure.get("status") != "failed"
                            or failure.get("decision") != "measurement_failed"
                            or failure.get("objective") != review.get("objective")
                            or not isinstance(error, dict)
                            or not isinstance(error.get("code"), str)
                            or not error["code"]
                            or not isinstance(error.get("message"), str)
                            or not error["message"]
                        ):
                            raise FrontendAPIError(
                                f"invalid measurement-failure record for {run_id}: "
                                "state or error detail is invalid"
                            )
                        failures.append(failure)
                    if failures:
                        entry["measurement_failures"] = failures
                candidates.append(entry)

        candidate_findings = [
            str((entry["candidate"].get("finding") or {}).get("finding_id", ""))
            for entry in candidates
        ]
        duplicate_findings = [
            finding_id
            for finding_id, count in Counter(candidate_findings).items()
            if finding_id and count > 1
        ]
        if duplicate_findings:
            raise FrontendAPIError(
                f"invalid candidate records for {run_id}: multiple candidates for "
                + ", ".join(sorted(duplicate_findings))
            )

        report: dict[str, Any] | None = None
        report_path = root / "report.json"
        if report_path.is_file():
            report = self._read_agent_record(
                report_path,
                kind="report",
                run_id=run_id,
            )
        records = {
            "root": root,
            "review": review,
            "input": input_record,
            "current_input_integrity": current_input_integrity,
            "candidates": candidates,
            "report": report,
        }
        if report is not None:
            derived_records = {**records, "report": None}
            completion = self._evidence_summary(derived_records)
            expected_decision = self._decision(derived_records)
            if report.get("decision") != expected_decision:
                raise FrontendAPIError(
                    f"invalid report record for {run_id}: final decision does not "
                    "match the hash-linked stages"
                )
            expected_status = "completed" if completion["complete"] else "incomplete"
            if report.get("status") != expected_status:
                raise FrontendAPIError(
                    f"invalid report record for {run_id}: completion status mismatch"
                )
            if report.get("completion") != completion:
                raise FrontendAPIError(
                    f"invalid report record for {run_id}: completeness summary mismatch"
                )
            expected_counts = {
                "formal": completion["formal_status_counts"],
                "synthesis": completion["measurement_decision_counts"],
            }
            if report.get("result_counts") != expected_counts:
                raise FrontendAPIError(
                    f"invalid report record for {run_id}: result counts mismatch"
                )
            self._validate_report_chain(root, records, report)
        return records

    @staticmethod
    def _evidence_summary(records: dict[str, Any]) -> dict[str, Any]:
        candidates = records.get("candidates") or []
        eligible_ids = [
            str(item.get("finding_id"))
            for item in records["review"].get("findings", [])
            if isinstance(item, dict)
        ]
        candidates_by_finding = {
            str((entry["candidate"].get("finding") or {}).get("finding_id", "")): entry
            for entry in candidates
        }
        missing_candidates = sorted(set(eligible_ids) - set(candidates_by_finding))
        missing_formal: list[str] = []
        missing_measurement: list[str] = []
        formal_counts: Counter[str] = Counter()
        measurement_counts: Counter[str] = Counter()
        terminal_count = 0
        for entry in candidates:
            candidate_id = str(entry.get("candidate_id", ""))
            verification = entry.get("verification")
            measurement = entry.get("measurement")
            if not isinstance(verification, dict):
                missing_formal.append(candidate_id)
                continue
            status = str(verification.get("status", "unknown"))
            formal_counts[status] += 1
            if status == "formal_passed":
                if not isinstance(measurement, dict):
                    missing_measurement.append(candidate_id)
                else:
                    measurement_counts[str(measurement.get("decision", "unknown"))] += 1
                    terminal_count += 1
            else:
                terminal_count += 1
        terminal_outcomes = {*formal_counts, *measurement_counts}
        terminal_outcomes.discard("formal_passed")
        return {
            "complete": not (missing_candidates or missing_formal or missing_measurement),
            "eligible_site_count": len(eligible_ids),
            "candidate_count": len(candidates),
            "formal_count": sum(formal_counts.values()),
            "measurement_count": sum(measurement_counts.values()),
            "terminal_candidate_count": terminal_count,
            "missing_candidate_finding_ids": missing_candidates,
            "missing_formal_candidate_ids": sorted(missing_formal),
            "missing_measurement_candidate_ids": sorted(missing_measurement),
            "formal_status_counts": dict(sorted(formal_counts.items())),
            "measurement_decision_counts": dict(
                sorted(measurement_counts.items())
            ),
            "mixed_outcomes": len(terminal_outcomes) > 1,
        }

    @classmethod
    def _decision(cls, records: dict[str, Any]) -> str:
        report = records.get("report")
        if isinstance(report, dict):
            return str(report.get("decision", "unsupported"))
        candidates = records.get("candidates") or []
        completion = cls._evidence_summary(records)
        measurements = [
            str(entry["measurement"].get("decision", ""))
            for entry in candidates
            if isinstance(entry.get("measurement"), dict)
        ]
        formal_statuses = [
            str(entry["verification"].get("status", ""))
            for entry in candidates
            if isinstance(entry.get("verification"), dict)
        ]
        if "regression" in measurements:
            return "regression"
        if "formal_failed" in formal_statuses:
            return "formal_failed"
        if "formal_inconclusive" in formal_statuses:
            return "formal_inconclusive"
        if completion["complete"] is not True and measurements:
            return "incomplete"
        if "flow_dependent" in measurements:
            return "flow_dependent"
        if "measured_improvement" in measurements:
            return "measured_improvement"
        if measurements and set(measurements) == {"synthesis_handles"}:
            return "synthesis_handles"
        if "formal_passed" in formal_statuses:
            return "formal_passed"
        if candidates:
            return "candidate_prepared"
        return str(records["review"].get("decision", "unsupported"))

    @staticmethod
    def _candidate_summary(entry: dict[str, Any]) -> dict[str, Any]:
        candidate = entry["candidate"]
        verification = entry.get("verification")
        measurement = entry.get("measurement")
        measurement_failures = entry.get("measurement_failures") or []
        latest_measurement_failure = (
            measurement_failures[-1] if measurement_failures else None
        )
        prepared = candidate.get("candidate") or {}
        return {
            "candidate_id": entry["candidate_id"],
            "status": (
                measurement.get("decision")
                if isinstance(measurement, dict)
                else "measurement_failed"
                if isinstance(latest_measurement_failure, dict)
                else verification.get("status")
                if isinstance(verification, dict)
                else candidate.get("status")
            ),
            "finding": candidate.get("finding"),
            "source_integrity": candidate.get("source_integrity"),
            "candidate": prepared,
            "formal": verification,
            "measurement": measurement,
            "measurement_failures": measurement_failures,
            "commands": {
                "candidate": _command_text(candidate.get("command")),
                "verify": _command_text(
                    verification.get("command") if isinstance(verification, dict) else None
                ),
                "measure": _command_text(
                    measurement.get("command")
                    if isinstance(measurement, dict)
                    else latest_measurement_failure.get("command")
                    if isinstance(latest_measurement_failure, dict)
                    else None
                ),
            },
            "semantic_hashes": {
                "candidate": candidate.get("semantic_hash"),
                "verification": (
                    verification.get("semantic_hash")
                    if isinstance(verification, dict)
                    else None
                ),
                "measurement": (
                    measurement.get("semantic_hash")
                    if isinstance(measurement, dict)
                    else None
                ),
                "measurement_failures": [
                    failure.get("semantic_hash")
                    for failure in measurement_failures
                    if isinstance(failure, dict)
                ],
            },
        }

    @staticmethod
    def _stages(
        decision: str,
        review: dict[str, Any],
        candidates: list[dict[str, Any]],
        completion: dict[str, Any],
    ) -> list[dict[str, str]]:
        has_finding = bool(review.get("findings"))
        has_candidate = bool(candidates)
        verifications = [
            entry.get("verification")
            for entry in candidates
            if isinstance(entry.get("verification"), dict)
        ]
        measurements = [
            entry.get("measurement")
            for entry in candidates
            if isinstance(entry.get("measurement"), dict)
        ]
        formal_failed = any(
            record.get("status") in {"formal_failed", "formal_inconclusive"}
            for record in verifications
        )
        formal_passed = any(record.get("status") == "formal_passed" for record in verifications)
        synthesis_failed = any(
            entry.get("measurement_failures") and not entry.get("measurement")
            for entry in candidates
        )

        candidate_status = (
            "complete"
            if has_candidate
            and not completion["missing_candidate_finding_ids"]
            else "active"
            if has_finding
            else "unavailable"
        )
        if formal_failed:
            formal_status = "failed"
        elif formal_passed:
            formal_status = "complete"
        elif has_candidate:
            formal_status = "active"
        else:
            formal_status = "pending" if has_finding else "unavailable"
        if synthesis_failed:
            synthesis_status = "failed"
        elif completion["complete"] and measurements:
            synthesis_status = "complete"
        elif formal_passed:
            synthesis_status = "active"
        elif formal_failed:
            synthesis_status = "blocked"
        else:
            synthesis_status = "pending" if has_finding else "unavailable"
        state = _run_state(decision)
        final_status = (
            "complete"
            if completion["complete"] and state in {"completed", "unsupported"}
            else "failed"
            if completion["complete"] and state == "failed"
            else "pending"
        )
        return [
            {"id": "review", "label": "Review", "status": "complete"},
            {"id": "candidate", "label": "Candidate", "status": candidate_status},
            {"id": "formal", "label": "Formal", "status": formal_status},
            {"id": "synthesis", "label": "Synthesis", "status": synthesis_status},
            {"id": "result", "label": "Final result", "status": final_status},
        ]

    def _run_payload(self, records: dict[str, Any], *, include_records: bool) -> dict[str, Any]:
        root: Path = records["root"]
        review = records["review"]
        candidates = records["candidates"]
        decision = self._decision(records)
        completion = self._evidence_summary(records)
        report = records.get("report") or {}
        state = (
            "incomplete"
            if report.get("status") == "incomplete"
            else _run_state(decision)
        )
        paths = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
        latest_path = max(paths, key=lambda path: path.stat().st_mtime, default=root)
        input_context = review.get("input") or {}
        item: dict[str, Any] = {
            "run_id": review.get("run_id"),
            "run_schema": RUN_SCHEMA_ID,
            "state": state,
            "decision": decision,
            "outcome": _outcome(decision),
            "objective": review.get("objective"),
            "top": input_context.get("top"),
            "input": input_context,
            "finding_count": len(review.get("findings") or []),
            "candidate_count": len(candidates),
            "completion": completion,
            "source_integrity": records.get("current_input_integrity"),
            "updated_at": _timestamp(latest_path),
            "semantic_hash": review.get("semantic_hash"),
        }
        if include_records:
            candidate_payloads = [self._candidate_summary(entry) for entry in candidates]
            item.update(
                {
                    "stages": self._stages(
                        decision, review, candidates, completion
                    ),
                    "findings": review.get("findings") or [],
                    "candidates": candidate_payloads,
                    "limitations": (
                        (records.get("report") or {}).get("limitations")
                        or review.get("limitations")
                        or []
                    ),
                    "commands": {
                        "review": _command_text(review.get("command")),
                        "report": _command_text(
                            (records.get("report") or {}).get("command")
                        ),
                    },
                    "artifact_count": len(paths),
                    "records": {
                        "review": review,
                        "report": records.get("report"),
                    },
                }
            )
        return item

    def runs(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        invalid: list[dict[str, str]] = []
        if self._runs_root.is_dir():
            for root in sorted(self._runs_root.iterdir(), key=lambda path: path.name):
                if not root.is_dir() or not RUN_ID_PATTERN.fullmatch(root.name):
                    continue
                try:
                    items.append(
                        self._run_payload(
                            self._load_run_records(root.name),
                            include_records=False,
                        )
                    )
                except FrontendAPIError as exc:
                    invalid.append({"run_id": root.name, "error": str(exc)})
        items.sort(key=lambda item: str(item["updated_at"]), reverse=True)
        return {
            "api_version": RUNS_API_VERSION,
            "schema_version": RUNS_API_SCHEMA_VERSION,
            "run_schema": RUN_SCHEMA_ID,
            "read_only": True,
            "items": items,
            "invalid": invalid,
            "count": len(items),
        }

    def run_detail(self, run_id: str) -> dict[str, Any]:
        return {
            "api_version": RUNS_API_VERSION,
            "schema_version": RUNS_API_SCHEMA_VERSION,
            "read_only": True,
            "run": self._run_payload(
                self._load_run_records(run_id),
                include_records=True,
            ),
        }

    @staticmethod
    def _safe_recorded_path(raw_path: Any, root: Path) -> Path | None:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = root / path
        if path.is_symlink():
            return None
        path = path.resolve()
        if not _is_within(path, root.resolve()) or not path.is_file():
            return None
        return path

    def run_diff(self, run_id: str) -> dict[str, Any]:
        records = self._load_run_records(run_id)
        root: Path = records["root"]
        items: list[dict[str, Any]] = []
        for entry in records["candidates"]:
            candidate = entry["candidate"]
            prepared = candidate.get("candidate") or {}
            paths = (
                (candidate.get("artifacts") or {}).get("diff"),
                prepared.get("diff_path"),
                (prepared.get("artifacts") or {}).get("diff"),
            )
            diff_path = next(
                (
                    path
                    for path in (
                        self._safe_recorded_path(value, root) for value in paths
                    )
                    if path is not None
                ),
                None,
            )
            if diff_path is None:
                continue
            try:
                content = diff_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise FrontendAPIError(
                    f"could not read candidate diff for {run_id}: {exc}"
                ) from exc
            items.append(
                {
                    "candidate_id": entry["candidate_id"],
                    "path": str(diff_path.relative_to(root)),
                    "sha256": _sha256(diff_path),
                    "content": content,
                }
            )
        return {
            "api_version": RUNS_API_VERSION,
            "schema_version": RUNS_API_SCHEMA_VERSION,
            "run_id": run_id,
            "read_only": True,
            "items": items,
        }

    def run_artifacts(self, run_id: str) -> dict[str, Any]:
        records = self._load_run_records(run_id)
        root: Path = records["root"]
        items: list[dict[str, Any]] = []
        preview_suffixes = {".diff", ".log", ".txt", ".ys"}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if not _is_within(resolved, root.resolve()):
                raise FrontendAPIError(f"artifact escapes run directory: {path}")
            relative = str(resolved.relative_to(root.resolve()))
            item: dict[str, Any] = {
                "path": relative,
                "size_bytes": resolved.stat().st_size,
                "sha256": _sha256(resolved),
                "kind": (
                    "log"
                    if resolved.suffix == ".log"
                    else "command"
                    if resolved.suffix == ".ys"
                    else "diff"
                    if resolved.suffix == ".diff"
                    else "record"
                    if resolved.suffix == ".json"
                    else "artifact"
                ),
            }
            if resolved.suffix == ".json":
                try:
                    raw = json.loads(resolved.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FrontendAPIError(
                        f"invalid JSON artifact for {run_id}: {relative}: {exc}"
                    ) from exc
                if isinstance(raw, dict) and "semantic_hash" in raw:
                    try:
                        read_hashed_json(resolved)
                    except MVPSchemaError as exc:
                        raise FrontendAPIError(
                            f"invalid hashed artifact for {run_id}: {relative}: {exc}"
                        ) from exc
                    item["semantic_hash"] = raw["semantic_hash"]
            if resolved.suffix in preview_suffixes and resolved.stat().st_size <= 64_000:
                try:
                    item["preview"] = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    pass
            items.append(item)
        commands: list[dict[str, str]] = []
        review_command = _command_text(records["review"].get("command"))
        if review_command:
            commands.append({"stage": "review", "command": review_command})
        for entry in records["candidates"]:
            for stage, record_key in (
                ("candidate", "candidate"),
                ("verify", "verification"),
                ("measure", "measurement"),
            ):
                record = entry.get(record_key)
                command = _command_text(record.get("command")) if isinstance(record, dict) else None
                if command:
                    commands.append(
                        {
                            "stage": stage,
                            "candidate_id": entry["candidate_id"],
                            "command": command,
                        }
                    )
        report_command = _command_text((records.get("report") or {}).get("command"))
        if report_command:
            commands.append({"stage": "report", "command": report_command})
        return {
            "api_version": RUNS_API_VERSION,
            "schema_version": RUNS_API_SCHEMA_VERSION,
            "run_id": run_id,
            "read_only": True,
            "commands": commands,
            "items": items,
        }


def category_options(cases: Iterable[dict[str, Any]]) -> list[str]:
    """Return stable unique categories; useful to clients and tests."""
    counts = Counter(
        str((case.get("classification") or {}).get("category", ""))
        for case in cases
    )
    return sorted(category for category in counts if category)
