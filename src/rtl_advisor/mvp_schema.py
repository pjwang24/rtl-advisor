from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from rtl_advisor.rtl_input import DesignInputV2, RTLInputError, normalize_design_input


AGENT_V2_SCHEMA_VERSION = 2
AGENT_V2_FLOW_VERSION = "rtl-advisor-agent-v2"
RUN_SCHEMA_VERSION = 1
RUN_SCHEMA_ID = "rtl-advisor-run-v1"
PILOT_MANIFEST_SCHEMA_VERSION = 1
PILOT_MANIFEST_DOCUMENT_TYPE = "rtl-advisor.pilot-manifest"
TRANSFORMATION_ID = "adder_reduction_association"
TRANSFORMATION_VERSION = "balanced-unsigned-add-chain-v1"
OBJECTIVES = ("timing", "area", "balanced")
SYNTHESIS_PROFILES = ("standard", "stronger")
COMPILE_CONTEXT_SCHEMA_VERSION = 1


class MVPSchemaError(ValueError):
    """Raised when an MVP manifest or append-only artifact is invalid."""

    def __init__(self, message: str, *, code: str = "invalid_mvp_artifact") -> None:
        super().__init__(message)
        self.code = code


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_id(kind: str, index: int, path: str | Path) -> str:
    """Return a relocation-stable identity for an ordered compile input."""

    return f"{kind}:{index:04d}:{Path(path).name}"


def _include_tree(path: str | Path, index: int) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise MVPSchemaError(
            f"include directory is unavailable: {root}",
            code="stale_compile_context",
        )
    entries: list[dict[str, str]] = []
    for entry in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            raise MVPSchemaError(
                f"include trees may not contain symbolic links: {entry}",
                code="unsupported_compile_context",
            )
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise MVPSchemaError(
                f"include tree contains a non-regular file: {entry}",
                code="unsupported_compile_context",
            )
        entries.append({"logical_path": relative, "sha256": file_sha256(entry)})
    tree_core = {
        "logical_id": _logical_id("include", index, root),
        "files": entries,
    }
    return {**tree_core, "tree_sha256": stable_hash(tree_core)}


def compile_context_snapshot(design: DesignInputV2) -> dict[str, Any]:
    """Fingerprint every compile input without binding the fingerprint to paths.

    ``DesignInputV2.design_hash`` intentionally contains absolute paths.  MVP
    candidates live in isolated directories, so baseline/candidate parity needs
    a second identity based on ordered logical inputs and their content.  This
    snapshot also closes the gap where headers and filelists were previously not
    content-hashed by ``DesignInputV2``.
    """

    sources: list[dict[str, str]] = []
    for index, source in enumerate(design.files):
        path = Path(source.path)
        if not path.is_file():
            raise MVPSchemaError(
                f"RTL source is unavailable: {path}",
                code="stale_compile_context",
            )
        sources.append(
            {
                "logical_id": _logical_id("source", index, path),
                "sha256": file_sha256(path),
            }
        )

    filelists: list[dict[str, str]] = []
    for index, raw_path in enumerate(design.filelists):
        path = Path(raw_path)
        if not path.is_file() or path.is_symlink():
            raise MVPSchemaError(
                f"filelist is unavailable or not a regular file: {path}",
                code="stale_compile_context",
            )
        filelists.append(
            {
                "logical_id": _logical_id("filelist", index, path),
                "sha256": file_sha256(path),
            }
        )

    core: dict[str, Any] = {
        "schema_version": COMPILE_CONTEXT_SCHEMA_VERSION,
        "top": design.top,
        "defines": list(design.defines),
        "sources": sources,
        "filelists": filelists,
        "include_trees": [
            _include_tree(path, index) for index, path in enumerate(design.include_dirs)
        ],
    }
    return {**core, "compile_context_hash": stable_hash(core)}


def validate_compile_context_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("schema_version") != COMPILE_CONTEXT_SCHEMA_VERSION:
        raise MVPSchemaError(
            "unsupported compile-context snapshot schema",
            code="unsupported_schema",
        )
    expected = snapshot.get("compile_context_hash")
    core = {
        key: value for key, value in snapshot.items() if key != "compile_context_hash"
    }
    if expected != stable_hash(core):
        raise MVPSchemaError(
            "compile-context snapshot hash mismatch",
            code="artifact_hash_mismatch",
        )


def compile_contexts_compatible(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Compare the compile context while allowing rewritten source contents."""

    validate_compile_context_snapshot(baseline)
    validate_compile_context_snapshot(candidate)
    return all(
        baseline.get(field) == candidate.get(field)
        for field in ("schema_version", "top", "defines", "filelists", "include_trees")
    ) and [item.get("logical_id") for item in baseline.get("sources", [])] == [
        item.get("logical_id") for item in candidate.get("sources", [])
    ]


def write_hashed_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    exclusive: bool = False,
) -> dict[str, Any]:
    """Write a self-hashed JSON record without exposing a partial document.

    Mutable compatibility records use an atomic replace. Append-only stage
    writers pass ``exclusive=True`` so two concurrent writers cannot silently
    replace one another after both observe a missing path.
    """

    final = dict(payload)
    final.pop("semantic_hash", None)
    final["semantic_hash"] = stable_hash(final)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(final, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if exclusive:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # A hard link publishes the complete temporary inode under the
                # final name and fails atomically if another writer won.
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise MVPSchemaError(
                    f"append-only artifact already exists: {path}",
                    code="append_only_conflict",
                ) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return final

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return final


def read_hashed_json(
    path: Path,
    *,
    document_type: str | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MVPSchemaError(f"invalid artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MVPSchemaError(f"artifact must be a JSON object: {path}")
    expected = payload.get("semantic_hash")
    core = {key: value for key, value in payload.items() if key != "semantic_hash"}
    if expected != stable_hash(core):
        raise MVPSchemaError(
            f"artifact semantic hash mismatch: {path}",
            code="artifact_hash_mismatch",
        )
    if document_type is not None and payload.get("document_type") != document_type:
        raise MVPSchemaError(
            f"unexpected document type in {path}: {payload.get('document_type')!r}"
        )
    if schema_version is not None and payload.get("schema_version") != schema_version:
        raise MVPSchemaError(
            f"unsupported schema version in {path}: {payload.get('schema_version')!r}",
            code="unsupported_schema",
        )
    return payload


@dataclass(frozen=True)
class PilotProvenanceV1:
    project: str
    source_url: str
    revision: str
    license: str
    license_path: str


@dataclass(frozen=True)
class PilotManifestV1:
    schema_version: int
    document_type: str
    top: str
    files: tuple[str, ...]
    filelist: str | None
    include_dirs: tuple[str, ...]
    defines: tuple[str, ...]
    objective: str
    provenance: PilotProvenanceV1
    source_hashes: tuple[tuple[str, str], ...]
    compile_context_hash: str
    synthesis_profiles: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_hashes"] = [
            {"path": path, "sha256": sha256}
            for path, sha256 in self.source_hashes
        ]
        return payload


def _require_string(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MVPSchemaError(f"pilot manifest field {name!r} must be a non-empty string")
    return value


def _string_tuple(raw: Any, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise MVPSchemaError(f"pilot manifest field {name!r} must be a string array")
    return tuple(raw)


def build_pilot_manifest(
    *,
    top: str,
    files: Iterable[str | Path] = (),
    filelist: str | Path | None = None,
    include_dirs: Iterable[str | Path] = (),
    defines: Iterable[str] = (),
    objective: str,
    provenance: PilotProvenanceV1,
    base: str | Path,
) -> tuple[PilotManifestV1, DesignInputV2]:
    if objective not in OBJECTIVES:
        raise MVPSchemaError(f"unsupported objective: {objective!r}")
    file_values = tuple(files)
    if bool(file_values) == (filelist is not None):
        raise MVPSchemaError(
            "PilotManifest requires exactly one of files or filelist",
            code="invalid_manifest_input",
        )
    try:
        design = normalize_design_input(
            top=top,
            files=file_values,
            filelist=filelist,
            include_dirs=include_dirs,
            defines=defines,
            base=base,
        )
    except RTLInputError as exc:
        raise MVPSchemaError(str(exc), code="invalid_manifest_input") from exc
    manifest_base = Path(base).expanduser().resolve()
    filelist_path = None
    if filelist is not None:
        filelist_path = Path(filelist).expanduser()
        if not filelist_path.is_absolute():
            filelist_path = manifest_base / filelist_path
        filelist_path = filelist_path.resolve()
    context = compile_context_snapshot(design)
    manifest = PilotManifestV1(
        schema_version=PILOT_MANIFEST_SCHEMA_VERSION,
        document_type=PILOT_MANIFEST_DOCUMENT_TYPE,
        top=design.top,
        files=(
            tuple(source.path for source in design.files)
            if filelist_path is None
            else ()
        ),
        filelist=str(filelist_path) if filelist_path else None,
        include_dirs=design.include_dirs,
        defines=design.defines,
        objective=objective,
        provenance=provenance,
        source_hashes=tuple((source.path, source.sha256) for source in design.files),
        compile_context_hash=str(context["compile_context_hash"]),
        synthesis_profiles=SYNTHESIS_PROFILES,
    )
    return manifest, design


def load_pilot_manifest(path: str | Path) -> tuple[PilotManifestV1, DesignInputV2]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MVPSchemaError(f"invalid pilot manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MVPSchemaError("pilot manifest must be a JSON object")
    allowed_fields = {
        "schema_version",
        "document_type",
        "top",
        "files",
        "filelist",
        "include_dirs",
        "defines",
        "objective",
        "provenance",
        "source_hashes",
        "compile_context_hash",
        "synthesis_profiles",
    }
    unknown_fields = sorted(set(raw) - allowed_fields)
    if unknown_fields:
        raise MVPSchemaError(
            f"pilot manifest contains unknown fields: {', '.join(unknown_fields)}"
        )
    if raw.get("schema_version") != PILOT_MANIFEST_SCHEMA_VERSION:
        raise MVPSchemaError("unsupported pilot manifest schema")
    if raw.get("document_type") != PILOT_MANIFEST_DOCUMENT_TYPE:
        raise MVPSchemaError("invalid pilot manifest document type")
    objective = _require_string(raw, "objective")
    if objective not in OBJECTIVES:
        raise MVPSchemaError(f"unsupported objective: {objective!r}")
    profiles = _string_tuple(raw.get("synthesis_profiles"), "synthesis_profiles")
    if profiles != SYNTHESIS_PROFILES:
        raise MVPSchemaError(
            f"synthesis_profiles must be exactly {list(SYNTHESIS_PROFILES)!r}"
        )
    provenance_raw = raw.get("provenance")
    if not isinstance(provenance_raw, dict):
        raise MVPSchemaError("pilot manifest provenance must be an object")
    provenance_fields = {
        "project", "source_url", "revision", "license", "license_path"
    }
    unknown_provenance = sorted(set(provenance_raw) - provenance_fields)
    if unknown_provenance:
        raise MVPSchemaError(
            "pilot manifest provenance contains unknown fields: "
            + ", ".join(unknown_provenance)
        )
    provenance = PilotProvenanceV1(
        project=_require_string(provenance_raw, "project"),
        source_url=_require_string(provenance_raw, "source_url"),
        revision=_require_string(provenance_raw, "revision"),
        license=_require_string(provenance_raw, "license"),
        license_path=_require_string(provenance_raw, "license_path"),
    )
    files = _string_tuple(raw.get("files"), "files")
    filelist_raw = raw.get("filelist")
    if filelist_raw is not None and not isinstance(filelist_raw, str):
        raise MVPSchemaError("pilot manifest filelist must be a string or null")
    manifest, design = build_pilot_manifest(
        top=_require_string(raw, "top"),
        files=files,
        filelist=filelist_raw,
        include_dirs=_string_tuple(raw.get("include_dirs"), "include_dirs"),
        defines=_string_tuple(raw.get("defines"), "defines"),
        objective=objective,
        provenance=provenance,
        base=manifest_path.parent,
    )
    expected_hashes_raw = raw.get("source_hashes")
    if not isinstance(expected_hashes_raw, list):
        raise MVPSchemaError("pilot manifest source_hashes must be an array")
    expected_hashes: tuple[tuple[str, str], ...] = ()
    parsed_hashes: list[tuple[str, str]] = []
    for item in expected_hashes_raw:
        if not isinstance(item, dict):
            raise MVPSchemaError("pilot manifest source_hashes contains a non-object")
        unknown_hash_fields = sorted(set(item) - {"path", "sha256"})
        if unknown_hash_fields:
            raise MVPSchemaError(
                "pilot manifest source_hashes entry contains unknown fields: "
                + ", ".join(unknown_hash_fields)
            )
        parsed_hashes.append(
            (_require_string(item, "path"), _require_string(item, "sha256"))
        )
    expected_hashes = tuple(parsed_hashes)
    actual_hashes = tuple((source.path, source.sha256) for source in design.files)
    if expected_hashes != actual_hashes:
        raise MVPSchemaError(
            "pilot source hashes are stale",
            code="stale_source_hashes",
        )
    context = compile_context_snapshot(design)
    if raw.get("compile_context_hash") != context["compile_context_hash"]:
        raise MVPSchemaError(
            "pilot compile-context hash is stale",
            code="stale_compile_context",
        )
    return manifest, design


def source_integrity(files: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    mismatches: list[dict[str, str | None]] = []
    for item in files:
        path = Path(str(item.get("path", "")))
        expected = str(item.get("sha256", ""))
        actual = file_sha256(path) if path.is_file() else None
        if actual != expected:
            mismatches.append(
                {
                    "path": str(path),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                }
            )
    return {"ok": not mismatches, "mismatches": mismatches}


def normalized_result_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the path- and timestamp-independent parity contract."""

    omitted_keys = {
        "run_id",
        "candidate_id",
        "semantic_hash",
        "proof_semantic_hash",
        "input_semantic_hash",
        "parents",
        "evidence_hashes",
        "artifacts",
        "command",
        "artifact_dir",
        "record_path",
        "diff_path",
        "design_hash",
        "baseline_design_hash",
        "candidate_design_hash",
    }

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: normalize(item)
                for key, item in value.items()
                if key not in omitted_keys
                and key not in {"path", "file", "root"}
                and not key.endswith("_path")
                and not key.endswith("_semantic_hash")
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, tuple):
            return [normalize(item) for item in value]
        return value

    projection = {
        key: payload.get(key)
        for key in (
            "schema_version",
            "run_schema",
            "document_type",
            "flow_version",
            "status",
            "decision",
            "objective",
            "operations",
            "tools",
            "transformation",
            "model",
            "findings",
            "candidate_generation_allowed",
            "evidence",
            "finding",
            "source_integrity",
            "formal",
            "measurements",
            "safe",
            "operation",
            "error",
            "limitations",
        )
        if key in payload
    }
    normalized = normalize(projection)
    assert isinstance(normalized, dict)
    return normalized
