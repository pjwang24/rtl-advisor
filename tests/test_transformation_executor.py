from __future__ import annotations

from dataclasses import replace

import pytest

from rtl_advisor.transformation_executor import (
    DEFAULT_EXECUTOR_REGISTRY,
    ExecutorSpec,
    TransformationExecutorError,
    TransformationExecutorRegistry,
)
from rtl_advisor.transformation_registry import ARBITER_TRANSFORMATION_ID


class _Executor:
    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec


def _spec(**changes: object) -> ExecutorSpec:
    base = ExecutorSpec(
        executor_id="test-executor",
        version="1",
        transformation_id=ARBITER_TRANSFORMATION_ID,
        reference_ids=("opentitan-prim-arbiter-ppc",),
        proof_levels=("P2",),
        candidate_origin="upstream_alternative",
        upstream_project_id="test",
        source_root="upstream/test",
    )
    return replace(base, **changes)


def test_default_executor_registry_is_hash_stable_and_owns_opentitan() -> None:
    manifest = DEFAULT_EXECUTOR_REGISTRY.manifest()

    assert manifest["registry_version"] == "rtl-advisor-executor-registry-v1"
    executor = DEFAULT_EXECUTOR_REGISTRY.for_reference(
        "opentitan-prim-arbiter-ppc"
    )
    assert executor.spec.executor_id == "opentitan-ppc-tree"
    assert DEFAULT_EXECUTOR_REGISTRY.for_candidate(
        {
            "reference_id": "opentitan-prim-arbiter-ppc",
            "executor_registry_hash": DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        }
    ) is executor


def test_executor_registry_rejects_duplicate_ids_and_reference_owners() -> None:
    first = _Executor(_spec())
    with pytest.raises(TransformationExecutorError) as duplicate:
        TransformationExecutorRegistry((first, first))
    assert duplicate.value.code == "duplicate_executor"

    second = _Executor(_spec(executor_id="other"))
    with pytest.raises(TransformationExecutorError) as owner:
        TransformationExecutorRegistry((first, second))
    assert owner.value.code == "duplicate_reference_executor"


def test_executor_registry_rejects_unknown_proof_and_stale_version() -> None:
    with pytest.raises(TransformationExecutorError) as proof:
        TransformationExecutorRegistry(
            (_Executor(_spec(proof_levels=("P3",))),)
        )
    assert proof.value.code == "unsupported_proof_level"

    with pytest.raises(TransformationExecutorError) as version:
        DEFAULT_EXECUTOR_REGISTRY.get("opentitan-ppc-tree", "stale")
    assert version.value.code == "stale_executor_version"
