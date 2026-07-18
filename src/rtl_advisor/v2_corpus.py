from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Iterable

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
    generate_case,
)
from rtl_advisor.topology_rtl import render_topology_variants


V2_SUITE_SCHEMA_VERSION = 2
V2_SUITE_FLOW_VERSION = "rtl-advisor-topology-suite-v2"
V2_SELECTION_SEED = 20260714
CALIBRATION_CASES_PER_FAMILY = 40
BLIND_CASES_PER_FAMILY = 8
TOTAL_CASES_PER_FAMILY = CALIBRATION_CASES_PER_FAMILY + BLIND_CASES_PER_FAMILY
V2_SPLITS = ("calibration-v2", "heldout-v2")


class V2CorpusError(RuntimeError):
    """Raised when the v2 topology corpus contract cannot be satisfied."""


TOPOLOGY_DOMAINS: dict[str, dict[str, tuple[Any, ...]]] = {
    RESOURCE_SHARING_FAMILY: {
        "operation": ("add", "sub", "multiply"),
        "width": (8, 12, 16, 24, 32),
        "signed": (False, True),
        "branch_count": (2, 3, 4),
        "common_operand_side": ("left", "right"),
    },
    ADDER_ASSOCIATION_FAMILY: {
        "operand_count": (4, 6, 8, 12),
        "width": (8, 12, 16, 24, 32),
        "signed": (False, True),
        "input_depth": ("flat", "one_late", "two_late"),
    },
    PRIORITY_SELECTION_FAMILY: {
        "request_count": (4, 8, 12, 16),
        "width": (1, 8, 16, 32),
        "priority_direction": ("low", "high"),
        "default_behavior": ("zero", "constant"),
    },
    MUX_PLACEMENT_FAMILY: {
        "operation": ("add", "sub", "multiply", "compare"),
        "width": (8, 12, 16, 24, 32),
        "fan_in": (2, 3, 4),
        "common_operand_side": ("left", "right"),
        "signed": (False, True),
    },
    DECODE_FACTORING_FAMILY: {
        "opcode_width": (4, 6, 8),
        "match_count": (4, 8, 12, 16),
        "reuse_count": (2, 4, 8),
        "decode_style": ("exact", "masked"),
        "width": (8, 16),
    },
    COMPARATOR_SELECTION_FAMILY: {
        "relation": ("eq", "lt", "le", "ge"),
        "width": (8, 12, 16, 24, 32),
        "signed": (False, True),
        "fanout": (1, 2, 4),
        "constant_shape": ("zero", "mid", "max"),
    },
    VARIABLE_SHIFT_FAMILY: {
        "direction": ("left", "logical_right", "arithmetic_right"),
        "width": (8, 12, 16, 24, 32),
        "amount_excess": (0, 1, 2),
        "guarded": (False, True),
        "signed": (False, True),
    },
    WIDTH_SIGNEDNESS_FAMILY: {
        "operation": ("add", "compare", "multiply"),
        "width": (8, 12, 16, 24),
        "extension": (2, 4, 8),
        "signedness_mix": ("uu", "ss", "su", "us"),
        "truncate_result": (False, True),
    },
    POPCOUNT_SATURATION_FAMILY: {
        "width": (8, 12, 16, 24, 32, 48, 64),
        "use": ("exact", "threshold", "saturating"),
        "structure": ("linear", "chunked", "tree"),
        "chunk_size": (2, 4, 8),
    },
}


@dataclass(frozen=True)
class V2CaseDescriptor:
    case_id: str
    family: str
    split: str
    index: int
    topology: dict[str, Any]
    topology_signature: str
    width: int
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _legal_tuples(family: str) -> tuple[dict[str, Any], ...]:
    try:
        domains = TOPOLOGY_DOMAINS[family]
    except KeyError as exc:
        raise V2CorpusError(f"unknown v2 topology family: {family}") from exc
    names = tuple(domains)
    candidates = tuple(
        dict(zip(names, values, strict=True))
        for values in itertools.product(*(domains[name] for name in names))
    )
    # Keep every declared value while excluding combinations that make the
    # open-source formal backend impractical. These are legal-space constraints,
    # not post-selection filtering, so coverage is measured against the same set.
    def formally_tractable(topology: dict[str, Any]) -> bool:
        operation = topology.get("operation")
        width = int(topology.get("width", 1))
        if (
            operation == "multiply"
            and family in {RESOURCE_SHARING_FAMILY, MUX_PLACEMENT_FAMILY}
            and width > 8
        ):
            return False
        if operation == "multiply" and family == RESOURCE_SHARING_FAMILY:
            if int(topology["branch_count"]) > 3:
                return False
        if operation == "multiply" and family == MUX_PLACEMENT_FAMILY:
            if int(topology["fan_in"]) > 3:
                return False
        if operation == "multiply" and width > 12:
            return False
        if family == ADDER_ASSOCIATION_FAMILY:
            return int(topology["operand_count"]) * width <= 128
        if family == COMPARATOR_SELECTION_FAMILY and not bool(topology["signed"]):
            relation = str(topology["relation"])
            shape = str(topology["constant_shape"])
            if (relation, shape) in {("lt", "zero"), ("ge", "zero"), ("le", "max")}:
                return False
        if family == DECODE_FACTORING_FAMILY:
            opcode_width = int(topology["opcode_width"])
            match_count = int(topology["match_count"])
            modulus = 1 << opcode_width
            values = {(index * 3 + 1) % modulus for index in range(match_count)}
            if topology["decode_style"] == "masked":
                mask = modulus - 2
                values = {value & mask for value in values}
                universe = {value & mask for value in range(modulus)}
            else:
                universe = set(range(modulus))
            if values == universe:
                return False
        return True

    return tuple(topology for topology in candidates if formally_tractable(topology))


def _pairs(topology: dict[str, Any]) -> frozenset[tuple[str, str, str, str]]:
    items = sorted(topology.items())
    return frozenset(
        (left_name, repr(left_value), right_name, repr(right_value))
        for (left_name, left_value), (right_name, right_value) in itertools.combinations(
            items, 2
        )
    )


def _greedy_cover(
    candidates: Iterable[dict[str, Any]],
    count: int,
    *,
    salt: str,
) -> tuple[dict[str, Any], ...]:
    remaining = list(candidates)
    if len(remaining) < count:
        raise V2CorpusError(f"cannot select {count} cases from {len(remaining)} tuples")
    uncovered = set().union(*(_pairs(candidate) for candidate in remaining))
    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        best = min(
            remaining,
            key=lambda candidate: (
                -len(_pairs(candidate) & uncovered),
                _stable_hash(
                    {
                        "seed": V2_SELECTION_SEED,
                        "salt": salt,
                        "topology": candidate,
                    }
                ),
            ),
        )
        selected.append(best)
        remaining.remove(best)
        uncovered.difference_update(_pairs(best))
    return tuple(selected)


def _descriptor(
    family: str,
    topology: dict[str, Any],
    *,
    split: str,
    index: int,
) -> V2CaseDescriptor:
    signature = _stable_hash({"family": family, "topology": topology})
    case_id = f"v2_{signature[:16]}"
    width = int(topology.get("width", topology.get("opcode_width", 16)))
    seed = int(signature[16:24], 16)
    return V2CaseDescriptor(
        case_id=case_id,
        family=family,
        split=split,
        index=index,
        topology=topology,
        topology_signature=signature,
        width=width,
        seed=seed,
    )


def family_descriptors(family: str) -> tuple[V2CaseDescriptor, ...]:
    selected = _greedy_cover(
        _legal_tuples(family),
        TOTAL_CASES_PER_FAMILY,
        salt=f"{family}:all",
    )
    blind = set(
        _stable_hash(item)
        for item in _greedy_cover(
            selected,
            BLIND_CASES_PER_FAMILY,
            salt=f"{family}:blind",
        )
    )
    calibration_index = 0
    blind_index = 0
    descriptors = []
    for topology in selected:
        if _stable_hash(topology) in blind:
            split = "heldout-v2"
            index = blind_index
            blind_index += 1
        else:
            split = "calibration-v2"
            index = calibration_index
            calibration_index += 1
        descriptors.append(
            _descriptor(family, topology, split=split, index=index)
        )
    return tuple(descriptors)


def all_descriptors() -> tuple[V2CaseDescriptor, ...]:
    return tuple(
        descriptor
        for family in TOPOLOGY_DOMAINS
        for descriptor in family_descriptors(family)
    )


def _pairwise_coverage(
    selected: Iterable[dict[str, Any]],
    legal: Iterable[dict[str, Any]],
) -> float:
    possible = set().union(*(_pairs(item) for item in legal))
    covered = set().union(*(_pairs(item) for item in selected))
    return 1.0 if not possible else len(covered) / len(possible)


def suite_statistics(descriptors: Iterable[V2CaseDescriptor]) -> dict[str, Any]:
    descriptors = tuple(descriptors)
    by_family: dict[str, list[V2CaseDescriptor]] = {}
    for descriptor in descriptors:
        by_family.setdefault(descriptor.family, []).append(descriptor)
    family_stats = {}
    for family, items in sorted(by_family.items()):
        selected = [item.topology for item in items]
        family_stats[family] = {
            "case_count": len(items),
            "calibration_count": sum(item.split == "calibration-v2" for item in items),
            "blind_count": sum(item.split == "heldout-v2" for item in items),
            "pairwise_coverage": round(
                _pairwise_coverage(selected, _legal_tuples(family)), 6
            ),
        }
    return {
        "case_count": len(descriptors),
        "family_count": len(by_family),
        "families": family_stats,
    }


def generate_v2_suite(
    corpus_root: Path,
    split: str,
    *,
    force: bool = False,
) -> Path:
    if split not in V2_SPLITS:
        raise V2CorpusError(f"unsupported v2 split: {split}")
    descriptors = tuple(
        descriptor for descriptor in all_descriptors() if descriptor.split == split
    )
    split_root = corpus_root / split
    cases = []
    for descriptor in descriptors:
        manifest_path = generate_case(
            split_root / descriptor.case_id,
            family=descriptor.family,
            suite="development" if split == "calibration-v2" else "heldout",
            case_id=descriptor.case_id,
            width=descriptor.width,
            seed=descriptor.seed,
            metadata={
                "v2": {
                    "split": descriptor.split,
                    "index": descriptor.index,
                    "topology": descriptor.topology,
                    "topology_signature": descriptor.topology_signature,
                    "selection_seed": V2_SELECTION_SEED,
                }
            },
            rendered_override=render_topology_variants(
                descriptor.family,
                descriptor.case_id,
                descriptor.width,
                descriptor.topology,
            ),
            force=force,
        )
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        cases.append(
            {
                **descriptor.to_dict(),
                "manifest": str(manifest_path.relative_to(split_root)),
                "manifest_sha256": manifest_sha256,
            }
        )
    payload = {
        "schema_version": V2_SUITE_SCHEMA_VERSION,
        "flow_version": V2_SUITE_FLOW_VERSION,
        "selection_seed": V2_SELECTION_SEED,
        "split": split,
        "case_count": len(cases),
        "cases_per_family": (
            CALIBRATION_CASES_PER_FAMILY
            if split == "calibration-v2"
            else BLIND_CASES_PER_FAMILY
        ),
        "cases": sorted(cases, key=lambda item: (item["family"], item["index"])),
    }
    payload["suite_hash"] = _stable_hash(payload)
    suite_path = split_root / "suite.json"
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return suite_path
