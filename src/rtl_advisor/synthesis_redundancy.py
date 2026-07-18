from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, VariantSpec, load_manifest
from rtl_advisor.synthesis import (
    FLOW_VERSION as STANDARD_FLOW_VERSION,
    SynthesisMetrics,
    SynthesisResult,
    _parse_abc_metrics,
    _parse_stat_metrics,
    _yosys_quote,
    _yosys_version,
    compare_results,
)
from rtl_advisor.tools import ToolExecutionError, run_command, sha256_file


SCHEMA_VERSION = 1
FLOW_VERSION = "rtl-advisor-synthesis-redundancy-v1"
SELECTION_SEED = 20260718
CASES_PER_FAMILY = 3
VARIANT_IDS = ("v0", "v1", "v2", "v3")
MISSED_CATEGORIES = {
    "no_candidate_clears_threshold",
    "unsupported_family",
    "ranking_selected_ineligible",
    "qualified_only_ineligible",
}
NEUTRAL_PERCENT = 1.0


class SynthesisRedundancyError(RuntimeError):
    """Raised when the synthesis-redundancy evidence is incomplete or stale."""


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SynthesisRedundancyError(
            f"could not read {description} {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SynthesisRedundancyError(f"expected object in {description} {path}")
    return payload


def _relative(config: ProjectConfig, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(config.root.resolve()))
    except ValueError:
        return str(path.resolve())


def _selection_key(
    case: dict[str, Any],
    *,
    family: str,
    role: str,
    seed: int,
) -> str:
    return _stable_hash(
        {
            "case_id": case["case_id"],
            "family": family,
            "role": role,
            "seed": seed,
            "topology_signature": case["topology_signature"],
        }
    )


def select_case_records(
    cases: list[dict[str, Any]],
    *,
    seed: int = SELECTION_SEED,
) -> list[dict[str, Any]]:
    """Select three deterministic calibration cases per family."""
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_family[str(case["family"])].append(case)
    selected: list[dict[str, Any]] = []
    for family in sorted(by_family):
        available = list(by_family[family])
        roles = (
            (
                "covered_best",
                lambda case: case["classification"]["category"] == "covered_best",
            ),
            (
                "missed_improvement",
                lambda case: case["classification"]["category"]
                in MISSED_CATEGORIES,
            ),
            (
                "true_abstention",
                lambda case: case["classification"]["category"]
                == "true_abstention",
            ),
        )
        family_selection: list[dict[str, Any]] = []
        for role, predicate in roles:
            candidates = [case for case in available if predicate(case)]
            if not candidates:
                continue
            chosen = min(
                candidates,
                key=lambda case: _selection_key(
                    case, family=family, role=role, seed=seed
                ),
            )
            family_selection.append({**chosen, "selection_role": role})
            available.remove(chosen)
        while len(family_selection) < CASES_PER_FAMILY:
            if not available:
                raise SynthesisRedundancyError(
                    f"family {family} has fewer than {CASES_PER_FAMILY} cases"
                )
            role = f"deterministic_fill_{len(family_selection) + 1}"
            chosen = min(
                available,
                key=lambda case: _selection_key(
                    case, family=family, role=role, seed=seed
                ),
            )
            family_selection.append({**chosen, "selection_role": role})
            available.remove(chosen)
        selected.extend(family_selection[:CASES_PER_FAMILY])
    return selected


def _manifest_path(config: ProjectConfig, case_id: str) -> Path:
    for split in ("calibration-v21", "calibration-v2"):
        path = config.corpus_dir / split / case_id / "manifest.json"
        if path.is_file():
            return path
    raise SynthesisRedundancyError(f"manifest missing for selected case {case_id}")


def _verify_diagnostic(config: ProjectConfig) -> tuple[Path, dict[str, Any]]:
    path = config.artifacts_dir / "models/v22/failure-diagnostics.json"
    payload = _load_json(path, "V2.2 failure diagnostic")
    core = {
        key: value
        for key, value in payload.items()
        if key not in {"diagnostic_hash", "json_path", "markdown_path"}
    }
    if payload.get("diagnostic_hash") != _stable_hash(core):
        raise SynthesisRedundancyError("V2.2 diagnostic content hash mismatch")
    if payload.get("blind_labels_used") is not False:
        raise SynthesisRedundancyError(
            "synthesis-redundancy pilot must not use blind labels"
        )
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SynthesisRedundancyError("V2.2 diagnostic has no calibration cases")
    return path, payload


def _verify_standard_result(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant: VariantSpec,
) -> dict[str, Any]:
    root = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "synthesis"
        / variant.variant_id
    )
    result_path = root / "result.json"
    mapped_path = root / "mapped.v"
    result = _load_json(result_path, "standard synthesis result")
    if result.get("status") != "passed":
        raise SynthesisRedundancyError(
            f"standard synthesis did not pass for {manifest.case_id}/{variant.variant_id}"
        )
    if result.get("source_sha256") != variant.sha256:
        raise SynthesisRedundancyError(
            f"stale standard synthesis source for {manifest.case_id}/{variant.variant_id}"
        )
    if result.get("provenance", {}).get("flow_version") != STANDARD_FLOW_VERSION:
        raise SynthesisRedundancyError(
            f"unexpected standard synthesis flow for {manifest.case_id}/{variant.variant_id}"
        )
    if not mapped_path.is_file():
        raise SynthesisRedundancyError(
            f"standard mapped netlist missing for {manifest.case_id}/{variant.variant_id}"
        )
    return {
        "result_path": _relative(config, result_path),
        "result_sha256": sha256_file(result_path),
        "mapped_path": _relative(config, mapped_path),
        "mapped_sha256": sha256_file(mapped_path),
    }


def _verify_equivalence_proof(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant: VariantSpec,
) -> dict[str, Any]:
    proof_path = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "equivalence"
        / f"{variant.variant_id}.json"
    )
    proof = _load_json(proof_path, "equivalence proof")
    if proof.get("status") != "equivalent" or proof.get("expectation_met") is not True:
        raise SynthesisRedundancyError(
            f"equivalence proof failed for {manifest.case_id}/{variant.variant_id}"
        )
    if proof.get("baseline_sha256") != manifest.baseline.sha256:
        raise SynthesisRedundancyError(
            f"stale equivalence baseline for {manifest.case_id}/{variant.variant_id}"
        )
    if proof.get("candidate_sha256") != variant.sha256:
        raise SynthesisRedundancyError(
            f"stale equivalence candidate for {manifest.case_id}/{variant.variant_id}"
        )
    return {
        "path": _relative(config, proof_path),
        "sha256": sha256_file(proof_path),
    }


def build_redundancy_plan(config: ProjectConfig) -> dict[str, Any]:
    diagnostic_path, diagnostic = _verify_diagnostic(config)
    selected = select_case_records(diagnostic["cases"])
    if len(selected) != 27:
        raise SynthesisRedundancyError(
            f"expected 27 selected cases, found {len(selected)}"
        )
    plan_cases: list[dict[str, Any]] = []
    for case in selected:
        manifest_path = _manifest_path(config, str(case["case_id"]))
        manifest = load_manifest(manifest_path)
        if manifest.family != case["family"]:
            raise SynthesisRedundancyError(
                f"family mismatch for selected case {manifest.case_id}"
            )
        evidence: dict[str, Any] = {}
        for variant_id in VARIANT_IDS:
            variant = manifest.variant(variant_id)
            entry: dict[str, Any] = {
                "source_sha256": variant.sha256,
                "standard_synthesis": _verify_standard_result(
                    config, manifest, variant
                ),
            }
            if variant_id != manifest.baseline_id:
                entry["equivalence"] = _verify_equivalence_proof(
                    config, manifest, variant
                )
            evidence[variant_id] = entry
        plan_cases.append(
            {
                "case_id": manifest.case_id,
                "family": manifest.family,
                "topology_signature": case["topology_signature"],
                "diagnostic_category": case["classification"]["category"],
                "selection_role": case["selection_role"],
                "manifest_path": _relative(config, manifest_path),
                "manifest_sha256": sha256_file(manifest_path),
                "variant_evidence": evidence,
            }
        )
    core = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "selection_seed": SELECTION_SEED,
        "cases_per_family": CASES_PER_FAMILY,
        "case_count": len(plan_cases),
        "run_count": len(plan_cases) * len(VARIANT_IDS),
        "candidate_count": len(plan_cases) * (len(VARIANT_IDS) - 1),
        "source": {
            "diagnostic_path": _relative(config, diagnostic_path),
            "diagnostic_sha256": sha256_file(diagnostic_path),
            "diagnostic_hash": diagnostic["diagnostic_hash"],
            "blind_labels_used": False,
        },
        "thresholds": {
            "delay_improvement_percent": 3.0,
            "area_guardrail_percent": -10.0,
            "area_improvement_percent": 5.0,
            "delay_guardrail_percent": -2.0,
            "neutral_absolute_percent": NEUTRAL_PERCENT,
        },
        "cases": plan_cases,
    }
    return {**core, "plan_hash": _stable_hash(core)}


def create_redundancy_plan(config: ProjectConfig) -> Path:
    path = config.artifacts_dir / "synthesis-redundancy/v1/plan.json"
    current = build_redundancy_plan(config)
    if path.is_file():
        frozen = _load_json(path, "frozen synthesis-redundancy plan")
        if frozen.get("plan_hash") != current["plan_hash"] or frozen != current:
            raise SynthesisRedundancyError(
                "frozen synthesis-redundancy plan no longer matches its inputs"
            )
        return path
    _write_json(path, current)
    return path


def _aggressive_script(
    *,
    source: Path,
    top: str,
    liberty: Path,
    constraints: Path,
    stat_json: Path,
    netlist: Path,
) -> str:
    return "\n".join(
        (
            f"read_liberty -lib {_yosys_quote(liberty)}",
            f"read_verilog -sv {_yosys_quote(source)}",
            f"synth -top {top} -flatten -noabc -run begin:fine",
            "share -aggressive",
            "opt -full",
            "clean",
            f"synth -top {top} -flatten -noabc -run fine:check",
            f"dfflibmap -liberty {_yosys_quote(liberty)}",
            (
                f"abc -liberty {_yosys_quote(liberty)} "
                f"-constr {_yosys_quote(constraints)}"
            ),
            "clean",
            "check -assert",
            (
                f"tee -o {_yosys_quote(stat_json)} stat -top {top} "
                f"-liberty {_yosys_quote(liberty)} -json"
            ),
            f"write_verilog -noattr -noexpr {_yosys_quote(netlist)}",
            "",
        )
    )


def _aggressive_cache_key(
    *,
    flow_version: str = FLOW_VERSION,
    plan_hash: str,
    variant: VariantSpec,
    yosys_version: str,
    liberty_sha256: str,
    driving_cell: str,
    output_load_ff: float,
) -> str:
    return _stable_hash(
        {
            "flow_version": flow_version,
            "plan_hash": plan_hash,
            "source_sha256": variant.sha256,
            "top": variant.wrapper_top,
            "yosys_version": yosys_version,
            "liberty_sha256": liberty_sha256,
            "driving_cell": driving_cell,
            "output_load_ff": output_load_ff,
        }
    )


def _run_aggressive_variant(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant: VariantSpec,
    *,
    plan_hash: str,
    yosys_version: str,
    liberty_sha256: str,
    output_root: Path | None = None,
    flow_version: str = FLOW_VERSION,
) -> SynthesisResult:
    cache_key = _aggressive_cache_key(
        flow_version=flow_version,
        plan_hash=plan_hash,
        variant=variant,
        yosys_version=yosys_version,
        liberty_sha256=liberty_sha256,
        driving_cell=config.synthesis.driving_cell,
        output_load_ff=config.synthesis.output_load_ff,
    )
    runs_root = output_root or (
        config.artifacts_dir / "synthesis-redundancy/v1/runs"
    )
    output_dir = runs_root / manifest.case_id / variant.variant_id
    result_path = output_dir / "result.json"
    log_path = output_dir / "synthesis.log"
    script_path = output_dir / "synthesis.ys"
    constraints_path = output_dir / "abc.constr"
    stat_path = output_dir / "stat.json"
    netlist_path = output_dir / "mapped.v"
    if result_path.is_file():
        try:
            cached = SynthesisResult.from_dict(
                json.loads(result_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            cached = None
        if (
            cached is not None
            and cached.status == "passed"
            and cached.cache_key == cache_key
            and log_path.is_file()
            and stat_path.is_file()
            and netlist_path.is_file()
        ):
            return replace(cached, cached=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    constraints_path.write_text(
        f"set_driving_cell {config.synthesis.driving_cell}\n"
        f"set_load {config.synthesis.output_load_ff}\n",
        encoding="utf-8",
    )
    source_path = manifest.variant_path(variant)
    script = _aggressive_script(
        source=source_path,
        top=variant.wrapper_top,
        liberty=config.liberty.path,
        constraints=constraints_path,
        stat_json=stat_path,
        netlist=netlist_path,
    )
    script_path.write_text(script, encoding="utf-8")
    command = (config.tools.yosys, "-Q", "-s", str(script_path))
    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=manifest.root,
        )
    except ToolExecutionError as exc:
        log_path.write_text(f"{exc}\n", encoding="utf-8")
        raise SynthesisRedundancyError(str(exc)) from exc
    combined = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    )
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    if completed.returncode != 0:
        raise SynthesisRedundancyError(
            f"aggressive synthesis failed for {manifest.case_id}/{variant.variant_id}; "
            f"see {log_path}"
        )
    try:
        abc_gate_count, abc_area, critical_delay_ps = _parse_abc_metrics(combined)
        area_total, area_sequential, cell_count, raw_cell_count, cells_by_type = (
            _parse_stat_metrics(stat_path, variant.wrapper_top)
        )
    except Exception as exc:
        raise SynthesisRedundancyError(str(exc)) from exc
    metrics = SynthesisMetrics(
        critical_delay_ps=critical_delay_ps,
        area_total=area_total,
        area_combinational=round(area_total - area_sequential, 6),
        area_sequential=area_sequential,
        abc_area_combinational=abc_area,
        cell_count=cell_count,
        raw_cell_count=raw_cell_count,
        abc_gate_count=abc_gate_count,
        cells_by_type=cells_by_type,
    )
    result = SynthesisResult(
        case_id=manifest.case_id,
        variant_id=variant.variant_id,
        status="passed",
        backend="yosys-aggressive-sharing-abc-liberty",
        cache_key=cache_key,
        cached=False,
        source_path=str(source_path),
        source_sha256=variant.sha256,
        top=variant.wrapper_top,
        metrics=metrics,
        constraints={
            "driving_cell": config.synthesis.driving_cell,
            "output_load_ff": config.synthesis.output_load_ff,
        },
        provenance={
            "flow_version": flow_version,
            "plan_hash": plan_hash,
            "yosys_version": yosys_version,
            "liberty_name": config.liberty.name,
            "liberty_path": str(config.liberty.path),
            "liberty_sha256": liberty_sha256,
            "liberty_source_commit": config.liberty.source_commit,
            "command": list(command),
            "script_path": str(script_path),
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "constraints_path": str(constraints_path),
            "log_path": str(log_path),
            "stat_path": str(stat_path),
            "netlist_path": str(netlist_path),
        },
    )
    _write_json(result_path, result.to_dict())
    return result


def _load_frozen_plan(config: ProjectConfig) -> dict[str, Any]:
    plan_path = create_redundancy_plan(config)
    plan = _load_json(plan_path, "synthesis-redundancy plan")
    core = {key: value for key, value in plan.items() if key != "plan_hash"}
    if plan.get("plan_hash") != _stable_hash(core):
        raise SynthesisRedundancyError("synthesis-redundancy plan hash mismatch")
    return plan


def run_redundancy_benchmark(
    config: ProjectConfig,
    *,
    workers: int = 4,
) -> dict[str, Any]:
    if not 1 <= workers <= 8:
        raise SynthesisRedundancyError("workers must be between 1 and 8")
    plan = _load_frozen_plan(config)
    liberty_sha256 = sha256_file(config.liberty.path)
    if liberty_sha256 != config.liberty.sha256:
        raise SynthesisRedundancyError("configured Liberty checksum mismatch")
    yosys_version = _yosys_version(config)
    jobs: list[tuple[CaseManifest, VariantSpec]] = []
    for case in plan["cases"]:
        manifest = load_manifest(config.root / case["manifest_path"])
        for variant_id in VARIANT_IDS:
            jobs.append((manifest, manifest.variant(variant_id)))
    results: list[SynthesisResult] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _run_aggressive_variant,
                config,
                manifest,
                variant,
                plan_hash=plan["plan_hash"],
                yosys_version=yosys_version,
                liberty_sha256=liberty_sha256,
            ): (manifest.case_id, variant.variant_id)
            for manifest, variant in jobs
        }
        for future in as_completed(futures):
            case_id, variant_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append(
                    {
                        "case_id": case_id,
                        "variant_id": variant_id,
                        "error": str(exc),
                    }
                )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "plan_hash": plan["plan_hash"],
        "status": "passed" if not failures and len(results) == len(jobs) else "failed",
        "run_count": len(jobs),
        "passed_count": len(results),
        "failed_count": len(failures),
        "fresh_count": sum(not result.cached for result in results),
        "cached_count": sum(result.cached for result in results),
        "failures": sorted(failures, key=lambda item: (item["case_id"], item["variant_id"])),
    }
    summary_path = config.artifacts_dir / "synthesis-redundancy/v1/run-summary.json"
    summary["summary_path"] = str(summary_path.resolve())
    _write_json(summary_path, summary)
    if summary["status"] != "passed":
        raise SynthesisRedundancyError(
            f"synthesis-redundancy run failed for {len(failures)} variants; "
            f"see {summary_path}"
        )
    report = build_redundancy_report(config)
    return {**summary, "report_path": report["markdown_path"]}


def _load_synthesis_result(path: Path, description: str) -> SynthesisResult:
    payload = _load_json(path, description)
    try:
        result = SynthesisResult.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise SynthesisRedundancyError(f"invalid {description} {path}: {exc}") from exc
    if result.status != "passed":
        raise SynthesisRedundancyError(f"{description} did not pass: {path}")
    return result


def _is_useful(comparison: dict[str, Any]) -> bool:
    delay = float(comparison["critical_delay_ps"]["improvement_percent"])
    area = float(comparison["area_total"]["improvement_percent"])
    return (delay >= 3.0 and area >= -10.0) or (area >= 5.0 and delay >= -2.0)


def _is_neutral(comparison: dict[str, Any]) -> bool:
    return all(
        abs(float(comparison[metric]["improvement_percent"])) <= NEUTRAL_PERCENT
        for metric in ("critical_delay_ps", "area_total", "cell_count")
    )


def _cell_signature(result: SynthesisResult) -> str:
    cells = {
        name: count
        for name, count in result.metrics.cells_by_type.items()
        if not name.startswith("$")
    }
    return _stable_hash(cells)


def classify_candidate(
    standard_comparison: dict[str, Any],
    aggressive_comparison: dict[str, Any],
    *,
    aggressive_cell_signatures_equal: bool,
) -> str:
    standard_useful = _is_useful(standard_comparison)
    aggressive_useful = _is_useful(aggressive_comparison)
    if standard_useful:
        return (
            "survives_aggressive_synthesis"
            if aggressive_useful
            else "absorbed_by_aggressive_synthesis"
        )
    if aggressive_useful:
        return "representation_sensitive_tradeoff"
    if _is_neutral(aggressive_comparison) and aggressive_cell_signatures_equal:
        return "synthesis_absorbed"
    if _is_neutral(aggressive_comparison):
        return "no_material_qor_change"
    return "representation_sensitive_tradeoff"


def _format_percent(value: float) -> str:
    return f"{value:.1%}"


def build_redundancy_report(config: ProjectConfig) -> dict[str, Any]:
    plan = _load_frozen_plan(config)
    records: list[dict[str, Any]] = []
    case_summaries: list[dict[str, Any]] = []
    proof_count = 0
    for case in plan["cases"]:
        case_id = case["case_id"]
        standard: dict[str, SynthesisResult] = {}
        aggressive: dict[str, SynthesisResult] = {}
        for variant_id in VARIANT_IDS:
            standard_path = (
                config.artifacts_dir
                / "cases"
                / case_id
                / "synthesis"
                / variant_id
                / "result.json"
            )
            aggressive_path = (
                config.artifacts_dir
                / "synthesis-redundancy/v1/runs"
                / case_id
                / variant_id
                / "result.json"
            )
            standard[variant_id] = _load_synthesis_result(
                standard_path, "standard synthesis result"
            )
            aggressive[variant_id] = _load_synthesis_result(
                aggressive_path, "aggressive synthesis result"
            )
            if aggressive[variant_id].provenance.get("plan_hash") != plan["plan_hash"]:
                raise SynthesisRedundancyError(
                    f"aggressive result belongs to another plan: {case_id}/{variant_id}"
                )
        case_standard_useful = False
        case_aggressive_useful = False
        for variant_id in VARIANT_IDS[1:]:
            proof_count += 1
            standard_comparison = compare_results(
                standard["v0"], standard[variant_id]
            )
            aggressive_comparison = compare_results(
                aggressive["v0"], aggressive[variant_id]
            )
            standard_useful = _is_useful(standard_comparison)
            aggressive_useful = _is_useful(aggressive_comparison)
            case_standard_useful |= standard_useful
            case_aggressive_useful |= aggressive_useful
            signatures_equal = _cell_signature(aggressive["v0"]) == _cell_signature(
                aggressive[variant_id]
            )
            classification = classify_candidate(
                standard_comparison,
                aggressive_comparison,
                aggressive_cell_signatures_equal=signatures_equal,
            )
            records.append(
                {
                    "case_id": case_id,
                    "family": case["family"],
                    "diagnostic_category": case["diagnostic_category"],
                    "selection_role": case["selection_role"],
                    "variant_id": variant_id,
                    "equivalence_proved": True,
                    "standard_useful": standard_useful,
                    "aggressive_useful": aggressive_useful,
                    "aggressive_cell_signatures_equal": signatures_equal,
                    "classification": classification,
                    "standard_comparison": standard_comparison,
                    "aggressive_comparison": aggressive_comparison,
                }
            )
        case_summaries.append(
            {
                "case_id": case_id,
                "family": case["family"],
                "diagnostic_category": case["diagnostic_category"],
                "standard_has_useful_candidate": case_standard_useful,
                "aggressive_has_useful_candidate": case_aggressive_useful,
                "incremental_rtl_value": case_aggressive_useful,
            }
        )
    classifications = Counter(record["classification"] for record in records)
    standard_useful_records = [record for record in records if record["standard_useful"]]
    aggressive_useful_records = [record for record in records if record["aggressive_useful"]]
    retained_useful_records = [
        record
        for record in records
        if record["standard_useful"] and record["aggressive_useful"]
    ]
    aggressive_only_useful_records = [
        record
        for record in records
        if not record["standard_useful"] and record["aggressive_useful"]
    ]
    standard_cases = [
        case for case in case_summaries if case["standard_has_useful_candidate"]
    ]
    aggressive_cases = [
        case for case in case_summaries if case["aggressive_has_useful_candidate"]
    ]
    retained_useful_cases = [
        case
        for case in case_summaries
        if case["standard_has_useful_candidate"]
        and case["aggressive_has_useful_candidate"]
    ]
    aggressive_only_useful_cases = [
        case
        for case in case_summaries
        if not case["standard_has_useful_candidate"]
        and case["aggressive_has_useful_candidate"]
    ]
    families: dict[str, dict[str, Any]] = {}
    for family in sorted({case["family"] for case in case_summaries}):
        family_cases = [case for case in case_summaries if case["family"] == family]
        family_records = [record for record in records if record["family"] == family]
        family_standard_useful = [
            record for record in family_records if record["standard_useful"]
        ]
        family_aggressive_useful = [
            record for record in family_records if record["aggressive_useful"]
        ]
        family_retained_useful = [
            record
            for record in family_records
            if record["standard_useful"] and record["aggressive_useful"]
        ]
        families[family] = {
            "case_count": len(family_cases),
            "candidate_count": len(family_records),
            "standard_useful_case_count": sum(
                case["standard_has_useful_candidate"] for case in family_cases
            ),
            "aggressive_useful_case_count": sum(
                case["aggressive_has_useful_candidate"] for case in family_cases
            ),
            "standard_useful_candidate_count": len(family_standard_useful),
            "aggressive_useful_candidate_count": len(family_aggressive_useful),
            "retained_standard_useful_candidate_count": len(
                family_retained_useful
            ),
            "candidate_survival_rate": (
                len(family_retained_useful) / len(family_standard_useful)
                if family_standard_useful
                else None
            ),
        }
    standard_candidate_count = len(standard_useful_records)
    aggressive_candidate_count = len(aggressive_useful_records)
    candidate_survival_rate = (
        len(retained_useful_records) / standard_candidate_count
        if standard_candidate_count
        else None
    )
    decision = (
        "continue_with_surviving_families"
        if aggressive_candidate_count
        else "pause_rewrite_prediction_and_refocus"
    )
    core = {
        "schema_version": SCHEMA_VERSION,
        "flow_version": FLOW_VERSION,
        "plan_hash": plan["plan_hash"],
        "blind_labels_used": False,
        "status": "passed",
        "case_count": len(case_summaries),
        "candidate_count": len(records),
        "equivalence_proof_count": proof_count,
        "standard_useful_case_count": len(standard_cases),
        "aggressive_useful_case_count": len(aggressive_cases),
        "retained_standard_useful_case_count": len(retained_useful_cases),
        "aggressive_only_useful_case_count": len(aggressive_only_useful_cases),
        "standard_useful_candidate_count": standard_candidate_count,
        "aggressive_useful_candidate_count": aggressive_candidate_count,
        "retained_standard_useful_candidate_count": len(retained_useful_records),
        "aggressive_only_useful_candidate_count": len(
            aggressive_only_useful_records
        ),
        "candidate_survival_rate": candidate_survival_rate,
        "classification_counts": dict(sorted(classifications.items())),
        "decision": decision,
        "families": families,
        "cases": case_summaries,
        "candidates": records,
    }
    report = {**core, "report_hash": _stable_hash(core)}
    root = config.artifacts_dir / "synthesis-redundancy/v1"
    json_path = root / "report.json"
    markdown_path = root / "report.md"
    report["json_path"] = str(json_path.resolve())
    report["markdown_path"] = str(markdown_path.resolve())
    _write_json(json_path, report)
    lines = [
        "# Synthesis Redundancy Pilot V1",
        "",
        "> Generated calibration RTL only. No company RTL or held-out labels were used.",
        "",
        "## Result",
        "",
        f"- Exact RTL equivalence proofs checked: {proof_count}/{len(records)}",
        f"- Cases with a useful candidate under the standard flow: {len(standard_cases)}/{len(case_summaries)}",
        f"- Cases with a useful candidate after stronger synthesis: {len(aggressive_cases)}/{len(case_summaries)}",
        f"- Useful candidates under the standard flow: {standard_candidate_count}/{len(records)}",
        f"- Useful candidates after stronger synthesis: {aggressive_candidate_count}/{len(records)}",
        f"- Standard-flow useful candidates that remain useful: {len(retained_useful_records)}/{standard_candidate_count}",
        f"- Candidates useful only under the stronger recipe: {len(aggressive_only_useful_records)}",
        (
            "- Candidate survival rate: n/a"
            if candidate_survival_rate is None
            else f"- Candidate survival rate: {_format_percent(candidate_survival_rate)}"
        ),
        "",
        "A candidate counts as useful when it improves delay by at least 3% without losing more than 10% area, or improves area by at least 5% without losing more than 2% delay.",
        "",
        "## Candidate outcomes",
        "",
        "| Outcome | Candidates | Meaning |",
        "|---|---:|---|",
        f"| Survives stronger synthesis | {classifications['survives_aggressive_synthesis']} | The RTL rewrite still changes the implementation enough to meet the useful-change rule. |",
        f"| Removed by stronger synthesis | {classifications['absorbed_by_aggressive_synthesis']} | It looked useful under the standard recipe, but the stronger recipe removed the benefit. |",
        f"| Effectively unchanged synthesis outcome | {classifications['synthesis_absorbed']} | PPA is effectively unchanged and the mapped cell mix matches; this is a structural hint, not mapped-netlist equivalence. |",
        f"| No meaningful PPA change | {classifications['no_material_qor_change']} | Delay, area, and cell count are all within 1%. |",
        f"| Result depends on synthesis recipe | {classifications['representation_sensitive_tradeoff']} | The PPA conclusion changes with the recipe, or neither recipe shows a balanced useful change. |",
        "",
        "## Family results",
        "",
        "| Family | Standard useful cases | Stronger-synthesis useful cases | Candidate survival |",
        "|---|---:|---:|---:|",
    ]
    for family, summary in families.items():
        survival = summary["candidate_survival_rate"]
        lines.append(
            f"| {family} | {summary['standard_useful_case_count']}/3 | "
            f"{summary['aggressive_useful_case_count']}/3 | "
            f"{'n/a' if survival is None else _format_percent(survival)} |"
        )
    lines.extend(
        (
            "",
            "## Candidates useful after stronger synthesis",
            "",
        )
    )
    if aggressive_useful_records:
        lines.extend(
            (
                "| Case | Family | Variant | Evidence status | Delay improvement | Area improvement |",
                "|---|---|---|---|---:|---:|",
            )
        )
        for record in aggressive_useful_records:
            comparison = record["aggressive_comparison"]
            evidence_status = (
                "Retained from standard flow"
                if record["standard_useful"]
                else "Useful only under stronger recipe"
            )
            lines.append(
                f"| {record['case_id']} | {record['family']} | {record['variant_id']} | "
                f"{evidence_status} | "
                f"{comparison['critical_delay_ps']['improvement_percent']:.2f}% | "
                f"{comparison['area_total']['improvement_percent']:.2f}% |"
            )
    else:
        lines.append("No candidate met the useful-change rule after stronger synthesis.")
    lines.extend(
        (
            "",
            "## Product implication",
            "",
            (
                "Continue only with rewrite families that survive the stronger flow, then repeat those families with an approved commercial synthesis tool on generated RTL."
                if aggressive_candidate_count
                else "Pause the rewrite-prediction track. Focus the product on pre-synthesis issues the synthesis tool cannot silently resolve, such as constraints, architecture, bit growth, reset or clocking structure, and review-time explanation."
            ),
            "",
            "The equivalence gate proves each RTL candidate has the same behavior as its baseline. It does not claim the two mapped netlists are structurally identical; the synthesis comparison answers whether the rewrite still provides implementation value.",
            "",
            f"Plan hash: `{plan['plan_hash']}`",
            "",
            f"Report hash: `{report['report_hash']}`",
            "",
        )
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report
