from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from rtl_advisor.config import ProjectConfig
from rtl_advisor.frontend_api import FrontendAPIError, FrontendDataStore
from rtl_advisor.mvp_schema import (
    MVPSchemaError,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.workflow_contract import WorkflowContractError


EVIDENCE_SCHEMA_VERSION = 1
EXPLORATION_SCHEMA = "rtl-advisor-evidence-exploration-v1"
DATASET_SCHEMA = "rtl-advisor-evidence-chart-data-v1"
EXPLORATION_DOCUMENT_TYPE = "rtl-advisor.evidence.exploration"
DATASET_DOCUMENT_TYPE = "rtl-advisor.evidence.chart-data"

PROFILES = ("standard", "stronger")
PROFILE_ALIASES = {
    "standard": "standard",
    "stronger": "stronger",
    "M0": "standard",
    "M1": "stronger",
}
OBJECTIVES = ("timing", "area", "balanced")
CLASSIFICATIONS = ("improved", "neutral", "regressed")
DECISIONS = (
    "measured_improvement",
    "synthesis_handles",
    "flow_dependent",
    "regression",
)
SOURCE_KINDS = ("agent_v2_run", "family_study")

_WORKFLOW_ID = re.compile(r"^workflow-[0-9a-f]{20}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceExplorerError(RuntimeError):
    """Raised when immutable evidence cannot support a trusted exploration."""

    def __init__(self, message: str, *, code: str = "evidence_explorer_error") -> None:
        super().__init__(message)
        self.code = code


def _persist_immutable(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected = dict(payload)
    expected.pop("semantic_hash", None)
    expected["semantic_hash"] = stable_hash(expected)
    if path.is_file():
        existing = read_hashed_json(path)
        if existing != expected:
            raise EvidenceExplorerError(
                f"immutable evidence exploration conflicts: {path}",
                code="append_only_conflict",
            )
        return existing
    try:
        return write_hashed_json(path, expected, exclusive=True)
    except MVPSchemaError as exc:
        if exc.code == "append_only_conflict" and path.is_file():
            existing = read_hashed_json(path)
            if existing == expected:
                return existing
        raise EvidenceExplorerError(str(exc), code=exc.code) from exc


def _workflow_bindings(
    config: ProjectConfig,
) -> tuple[dict[str, str], list[dict[str, str]], set[str]]:
    """Map validated Agent run IDs to workflow IDs without writing workflow state."""

    from rtl_advisor.workflow_runner import (
        WorkflowRunnerError,
        _load_latest_authorization,
        _load_latest_state,
        _load_request,
        _load_stage,
    )

    root = config.artifacts_dir / "workflows-v1"
    bindings: dict[str, str] = {}
    invalid: list[dict[str, str]] = []
    workflow_ids: set[str] = set()
    if not root.is_dir():
        return bindings, invalid, workflow_ids
    for workflow_root in sorted(root.iterdir(), key=lambda item: item.name):
        if not workflow_root.is_dir() or not _WORKFLOW_ID.fullmatch(
            workflow_root.name
        ):
            continue
        workflow_id = workflow_root.name
        workflow_ids.add(workflow_id)
        try:
            request = _load_request(workflow_root)
            authorization = _load_latest_authorization(
                workflow_root, request=request
            )
            state = _load_latest_state(
                workflow_root,
                request=request,
                authorization=authorization,
            )
            if "review" not in state["completed_stages"]:
                continue
            review = _load_stage(workflow_root, "review")
            expected = state["stage_results"]["review"]["semantic_hash"]
            if review.get("semantic_hash") != expected:
                raise EvidenceExplorerError(
                    "workflow review does not match the current state",
                    code="artifact_parent_mismatch",
                )
            run_id = review.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise EvidenceExplorerError(
                    "workflow review has no run ID", code="invalid_artifact"
                )
            previous = bindings.get(run_id)
            if previous is not None and previous != workflow_id:
                raise EvidenceExplorerError(
                    f"Agent run {run_id} is bound to multiple workflows",
                    code="ambiguous_workflow_binding",
                )
            bindings[run_id] = workflow_id
        except (
            EvidenceExplorerError,
            MVPSchemaError,
            WorkflowContractError,
            WorkflowRunnerError,
        ) as exc:
            invalid.append(
                {
                    "source_kind": "workflow",
                    "source_id": workflow_id,
                    "error_code": str(
                        getattr(exc, "code", "invalid_workflow_evidence")
                    ),
                    "message": str(exc),
                }
            )
    return bindings, invalid, workflow_ids


def _optional_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceExplorerError(
            f"chart field {name} must be numeric", code="invalid_chart_data"
        ) from exc
    if not math.isfinite(result):
        raise EvidenceExplorerError(
            f"chart field {name} must be finite", code="invalid_chart_data"
        )
    return result


def _artifact_path(config: ProjectConfig, raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise EvidenceExplorerError(
            "measurement row has no artifact path", code="invalid_chart_data"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.artifacts_dir / path
    path = path.resolve()
    artifacts_root = config.artifacts_dir.resolve()
    try:
        path.relative_to(artifacts_root)
    except ValueError as exc:
        raise EvidenceExplorerError(
            f"measurement artifact escapes the artifacts directory: {path}",
            code="unsafe_path",
        ) from exc
    return str(path)


def _normalized_row(
    config: ProjectConfig,
    row: Mapping[str, Any],
    workflow_bindings: Mapping[str, str],
) -> dict[str, Any]:
    recorded_profile = str(row.get("profile") or "")
    profile = PROFILE_ALIASES.get(recorded_profile, "")
    objective = str(row.get("objective") or "")
    classification = str(row.get("classification") or "")
    decision = str(row.get("decision") or "")
    source_kind = str(row.get("source_kind") or "")
    if profile not in PROFILES:
        raise EvidenceExplorerError(
            f"unsupported synthesis profile: {recorded_profile!r}",
            code="invalid_chart_data",
        )
    if objective not in OBJECTIVES:
        raise EvidenceExplorerError(
            f"unsupported objective: {objective!r}", code="invalid_chart_data"
        )
    if classification not in CLASSIFICATIONS:
        raise EvidenceExplorerError(
            f"unsupported profile classification: {classification!r}",
            code="invalid_chart_data",
        )
    if decision not in DECISIONS:
        raise EvidenceExplorerError(
            f"unsupported candidate decision: {decision!r}",
            code="invalid_chart_data",
        )
    if source_kind not in SOURCE_KINDS:
        raise EvidenceExplorerError(
            f"unsupported evidence source kind: {source_kind!r}",
            code="invalid_chart_data",
        )
    if row.get("safe") is not True or row.get("formal_status") != "formal_passed":
        raise EvidenceExplorerError(
            "chart data must be backed by a current passing formal result",
            code="formal_pass_required",
        )
    measurement_hash = str(row.get("measurement_semantic_hash") or "")
    if not _SHA256.fullmatch(measurement_hash):
        raise EvidenceExplorerError(
            "measurement row has no valid semantic hash",
            code="invalid_chart_data",
        )
    run_id = str(row.get("run_id") or "")
    candidate_id = str(row.get("candidate_id") or "")
    row_id = str(row.get("row_id") or "")
    if not run_id or not candidate_id or not row_id:
        raise EvidenceExplorerError(
            "measurement row identity is incomplete", code="invalid_chart_data"
        )
    limitations = row.get("limitations") or []
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) for item in limitations
    ):
        raise EvidenceExplorerError(
            "measurement limitations must be a string array",
            code="invalid_chart_data",
        )
    normalized = {
        "point_id": row_id,
        "evidence_group_id": str(
            row.get("study_id") or workflow_bindings.get(run_id) or run_id
        ),
        "workflow_id": workflow_bindings.get(run_id),
        "run_id": run_id,
        "candidate_id": candidate_id,
        "study_id": row.get("study_id"),
        "reference_id": row.get("reference_id"),
        "configuration_id": row.get("configuration_id"),
        "top": row.get("top"),
        "objective": objective,
        "transformation_id": row.get("transformation_id"),
        "profile": profile,
        "recorded_profile": recorded_profile,
        "classification": classification,
        "classification_reason": str(row.get("classification_reason") or ""),
        "decision": decision,
        "candidate_decision_reason": str(
            row.get("candidate_decision_reason") or ""
        ),
        "formal_status": "formal_passed",
        "safe": True,
        "delay_improvement_percent": _optional_number(
            row.get("delay_improvement_percent"), "delay_improvement_percent"
        ),
        "area_improvement_percent": _optional_number(
            row.get("area_improvement_percent"), "area_improvement_percent"
        ),
        "cell_count_improvement_percent": _optional_number(
            row.get("cell_count_improvement_percent"),
            "cell_count_improvement_percent",
        ),
        "recipe_hash": row.get("recipe_hash"),
        "measurement_semantic_hash": measurement_hash,
        "artifact_path": _artifact_path(config, row.get("artifact_path")),
        "source_kind": source_kind,
        "limitations": limitations,
    }
    return normalized


def _filter_values(
    values: Sequence[str], *, allowed: Sequence[str] | None, name: str
) -> tuple[str, ...]:
    normalized = tuple(sorted(set(str(value) for value in values if str(value))))
    if allowed is not None:
        invalid = sorted(set(normalized) - set(allowed))
        if invalid:
            raise EvidenceExplorerError(
                f"unsupported {name}: {invalid[0]!r}", code="invalid_filter"
            )
    return normalized


def _count_rows(values: Sequence[str], order: Sequence[str]) -> list[dict[str, Any]]:
    counts = Counter(values)
    return [
        {"category": category, "count": int(counts[category])}
        for category in order
        if counts[category]
    ]


def explore_evidence(
    config: ProjectConfig,
    *,
    workflow_ids: Sequence[str] = (),
    run_ids: Sequence[str] = (),
    profiles: Sequence[str] = (),
    objectives: Sequence[str] = (),
    classifications: Sequence[str] = (),
    decisions: Sequence[str] = (),
    transformations: Sequence[str] = (),
    source_kinds: Sequence[str] = (),
    output_dir: str | Path | None = None,
    normalized_command: Sequence[str] = (),
) -> dict[str, Any]:
    """Build compact chart specs from validated, immutable measurement evidence."""

    filters = {
        "workflow_ids": _filter_values(
            workflow_ids, allowed=None, name="workflow ID"
        ),
        "run_ids": _filter_values(run_ids, allowed=None, name="run ID"),
        "profiles": _filter_values(profiles, allowed=PROFILES, name="profile"),
        "objectives": _filter_values(
            objectives, allowed=OBJECTIVES, name="objective"
        ),
        "classifications": _filter_values(
            classifications,
            allowed=CLASSIFICATIONS,
            name="classification",
        ),
        "decisions": _filter_values(decisions, allowed=DECISIONS, name="decision"),
        "transformations": _filter_values(
            transformations, allowed=None, name="transformation"
        ),
        "source_kinds": _filter_values(
            source_kinds, allowed=SOURCE_KINDS, name="source kind"
        ),
    }
    for workflow_id in filters["workflow_ids"]:
        if not _WORKFLOW_ID.fullmatch(workflow_id):
            raise EvidenceExplorerError(
                f"invalid workflow ID: {workflow_id!r}", code="invalid_filter"
            )

    try:
        analytics = FrontendDataStore(config).analytics()
    except FrontendAPIError as exc:
        raise EvidenceExplorerError(str(exc), code="invalid_evidence") from exc
    workflow_bindings, invalid_workflows, known_workflows = _workflow_bindings(config)
    unknown_workflows = sorted(set(filters["workflow_ids"]) - known_workflows)
    if unknown_workflows:
        raise EvidenceExplorerError(
            f"workflow not found: {unknown_workflows[0]}", code="workflow_not_found"
        )

    rows = [
        _normalized_row(config, row, workflow_bindings)
        for row in analytics.get("measurements") or []
        if isinstance(row, Mapping)
    ]
    field_map = {
        "workflow_ids": "workflow_id",
        "run_ids": "run_id",
        "profiles": "profile",
        "objectives": "objective",
        "classifications": "classification",
        "decisions": "decision",
        "transformations": "transformation_id",
        "source_kinds": "source_kind",
    }
    for filter_name, field in field_map.items():
        selected = set(filters[filter_name])
        if selected:
            rows = [row for row in rows if row.get(field) in selected]
    rows.sort(key=lambda row: str(row["point_id"]))

    invalid_sources = [
        {
            "source_kind": "agent_v2_run",
            "source_id": str(item.get("run_id") or "unknown"),
            "error_code": "invalid_run_evidence",
            "message": str(item.get("error") or "invalid run evidence"),
        }
        for item in analytics.get("invalid") or []
        if isinstance(item, Mapping)
    ]
    invalid_sources.extend(invalid_workflows)
    sources = sorted(
        {
            (row["measurement_semantic_hash"], row["artifact_path"])
            for row in rows
        }
    )
    dataset_core = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "schema": DATASET_SCHEMA,
        "document_type": DATASET_DOCUMENT_TYPE,
        "read_only": True,
        "filters": {name: list(values) for name, values in filters.items()},
        "metric_definitions": dict(analytics.get("metric_definitions") or {}),
        "classification_policies": dict(
            analytics.get("classification_policies") or {}
        ),
        "sources": [
            {"semantic_hash": semantic_hash, "artifact_path": artifact_path}
            for semantic_hash, artifact_path in sources
        ],
        "measurements": rows,
        "invalid_sources": invalid_sources,
    }
    dataset_identity = stable_hash(dataset_core)
    dataset = {
        **dataset_core,
        "dataset_id": f"evidence-dataset-{dataset_identity[:20]}",
    }
    dataset["semantic_hash"] = stable_hash(dataset)
    exploration_id = f"exploration-{dataset['semantic_hash'][:20]}"
    if output_dir is None:
        root = config.artifacts_dir / "evidence-explorations-v1" / exploration_id
    else:
        root = Path(output_dir).expanduser()
        if not root.is_absolute():
            root = config.root / root
        root = root.resolve()
    dataset_path = root / "chart-data.json"
    exploration_path = root / "exploration.json"
    _persist_immutable(dataset_path, dataset)

    candidate_decisions: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (str(row["run_id"]), str(row["candidate_id"]))
        decision = str(row["decision"])
        previous = candidate_decisions.get(key)
        if previous is not None and previous != decision:
            raise EvidenceExplorerError(
                f"candidate {key[1]} has conflicting decisions",
                code="invalid_chart_data",
            )
        candidate_decisions[key] = decision
    decision_rows = _count_rows(list(candidate_decisions.values()), DECISIONS)
    classification_rows = _count_rows(
        [str(row["classification"]) for row in rows], CLASSIFICATIONS
    )
    profile_rows = _count_rows([str(row["profile"]) for row in rows], PROFILES)
    status = "partial" if invalid_sources else "ready" if rows else "empty"
    limitations = [
        "Only hash-validated measurements with a current formal pass are included.",
        "Results describe the recorded Yosys/ABC recipes and Liberty file, not a target implementation flow.",
        "Profile classification and candidate decision are separate fields and must not be conflated.",
    ]
    if invalid_sources:
        limitations.append(
            "Invalid evidence sources were excluded; inspect chart-data.json for details."
        )
    exploration = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "schema": EXPLORATION_SCHEMA,
        "document_type": EXPLORATION_DOCUMENT_TYPE,
        "exploration_id": exploration_id,
        "status": status,
        "read_only": True,
        "dataset_semantic_hash": dataset["semantic_hash"],
        "filters": {name: list(values) for name, values in filters.items()},
        "summary": {
            "evidence_group_count": len(
                {str(row["evidence_group_id"]) for row in rows}
            ),
            "workflow_count": len(
                {str(row["workflow_id"]) for row in rows if row["workflow_id"]}
            ),
            "run_count": len({str(row["run_id"]) for row in rows}),
            "measured_candidate_count": len(candidate_decisions),
            "profile_observation_count": len(rows),
            "formal_safe_observation_count": sum(row["safe"] is True for row in rows),
            "invalid_source_count": len(invalid_sources),
            "decision_counts": {
                row["category"]: row["count"] for row in decision_rows
            },
            "classification_counts": {
                row["category"]: row["count"] for row in classification_rows
            },
        },
        "charts": [
            {
                "chart_id": "ppa-relationship",
                "family": "relationship",
                "type": "scatter",
                "title": "Area vs. delay improvement",
                "point_count": len(rows),
                "data_ref": {
                    "artifact": "dataset",
                    "collection": "measurements",
                },
                "encoding": {
                    "x": "area_improvement_percent",
                    "y": "delay_improvement_percent",
                    "label": "candidate_id",
                    "series": "classification",
                    "detail": "profile",
                },
                "units": {"x": "percent", "y": "percent"},
            },
            {
                "chart_id": "candidate-outcomes",
                "family": "comparison",
                "type": "bar",
                "title": "Candidate outcomes",
                "rows": decision_rows,
                "encoding": {"category": "category", "value": "count"},
                "unit": "candidates",
            },
            {
                "chart_id": "profile-classifications",
                "family": "comparison",
                "type": "bar",
                "title": "Profile classifications",
                "rows": classification_rows,
                "encoding": {"category": "category", "value": "count"},
                "unit": "profile observations",
            },
            {
                "chart_id": "profile-coverage",
                "family": "comparison",
                "type": "bar",
                "title": "Synthesis profile coverage",
                "rows": profile_rows,
                "encoding": {"category": "category", "value": "count"},
                "unit": "profile observations",
            },
        ],
        "classification_authority": {
            "profile": "rtl_advisor.mvp_measure.classify_recipe",
            "candidate": "rtl_advisor.mvp_measure.aggregate_measurements",
        },
        "dashboard": {
            "route": "/?view=explore",
            "api": "/api/analytics/v1",
        },
        "artifacts": {
            "exploration": str(exploration_path.resolve()),
            "dataset": str(dataset_path.resolve()),
            "dashboard_index": str(
                Path(__file__).with_name("frontend").joinpath("index.html").resolve()
            ),
        },
        "limitations": limitations,
        "command": list(normalized_command),
    }
    exploration["semantic_hash"] = stable_hash(exploration)
    return _persist_immutable(exploration_path, exploration)


def evidence_error_payload(
    error: Exception, *, normalized_command: Sequence[str] = ()
) -> dict[str, Any]:
    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "schema": "rtl-advisor-evidence-error-v1",
        "document_type": "rtl-advisor.evidence.error",
        "status": "failed",
        "error": {
            "code": str(getattr(error, "code", "evidence_explorer_error")),
            "message": str(error),
        },
        "command": list(normalized_command),
    }
    payload["semantic_hash"] = stable_hash(payload)
    return payload


def evidence_exit_code(payload: Mapping[str, Any]) -> int:
    if payload.get("document_type") == "rtl-advisor.evidence.error":
        return 2
    if payload.get("status") == "partial":
        return 4
    return 0
