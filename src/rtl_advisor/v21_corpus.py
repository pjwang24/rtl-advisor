from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Iterable

from rtl_advisor.advisor_v2 import PROFILES
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
    load_manifest,
)
from rtl_advisor.topology_rtl import render_topology_variants
from rtl_advisor.v2_corpus import TOPOLOGY_DOMAINS as V2_TOPOLOGY_DOMAINS
from rtl_advisor.v2_corpus import all_descriptors as all_v2_descriptors


V21_SUITE_SCHEMA_VERSION = 21
V21_SUITE_FLOW_VERSION = "rtl-advisor-topology-suite-v21"
V21_SELECTION_SEED = 20260715
V21_CALIBRATION_CASES_PER_FAMILY = 64
V21_BLIND_CASES_PER_FAMILY = 8
V21_TOTAL_CASES_PER_FAMILY = 72
V21_SPLITS = ("calibration-v21", "heldout-v21")


class V21CorpusError(RuntimeError):
    """Raised when the frozen V2.1 corpus contract cannot be satisfied."""


V21_TOPOLOGY_DOMAINS: dict[str, dict[str, tuple[Any, ...]]] = {
    family: {name: tuple(values) for name, values in domains.items()}
    for family, domains in V2_TOPOLOGY_DOMAINS.items()
}
V21_TOPOLOGY_DOMAINS[ADDER_ASSOCIATION_FAMILY] = {
    "operand_count": (4, 5, 6, 7, 8, 10, 12, 16),
    "width": (8, 10, 12, 14, 16, 20, 24, 28, 32),
    "signed": (False, True),
    "input_depth": ("flat", "one_late", "two_late"),
}
V21_TOPOLOGY_DOMAINS[PRIORITY_SELECTION_FAMILY] = {
    "request_count": (4, 6, 8, 10, 12, 16, 20),
    "width": (1, 4, 8, 12, 16, 24, 32),
    "priority_direction": ("low", "high"),
    "default_behavior": ("zero", "constant"),
}


@dataclass(frozen=True)
class V21CaseDescriptor:
    case_id: str
    family: str
    split: str
    index: int
    topology: dict[str, Any]
    topology_signature: str
    width: int
    seed: int
    opportunity_propensity: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pairs(topology: dict[str, Any]) -> frozenset[tuple[str, str, str, str]]:
    items = sorted(topology.items())
    return frozenset(
        (left_name, repr(left_value), right_name, repr(right_value))
        for (left_name, left_value), (right_name, right_value) in itertools.combinations(
            items, 2
        )
    )


def _signature(family: str, topology: dict[str, Any]) -> str:
    return _stable_hash({"family": family, "topology": topology})


def _formally_tractable(family: str, topology: dict[str, Any]) -> bool:
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
        if (str(topology["relation"]), str(topology["constant_shape"])) in {
            ("lt", "zero"),
            ("ge", "zero"),
            ("le", "max"),
        }:
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


def _legal_tuples(family: str) -> tuple[dict[str, Any], ...]:
    try:
        domains = V21_TOPOLOGY_DOMAINS[family]
    except KeyError as exc:
        raise V21CorpusError(f"unknown V2.1 family: {family}") from exc
    names = tuple(domains)
    v2_signatures = {item.topology_signature for item in all_v2_descriptors()}
    return tuple(
        topology
        for values in itertools.product(*(domains[name] for name in names))
        if _formally_tractable(
            family,
            topology := dict(zip(names, values, strict=True)),
        )
        and _signature(family, topology) not in v2_signatures
    )


def _greedy_cover(
    candidates: Iterable[dict[str, Any]],
    count: int,
    *,
    salt: str,
    propensity: dict[str, float] | None = None,
) -> tuple[dict[str, Any], ...]:
    remaining = list(candidates)
    if len(remaining) < count:
        raise V21CorpusError(f"cannot select {count} cases from {len(remaining)} tuples")
    uncovered = set().union(*(_pairs(candidate) for candidate in remaining))
    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        best = min(
            remaining,
            key=lambda candidate: (
                -len(_pairs(candidate) & uncovered),
                -float((propensity or {}).get(_stable_hash(candidate), 0.0)),
                _stable_hash(
                    {
                        "seed": V21_SELECTION_SEED,
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


def _load_v2_propensity_rows(path: Path | None) -> tuple[list[dict[str, Any]], str]:
    if path is None or not path.is_file():
        return [], "unavailable-neutral-prior"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V21CorpusError(f"invalid V2 calibration rows {path}: {exc}") from exc
    rows = payload.get("rows") or []
    if payload.get("row_count") != len(rows):
        raise V21CorpusError("V2 propensity source row count mismatch")
    return rows, _stable_hash(payload)


def _propensity_scores(
    family: str,
    candidates: Iterable[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> dict[str, float]:
    topology_by_signature = {
        item.topology_signature: item.topology
        for item in all_v2_descriptors()
        if item.split == "calibration-v2" and item.family == family
    }
    targets_by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("family") == family and row.get("topology_signature") in topology_by_signature:
            targets_by_signature[str(row["topology_signature"])].append(row)
    labels: list[tuple[dict[str, Any], bool]] = []
    for signature, topology in topology_by_signature.items():
        candidate_rows = targets_by_signature.get(signature, [])
        opportunity = any(
            PROFILES["balanced"].eligible(
                float(row["targets"]["delay"]),
                float(row["targets"]["area"]),
            )
            for row in candidate_rows
        )
        labels.append((topology, opportunity))
    if not labels:
        return {_stable_hash(candidate): 0.5 for candidate in candidates}
    family_prior = (1.0 + sum(label for _, label in labels)) / (2.0 + len(labels))
    value_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for topology, label in labels:
        for name, value in topology.items():
            bucket = value_counts[(name, repr(value))]
            bucket[0] += int(label)
            bucket[1] += 1
    result = {}
    for candidate in candidates:
        rates = []
        for name, value in candidate.items():
            positives, total = value_counts[(name, repr(value))]
            rates.append(
                (positives + 2.0 * family_prior) / (total + 2.0)
                if total
                else family_prior
            )
        result[_stable_hash(candidate)] = sum(rates) / len(rates)
    return result


def family_descriptors(
    family: str,
    *,
    propensity_rows_path: Path | None = None,
) -> tuple[V21CaseDescriptor, ...]:
    legal = _legal_tuples(family)
    selected = _greedy_cover(legal, V21_TOTAL_CASES_PER_FAMILY, salt=f"{family}:all")
    rows, _ = _load_v2_propensity_rows(propensity_rows_path)
    propensity = _propensity_scores(family, selected, rows)
    blind_topologies = _greedy_cover(
        selected,
        V21_BLIND_CASES_PER_FAMILY,
        salt=f"{family}:blind",
        propensity=propensity,
    )
    blind = {_stable_hash(topology) for topology in blind_topologies}
    indices = {"calibration-v21": 0, "heldout-v21": 0}
    descriptors = []
    for topology in selected:
        split = "heldout-v21" if _stable_hash(topology) in blind else "calibration-v21"
        signature = _signature(family, topology)
        descriptors.append(
            V21CaseDescriptor(
                case_id=f"v21_{signature[:16]}",
                family=family,
                split=split,
                index=indices[split],
                topology=topology,
                topology_signature=signature,
                width=int(topology.get("width", topology.get("opcode_width", 16))),
                seed=int(signature[16:24], 16),
                opportunity_propensity=round(propensity[_stable_hash(topology)], 9),
            )
        )
        indices[split] += 1
    return tuple(descriptors)


def all_descriptors(
    *,
    propensity_rows_path: Path | None = None,
) -> tuple[V21CaseDescriptor, ...]:
    return tuple(
        descriptor
        for family in V21_TOPOLOGY_DOMAINS
        for descriptor in family_descriptors(
            family,
            propensity_rows_path=propensity_rows_path,
        )
    )


def _pairwise_coverage(
    selected: Iterable[dict[str, Any]], legal: Iterable[dict[str, Any]]
) -> float:
    possible = set().union(*(_pairs(item) for item in legal))
    covered = set().union(*(_pairs(item) for item in selected))
    return 1.0 if not possible else len(covered) / len(possible)


def suite_statistics(descriptors: Iterable[V21CaseDescriptor]) -> dict[str, Any]:
    descriptors = tuple(descriptors)
    by_family: dict[str, list[V21CaseDescriptor]] = defaultdict(list)
    for descriptor in descriptors:
        by_family[descriptor.family].append(descriptor)
    return {
        "case_count": len(descriptors),
        "family_count": len(by_family),
        "families": {
            family: {
                "case_count": len(items),
                "calibration_count": sum(item.split == "calibration-v21" for item in items),
                "blind_count": sum(item.split == "heldout-v21" for item in items),
                "pairwise_coverage": round(
                    _pairwise_coverage(
                        [item.topology for item in items],
                        _legal_tuples(family),
                    ),
                    6,
                ),
            }
            for family, items in sorted(by_family.items())
        },
    }


def generate_v21_suite(corpus_root: Path, split: str, *, force: bool = False) -> Path:
    if split not in V21_SPLITS:
        raise V21CorpusError(f"unsupported V2.1 split: {split}")
    propensity_path = corpus_root.parent / "artifacts/models/v2/calibration-rows.json"
    propensity_rows, propensity_source_hash = _load_v2_propensity_rows(propensity_path)
    descriptors = tuple(
        descriptor
        for descriptor in all_descriptors(propensity_rows_path=propensity_path)
        if descriptor.split == split
    )
    split_root = corpus_root / split
    cases = []
    for descriptor in descriptors:
        manifest_path = generate_case(
            split_root / descriptor.case_id,
            family=descriptor.family,
            suite="development" if split == "calibration-v21" else "heldout",
            case_id=descriptor.case_id,
            width=descriptor.width,
            seed=descriptor.seed,
            metadata={
                "v21": {
                    "split": descriptor.split,
                    "index": descriptor.index,
                    "topology": descriptor.topology,
                    "topology_signature": descriptor.topology_signature,
                    "selection_seed": V21_SELECTION_SEED,
                    "opportunity_propensity": descriptor.opportunity_propensity,
                    "propensity_source_hash": propensity_source_hash,
                    "propensity_training_rows": len(propensity_rows),
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
        manifest = load_manifest(manifest_path)
        cases.append(
            {
                **descriptor.to_dict(),
                "manifest": str(manifest_path.relative_to(split_root)),
                "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "rtl_sha256": {
                    variant.variant_id: variant.sha256 for variant in manifest.variants
                },
            }
        )
    v2_signatures = {item.topology_signature for item in all_v2_descriptors()}
    overlap = sorted(
        case["topology_signature"]
        for case in cases
        if case["topology_signature"] in v2_signatures
    )
    if overlap:
        raise V21CorpusError(f"V2.1/V2 topology overlap: {overlap[0]}")
    core = {
        "schema_version": V21_SUITE_SCHEMA_VERSION,
        "flow_version": V21_SUITE_FLOW_VERSION,
        "selection_seed": V21_SELECTION_SEED,
        "split": split,
        "case_count": len(cases),
        "cases_per_family": (
            V21_CALIBRATION_CASES_PER_FAMILY
            if split == "calibration-v21"
            else V21_BLIND_CASES_PER_FAMILY
        ),
        "v2_disjoint": True,
        "propensity": {
            "source": str(propensity_path.resolve()),
            "source_hash": propensity_source_hash,
            "training_row_count": len(propensity_rows),
            "blind_selection_role": "tie_break_after_pairwise_topology_coverage",
        },
        "cases": sorted(cases, key=lambda item: (item["family"], item["index"])),
    }
    payload = {**core, "suite_hash": _stable_hash(core)}
    suite_path = split_root / "suite.json"
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return suite_path
