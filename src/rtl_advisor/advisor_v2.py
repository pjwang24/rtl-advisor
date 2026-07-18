from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import (
    ADDER_ASSOCIATION_FAMILY,
    COMPARATOR_SELECTION_FAMILY,
    DECODE_FACTORING_FAMILY,
    MUX_PLACEMENT_FAMILY,
    POPCOUNT_SATURATION_FAMILY,
    PRIORITY_SELECTION_FAMILY,
    RESOURCE_SHARING_FAMILY,
    VARIABLE_SHIFT_FAMILY,
    WIDTH_SIGNEDNESS_FAMILY,
)
from rtl_advisor.graph import GraphError
from rtl_advisor.rtl_input import (
    DesignInputV2,
    RTLInputError,
    build_live_graph,
    lint_with_pyslang,
    normalize_design_input,
)
from rtl_advisor.rules import analyze_rules


ANALYSIS_SCHEMA_VERSION = 2
GATE_MODEL_SCHEMA_VERSION = 2
FEATURE_SCHEMA_VERSION = "rtl-advisor-premap-features-v2"
GATE_FLOW_VERSION = "rtl-advisor-calibrated-gate-v2"
DEFAULT_GATE_MODEL = "models/v2/gate.json"

TRANSFORMATION_FAMILIES = {
    "share_arithmetic_by_muxing_inputs": RESOURCE_SHARING_FAMILY,
    "reassociate_arithmetic_tree": ADDER_ASSOCIATION_FAMILY,
    "move_mux_across_operation": MUX_PLACEMENT_FAMILY,
    "balance_priority_selection": PRIORITY_SELECTION_FAMILY,
    "factor_repeated_decode": DECODE_FACTORING_FAMILY,
    "factor_comparator_selection": COMPARATOR_SELECTION_FAMILY,
    "bound_variable_shift": VARIABLE_SHIFT_FAMILY,
    "narrow_intermediate_width": WIDTH_SIGNEDNESS_FAMILY,
    "restructure_popcount_or_saturation": POPCOUNT_SATURATION_FAMILY,
}
TRANSFORMATION_CODES = {
    transformation: float(index)
    for index, transformation in enumerate(sorted(TRANSFORMATION_FAMILIES))
}
FAMILY_CODES = {
    family: float(index)
    for index, family in enumerate(sorted(set(TRANSFORMATION_FAMILIES.values())))
}

FEATURE_ORDER = (
    "module_count",
    "node_count",
    "edge_count",
    "operator_count",
    "register_count",
    "mux_count",
    "add_count",
    "subtract_count",
    "multiply_count",
    "compare_count",
    "shift_count",
    "logic_count",
    "maximum_port_width",
    "mean_operator_width",
    "maximum_operator_width",
    "arithmetic_depth",
    "control_depth",
    "maximum_fanin",
    "mean_fanin",
    "maximum_fanout",
    "mean_fanout",
    "signed_operand_count",
    "mixed_signed_operator_count",
    "widening_operator_count",
    "narrowing_operator_count",
    "repeated_fingerprint_count",
    "shift_data_width",
    "shift_amount_width",
    "shift_excess_bits",
    "serial_depth",
    "branch_count",
    "request_count",
    "duplicate_count",
    "fanout_estimate",
    "result_width",
    "local_cone_size",
    "local_cone_depth",
    "source_span_count",
    "family_code",
    "transformation_code",
    "template_code",
)
FEATURE_SCHEMA_HASH = hashlib.sha256(
    json.dumps(
        {"version": FEATURE_SCHEMA_VERSION, "feature_order": FEATURE_ORDER},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


class AdvisorV2Error(RuntimeError):
    """Raised when a v2 analysis cannot satisfy its contract."""


@dataclass(frozen=True)
class ProfileSpec:
    profile_id: str
    delay_weight: float
    area_weight: float
    cell_weight: float

    def eligible(self, delay: float, area: float) -> bool:
        if self.profile_id == "balanced":
            return (delay >= 3.0 and area >= -10.0) or (
                area >= 5.0 and delay >= -2.0
            )
        if self.profile_id == "timing-first":
            return delay >= 3.0 and area >= -20.0
        if self.profile_id == "area-first":
            return area >= 5.0 and delay >= -10.0
        raise AdvisorV2Error(f"unknown PPA profile: {self.profile_id}")

    def utility(self, delay: float, area: float, cells: float) -> float:
        return (
            self.delay_weight * delay
            + self.area_weight * area
            + self.cell_weight * cells
        )


PROFILES = {
    "balanced": ProfileSpec("balanced", 1.0, 1.0, 0.1),
    "timing-first": ProfileSpec("timing-first", 2.0, 0.5, 0.05),
    "area-first": ProfileSpec("area-first", 0.5, 2.0, 0.1),
}


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


def _numeric(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return float(len(value))
    return default


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _weighted_graph_depth(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    counted_operations: set[str],
) -> float:
    operations = {str(node.get("id")): str(node.get("operation", "")) for node in nodes}
    predecessors: dict[str, set[str]] = {node_id: set() for node_id in operations}
    for edge in edges:
        source = str(edge.get("source", ""))
        destination = str(edge.get("destination", ""))
        if source in operations and destination in operations:
            predecessors[destination].add(source)
    memo: dict[str, int] = {}

    def depth(node_id: str, active: frozenset[str]) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id in active:
            return 0
        upstream = max(
            (depth(parent, active | {node_id}) for parent in predecessors[node_id]),
            default=0,
        )
        value = upstream + int(operations[node_id] in counted_operations)
        memo[node_id] = value
        return value

    return float(max((depth(node_id, frozenset()) for node_id in operations), default=0))


def extract_design_features(graph: dict[str, Any]) -> dict[str, float]:
    modules = graph.get("modules") or []
    nodes = [node for module in modules for node in module.get("nodes", [])]
    edges = [edge for module in modules for edge in module.get("edges", [])]
    operations = [str(node.get("operation", "")) for node in nodes]
    operators = [node for node in nodes if node.get("kind") == "operator"]
    port_widths = [
        int(port.get("width", 0))
        for module in modules
        for port in module.get("ports", [])
    ]
    compare_ops = {"eq", "ne", "eqx", "nex", "lt", "le", "gt", "ge"}
    shift_ops = {"shl", "shr", "sshl", "sshr"}
    arithmetic_ops = {"add", "subtract", "multiply"}
    control_ops = compare_ops | {"mux", "priority_mux", "logic_and", "logic_or", "logic_not"}
    logic_ops = {"and", "or", "xor", "xnor", "not", "logic_and", "logic_or", "logic_not"}
    operator_widths = [
        float(port.get("width", 0))
        for node in operators
        for port in node.get("ports", {}).values()
    ]
    fanin = {str(node.get("id")): 0 for node in nodes}
    fanout = {str(node.get("id")): 0 for node in nodes}
    for edge in edges:
        source = str(edge.get("source", ""))
        destination = str(edge.get("destination", ""))
        if source in fanout:
            fanout[source] += 1
        if destination in fanin:
            fanin[destination] += 1
    signed_operand_count = 0
    mixed_signed_operator_count = 0
    widening_operator_count = 0
    narrowing_operator_count = 0
    fingerprints: dict[tuple[Any, ...], int] = {}
    shift_data_width = 0
    shift_amount_width = 0
    for node in operators:
        parameters = node.get("parameters") or {}
        a_signed = int(parameters.get("A_SIGNED", 0))
        b_signed = int(parameters.get("B_SIGNED", 0))
        signed_operand_count += a_signed + b_signed
        mixed_signed_operator_count += int(a_signed != b_signed)
        input_widths = [
            int(port.get("width", 0))
            for port in node.get("ports", {}).values()
            if port.get("direction") == "input"
        ]
        output_widths = [
            int(port.get("width", 0))
            for port in node.get("ports", {}).values()
            if port.get("direction") == "output"
        ]
        if input_widths and output_widths:
            widening_operator_count += int(max(output_widths) > max(input_widths))
            narrowing_operator_count += int(max(output_widths) < max(input_widths))
        fingerprint = (
            str(node.get("operation", "")),
            tuple(sorted(input_widths)),
            tuple(sorted(output_widths)),
            a_signed,
            b_signed,
        )
        fingerprints[fingerprint] = fingerprints.get(fingerprint, 0) + 1
        if str(node.get("operation", "")) in shift_ops:
            ports = node.get("ports") or {}
            shift_data_width = max(shift_data_width, int((ports.get("A") or {}).get("width", 0)))
            shift_amount_width = max(shift_amount_width, int((ports.get("B") or {}).get("width", 0)))
    required_shift_bits = (
        max(1, math.ceil(math.log2(shift_data_width))) if shift_data_width else 0
    )
    return {
        "module_count": float(len(modules)),
        "node_count": float(len(nodes)),
        "edge_count": float(len(edges)),
        "operator_count": float(sum(node.get("kind") == "operator" for node in nodes)),
        "register_count": float(sum(node.get("kind") == "register" for node in nodes)),
        "mux_count": float(sum(operation in {"mux", "priority_mux"} for operation in operations)),
        "add_count": float(operations.count("add")),
        "subtract_count": float(operations.count("subtract")),
        "multiply_count": float(operations.count("multiply")),
        "compare_count": float(sum(operation in compare_ops for operation in operations)),
        "shift_count": float(sum(operation in shift_ops for operation in operations)),
        "logic_count": float(sum(operation in logic_ops for operation in operations)),
        "maximum_port_width": float(max(port_widths, default=0)),
        "mean_operator_width": _mean(operator_widths),
        "maximum_operator_width": float(max(operator_widths, default=0.0)),
        "arithmetic_depth": _weighted_graph_depth(nodes, edges, arithmetic_ops),
        "control_depth": _weighted_graph_depth(nodes, edges, control_ops),
        "maximum_fanin": float(max(fanin.values(), default=0)),
        "mean_fanin": _mean([float(value) for value in fanin.values()]),
        "maximum_fanout": float(max(fanout.values(), default=0)),
        "mean_fanout": _mean([float(value) for value in fanout.values()]),
        "signed_operand_count": float(signed_operand_count),
        "mixed_signed_operator_count": float(mixed_signed_operator_count),
        "widening_operator_count": float(widening_operator_count),
        "narrowing_operator_count": float(narrowing_operator_count),
        "repeated_fingerprint_count": float(
            sum(max(0, count - 1) for count in fingerprints.values())
        ),
        "shift_data_width": float(shift_data_width),
        "shift_amount_width": float(shift_amount_width),
        "shift_excess_bits": float(max(0, shift_amount_width - required_shift_bits)),
    }


def _evidence_node_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            if key.endswith("node") and isinstance(item, str):
                found.add(item)
            elif key.endswith("nodes") and isinstance(item, list):
                found.update(str(entry) for entry in item if isinstance(entry, str))
            else:
                found.update(_evidence_node_ids(item))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(_evidence_node_ids(item))
        return found
    return set()


def candidate_features(
    design_features: dict[str, float],
    finding: dict[str, Any],
    template_id: str = "v1",
) -> dict[str, float]:
    evidence = finding.get("evidence") or {}
    transformation = str(finding.get("transformation_id", ""))
    family = TRANSFORMATION_FAMILIES.get(transformation, "")
    source = finding.get("source") or {}
    source_locations = source.get("locations") if isinstance(source, dict) else []
    local_node_ids = _evidence_node_ids(evidence)
    local_depth = _numeric(
        evidence.get(
            "serial_depth",
            evidence.get("mux_depth", evidence.get("local_cone_depth", 0)),
        )
    )
    merged = {
        **design_features,
        "serial_depth": _numeric(
            evidence.get("serial_depth", evidence.get("mux_depth", 0))
        ),
        "branch_count": _numeric(
            evidence.get("branch_count", evidence.get("operand_count_estimate", 0))
        ),
        "request_count": _numeric(
            evidence.get("request_count", evidence.get("branch_count", 0))
        ),
        "duplicate_count": _numeric(evidence.get("duplicate_count", 0)),
        "fanout_estimate": _numeric(
            evidence.get("fanout", evidence.get("reuse_count", 0))
        ),
        "result_width": _numeric(
            evidence.get("result_width", evidence.get("maximum_width", 0))
        ),
        "local_cone_size": float(len(local_node_ids)),
        "local_cone_depth": local_depth,
        "source_span_count": _numeric(source_locations),
        "family_code": FAMILY_CODES.get(family, -1.0),
        "transformation_code": TRANSFORMATION_CODES.get(transformation, -1.0),
        "template_code": {"v1": 1.0, "v2": 2.0, "v3": 3.0}.get(
            template_id, -1.0
        ),
    }
    return {name: float(merged.get(name, 0.0)) for name in FEATURE_ORDER}


def _tree_value(tree: dict[str, Any], features: dict[str, float]) -> float:
    nodes = tree.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise AdvisorV2Error("gate estimator has no nodes")
    index = 0
    visited: set[int] = set()
    while True:
        if index in visited or index < 0 or index >= len(nodes):
            raise AdvisorV2Error("invalid cycle or child index in gate estimator")
        visited.add(index)
        node = nodes[index]
        if not isinstance(node, dict):
            raise AdvisorV2Error("invalid gate estimator node")
        if "value" in node:
            return float(node["value"])
        feature = str(node.get("feature"))
        if feature not in features:
            raise AdvisorV2Error(f"gate estimator references unknown feature {feature}")
        threshold = float(node["threshold"])
        index = int(node["left"] if features[feature] <= threshold else node["right"])


def _load_gate_model(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorV2Error(f"invalid gate model {path}: {exc}") from exc
    if not isinstance(model, dict) or model.get("schema_version") != GATE_MODEL_SCHEMA_VERSION:
        raise AdvisorV2Error(f"unsupported gate model schema in {path}")
    if model.get("feature_schema_hash") != FEATURE_SCHEMA_HASH:
        raise AdvisorV2Error("gate model feature schema hash mismatch")
    expected_hash = model.get("model_hash")
    core = {key: value for key, value in model.items() if key != "model_hash"}
    actual_hash = _json_hash(core)
    if expected_hash != actual_hash:
        raise AdvisorV2Error("gate model content hash mismatch")
    return model, actual_hash


def _outside_envelope(
    model: dict[str, Any],
    family: str,
    features: dict[str, float],
) -> list[str]:
    family_envelope = (model.get("envelopes") or {}).get(family, {})
    reasons = []
    for name, bounds in family_envelope.items():
        if name not in features or not isinstance(bounds, list) or len(bounds) != 2:
            continue
        if features[name] < float(bounds[0]) or features[name] > float(bounds[1]):
            reasons.append(
                f"feature {name}={features[name]:g} outside [{bounds[0]}, {bounds[1]}]"
            )
    return reasons


def _score_finding(
    finding: dict[str, Any],
    features: dict[str, float],
    model: dict[str, Any] | None,
    profile: ProfileSpec,
    template_id: str,
) -> dict[str, Any]:
    transformation = str(finding.get("transformation_id", ""))
    family = TRANSFORMATION_FAMILIES.get(transformation)
    identity = {
        "finding_id": finding.get("finding_id"),
        "transformation_id": transformation,
        "template_id": template_id,
        "source": finding.get("source"),
    }
    candidate_id = _json_hash(identity)[:16]
    rejection_reasons: list[str] = []
    predictions: dict[str, dict[str, float] | None] = {
        "delay": None,
        "area": None,
        "cell_count": None,
    }
    if family is None:
        rejection_reasons.append("unregistered transformation")
    elif model is None:
        rejection_reasons.append("calibrated gate model is not installed")
    else:
        rejection_reasons.extend(_outside_envelope(model, family, features))
        family_estimators = (model.get("estimators") or {}).get(family, {})
        family_intervals = (model.get("intervals") or {}).get(family, {})
        interval_table = family_intervals.get(
            f"{transformation}:{template_id}",
            family_intervals.get(transformation, {}),
        )
        for metric in predictions:
            tree = family_estimators.get(metric)
            radius = interval_table.get(metric)
            if not isinstance(tree, dict) or radius is None:
                rejection_reasons.append(f"missing calibrated {metric} estimator")
                continue
            estimate = _tree_value(tree, features)
            radius_value = float(radius)
            predictions[metric] = {
                "estimate": round(estimate, 6),
                "lower": round(estimate - radius_value, 6),
                "upper": round(estimate + radius_value, 6),
            }

    complete = all(prediction is not None for prediction in predictions.values())
    eligible = False
    utility: float | None = None
    if complete and not rejection_reasons:
        delay = predictions["delay"]["lower"]  # type: ignore[index]
        area = predictions["area"]["lower"]  # type: ignore[index]
        cells = predictions["cell_count"]["lower"]  # type: ignore[index]
        eligible = profile.eligible(delay, area)
        utility = round(profile.utility(delay, area, cells), 6)
        if not eligible:
            rejection_reasons.append(f"does not conservatively clear {profile.profile_id}")
    return {
        "candidate_id": candidate_id,
        "transformation_id": transformation,
        "template_id": template_id,
        "family": family,
        "finding_id": finding.get("finding_id"),
        "source": finding.get("source"),
        "preconditions": finding.get("risks") or [],
        "features": features,
        "predicted_improvement_percent": predictions,
        "eligible": eligible,
        "conservative_utility": utility,
        "rank": None,
        "rejection_reasons": rejection_reasons,
        "generation": {"status": "not_requested"},
        "verification": {"status": "not_run"},
    }


def score_rule_candidates(
    graph: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    profile_id: str,
    model: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    try:
        profile = PROFILES[profile_id]
    except KeyError as exc:
        raise AdvisorV2Error(f"unknown PPA profile: {profile_id}") from exc
    design_features = extract_design_features(graph)
    candidates = [
        _score_finding(
            finding,
            candidate_features(design_features, finding, template_id),
            model,
            profile,
            template_id,
        )
        for finding in findings
        for template_id in ("v1", "v2", "v3")
    ]
    candidates.sort(
        key=lambda candidate: (
            not candidate["eligible"],
            -(
                candidate["conservative_utility"]
                if candidate["conservative_utility"] is not None
                else float("-inf")
            ),
            candidate["candidate_id"],
        )
    )
    candidates = candidates[:3]
    for index, candidate in enumerate(candidates, start=1):
        candidate["rank"] = index
    return candidates, design_features


def analyze_live_rtl(
    config: ProjectConfig,
    *,
    top: str,
    files: tuple[str, ...] = (),
    filelist: str | None = None,
    include_dirs: tuple[str, ...] = (),
    defines: tuple[str, ...] = (),
    profile_id: str = "balanced",
    mode: str = "calibrated",
    output_dir: str | Path | None = None,
    gate_model_path: str | Path | None = None,
    force: bool = False,
) -> tuple[dict[str, Any], Path]:
    if mode not in {"calibrated", "advisor"}:
        raise AdvisorV2Error(f"unsupported v2 mode: {mode}")
    try:
        design = normalize_design_input(
            top=top,
            files=files,
            filelist=filelist,
            include_dirs=include_dirs,
            defines=defines,
            base=config.root,
        )
        lint = lint_with_pyslang(design)
    except RTLInputError as exc:
        raise AdvisorV2Error(str(exc)) from exc

    root = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else config.artifacts_dir / "designs" / design.design_hash
    )
    _write_json(root / "input.json", design.to_dict())
    _write_json(root / "lint/slang.json", lint.to_dict())
    if not lint.ok:
        result = {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "flow_version": GATE_FLOW_VERSION,
            "design_hash": design.design_hash,
            "profile": profile_id,
            "mode": mode,
            "decision": "unsupported",
            "selected_candidate_id": None,
            "candidates": [],
            "gate": {
                "status": "blocked",
                "reason": "PySlang compile or lint failed",
                "model_hash": None,
            },
            "explanation": {"status": "not_run"},
            "lint": lint.to_dict(),
            "artifacts": {"root": str(root)},
        }
        output_path = root / "analysis-v2.json"
        _write_json(output_path, result)
        return result, output_path

    try:
        graph_build = build_live_graph(config, design, force=force)
    except GraphError as exc:
        result = {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "flow_version": GATE_FLOW_VERSION,
            "design_hash": design.design_hash,
            "profile": profile_id,
            "mode": mode,
            "decision": "unsupported",
            "selected_candidate_id": None,
            "candidates": [],
            "gate": {
                "status": "blocked",
                "reason": str(exc),
                "model_hash": None,
            },
            "explanation": {"status": "not_run"},
            "lint": lint.to_dict(),
            "artifacts": {"root": str(root)},
        }
        output_path = root / "analysis-v2.json"
        _write_json(output_path, result)
        return result, output_path

    rules_result = analyze_rules(graph_build.graph)
    model_path = (
        Path(gate_model_path).expanduser().resolve()
        if gate_model_path is not None
        else config.artifacts_dir / DEFAULT_GATE_MODEL
    )
    model, model_hash = _load_gate_model(model_path)
    candidates, features = score_rule_candidates(
        graph_build.graph,
        list(rules_result.get("findings") or []),
        profile_id=profile_id,
        model=model,
    )
    selected = next((candidate for candidate in candidates if candidate["eligible"]), None)
    if selected is not None:
        decision = "recommend"
        gate_reason = "highest conservative eligible utility"
    else:
        decision = "abstain"
        gate_reason = (
            "no registered structural opportunity"
            if not candidates
            else "no candidate conservatively clears the profile"
        )
    result = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "flow_version": GATE_FLOW_VERSION,
        "design_hash": design.design_hash,
        "profile": profile_id,
        "mode": mode,
        "decision": decision,
        "selected_candidate_id": selected["candidate_id"] if selected else None,
        "candidates": candidates,
        "gate": {
            "status": "calibrated" if model is not None else "model_missing",
            "reason": gate_reason,
            "model_path": str(model_path),
            "model_hash": model_hash,
            "feature_schema_hash": FEATURE_SCHEMA_HASH,
        },
        "explanation": {
            "status": "not_requested" if mode == "calibrated" else "pending"
        },
        "lint": lint.to_dict(),
        "features": features,
        "rules": {
            "ruleset_version": rules_result.get("ruleset_version"),
            "finding_count": len(rules_result.get("findings") or []),
        },
        "artifacts": {
            "root": str(root),
            "graph": str(graph_build.graph_path),
        },
    }
    output_path = root / "analysis-v2.json"
    _write_json(output_path, result)
    return result, output_path


def gate_model_payload(core: dict[str, Any]) -> dict[str, Any]:
    """Return a gate model with its deterministic integrity hash attached."""
    payload = {
        "schema_version": GATE_MODEL_SCHEMA_VERSION,
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        **core,
    }
    payload["model_hash"] = _json_hash(payload)
    return payload


def profile_payload() -> dict[str, Any]:
    return {
        profile_id: asdict(profile)
        for profile_id, profile in PROFILES.items()
    }
