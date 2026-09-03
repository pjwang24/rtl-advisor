from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_registry import CorpusRegistryError, parse_reference_manifest
from rtl_advisor.mvp_schema import read_hashed_json, stable_hash
from rtl_advisor.transformation_registry import (
    ARBITER_TRANSFORMATION_ID,
    DEFAULT_TRANSFORMATION_REGISTRY,
    PROOF_LEVELS,
)


EXECUTOR_REGISTRY_VERSION = "rtl-advisor-executor-registry-v1"


class TransformationExecutorError(RuntimeError):
    """Raised when an executor declaration or dispatch cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_transformation_executor",
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutorSpec:
    executor_id: str
    version: str
    transformation_id: str
    reference_ids: tuple[str, ...]
    proof_levels: tuple[str, ...]
    candidate_origin: str
    upstream_project_id: str
    source_root: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class TransformationExecutor(Protocol):
    """Stable execution boundary used by Agent V2 and family studies."""

    spec: ExecutorSpec

    def load_reference(
        self,
        config: ProjectConfig,
        manifest_path: str | Path,
    ) -> dict[str, Any]: ...

    def findings(self, reference: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    def write_reference_input(
        self,
        path: Path,
        reference: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def validate_reference_input(self, record: Mapping[str, Any]) -> None: ...

    def prepare_candidate(
        self,
        config: ProjectConfig,
        reference_input: Mapping[str, Any],
        finding: Mapping[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]: ...

    def validate_candidate(self, candidate: Mapping[str, Any]) -> None: ...

    def verify_candidate(
        self,
        config: ProjectConfig,
        candidate: Mapping[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]: ...

    def measurement_designs(
        self,
        candidate: Mapping[str, Any],
    ) -> tuple[Any, Any]: ...

    def negative_controls(
        self,
        candidate: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]: ...


class TransformationExecutorRegistry:
    def __init__(self, executors: Iterable[TransformationExecutor]) -> None:
        records = tuple(executors)
        executor_ids = [item.spec.executor_id for item in records]
        duplicate_ids = sorted(
            value for value in set(executor_ids) if executor_ids.count(value) > 1
        )
        if duplicate_ids:
            raise TransformationExecutorError(
                f"duplicate executor ID: {duplicate_ids[0]}",
                code="duplicate_executor",
            )

        reference_owners: dict[str, str] = {}
        for executor in records:
            spec = executor.spec
            if not spec.executor_id or not spec.version:
                raise TransformationExecutorError(
                    "executor ID and version are required"
                )
            transformation = DEFAULT_TRANSFORMATION_REGISTRY.get(
                spec.transformation_id
            )
            unsupported_levels = sorted(
                set(spec.proof_levels) - set(transformation.proof_levels)
            )
            if unsupported_levels or not spec.proof_levels:
                value = unsupported_levels[0] if unsupported_levels else "<empty>"
                raise TransformationExecutorError(
                    f"executor {spec.executor_id} uses unsupported proof level {value}",
                    code="unsupported_proof_level",
                )
            if set(spec.proof_levels) - set(PROOF_LEVELS):
                raise TransformationExecutorError(
                    f"executor {spec.executor_id} uses an unknown proof level",
                    code="unsupported_proof_level",
                )
            if spec.candidate_origin not in transformation.candidate_origins:
                raise TransformationExecutorError(
                    f"executor {spec.executor_id} has unsupported candidate origin",
                    code="unsupported_candidate_origin",
                )
            for reference_id in spec.reference_ids:
                if reference_id not in transformation.reference_ids:
                    raise TransformationExecutorError(
                        f"executor {spec.executor_id} owns unregistered reference "
                        f"{reference_id}",
                        code="unknown_reference",
                    )
                if reference_id in reference_owners:
                    raise TransformationExecutorError(
                        f"reference {reference_id} is owned by both "
                        f"{reference_owners[reference_id]} and {spec.executor_id}",
                        code="duplicate_reference_executor",
                    )
                reference_owners[reference_id] = spec.executor_id

        self._executors = records
        self._by_id = {item.spec.executor_id: item for item in records}
        self._by_reference = {
            reference_id: self._by_id[executor_id]
            for reference_id, executor_id in reference_owners.items()
        }
        core = {
            "registry_version": EXECUTOR_REGISTRY_VERSION,
            "transformation_registry_hash": (
                DEFAULT_TRANSFORMATION_REGISTRY.registry_hash
            ),
            "executors": [item.spec.to_dict() for item in records],
        }
        self._manifest = {**core, "registry_hash": stable_hash(core)}

    @property
    def registry_hash(self) -> str:
        return str(self._manifest["registry_hash"])

    def manifest(self) -> dict[str, Any]:
        return {
            **self._manifest,
            "executors": [dict(item) for item in self._manifest["executors"]],
        }

    def executors(self) -> tuple[TransformationExecutor, ...]:
        return self._executors

    def get(self, executor_id: str, version: str | None = None) -> TransformationExecutor:
        try:
            executor = self._by_id[executor_id]
        except KeyError as exc:
            raise TransformationExecutorError(
                f"unknown executor ID: {executor_id}",
                code="missing_executor",
            ) from exc
        if version is not None and executor.spec.version != version:
            raise TransformationExecutorError(
                f"stale executor version for {executor_id}",
                code="stale_executor_version",
            )
        return executor

    def for_reference(self, reference_id: str) -> TransformationExecutor:
        try:
            return self._by_reference[reference_id]
        except KeyError as exc:
            raise TransformationExecutorError(
                f"no executor supports reference {reference_id}",
                code="unsupported_reference",
            ) from exc

    def for_candidate(self, candidate: Mapping[str, Any]) -> TransformationExecutor:
        executor_id = candidate.get("executor_id")
        executor_version = candidate.get("executor_version")
        if not isinstance(executor_id, str):
            # Legacy realistic-evidence records predate explicit executor fields.
            reference_id = str(candidate.get("reference_id", ""))
            executor = self.for_reference(reference_id)
        else:
            executor = self.get(
                executor_id,
                str(executor_version) if executor_version is not None else None,
            )
        if candidate.get("executor_registry_hash") not in {
            None,
            self.registry_hash,
        }:
            raise TransformationExecutorError(
                "candidate executor registry hash is stale",
                code="stale_executor_registry",
            )
        return executor

    def for_manifest(self, path: str | Path) -> TransformationExecutor:
        manifest_path = Path(path).expanduser().resolve()
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping) and "semantic_hash" in raw:
                raw = read_hashed_json(manifest_path)
            manifest = parse_reference_manifest(raw)
        except (OSError, json.JSONDecodeError, CorpusRegistryError) as exc:
            raise TransformationExecutorError(
                f"invalid corpus reference manifest {manifest_path}: {exc}",
                code=getattr(exc, "code", "invalid_reference_manifest"),
            ) from exc
        return self.for_reference(manifest.reference_id)


class OpenTitanPpcTreeExecutor:
    """Compatibility adapter for the already implemented realistic slice."""

    spec = ExecutorSpec(
        executor_id="opentitan-ppc-tree",
        version="1",
        transformation_id=ARBITER_TRANSFORMATION_ID,
        reference_ids=("opentitan-prim-arbiter-ppc",),
        proof_levels=("P2",),
        candidate_origin="upstream_alternative",
        upstream_project_id="lowrisc-opentitan",
        source_root="upstream/opentitan",
    )

    def load_reference(
        self,
        config: ProjectConfig,
        manifest_path: str | Path,
    ) -> dict[str, Any]:
        from rtl_advisor.realistic_evidence import load_supported_reference

        return load_supported_reference(config, manifest_path)

    def findings(self, reference: Mapping[str, Any]) -> list[dict[str, Any]]:
        from rtl_advisor.realistic_evidence import arbiter_findings

        return [
            {
                **item,
                "executor_id": self.spec.executor_id,
                "executor_version": self.spec.version,
                "executor_registry_hash": DEFAULT_EXECUTOR_REGISTRY.registry_hash,
            }
            for item in arbiter_findings(reference)
        ]

    def write_reference_input(
        self,
        path: Path,
        reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        from rtl_advisor.realistic_evidence import write_reference_input

        record = write_reference_input(path, reference)
        # Preserve old append-only inputs. New records gain executor metadata in
        # their review/finding/candidate chain without rewriting old artifacts.
        return record

    def validate_reference_input(self, record: Mapping[str, Any]) -> None:
        from rtl_advisor.realistic_evidence import validate_reference_input

        validate_reference_input(record)

    def prepare_candidate(
        self,
        config: ProjectConfig,
        reference_input: Mapping[str, Any],
        finding: Mapping[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        from rtl_advisor.realistic_evidence import prepare_arbiter_candidate

        return prepare_arbiter_candidate(
            config,
            reference_input,
            finding,
            artifact_root,
        )

    def validate_candidate(self, candidate: Mapping[str, Any]) -> None:
        from rtl_advisor.realistic_evidence import validate_arbiter_candidate

        validate_arbiter_candidate(candidate)

    def verify_candidate(
        self,
        config: ProjectConfig,
        candidate: Mapping[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        from rtl_advisor.realistic_evidence import verify_arbiter_candidate

        return verify_arbiter_candidate(config, candidate, artifact_root)

    def measurement_designs(
        self,
        candidate: Mapping[str, Any],
    ) -> tuple[Any, Any]:
        from rtl_advisor.realistic_evidence import arbiter_designs_from_candidate

        return arbiter_designs_from_candidate(candidate)

    def negative_controls(
        self,
        candidate: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        return (
            {"control_id": "bad_reset", "mutation_kind": 1},
            {"control_id": "bad_state_update", "mutation_kind": 2},
            {"control_id": "bad_grant", "mutation_kind": 3},
            {"control_id": "bad_data_selection", "mutation_kind": 4},
        )


class FamilyPairExecutor:
    """Executor adapter for one frozen curated arbiter pair."""

    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec

    def load_reference(
        self,
        config: ProjectConfig,
        manifest_path: str | Path,
    ) -> dict[str, Any]:
        from rtl_advisor.arbiter_family_execution import load_family_reference

        return load_family_reference(config, manifest_path, self.spec)

    def findings(self, reference: Mapping[str, Any]) -> list[dict[str, Any]]:
        from rtl_advisor.arbiter_family_execution import family_findings

        return family_findings(
            reference,
            self.spec,
            executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        )

    def write_reference_input(
        self,
        path: Path,
        reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        from rtl_advisor.arbiter_family_execution import write_family_reference_input

        return write_family_reference_input(
            path,
            reference,
            self.spec,
            executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        )

    def validate_reference_input(self, record: Mapping[str, Any]) -> None:
        from rtl_advisor.arbiter_family_execution import validate_family_reference_input

        validate_family_reference_input(
            record,
            self.spec,
            executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        )

    def prepare_candidate(
        self,
        config: ProjectConfig,
        reference_input: Mapping[str, Any],
        finding: Mapping[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        from rtl_advisor.arbiter_family_execution import prepare_family_candidate

        return prepare_family_candidate(
            config,
            reference_input,
            finding,
            artifact_root,
            self.spec,
            executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        )

    def validate_candidate(self, candidate: Mapping[str, Any]) -> None:
        from rtl_advisor.arbiter_family_execution import validate_family_candidate

        validate_family_candidate(
            candidate,
            self.spec,
            executor_registry_hash=DEFAULT_EXECUTOR_REGISTRY.registry_hash,
        )

    def verify_candidate(
        self,
        config: ProjectConfig,
        candidate: Mapping[str, Any],
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        from rtl_advisor.arbiter_family_execution import verify_family_candidate

        return verify_family_candidate(config, candidate, artifact_root, self.spec)

    def measurement_designs(
        self,
        candidate: Mapping[str, Any],
    ) -> tuple[Any, Any]:
        from rtl_advisor.arbiter_family_execution import family_designs_from_candidate

        return family_designs_from_candidate(candidate, self.spec)

    def negative_controls(
        self,
        candidate: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        proof_level = str((candidate.get("proof_contract") or {}).get("level", ""))
        controls = [
            {"control_id": "bad_priority_direction", "mutation_kind": 1},
            {"control_id": "bad_grant", "mutation_kind": 2},
        ]
        if proof_level == "P2":
            controls.append(
                {"control_id": "bad_reset_state_update", "mutation_kind": 3}
            )
        if candidate.get("has_payload") is True:
            controls.append(
                {"control_id": "bad_data_selection", "mutation_kind": 4}
            )
        return tuple(controls)


def _family_pair(
    executor_id: str,
    reference_id: str,
    proof_level: str,
    candidate_origin: str,
    upstream_project_id: str,
    source_root: str,
) -> FamilyPairExecutor:
    return FamilyPairExecutor(
        ExecutorSpec(
            executor_id=executor_id,
            version="1",
            transformation_id=ARBITER_TRANSFORMATION_ID,
            reference_ids=(reference_id,),
            proof_levels=(proof_level,),
            candidate_origin=candidate_origin,
            upstream_project_id=upstream_project_id,
            source_root=source_root,
        )
    )


DEFAULT_EXECUTOR_REGISTRY = TransformationExecutorRegistry(
    (
        OpenTitanPpcTreeExecutor(),
        _family_pair(
            "opentitan-fixed-flat-prefix",
            "opentitan-prim-arbiter-fixed",
            "P1",
            "reviewed_codex_candidate",
            "lowrisc-opentitan",
            "upstream/opentitan",
        ),
        _family_pair(
            "pulp-rr-tree-rotated-mask",
            "pulp-common-cells-rr-arb-tree",
            "P2",
            "reviewed_codex_candidate",
            "pulp-common-cells",
            "upstream/common_cells",
        ),
        _family_pair(
            "pulp-stream-arbiter-alternative-cone",
            "pulp-common-cells-stream-arbiter-flushable",
            "P2",
            "reviewed_codex_candidate",
            "pulp-common-cells",
            "upstream/common_cells",
        ),
        _family_pair(
            "verilog-axis-arbiter-hierarchical",
            "verilog-axis-arbiter",
            "P2",
            "reviewed_codex_candidate",
            "alexforencich-verilog-axis",
            "upstream/verilog-axis",
        ),
        _family_pair(
            "verilog-axis-arb-mux-balanced",
            "verilog-axis-axis-arb-mux",
            "P2",
            "reviewed_codex_candidate",
            "alexforencich-verilog-axis",
            "upstream/verilog-axis",
        ),
        _family_pair(
            "basejump-fixed-balanced-tree",
            "basejump-bsg-arb-fixed",
            "P1",
            "reviewed_codex_candidate",
            "basejump-stl",
            "upstream/basejump_stl",
        ),
        _family_pair(
            "basejump-round-robin-rotated-mask",
            "basejump-bsg-arb-round-robin",
            "P2",
            "reviewed_codex_candidate",
            "basejump-stl",
            "upstream/basejump_stl",
        ),
        _family_pair(
            "basejump-locking-fixed-alternative-cone",
            "basejump-bsg-locking-arb-fixed",
            "P2",
            "reviewed_codex_candidate",
            "basejump-stl",
            "upstream/basejump_stl",
        ),
        _family_pair(
            "basejump-n-to-1-upstream-parameter",
            "basejump-bsg-round-robin-n-to-1",
            "P2",
            "upstream_alternative",
            "basejump-stl",
            "upstream/basejump_stl",
        ),
    )
)
