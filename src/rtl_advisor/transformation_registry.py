from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from rtl_advisor.mvp_schema import (
    TRANSFORMATION_ID,
    TRANSFORMATION_VERSION,
    stable_hash,
)


TRANSFORMATION_REGISTRY_VERSION = "rtl-advisor-transformation-registry-v1"
PROOF_LEVELS = ("P1", "P2", "P3")
CANDIDATE_ORIGINS = (
    "deterministic_rewrite",
    "upstream_alternative",
    "reviewed_codex_candidate",
)
ARBITER_TRANSFORMATION_ID = "same_cycle_arbiter_topology"
ARBITER_TRANSFORMATION_VERSION = "opentitan-ppc-tree-v5"
LEGACY_TRANSFORMATION_REGISTRY_HASHES = (
    "02cb30ba16c983b88bfc7c426659405b5acf59bff86b6dc0f02bcf26055c3a89",
)

ARBITER_REFERENCE_IDS = (
    "opentitan-prim-arbiter-ppc",
    "opentitan-prim-arbiter-fixed",
    "pulp-common-cells-rr-arb-tree",
    "pulp-common-cells-stream-arbiter-flushable",
    "verilog-axis-arbiter",
    "verilog-axis-axis-arb-mux",
    "basejump-bsg-arb-fixed",
    "basejump-bsg-arb-round-robin",
    "basejump-bsg-locking-arb-fixed",
    "basejump-bsg-round-robin-n-to-1",
)


class TransformationRegistryError(ValueError):
    """Raised when a transformation declaration is invalid or ambiguous."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_transformation_registry",
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TransformationSpec:
    transformation_id: str
    version: str
    display_name: str
    candidate_origins: tuple[str, ...]
    proof_levels: tuple[str, ...]
    reference_ids: tuple[str, ...] = ()
    analysis_supported: bool = True
    rewriter_available: bool = True
    sequential_supported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TransformationRegistry:
    def __init__(self, specs: Iterable[TransformationSpec]) -> None:
        records = tuple(specs)
        identifiers = [item.transformation_id for item in records]
        duplicates = sorted(
            identifier for identifier in set(identifiers)
            if identifiers.count(identifier) > 1
        )
        if duplicates:
            raise TransformationRegistryError(
                f"duplicate transformation ID: {duplicates[0]}",
                code="duplicate_transformation",
            )
        for item in records:
            if not item.transformation_id or not item.version or not item.display_name:
                raise TransformationRegistryError(
                    "transformation ID, version, and display name are required"
                )
            unsupported_levels = sorted(set(item.proof_levels) - set(PROOF_LEVELS))
            if unsupported_levels or not item.proof_levels:
                value = unsupported_levels[0] if unsupported_levels else "<empty>"
                raise TransformationRegistryError(
                    f"unsupported proof level: {value}",
                    code="unsupported_proof_level",
                )
            unsupported_origins = sorted(
                set(item.candidate_origins) - set(CANDIDATE_ORIGINS)
            )
            if unsupported_origins or not item.candidate_origins:
                value = unsupported_origins[0] if unsupported_origins else "<empty>"
                raise TransformationRegistryError(
                    f"unsupported candidate origin: {value}",
                    code="unsupported_candidate_origin",
                )
            if item.sequential_supported != any(
                level in {"P2", "P3"} for level in item.proof_levels
            ):
                raise TransformationRegistryError(
                    f"{item.transformation_id} sequential capability disagrees "
                    "with its proof levels"
                )
        self._specs = records
        self._by_id = {item.transformation_id: item for item in records}
        core = {
            "registry_version": TRANSFORMATION_REGISTRY_VERSION,
            "transformations": [item.to_dict() for item in records],
        }
        self._manifest = {**core, "registry_hash": stable_hash(core)}

    @property
    def registry_hash(self) -> str:
        return str(self._manifest["registry_hash"])

    def manifest(self) -> dict[str, Any]:
        return {
            **self._manifest,
            "transformations": [
                dict(item) for item in self._manifest["transformations"]
            ],
        }

    def specs(self) -> tuple[TransformationSpec, ...]:
        return self._specs

    def get(self, transformation_id: str) -> TransformationSpec:
        try:
            return self._by_id[transformation_id]
        except KeyError as exc:
            raise TransformationRegistryError(
                f"unknown transformation ID: {transformation_id}",
                code="unknown_transformation",
            ) from exc

    def for_reference(self, reference_id: str) -> tuple[TransformationSpec, ...]:
        return tuple(
            item for item in self._specs if reference_id in item.reference_ids
        )

    def validate_candidate_metadata(self, value: Mapping[str, Any]) -> None:
        transformation_id = str(value.get("transformation_id", ""))
        spec = self.get(transformation_id)
        if value.get("transformation_version") != spec.version:
            raise TransformationRegistryError(
                f"stale transformation version for {transformation_id}",
                code="stale_transformation",
            )
        if value.get("transformation_registry_hash") not in {
            self.registry_hash,
            *LEGACY_TRANSFORMATION_REGISTRY_HASHES,
        }:
            raise TransformationRegistryError(
                "candidate transformation registry hash is stale",
                code="stale_transformation_registry",
            )
        origin = value.get("candidate_origin")
        if origin not in spec.candidate_origins:
            raise TransformationRegistryError(
                f"unsupported candidate origin for {transformation_id}: {origin!r}",
                code="unsupported_candidate_origin",
            )
        proof = value.get("proof_contract")
        if not isinstance(proof, Mapping) or proof.get("level") not in spec.proof_levels:
            raise TransformationRegistryError(
                f"candidate proof contract does not match {transformation_id}",
                code="unsupported_proof_level",
            )
        contract_hash = value.get("proof_contract_hash")
        if contract_hash != stable_hash(dict(proof)):
            raise TransformationRegistryError(
                "candidate proof-contract hash mismatch",
                code="artifact_hash_mismatch",
            )


DEFAULT_TRANSFORMATION_REGISTRY = TransformationRegistry(
    (
        TransformationSpec(
            transformation_id=TRANSFORMATION_ID,
            version=TRANSFORMATION_VERSION,
            display_name="Balanced unsigned addition chain",
            candidate_origins=("deterministic_rewrite",),
            proof_levels=("P1",),
        ),
        TransformationSpec(
            transformation_id=ARBITER_TRANSFORMATION_ID,
            version=ARBITER_TRANSFORMATION_VERSION,
            display_name="Same-cycle arbiter topology alternative",
            candidate_origins=(
                "upstream_alternative",
                "reviewed_codex_candidate",
            ),
            proof_levels=("P1", "P2"),
            reference_ids=ARBITER_REFERENCE_IDS,
            sequential_supported=True,
        ),
    )
)
