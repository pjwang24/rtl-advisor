from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
from typing import Any, Mapping

from rtl_advisor.config import ProjectConfig
from rtl_advisor.mvp_agent import (
    agent_v2_candidate,
    agent_v2_measure,
    agent_v2_report,
    agent_v2_review,
    agent_v2_verify,
)
from rtl_advisor.mvp_schema import (
    file_sha256,
    read_hashed_json,
    source_integrity,
    stable_hash,
    write_hashed_json,
)
from rtl_advisor.realistic_evidence import (
    M2_CONFIGURATION_IDS,
    REFERENCE_ID,
    SLANG_PLUGIN_PATH,
    arbiter_designs_from_candidate,
    run_arbiter_negative_controls,
)
from rtl_advisor.tools import ToolExecutionError, run_command, sha256_file


STUDY_ID = "realistic-rtl-evidence-slice-v1"
STUDY_DOCUMENT_TYPE = "rtl-advisor.realistic-evidence-study"
REPRODUCIBILITY_DOCUMENT_TYPE = "rtl-advisor.realistic-evidence-reproducibility"
_REPEAT_ID = re.compile(r"^repeat-[12]$")


class RealisticStudyError(RuntimeError):
    """Raised when the frozen realistic evidence study is incomplete."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "realistic_study_failed",
    ) -> None:
        super().__init__(message)
        self.code = code


def _measurement_summary(measurement: Mapping[str, Any]) -> dict[str, Any]:
    profiles = measurement.get("measurements")
    if not isinstance(profiles, Mapping):
        raise RealisticStudyError(
            "completed measurement has no profile evidence",
            code="invalid_measurement",
        )
    normalized: dict[str, Any] = {}
    for profile in ("standard", "stronger"):
        value = profiles.get(profile)
        if not isinstance(value, Mapping):
            raise RealisticStudyError(
                f"completed measurement lacks {profile} evidence",
                code="invalid_measurement",
            )
        baseline = value.get("baseline")
        candidate = value.get("candidate")
        recipe = value.get("recipe")
        if not all(
            isinstance(item, Mapping)
            for item in (baseline, candidate, recipe)
        ):
            raise RealisticStudyError(
                f"{profile} measurement evidence is incomplete",
                code="invalid_measurement",
            )
        assert isinstance(baseline, Mapping)
        assert isinstance(candidate, Mapping)
        assert isinstance(recipe, Mapping)
        normalized[profile] = {
            "classification": value.get("classification"),
            "comparison": value.get("comparison"),
            "recipe_hash": recipe.get("recipe_hash"),
            "constraints_sha256": (
                (baseline.get("constraints") or {}).get("sha256")
            ),
            "baseline": {
                "metrics": baseline.get("metrics"),
                "netlist_sha256": (baseline.get("netlist") or {}).get(
                    "sha256"
                ),
                "cell_signature": (baseline.get("netlist") or {}).get(
                    "cell_signature"
                ),
            },
            "candidate": {
                "metrics": candidate.get("metrics"),
                "netlist_sha256": (candidate.get("netlist") or {}).get(
                    "sha256"
                ),
                "cell_signature": (candidate.get("netlist") or {}).get(
                    "cell_signature"
                ),
            },
        }
    return {
        "decision": measurement.get("decision"),
        "profiles": normalized,
    }


def _control_summary(controls: Mapping[str, Any]) -> dict[str, Any]:
    results = controls.get("results")
    if not isinstance(results, Mapping):
        raise RealisticStudyError(
            "negative-control record is incomplete",
            code="invalid_formal_controls",
        )
    return {
        "status": controls.get("status"),
        "results": {
            label: {
                "status": result.get("status"),
                "observed_relation": result.get("observed_relation"),
                "expectation_met": result.get("expectation_met"),
                "counterexample_sha256": (
                    (result.get("counterexample") or {}).get("sha256")
                ),
                "tool_identity_hash": (
                    (result.get("tool_identity") or {}).get("identity_hash")
                ),
            }
            for label, result in sorted(results.items())
            if isinstance(result, Mapping)
        },
    }


def run_study_repeat(
    config: ProjectConfig,
    *,
    reference_manifest: str | Path,
    repeat_id: str,
) -> dict[str, Any]:
    """Run one clean, isolated P2 + M0/M1 study repeat."""

    if not _REPEAT_ID.fullmatch(repeat_id):
        raise RealisticStudyError(
            "repeat_id must be repeat-1 or repeat-2",
            code="invalid_repeat_id",
        )
    repeat_artifacts = (
        config.artifacts_dir / STUDY_ID / repeat_id
    ).resolve()
    study_path = repeat_artifacts / "study.json"
    if study_path.is_file():
        return read_hashed_json(
            study_path,
            document_type=STUDY_DOCUMENT_TYPE,
            schema_version=1,
        )
    repeat_config = replace(config, artifacts_dir=repeat_artifacts)
    manifest_path = Path(reference_manifest).expanduser()
    if not manifest_path.is_absolute():
        manifest_path = config.root / manifest_path
    manifest_path = manifest_path.resolve()

    review = agent_v2_review(
        repeat_config,
        str(manifest_path),
        objective="timing",
        normalized_command=(STUDY_ID, repeat_id, "review"),
    )
    if review.get("decision") != "candidate_available":
        raise RealisticStudyError(
            "frozen reference did not expose the parameter matrix",
            code="study_reference_unsupported",
        )

    configurations: list[dict[str, Any]] = []
    controls: dict[str, Any] | None = None
    for finding in review.get("findings") or []:
        if not isinstance(finding, Mapping):
            raise RealisticStudyError(
                "review emitted an invalid study finding",
                code="invalid_finding",
            )
        candidate = agent_v2_candidate(
            repeat_config,
            str(review["run_id"]),
            finding_id=str(finding["finding_id"]),
            normalized_command=(STUDY_ID, repeat_id, "candidate"),
        )
        candidate_id = str(candidate["candidate_id"])
        verification = agent_v2_verify(
            repeat_config,
            str(review["run_id"]),
            candidate_id=candidate_id,
            normalized_command=(STUDY_ID, repeat_id, "verify"),
        )
        configuration_id = str(candidate["configuration_id"])
        if configuration_id == "n04-dw32":
            raw_controls = run_arbiter_negative_controls(
                repeat_config,
                candidate["candidate"],
            )
            controls = _control_summary(raw_controls)
            write_hashed_json(
                repeat_artifacts / "negative-controls.json",
                {
                    "schema_version": 1,
                    "document_type": "rtl-advisor.realistic-evidence-controls",
                    "study_id": STUDY_ID,
                    "repeat_id": repeat_id,
                    "configuration_id": configuration_id,
                    **controls,
                },
                exclusive=True,
            )

        measurement: dict[str, Any] | None = None
        if (
            verification.get("status") == "formal_passed"
            and verification.get("safe") is True
        ):
            measurement = agent_v2_measure(
                repeat_config,
                str(review["run_id"]),
                candidate_id=candidate_id,
                normalized_command=(STUDY_ID, repeat_id, "measure"),
            )
        configurations.append(
            {
                "configuration_id": configuration_id,
                "parameters": finding.get("configuration"),
                "candidate_id": candidate_id,
                "candidate_origin": candidate.get("candidate_origin"),
                "reference_id": candidate.get("reference_id"),
                "transformation": candidate.get("transformation"),
                "proof_contract_hash": candidate.get(
                    "proof_contract_hash"
                ),
                "measurement_levels": candidate.get("measurement_levels"),
                "formal": {
                    "status": verification.get("status"),
                    "safe": verification.get("safe"),
                    "observed_relation": (
                        (verification.get("formal") or {}).get(
                            "observed_relation"
                        )
                    ),
                    "tool_identity_hash": (
                        (
                            (verification.get("formal") or {}).get(
                                "tool_identity"
                            )
                            or {}
                        ).get("identity_hash")
                    ),
                },
                "measurement": (
                    _measurement_summary(measurement)
                    if measurement is not None
                    else None
                ),
            }
        )

    report = agent_v2_report(
        repeat_config,
        str(review["run_id"]),
        normalized_command=(STUDY_ID, repeat_id, "report"),
    )
    input_record = read_hashed_json(
        Path(str(review["artifacts"]["input"])),
        document_type="rtl-advisor.run.reference-input",
        schema_version=1,
    )
    source_rows = [
        {
            "path": str(
                Path(str(input_record["source_root"])) / str(relative)
            ),
            "sha256": digest,
        }
        for relative, digest in input_record["source_hashes"].items()
    ]
    normalized = {
        "study_id": STUDY_ID,
        "reference_id": REFERENCE_ID,
        "reference_manifest_hash": input_record["manifest_semantic_hash"],
        "transformation_registry_hash": input_record[
            "transformation_registry_hash"
        ],
        "controls": controls,
        "configurations": configurations,
    }
    payload = {
        "schema_version": 1,
        "document_type": STUDY_DOCUMENT_TYPE,
        "study_id": STUDY_ID,
        "repeat_id": repeat_id,
        "status": (
            "completed"
            if len(configurations) == 4
            and all(
                item["formal"]["status"] == "formal_passed"
                and item["measurement"] is not None
                for item in configurations
            )
            and controls is not None
            and controls["status"] == "passed"
            else "incomplete"
        ),
        "run_id": review["run_id"],
        "reference_manifest": str(manifest_path),
        "source_integrity": source_integrity(source_rows),
        "normalized": normalized,
        "normalized_hash": stable_hash(normalized),
        "artifacts": {
            "root": str(repeat_artifacts),
            "review": review["artifacts"]["review"],
            "report": report["artifacts"]["report"],
            "html": report["artifacts"]["html"],
        },
    }
    return write_hashed_json(study_path, payload, exclusive=True)


def compare_study_repeats(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare the normalized formal and M0/M1 result set exactly."""

    def reproducibility_core(value: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(value.get("normalized") or {})
        controls = dict(normalized.get("controls") or {})
        results = controls.get("results")
        if isinstance(results, Mapping):
            controls["results"] = {
                label: {
                    key: item.get(key)
                    for key in (
                        "status",
                        "observed_relation",
                        "expectation_met",
                        "tool_identity_hash",
                    )
                }
                for label, item in sorted(results.items())
                if isinstance(item, Mapping)
            }
        normalized["controls"] = controls
        return normalized

    first_core = reproducibility_core(first)
    second_core = reproducibility_core(second)
    mismatches: list[str] = []
    if first.get("status") != "completed":
        mismatches.append("repeat-1-incomplete")
    if second.get("status") != "completed":
        mismatches.append("repeat-2-incomplete")
    if first_core != second_core:
        mismatches.append("normalized-result-mismatch")
    core = {
        "schema_version": 1,
        "document_type": REPRODUCIBILITY_DOCUMENT_TYPE,
        "study_id": STUDY_ID,
        "status": "passed" if not mismatches else "failed",
        "exact_match": not mismatches,
        "mismatches": mismatches,
        "repeat_hashes": {
            str(first.get("repeat_id")): stable_hash(first_core),
            str(second.get("repeat_id")): stable_hash(second_core),
        },
        "counterexample_note": (
            "Counterexample file hashes may differ because proof engines can "
            "emit different valid traces; each repeat still requires every "
            "negative control to fail."
        ),
    }
    if output_path is None:
        return {**core, "semantic_hash": stable_hash(core)}
    return write_hashed_json(
        Path(output_path),
        core,
        exclusive=True,
    )


def prepare_m2_sources(
    config: ProjectConfig,
    study: Mapping[str, Any],
) -> dict[str, Any]:
    """Emit hash-bound, Slang-elaborated Verilog for the selected M2 points."""

    if study.get("status") != "completed":
        raise RealisticStudyError(
            "M2 source preparation requires a completed P2/M0/M1 repeat",
            code="study_incomplete",
        )
    root = Path(str((study.get("artifacts") or {}).get("root", ""))).resolve()
    output_path = root / "m2-sources.json"
    if output_path.is_file():
        cached = read_hashed_json(
            output_path,
            document_type="rtl-advisor.realistic-evidence-m2-sources",
            schema_version=1,
        )
        for configuration in cached.get("configurations") or []:
            for role in ("baseline", "candidate"):
                item = configuration[role]
                path = Path(str(item["path"]))
                if not path.is_file() or file_sha256(path) != item["sha256"]:
                    raise RealisticStudyError(
                        "cached M2 source changed",
                        code="stale_m2_source",
                    )
        return cached
    plugin = Path(SLANG_PLUGIN_PATH)
    if not plugin.is_file():
        raise RealisticStudyError(
            "M2 source preparation requires the pinned Yosys Slang plugin",
            code="missing_synthesis_frontend",
        )
    from rtl_advisor.mvp_measure import _read_command, _yosys_identity

    tool = _yosys_identity(config)
    configurations: list[dict[str, Any]] = []
    run_root = (
        root
        / "agent-v2"
        / "runs"
        / str(study["run_id"])
        / "candidates"
    )
    for item in (study.get("normalized") or {}).get("configurations") or []:
        configuration_id = str(item.get("configuration_id"))
        if configuration_id not in M2_CONFIGURATION_IDS:
            continue
        candidate_core = read_hashed_json(
            run_root / str(item["candidate_id"]) / "candidate-core.json",
            document_type="rtl-advisor.candidate",
            schema_version=1,
        )
        baseline, candidate = arbiter_designs_from_candidate(candidate_core)
        row: dict[str, Any] = {
            "configuration_id": configuration_id,
            "parameters": item["parameters"],
            "candidate_id": item["candidate_id"],
        }
        for role, design in (("baseline", baseline), ("candidate", candidate)):
            role_root = root / "m2" / "preelaborated" / configuration_id / role
            role_root.mkdir(parents=True, exist_ok=True)
            source_path = role_root / "rtl_advisor_arbiter_evidence_top.v"
            script_path = role_root / "elaborate.ys"
            log_path = role_root / "elaborate.log"
            script = "\n".join(
                (
                    f'plugin -i "{SLANG_PLUGIN_PATH}"',
                    _read_command(
                        design,
                        frontend="yosys-slang",
                        path_base=config.root,
                    ),
                    f"hierarchy -check -top {design.top}",
                    "proc",
                    "flatten",
                    "opt_clean",
                    f'write_verilog -noattr "{source_path}"',
                    "",
                )
            )
            script_path.write_text(script, encoding="utf-8")
            try:
                completed = run_command(
                    (config.tools.yosys, "-Q", "-s", str(script_path)),
                    timeout_seconds=config.tools.timeout_seconds,
                    cwd=config.root,
                )
            except ToolExecutionError as exc:
                raise RealisticStudyError(
                    str(exc),
                    code="m2_elaboration_failed",
                ) from exc
            transcript = "\n".join(
                value
                for value in (completed.stdout, completed.stderr)
                if value
            )
            log_path.write_text(
                transcript + ("\n" if transcript else ""),
                encoding="utf-8",
            )
            if completed.returncode != 0 or not source_path.is_file():
                raise RealisticStudyError(
                    f"M2 source elaboration failed for "
                    f"{configuration_id} {role}; see {log_path}",
                    code="m2_elaboration_failed",
                )
            row[role] = {
                "path": str(source_path),
                "sha256": file_sha256(source_path),
                "design_hash": design.design_hash,
                "script_path": str(script_path),
                "script_sha256": file_sha256(script_path),
                "log_path": str(log_path),
                "log_sha256": file_sha256(log_path),
            }
        configurations.append(row)
    core = {
        "schema_version": 1,
        "document_type": "rtl-advisor.realistic-evidence-m2-sources",
        "study_id": STUDY_ID,
        "repeat_id": study["repeat_id"],
        "source_kind": "slang_elaborated_rtlil_verilog",
        "frontend": {
            "kind": "yosys-slang",
            "plugin_path": SLANG_PLUGIN_PATH,
            "plugin_sha256": sha256_file(plugin),
        },
        "yosys": tool,
        "configurations": configurations,
        "limitations": [
            "Slang elaboration normalizes SystemVerilog for the pinned ORFS Yosys frontend.",
            "The M2 input is pre-elaborated RTL, not a technology-mapped M0/M1 netlist.",
        ],
    }
    return write_hashed_json(output_path, core, exclusive=True)
