from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus_registry import (
    CorpusRegistryError,
    CorpusRegistryV1,
    VARIANT_STATES,
    VariantManifestV1,
    parse_variant_manifest,
)
from rtl_advisor.mvp_schema import (
    file_sha256,
    read_hashed_json,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


P2_PLAN_SCHEMA_ID = "rtl-advisor-p2-proof-plan-v1"
P2_PLAN_DOCUMENT_TYPE = "rtl-advisor.p2-proof-plan"
P2_RESULT_DOCUMENT_TYPE = "rtl-advisor.p2-proof-result"
P2_SCHEMA_VERSION = 1
EXPECTED_RELATIONS = ("equivalent", "inequivalent_control")


class SequentialEquivalenceError(RuntimeError):
    """Raised when a P2 proof cannot produce trustworthy evidence."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "sequential_equivalence_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


def _relative_path(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SequentialEquivalenceError(f"{context} must be a non-empty path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ""}:
        raise SequentialEquivalenceError(
            f"{context} must stay within the project workspace",
            code="unsafe_path",
        )
    return path.as_posix()


def _string_list(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SequentialEquivalenceError(f"{context} must be an array of strings")
    return list(value)


def load_p2_proof_plan(
    path: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    plan_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SequentialEquivalenceError(
            f"invalid P2 proof plan {plan_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SequentialEquivalenceError("P2 proof plan must be a JSON object")
    expected_hash = raw.get("semantic_hash")
    core = {key: value for key, value in raw.items() if key != "semantic_hash"}
    if not isinstance(expected_hash, str) or stable_hash(core) != expected_hash:
        raise SequentialEquivalenceError(
            "P2 proof plan semantic hash mismatch",
            code="artifact_hash_mismatch",
        )
    if (
        raw.get("schema_version") != P2_SCHEMA_VERSION
        or raw.get("schema") != P2_PLAN_SCHEMA_ID
        or raw.get("document_type") != P2_PLAN_DOCUMENT_TYPE
    ):
        raise SequentialEquivalenceError(
            "unsupported P2 proof plan schema",
            code="unsupported_schema",
        )
    required_strings = (
        "proof_id",
        "variant_id",
        "parent_reference_id",
        "tranche_id",
        "tranche_semantic_hash",
    )
    for field in required_strings:
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise SequentialEquivalenceError(f"P2 proof plan lacks {field}")
    if raw.get("expected_relation") not in EXPECTED_RELATIONS:
        raise SequentialEquivalenceError("P2 proof plan has an invalid expected_relation")

    registration = raw.get("registration")
    if not isinstance(registration, dict):
        raise SequentialEquivalenceError("P2 proof plan lacks variant registration")
    required_registration_fields = {
        "variant_lineage_id",
        "display_name",
        "origin",
        "source_locations",
        "source_changes",
    }
    if set(registration) != required_registration_fields:
        raise SequentialEquivalenceError(
            "P2 variant registration fields do not match the V1 contract"
        )

    contract = raw.get("contract")
    if not isinstance(contract, dict):
        raise SequentialEquivalenceError("P2 proof plan lacks a contract")
    if contract.get("level") != "P2" or contract.get("latency_relation") != "same_cycle":
        raise SequentialEquivalenceError(
            "P2 proof requires a same-cycle contract",
            code="invalid_proof_contract",
        )
    assumptions = _string_list(contract.get("assumptions"), "contract.assumptions")
    observables = _string_list(contract.get("observables"), "contract.observables")
    if not assumptions or not observables:
        raise SequentialEquivalenceError(
            "P2 proof contract requires explicit assumptions and observables",
            code="invalid_proof_contract",
        )
    reset = contract.get("reset")
    if not isinstance(reset, dict) or not all(
        field in reset for field in ("clock", "signal", "sequence")
    ):
        raise SequentialEquivalenceError(
            "P2 proof contract requires an explicit reset sequence",
            code="invalid_proof_contract",
        )

    if raw.get("backend") != "sby_miter":
        raise SequentialEquivalenceError(
            "P2 proof backend must be sby_miter",
            code="unsupported_proof_backend",
        )
    config_path = _relative_path(raw.get("formal_config"), "formal_config")
    if not isinstance(raw.get("task"), str) or not raw["task"].strip():
        raise SequentialEquivalenceError("P2 proof plan requires an SBY task")
    inputs = raw.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise SequentialEquivalenceError("P2 proof plan requires hashed inputs")
    parsed_inputs: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(inputs):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise SequentialEquivalenceError(
                f"inputs[{index}] must contain only path and sha256"
            )
        input_path = _relative_path(item.get("path"), f"inputs[{index}].path")
        digest = item.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SequentialEquivalenceError(f"inputs[{index}].sha256 is invalid")
        if input_path in seen_paths:
            raise SequentialEquivalenceError(f"duplicate P2 proof input: {input_path}")
        seen_paths.add(input_path)
        parsed_inputs.append({"path": input_path, "sha256": digest})
    if config_path not in seen_paths:
        raise SequentialEquivalenceError("the formal configuration must be a hashed input")

    tools = raw.get("tools")
    if not isinstance(tools, dict) or set(tools) != {
        "yosys_version",
        "sby_version",
        "make_version",
    }:
        raise SequentialEquivalenceError("P2 proof plan requires exact tool constraints")
    if any(not isinstance(value, str) or not value for value in tools.values()):
        raise SequentialEquivalenceError("P2 proof tool constraints must be strings")

    root = Path(project_root).expanduser().resolve()
    for item in parsed_inputs:
        candidate = (root / item["path"]).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise SequentialEquivalenceError(
                f"P2 proof input escapes the project workspace: {item['path']}",
                code="unsafe_path",
            ) from exc
        if not candidate.is_file():
            raise SequentialEquivalenceError(
                f"P2 proof input is unavailable: {item['path']}",
                code="stale_proof_input",
            )
        if file_sha256(candidate) != item["sha256"]:
            raise SequentialEquivalenceError(
                f"P2 proof input hash changed: {item['path']}",
                code="stale_proof_input",
            )
    return raw


def _registry_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    contract = plan["contract"]
    return {
        "schema": "rtl-advisor-proof-v1",
        "level": contract["level"],
        "kind": contract["kind"],
        "latency_relation": contract["latency_relation"],
        "assumptions": contract["assumptions"],
        "observables": contract["observables"],
    }


def _variant_manifest(
    plan: Mapping[str, Any],
    *,
    state: str,
    source_changes: list[dict[str, str]],
    artifact_hashes: Mapping[str, str],
) -> VariantManifestV1:
    registration = plan["registration"]
    try:
        return parse_variant_manifest(
            {
                "schema_version": 1,
                "schema": "rtl-advisor-variant-v1",
                "document_type": "rtl-advisor.variant-manifest",
                "variant_id": plan["variant_id"],
                "parent_reference_id": plan["parent_reference_id"],
                "variant_lineage_id": registration["variant_lineage_id"],
                "display_name": registration["display_name"],
                "state": state,
                "origin": registration["origin"],
                "source_locations": registration["source_locations"],
                "source_changes": source_changes,
                "expected_relation": plan["expected_relation"],
                "proof_contract": _registry_contract(plan),
                "artifact_hashes": dict(artifact_hashes),
            }
        )
    except CorpusRegistryError as exc:
        raise SequentialEquivalenceError(
            f"invalid P2 variant registration: {exc}",
            code=exc.code,
        ) from exc


def record_p2_proof(
    config: ProjectConfig,
    *,
    registry: CorpusRegistryV1,
    plan_path: str | Path,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Append the declared, prepared, and observed proof states to the corpus."""

    plan_file = Path(plan_path).expanduser().resolve()
    plan = load_p2_proof_plan(plan_file, project_root=config.root)
    if result.get("plan_semantic_hash") != plan["semantic_hash"]:
        raise SequentialEquivalenceError(
            "P2 result does not match the registration plan",
            code="artifact_hash_mismatch",
        )
    status = result.get("status")
    if status not in {"formal_passed", "formal_failed", "formal_inconclusive"}:
        raise SequentialEquivalenceError("P2 result has an invalid formal status")

    config_path = (config.root / str(plan["formal_config"])).resolve()
    miter_inputs = [
        item for item in plan["inputs"] if str(item["path"]).endswith("_miter.sv")
    ]
    prepared_hashes = {
        "proof_plan": file_sha256(plan_file),
        "formal_config": file_sha256(config_path),
    }
    if len(miter_inputs) == 1:
        prepared_hashes["formal_miter"] = miter_inputs[0]["sha256"]

    artifact_root = (
        config.artifacts_dir
        / "formal"
        / "p2"
        / str(plan["proof_id"])
        / str(plan["semantic_hash"])
    )
    result_path = artifact_root / "result.json"
    transcript_path = artifact_root / "transcript.log"
    if not result_path.is_file() or not transcript_path.is_file():
        raise SequentialEquivalenceError(
            "P2 proof artifacts are unavailable for registry recording",
            code="stale_proof_input",
        )
    terminal_hashes = {
        **prepared_hashes,
        "proof_result": file_sha256(result_path),
        "proof_transcript": file_sha256(transcript_path),
    }
    counterexample = result.get("counterexample")
    if isinstance(counterexample, Mapping) and isinstance(
        counterexample.get("sha256"), str
    ):
        terminal_hashes["counterexample"] = str(counterexample["sha256"])

    registration = plan["registration"]
    declared = _variant_manifest(
        plan,
        state="declared",
        source_changes=[],
        artifact_hashes={},
    )
    prepared = _variant_manifest(
        plan,
        state="prepared",
        source_changes=list(registration["source_changes"]),
        artifact_hashes=prepared_hashes,
    )
    terminal_state = {
        "formal_passed": "proof_passed",
        "formal_failed": "proof_failed",
        "formal_inconclusive": "proof_inconclusive",
    }[str(status)]
    terminal = _variant_manifest(
        plan,
        state=terminal_state,
        source_changes=list(registration["source_changes"]),
        artifact_hashes=terminal_hashes,
    )

    current = {item.variant_id: item for item in registry.variants()}.get(
        str(plan["variant_id"])
    )
    events: list[dict[str, Any]] = []
    targets = (declared, prepared, terminal)
    if current is None:
        events.append(registry.add_manifest(declared))
        current = declared
    for target in targets[1:]:
        if current.state == target.state:
            if current != target:
                raise SequentialEquivalenceError(
                    f"registry state {target.state!r} conflicts with P2 evidence",
                    code="artifact_hash_mismatch",
                )
            continue
        if VARIANT_STATES.index(current.state) > VARIANT_STATES.index(target.state):
            continue
        try:
            event = registry.advance_manifest(target)
        except CorpusRegistryError as exc:
            raise SequentialEquivalenceError(str(exc), code=exc.code) from exc
        events.append(event)
        current = target
    return {
        "status": "recorded" if events else "already_recorded",
        "variant_id": plan["variant_id"],
        "variant_state": terminal.state,
        "events": events,
        "registry_root": str(registry.root),
    }


def _tool_identity(executable: str, args: tuple[str, ...]) -> dict[str, str]:
    path = shutil.which(executable)
    if path is None:
        raise SequentialEquivalenceError(
            f"required P2 proof tool is unavailable: {executable}",
            code="missing_tool",
        )
    resolved = Path(path).resolve()
    try:
        result = run_command((executable, *args), timeout_seconds=30)
    except ToolExecutionError as exc:
        raise SequentialEquivalenceError(str(exc), code="missing_tool") from exc
    if result.returncode != 0:
        raise SequentialEquivalenceError(
            f"could not identify P2 proof tool {executable}: "
            f"{result.stderr or result.stdout}",
            code="missing_tool",
        )
    version = first_output_line(result)
    if version is None:
        raise SequentialEquivalenceError(
            f"P2 proof tool returned no version: {executable}",
            code="missing_tool",
        )
    return {
        "command": executable,
        "path": str(resolved),
        "sha256": file_sha256(resolved),
        "version": version,
    }


def collect_p2_tool_identity(formal_command: str = "sby") -> dict[str, Any]:
    tools = {
        "sby": _tool_identity(formal_command, ("--version",)),
        "yosys": _tool_identity("yosys", ("-V",)),
        "make": _tool_identity("make", ("--version",)),
        "abc": _tool_identity("yosys-abc", ("-c", "version")),
    }
    return {**tools, "identity_hash": stable_hash(tools)}


def _validate_tool_contract(plan: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    expected = plan["tools"]
    actual = {
        "yosys_version": identity["yosys"]["version"],
        "sby_version": identity["sby"]["version"],
        "make_version": identity["make"]["version"],
    }
    for key, expected_fragment in expected.items():
        if str(expected_fragment) not in actual[key]:
            raise SequentialEquivalenceError(
                f"P2 proof tool mismatch for {key}: expected "
                f"{expected_fragment!r}, got {actual[key]!r}",
                code="stale_proof_tool",
            )


def _classify_sby_result(returncode: int, output_dir: Path, transcript: str) -> str:
    if (
        returncode == 0
        and (output_dir / "PASS").is_file()
        and "DONE (PASS, rc=0)" in transcript
    ):
        return "equivalent"
    inequivalence_markers = ("Status returned by engine: FAIL", "Assert failed")
    if (
        (output_dir / "FAIL").is_file()
        and "DONE (FAIL" in transcript
        and any(marker in transcript for marker in inequivalence_markers)
    ):
        return "inequivalent"
    if (output_dir / "ERROR").is_file() or "DONE (ERROR" in transcript:
        return "error"
    return "inconclusive"


def run_p2_proof(
    config: ProjectConfig,
    *,
    plan_path: str | Path,
    formal_command: str = "sby",
) -> dict[str, Any]:
    plan = load_p2_proof_plan(plan_path, project_root=config.root)
    tool_identity = collect_p2_tool_identity(formal_command)
    _validate_tool_contract(plan, tool_identity)

    artifact_root = (
        config.artifacts_dir
        / "formal"
        / "p2"
        / str(plan["proof_id"])
        / str(plan["semantic_hash"])
    )
    summary_path = artifact_root / "result.json"
    if summary_path.is_file():
        cached = read_hashed_json(
            summary_path,
            document_type=P2_RESULT_DOCUMENT_TYPE,
            schema_version=P2_SCHEMA_VERSION,
        )
        if cached.get("tool_identity", {}).get("identity_hash") != tool_identity["identity_hash"]:
            raise SequentialEquivalenceError(
                "cached P2 proof used a different tool identity",
                code="stale_proof_tool",
            )
        return {**cached, "cached": True}

    artifact_root.mkdir(parents=True, exist_ok=True)
    formal_output = artifact_root / "formal"
    formal_config = (config.root / str(plan["formal_config"])).resolve()
    command = (
        formal_command,
        "-f",
        "-d",
        str(formal_output),
        str(formal_config),
        str(plan["task"]),
    )
    try:
        result = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=config.root,
        )
        transcript = "\n".join(
            part for part in (result.stdout, result.stderr) if part
        )
        returncode: int | None = result.returncode
    except ToolExecutionError as exc:
        transcript = str(exc)
        returncode = None
    transcript_path = artifact_root / "transcript.log"
    transcript_path.write_text(transcript + ("\n" if transcript else ""), encoding="utf-8")
    relation = _classify_sby_result(
        -1 if returncode is None else returncode,
        formal_output,
        transcript,
    )
    expected = str(plan["expected_relation"])
    expectation_met = (
        relation == "equivalent"
        if expected == "equivalent"
        else relation == "inequivalent"
    )
    status = {
        "equivalent": "formal_passed",
        "inequivalent": "formal_failed",
        "inconclusive": "formal_inconclusive",
        "error": "formal_inconclusive",
    }[relation]
    pass_marker = formal_output / "PASS"
    fail_marker = formal_output / "FAIL"
    counterexample = formal_output / "engine_0" / "trace.vcd"
    core = {
        "schema_version": P2_SCHEMA_VERSION,
        "schema": P2_PLAN_SCHEMA_ID,
        "document_type": P2_RESULT_DOCUMENT_TYPE,
        "proof_id": plan["proof_id"],
        "plan_semantic_hash": plan["semantic_hash"],
        "tranche_id": plan["tranche_id"],
        "tranche_semantic_hash": plan["tranche_semantic_hash"],
        "variant_id": plan["variant_id"],
        "parent_reference_id": plan["parent_reference_id"],
        "expected_relation": expected,
        "observed_relation": relation,
        "status": status,
        "expectation_met": expectation_met,
        "safe": relation == "equivalent" and expectation_met,
        "contract": plan["contract"],
        "inputs": plan["inputs"],
        "backend": plan["backend"],
        "formal_config": plan["formal_config"],
        "formal_task": plan["task"],
        "tool_identity": tool_identity,
        "command": list(command),
        "returncode": returncode,
        "transcript_path": str(transcript_path),
        "transcript_sha256": file_sha256(transcript_path),
        "formal_marker": (
            {"kind": "PASS", "sha256": file_sha256(pass_marker)}
            if pass_marker.is_file()
            else {"kind": "FAIL", "sha256": file_sha256(fail_marker)}
            if fail_marker.is_file()
            else None
        ),
        "counterexample": (
            {"path": str(counterexample), "sha256": file_sha256(counterexample)}
            if counterexample.is_file()
            else None
        ),
    }
    stored = write_hashed_json(summary_path, core, exclusive=True)
    return {**stored, "cached": False}
