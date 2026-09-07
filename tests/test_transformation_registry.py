from __future__ import annotations

import pytest

from rtl_advisor.mvp_schema import stable_hash
from rtl_advisor.transformation_registry import (
    ARBITER_TRANSFORMATION_ID,
    DEFAULT_TRANSFORMATION_REGISTRY,
    TransformationRegistry,
    TransformationRegistryError,
    TransformationSpec,
)


def test_default_registry_is_deterministic_and_contains_both_mvp_families() -> None:
    manifest = DEFAULT_TRANSFORMATION_REGISTRY.manifest()

    assert manifest["registry_hash"] == stable_hash(
        {
            "registry_version": manifest["registry_version"],
            "transformations": manifest["transformations"],
        }
    )
    assert [item["transformation_id"] for item in manifest["transformations"]] == [
        "adder_reduction_association",
        ARBITER_TRANSFORMATION_ID,
    ]
    arbiter = DEFAULT_TRANSFORMATION_REGISTRY.get(ARBITER_TRANSFORMATION_ID)
    assert arbiter.proof_levels == ("P1", "P2")
    assert arbiter.sequential_supported is True


def test_registry_rejects_duplicate_ids_and_unknown_proof_levels() -> None:
    item = TransformationSpec(
        transformation_id="duplicate",
        version="v1",
        display_name="Duplicate",
        candidate_origins=("deterministic_rewrite",),
        proof_levels=("P1",),
    )
    with pytest.raises(TransformationRegistryError) as duplicate:
        TransformationRegistry((item, item))
    assert duplicate.value.code == "duplicate_transformation"

    with pytest.raises(TransformationRegistryError) as unsupported:
        TransformationRegistry(
            (
                TransformationSpec(
                    transformation_id="bad-proof",
                    version="v1",
                    display_name="Bad proof",
                    candidate_origins=("deterministic_rewrite",),
                    proof_levels=("P4",),
                ),
            )
        )
    assert unsupported.value.code == "unsupported_proof_level"


def test_candidate_metadata_is_bound_to_registry_and_contract() -> None:
    spec = DEFAULT_TRANSFORMATION_REGISTRY.get(ARBITER_TRANSFORMATION_ID)
    proof = {"level": "P2", "latency_relation": "same_cycle"}
    metadata = {
        "transformation_id": spec.transformation_id,
        "transformation_version": spec.version,
        "transformation_registry_hash": (
            DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
        ),
        "candidate_origin": "upstream_alternative",
        "proof_contract": proof,
        "proof_contract_hash": stable_hash(proof),
    }

    DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(metadata)

    with pytest.raises(TransformationRegistryError) as stale:
        DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(
            {**metadata, "transformation_registry_hash": "0" * 64}
        )
    assert stale.value.code == "stale_transformation_registry"

    with pytest.raises(TransformationRegistryError) as origin:
        DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(
            {**metadata, "candidate_origin": "unreviewed_generator"}
        )
    assert origin.value.code == "unsupported_candidate_origin"

    with pytest.raises(TransformationRegistryError) as contract:
        DEFAULT_TRANSFORMATION_REGISTRY.validate_candidate_metadata(
            {**metadata, "proof_contract_hash": "f" * 64}
        )
    assert contract.value.code == "artifact_hash_mismatch"
