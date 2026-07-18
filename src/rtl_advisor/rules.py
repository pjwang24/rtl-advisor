from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


RULESET_VERSION = "rtl-advisor-rules-v11"
_SHAREABLE_OPERATIONS = {"add", "subtract", "multiply"}
_ASSOCIATIVE_OPERATIONS = {"add"}
_COMPARISON_OPERATIONS = {"eq", "ne", "eqx", "nex", "lt", "le", "gt", "ge"}
_MUX_PLACEMENT_OPERATIONS = {"add", "subtract", "multiply"} | _COMPARISON_OPERATIONS
_VARIABLE_SHIFT_OPERATIONS = {"shl", "shr", "sshl", "sshr"}


def _finding_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _single_producer(
    bits: list[int | str],
    producers: dict[str, tuple[dict[str, Any], str]],
) -> tuple[dict[str, Any], str] | None:
    found: dict[str, tuple[dict[str, Any], str]] = {}
    for bit in bits:
        if not isinstance(bit, int):
            continue
        producer = producers.get(f"n:{bit}")
        if producer is not None:
            found[producer[0]["id"]] = producer
    return next(iter(found.values())) if len(found) == 1 else None


def _resource_sharing_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    producers: dict[str, tuple[dict[str, Any], str]] = {}
    for node in module["nodes"]:
        for port_name, port in node["ports"].items():
            if port["direction"] == "output":
                for bit in port["bits"]:
                    if isinstance(bit, int):
                        producers[f"n:{bit}"] = (node, port_name)

    findings = []
    for mux in module["nodes"]:
        if mux["operation"] != "mux":
            continue
        a_port = mux["ports"].get("A")
        b_port = mux["ports"].get("B")
        if not a_port or not b_port:
            continue
        a_producer = _single_producer(a_port["bits"], producers)
        b_producer = _single_producer(b_port["bits"], producers)
        if a_producer is None or b_producer is None:
            continue
        a_node, _ = a_producer
        b_node, _ = b_producer
        if a_node["id"] == b_node["id"]:
            continue
        if (
            a_node["operation"] != b_node["operation"]
            or a_node["operation"] not in _SHAREABLE_OPERATIONS
        ):
            continue
        common_operands = []
        for a_port_name in ("A", "B"):
            for b_port_name in ("A", "B"):
                a_operand = a_node["ports"].get(a_port_name)
                b_operand = b_node["ports"].get(b_port_name)
                if (
                    a_operand
                    and b_operand
                    and a_operand["bits"] == b_operand["bits"]
                ):
                    common_operands.append(
                        {
                            "first_operator_port": a_port_name,
                            "second_operator_port": b_port_name,
                            "width": a_operand["width"],
                        }
                    )
        evidence = {
            "selection_node": mux["id"],
            "operator": a_node["operation"],
            "operator_nodes": sorted((a_node["id"], b_node["id"])),
            "duplicate_count": 2,
            "result_width": min(a_port["width"], b_port["width"]),
            "common_operands": common_operands,
        }
        is_mux_placement = bool(common_operands)
        identity = {
            "rule_id": (
                "mux_placement.post_operation.v1"
                if is_mux_placement
                else "resource_sharing.output_mux.v1"
            ),
            "module": module["name"],
            "selection_node": mux["id"],
            "operator_nodes": evidence["operator_nodes"],
        }
        findings.append(
            {
                "finding_id": _finding_id(identity),
                "rule_id": identity["rule_id"],
                "category": (
                    "mux_placement"
                    if is_mux_placement
                    else "arithmetic_resource_sharing"
                ),
                "severity": "advisory",
                "module": module["name"],
                "module_display_name": module["display_name"],
                "source": mux["source"],
                "confidence": 0.9,
                "evidence": evidence,
                "recommendation": (
                    "Consider moving selection to the differing operand before one "
                    "shared arithmetic operator."
                    if is_mux_placement
                    else "Consider selecting operands before the arithmetic operator "
                    "so mutually exclusive operations can share one implementation."
                ),
                "transformation_id": (
                    "move_mux_across_operation"
                    if is_mux_placement
                    else "share_arithmetic_by_muxing_inputs"
                ),
                "predicted_effect": {
                    "area": "uncertain" if is_mux_placement else "improve",
                    "cell_count": "improve",
                    "delay": "improve" if is_mux_placement else "uncertain",
                },
                "risks": [
                    "Moving muxes before arithmetic may lengthen the critical path.",
                    "Apply only when the operations are mutually exclusive and equivalent.",
                ],
            }
        )
    return findings


def _longest_operator_path(
    module: dict[str, Any],
    operation: str,
    *,
    prefer_width: bool = False,
) -> list[dict[str, Any]]:
    nodes = {
        node["id"]: node
        for node in module["nodes"]
        if node["operation"] == operation
    }
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in module["edges"]:
        source = edge["source"]
        destination = edge["destination"]
        if source in nodes and destination in nodes:
            adjacency[source].add(destination)

    memo: dict[str, tuple[str, ...]] = {}

    def visit(node_id: str, active: frozenset[str]) -> tuple[str, ...]:
        if node_id in memo:
            return memo[node_id]
        if node_id in active:
            return (node_id,)
        suffixes = [
            visit(successor, active | {node_id})
            for successor in sorted(adjacency[node_id])
        ]
        best_suffix = max(suffixes, key=len) if suffixes else ()
        path = (node_id, *best_suffix)
        memo[node_id] = path
        return path

    paths = [visit(node_id, frozenset()) for node_id in sorted(nodes)]
    def path_key(path: tuple[str, ...]) -> tuple[Any, ...]:
        maximum_width = max(
            (nodes[node_id]["ports"].get("Y", {}).get("width", 0) for node_id in path),
            default=0,
        )
        return (
            (maximum_width, len(path), path)
            if prefer_width
            else (len(path), maximum_width, path)
        )

    longest = max(paths, key=path_key, default=())
    return [nodes[node_id] for node_id in longest]


def _is_single_bit_term(port: dict[str, Any] | None) -> bool:
    if not port:
        return False
    variable_bits = [bit for bit in port["bits"] if isinstance(bit, int)]
    return len(variable_bits) == 1


def _looks_like_serial_popcount(path: list[dict[str, Any]]) -> bool:
    if len(path) < 6:
        return False
    nodes_with_bit_term = sum(
        any(_is_single_bit_term(node["ports"].get(name)) for name in ("A", "B"))
        for node in path
    )
    return nodes_with_bit_term >= len(path) - 1


def _looks_like_popcount_tree(path: list[dict[str, Any]]) -> bool:
    return any(
        _is_single_bit_term(node["ports"].get("A"))
        and _is_single_bit_term(node["ports"].get("B"))
        for node in path
    )


def _serial_arithmetic_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for operation in sorted(_ASSOCIATIVE_OPERATIONS):
        path = _longest_operator_path(module, operation)
        if len(path) < 3:
            continue
        if operation == "add" and (
            _looks_like_serial_popcount(path) or _looks_like_popcount_tree(path)
        ):
            continue
        terminal = path[-1]
        result_port = terminal["ports"].get("Y", {})
        evidence = {
            "operator": operation,
            "operator_nodes": [node["id"] for node in path],
            "serial_depth": len(path),
            "balanced_depth_estimate": len(path).bit_length(),
            "operand_count_estimate": len(path) + 1,
            "result_width": result_port.get("width"),
        }
        identity = {
            "rule_id": "arithmetic.serial_chain.v1",
            "module": module["name"],
            "operator_nodes": evidence["operator_nodes"],
        }
        findings.append(
            {
                "finding_id": _finding_id(identity),
                "rule_id": identity["rule_id"],
                "category": "arithmetic_association",
                "severity": "advisory",
                "module": module["name"],
                "module_display_name": module["display_name"],
                "source": terminal["source"],
                "confidence": 0.88,
                "evidence": evidence,
                "recommendation": (
                    "Consider reassociating this serial addition chain into a "
                    "balanced tree while preserving full intermediate widths."
                ),
                "transformation_id": "reassociate_arithmetic_tree",
                "predicted_effect": {
                    "area": "uncertain",
                    "cell_count": "neutral",
                    "delay": "improve",
                },
                "risks": [
                    "Reassociation is unsafe if intermediate truncation changes results.",
                    "Signedness and four-state simulation behavior must be preserved.",
                ],
            }
        )
    return findings


def _priority_mux_depth_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    path = _longest_operator_path(module, "mux", prefer_width=True)
    if len(path) < 4:
        return []
    terminal = path[-1]
    widths = [node["ports"].get("Y", {}).get("width", 0) for node in path]
    evidence = {
        "operator": "mux",
        "mux_nodes": [node["id"] for node in path],
        "serial_depth": len(path),
        "maximum_width": max(widths, default=0),
        "distinct_source_lines": sorted(
            {
                location["start_line"]
                for node in path
                for location in (node.get("source") or {}).get("locations", [])
                if "start_line" in location
            }
        ),
    }
    identity = {
        "rule_id": "priority_selection.mux_depth.v1",
        "module": module["name"],
        "mux_nodes": evidence["mux_nodes"],
    }
    return [
        {
            "finding_id": _finding_id(identity),
            "rule_id": identity["rule_id"],
            "category": "priority_selection",
            "severity": "advisory",
            "module": module["name"],
            "module_display_name": module["display_name"],
            "source": terminal["source"],
            "confidence": 0.85,
            "evidence": evidence,
            "recommendation": (
                "Consider replacing this deep priority mux chain with an explicit "
                "priority decode and compact data-selection structure."
            ),
            "transformation_id": "balance_priority_selection",
            "predicted_effect": {
                "area": "improve",
                "cell_count": "improve",
                "delay": "uncertain",
            },
            "risks": [
                "The exact priority order must remain unchanged for overlapping requests.",
                "Four-state case semantics can differ between if, casez, and decoded forms.",
                "Logic mapping may favor the original chain for timing despite its RTL depth.",
            ],
        }
    ]


def _pre_operation_mux_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    producers: dict[str, tuple[dict[str, Any], str]] = {}
    for node in module["nodes"]:
        for port_name, port in node["ports"].items():
            if port["direction"] != "output":
                continue
            for bit in port["bits"]:
                if isinstance(bit, int):
                    producers[f"n:{bit}"] = (node, port_name)

    findings = []
    for operator in module["nodes"]:
        if operator["operation"] not in _MUX_PLACEMENT_OPERATIONS:
            continue
        mux_inputs = []
        for port_name in ("A", "B"):
            port = operator["ports"].get(port_name)
            if not port:
                continue
            producer = _single_producer(port["bits"], producers)
            if producer is not None and producer[0]["operation"] == "mux":
                mux_inputs.append(
                    {
                        "operator_port": port_name,
                        "mux_node": producer[0]["id"],
                    }
                )
        if not mux_inputs:
            continue
        result_width = operator["ports"].get("Y", {}).get("width")
        evidence = {
            "operator": operator["operation"],
            "operator_node": operator["id"],
            "mux_inputs": mux_inputs,
            "selected_input_count": len(mux_inputs),
            "result_width": result_width,
        }
        identity = {
            "rule_id": "mux_placement.pre_operation.v1",
            "module": module["name"],
            "operator_node": operator["id"],
            "mux_inputs": mux_inputs,
        }
        findings.append(
            {
                "finding_id": _finding_id(identity),
                "rule_id": identity["rule_id"],
                "category": "mux_placement",
                "severity": "advisory",
                "module": module["name"],
                "module_display_name": module["display_name"],
                "source": operator["source"],
                "confidence": 0.86,
                "evidence": evidence,
                "recommendation": (
                    "If timing is the priority, consider computing the mutually "
                    "exclusive operation results in parallel and selecting afterward."
                ),
                "transformation_id": "move_mux_across_operation",
                "predicted_effect": {
                    "area": "uncertain",
                    "cell_count": "degrade",
                    "delay": "uncertain",
                },
                "risks": [
                    "Moving the mux after the operation duplicates arithmetic hardware.",
                    "Area growth may outweigh timing benefit for wide operators.",
                    "Selector polarity, widths, signedness, and cycle latency must match.",
                ],
            }
        )
    return findings


def _repeated_decode_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons = {"eq", "ne", "eqx", "nex"}
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in module["nodes"]:
        if node["operation"] not in comparisons:
            continue
        signature = {
            "operation": node["operation"],
            "parameters": node["parameters"],
            "A": node["ports"].get("A", {}).get("bits"),
            "B": node["ports"].get("B", {}).get("bits"),
        }
        groups.setdefault(json.dumps(signature, sort_keys=True), []).append(node)
    repeated = [nodes for nodes in groups.values() if len(nodes) >= 2]
    if not repeated:
        return []
    repeated.sort(key=lambda nodes: tuple(node["id"] for node in nodes))
    group_evidence = [
        {
            "operation": nodes[0]["operation"],
            "comparison_nodes": [node["id"] for node in nodes],
            "duplicate_count": len(nodes),
            "input_width": nodes[0]["ports"].get("A", {}).get("width"),
            "constant_bits": nodes[0]["ports"].get("B", {}).get("bits"),
        }
        for nodes in repeated
    ]
    all_nodes = [node for nodes in repeated for node in nodes]
    source_node = max(
        all_nodes,
        key=lambda node: (
            max(
                (
                    location.get("start_line", 0)
                    for location in (node.get("source") or {}).get("locations", [])
                ),
                default=0,
            ),
            node["id"],
        ),
    )
    evidence = {
        "operator": "comparison",
        "duplicate_groups": group_evidence,
        "redundant_node_count": sum(len(nodes) - 1 for nodes in repeated),
    }
    identity = {
        "rule_id": "decode.repeated_compare.v1",
        "module": module["name"],
        "groups": [group["comparison_nodes"] for group in group_evidence],
    }
    return [
        {
            "finding_id": _finding_id(identity),
            "rule_id": identity["rule_id"],
            "category": "decode_factoring",
            "severity": "advisory",
            "module": module["name"],
            "module_display_name": module["display_name"],
            "source": source_node["source"],
            "confidence": 0.68,
            "evidence": evidence,
            "recommendation": (
                "Consider computing each repeated decode once and reusing the "
                "result across selection, control, and status logic; treat this "
                "primarily as a clarity change unless mapping shows a benefit."
            ),
            "transformation_id": "factor_repeated_decode",
            "predicted_effect": {
                "area": "neutral",
                "cell_count": "neutral",
                "delay": "neutral",
            },
            "risks": [
                "Synthesis may already merge identical comparisons automatically.",
                "Factoring must preserve priority, default behavior, and X semantics.",
            ],
        }
    ]


def _comparator_selection_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    producers: dict[str, tuple[dict[str, Any], str]] = {}
    for node in module["nodes"]:
        for port_name, port in node["ports"].items():
            if port["direction"] != "output":
                continue
            for bit in port["bits"]:
                if isinstance(bit, int):
                    producers[f"n:{bit}"] = (node, port_name)

    findings = []
    for mux in module["nodes"]:
        if mux["operation"] != "mux":
            continue
        a_port = mux["ports"].get("A")
        b_port = mux["ports"].get("B")
        if not a_port or not b_port:
            continue
        a_producer = _single_producer(a_port["bits"], producers)
        b_producer = _single_producer(b_port["bits"], producers)
        if a_producer is None or b_producer is None:
            continue
        a_node, _ = a_producer
        b_node, _ = b_producer
        operation = a_node["operation"]
        if (
            a_node["id"] == b_node["id"]
            or operation != b_node["operation"]
            or operation not in _COMPARISON_OPERATIONS
        ):
            continue
        comparator_nodes = sorted((a_node["id"], b_node["id"]))
        operand_widths = sorted(
            {
                node["ports"].get(port_name, {}).get("width")
                for node in (a_node, b_node)
                for port_name in ("A", "B")
                if node["ports"].get(port_name, {}).get("width") is not None
            }
        )
        evidence = {
            "selection_node": mux["id"],
            "comparison": operation,
            "comparator_nodes": comparator_nodes,
            "comparator_count": 2,
            "operand_widths": operand_widths,
            "selected_result_width": mux["ports"].get("Y", {}).get("width"),
            "comparison_parameters": [
                a_node["parameters"],
                b_node["parameters"],
            ],
        }
        identity = {
            "rule_id": "comparator_selection.output_mux.v1",
            "module": module["name"],
            "selection_node": mux["id"],
            "comparator_nodes": comparator_nodes,
        }
        findings.append(
            {
                "finding_id": _finding_id(identity),
                "rule_id": identity["rule_id"],
                "category": "comparator_selection",
                "severity": "advisory",
                "module": module["name"],
                "module_display_name": module["display_name"],
                "source": mux["source"],
                "confidence": 0.79,
                "evidence": evidence,
                "recommendation": (
                    "Do not automatically replace these parallel comparisons with "
                    "operand muxes before one comparator; retain result selection "
                    "unless target synthesis demonstrates a worthwhile tradeoff."
                ),
                "transformation_id": "factor_comparator_selection",
                "predicted_effect": {
                    "area": "degrade",
                    "cell_count": "uncertain",
                    "delay": "degrade",
                },
                "risks": [
                    "Two full-width input mux banks can cost more than result selection.",
                    "Input muxing can add delay before the shared comparator.",
                    "Operand order, signedness, width extension, and selector polarity must match.",
                    "Synthesis may already share or restructure mutually exclusive comparisons.",
                ],
            }
        )
    return findings


def _wide_variable_shift_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for node in module["nodes"]:
        if node["operation"] not in _VARIABLE_SHIFT_OPERATIONS:
            continue
        data_port = node["ports"].get("A")
        amount_port = node["ports"].get("B")
        if not data_port or not amount_port:
            continue
        data_width = data_port["width"]
        amount_width = amount_port["width"]
        required_index_bits = max(1, (data_width - 1).bit_length())
        variable_amount_bits = sum(
            isinstance(bit, int) for bit in amount_port["bits"]
        )
        if amount_width <= required_index_bits or variable_amount_bits == 0:
            continue
        evidence = {
            "shift_node": node["id"],
            "operation": node["operation"],
            "data_width": data_width,
            "amount_width": amount_width,
            "required_index_bits": required_index_bits,
            "excess_amount_bits": amount_width - required_index_bits,
            "variable_amount_bits": variable_amount_bits,
        }
        identity = {
            "rule_id": "variable_shift.wide_amount.v1",
            "module": module["name"],
            "shift_node": node["id"],
        }
        findings.append(
            {
                "finding_id": _finding_id(identity),
                "rule_id": identity["rule_id"],
                "category": "variable_shift",
                "severity": "advisory",
                "module": module["name"],
                "module_display_name": module["display_name"],
                "source": node["source"],
                "confidence": 0.84,
                "evidence": evidence,
                "recommendation": (
                    "Guard out-of-range shift amounts and feed only the minimum "
                    "required low-order bits into the variable shifter."
                ),
                "transformation_id": "bound_variable_shift",
                "predicted_effect": {
                    "area": "improve",
                    "cell_count": "improve",
                    "delay": "improve",
                },
                "risks": [
                    "Dropping high shift bits without an out-of-range guard changes behavior.",
                    "Right shifts must preserve the original logical or arithmetic semantics.",
                    "Non-power-of-two widths require an explicit comparison against the data width.",
                ],
            }
        )
    return findings


def _inferred_sign_extended_width(bits: list[int | str]) -> int | None:
    if len(bits) < 3 or not isinstance(bits[-1], int):
        return None
    sign_bit = bits[-1]
    repeated = 0
    for bit in reversed(bits):
        if bit != sign_bit:
            break
        repeated += 1
    if repeated < 2:
        return None
    inferred_width = len(bits) - repeated + 1
    return inferred_width if inferred_width >= 2 else None


def _over_wide_signed_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for node in module["nodes"]:
        if node["operation"] not in {"lt", "le", "gt", "ge"}:
            continue
        parameters = node["parameters"]
        if not parameters.get("A_SIGNED") or not parameters.get("B_SIGNED"):
            continue
        a_port = node["ports"].get("A")
        b_port = node["ports"].get("B")
        if not a_port or not b_port:
            continue
        inferred_a_width = _inferred_sign_extended_width(a_port["bits"])
        inferred_b_width = _inferred_sign_extended_width(b_port["bits"])
        if inferred_a_width is None or inferred_b_width is None:
            continue
        if inferred_a_width >= a_port["width"] or inferred_b_width >= b_port["width"]:
            continue
        evidence = {
            "operator_node": node["id"],
            "operation": node["operation"],
            "signed": True,
            "operand_widths": {
                "A": a_port["width"],
                "B": b_port["width"],
            },
            "inferred_source_widths": {
                "A": inferred_a_width,
                "B": inferred_b_width,
            },
            "redundant_sign_bits": {
                "A": a_port["width"] - inferred_a_width,
                "B": b_port["width"] - inferred_b_width,
            },
        }
        identity = {
            "rule_id": "width_signedness.redundant_sign_extension.v1",
            "module": module["name"],
            "operator_node": node["id"],
        }
        findings.append(
            {
                "finding_id": _finding_id(identity),
                "rule_id": identity["rule_id"],
                "category": "width_or_signedness",
                "severity": "advisory",
                "module": module["name"],
                "module_display_name": module["display_name"],
                "source": node["source"],
                "confidence": 0.72,
                "evidence": evidence,
                "recommendation": (
                    "Keep this signed comparison at the operands' proven natural "
                    "width for clarity and sizing safety; do not assume a mapped "
                    "PPA gain because synthesis can remove the extension."
                ),
                "transformation_id": "narrow_intermediate_width",
                "predicted_effect": {
                    "area": "neutral",
                    "cell_count": "neutral",
                    "delay": "neutral",
                },
                "risks": [
                    "Both operands must remain signed and have matching widths.",
                    "A cast can change expression sizing if applied after arithmetic.",
                    "Synthesis may already eliminate redundant sign-extension logic.",
                ],
            }
        )
    return findings


def _serial_popcount_findings(module: dict[str, Any]) -> list[dict[str, Any]]:
    path = _longest_operator_path(module, "add")
    if not _looks_like_serial_popcount(path):
        return []
    terminal = path[-1]
    bit_term_nodes = [
        node["id"]
        for node in path
        if any(
            _is_single_bit_term(node["ports"].get(name))
            for name in ("A", "B")
        )
    ]
    estimated_terms = len(bit_term_nodes)
    evidence = {
        "operator": "add",
        "adder_nodes": [node["id"] for node in path],
        "serial_depth": len(path),
        "balanced_depth_estimate": max(1, (estimated_terms - 1).bit_length()),
        "input_bit_terms_estimate": estimated_terms,
        "nodes_with_single_bit_term": bit_term_nodes,
        "count_width": terminal["ports"].get("Y", {}).get("width"),
    }
    identity = {
        "rule_id": "popcount.serial_accumulation.v1",
        "module": module["name"],
        "adder_nodes": evidence["adder_nodes"],
    }
    return [
        {
            "finding_id": _finding_id(identity),
            "rule_id": identity["rule_id"],
            "category": "popcount_or_saturation",
            "severity": "advisory",
            "module": module["name"],
            "module_display_name": module["display_name"],
            "source": terminal["source"],
            "confidence": 0.84,
            "evidence": evidence,
            "recommendation": (
                "If timing justifies an area tradeoff, restructure this serial "
                "one-bit accumulation as a balanced population-count tree while "
                "preserving the full count width."
            ),
            "transformation_id": "restructure_popcount_or_saturation",
            "predicted_effect": {
                "area": "degrade",
                "cell_count": "uncertain",
                "delay": "improve",
            },
            "risks": [
                "Every input bit must appear exactly once in the balanced tree.",
                "Intermediate widths must represent the maximum partial count.",
                "Balanced and decoded forms can trade substantial area for timing.",
            ],
        }
    ]


def analyze_rules(graph: dict[str, Any]) -> dict[str, Any]:
    findings = []
    for module in graph["modules"]:
        findings.extend(_resource_sharing_findings(module))
        findings.extend(_serial_arithmetic_findings(module))
        findings.extend(_priority_mux_depth_findings(module))
        findings.extend(_pre_operation_mux_findings(module))
        findings.extend(_repeated_decode_findings(module))
        findings.extend(_comparator_selection_findings(module))
        findings.extend(_wide_variable_shift_findings(module))
        findings.extend(_over_wide_signed_findings(module))
        findings.extend(_serial_popcount_findings(module))
    findings.sort(key=lambda finding: finding["finding_id"])
    result = {
        "schema_version": 1,
        "ruleset_version": RULESET_VERSION,
        "case_id": graph["case_id"],
        "variant_id": graph["variant_id"],
        "graph_hash": graph["graph_hash"],
        "mode": "rules",
        "findings": findings,
    }
    result["analysis_hash"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def write_rule_analysis(
    graph: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    result = analyze_rules(graph)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result
