#!/usr/bin/env python3
"""Validate, packetize, and score the frozen RTL Advisor plugin A/B/C test."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT = ROOT / "experiments" / "plugin-abc-v1"
HEX64 = frozenset("0123456789abcdef")
ARMS = {"A", "B", "C"}
DECISIONS = {"recommend_change", "no_change", "unsupported", "inconclusive"}
FINAL_STATES = {
    "measured_improvement",
    "synthesis_handles",
    "flow_dependent",
    "regression",
    "unproven",
    "not_applicable",
}


class FrameworkError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrameworkError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FrameworkError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX64


def _resolve(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise FrameworkError(f"path must be repository-relative: {relative!r}")
    return ROOT / path


def validate_manifest(experiment: Path = DEFAULT_EXPERIMENT) -> dict[str, Any]:
    manifest_path = experiment / "manifest.json"
    manifest = _load(manifest_path)
    if manifest.get("schema") != "rtl-advisor-plugin-abc-manifest-v1":
        raise FrameworkError("unexpected manifest schema")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != 24:
        raise FrameworkError("manifest must freeze exactly 24 cases")
    identifiers: set[str] = set()
    kinds: dict[str, int] = {"generated": 0, "open": 0}
    for case in cases:
        if not isinstance(case, dict):
            raise FrameworkError("every manifest case must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in identifiers:
            raise FrameworkError(f"duplicate or invalid case_id: {case_id!r}")
        identifiers.add(case_id)
        kind = case.get("source_kind")
        if kind not in kinds:
            raise FrameworkError(f"{case_id}: invalid source_kind")
        kinds[kind] += 1
        expected = case.get("source_sha256")
        if not _is_sha256(expected):
            raise FrameworkError(f"{case_id}: invalid source_sha256")
        sources = case.get("sources")
        if not isinstance(sources, list) or not sources:
            raise FrameworkError(f"{case_id}: sources must be non-empty")
        primary = _resolve(str(sources[-1]))
        if not primary.is_file():
            raise FrameworkError(f"{case_id}: missing source {sources[-1]}")
        actual = _sha256(primary)
        if actual != expected:
            raise FrameworkError(
                f"{case_id}: source hash mismatch; expected {expected}, got {actual}"
            )
        if kind == "open" and not isinstance(case.get("reference_id"), str):
            raise FrameworkError(f"{case_id}: open case requires reference_id")
    if kinds != {"generated": 12, "open": 12}:
        raise FrameworkError(f"expected 12 generated and 12 open cases; got {kinds}")

    tranche = manifest.get("open_tranche")
    if not isinstance(tranche, dict):
        raise FrameworkError("open_tranche must be an object")
    for key in ("lock", "qualification_plan", "behavior_plan"):
        path = _resolve(str(tranche.get(key, "")))
        if not path.is_file():
            raise FrameworkError(f"missing open-tranche input: {key}")
    lock = _load(_resolve(str(tranche["lock"])))
    if lock.get("semantic_hash") != tranche.get("tranche_semantic_hash"):
        raise FrameworkError("open-tranche semantic hash mismatch")

    oracle = _load(experiment / "oracle.json")
    oracle_cases = oracle.get("cases")
    if not isinstance(oracle_cases, list):
        raise FrameworkError("oracle cases must be an array")
    oracle_ids = [item.get("case_id") for item in oracle_cases if isinstance(item, dict)]
    if set(oracle_ids) != identifiers or len(oracle_ids) != len(identifiers):
        raise FrameworkError("oracle and manifest case sets must match exactly")
    lock = _load(experiment / "evidence.lock.json")
    if lock.get("schema") != "rtl-advisor-plugin-abc-lock-v1":
        raise FrameworkError("unexpected experiment lock schema")
    locked_files = lock.get("files")
    if not isinstance(locked_files, list) or not locked_files:
        raise FrameworkError("experiment lock must contain files")
    for item in locked_files:
        if not isinstance(item, dict) or not _is_sha256(item.get("sha256")):
            raise FrameworkError("invalid experiment lock entry")
        locked_path = _resolve(str(item.get("path", "")))
        if not locked_path.is_file():
            raise FrameworkError(f"missing locked file: {item.get('path')}")
        actual = _sha256(locked_path)
        if actual != item["sha256"]:
            raise FrameworkError(
                f"locked file changed: {item['path']}; expected {item['sha256']}, got {actual}"
            )
    return {
        "ok": True,
        "experiment_id": manifest.get("experiment_id"),
        "case_count": len(cases),
        "source_kinds": kinds,
        "manifest_sha256": _sha256(manifest_path),
        "oracle_sha256": _sha256(experiment / "oracle.json"),
    }


def make_packet(
    arm: str,
    repetition: int,
    experiment: Path = DEFAULT_EXPERIMENT,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    validation = validate_manifest(experiment)
    if arm not in ARMS:
        raise FrameworkError(f"unknown arm {arm!r}")
    if repetition not in {1, 2, 3}:
        raise FrameworkError("repetition must be 1, 2, or 3")
    manifest = _load(experiment / "manifest.json")
    selected_cases = manifest["cases"]
    if case_ids:
        if repetition != 3:
            raise FrameworkError("case filtering is reserved for repetition 3")
        requested = set(case_ids)
        if len(requested) != len(case_ids):
            raise FrameworkError("case filter contains duplicates")
        known = {item["case_id"] for item in manifest["cases"]}
        missing = requested - known
        if missing:
            raise FrameworkError(f"unknown filtered cases: {sorted(missing)}")
        selected_cases = [
            item for item in manifest["cases"] if item["case_id"] in requested
        ]
    access = {
        "A": "Use ordinary reasoning and EDA tools. Do not inspect or invoke RTL Advisor skills, source code, artifacts, or other arm outputs.",
        "B": "Use the installed rtl-advisor:analyze-rtl skill and released CLI exactly. Do not invent rewrites outside its declared support.",
        "C": "Reason broadly and use ordinary tools, but delegate every released-family candidate, proof, and measurement to RTL Advisor. Never override its formal or synthesis result.",
    }[arm]
    return {
        "schema": "rtl-advisor-plugin-abc-packet-v1",
        "experiment_id": manifest["experiment_id"],
        "arm": arm,
        "repetition": repetition,
        "model": manifest["agent_contract"]["model"],
        "reasoning_effort": manifest["agent_contract"]["reasoning_effort"],
        "manifest_sha256": validation["manifest_sha256"],
        "objective": manifest["objective"],
        "access_rule": access,
        "isolation_rules": [
            "Do not read experiments/plugin-abc-v1/oracle.json.",
            "Do not read any experiments/plugin-abc-v1/runs/arm-* directory except your assigned arm directory.",
            f"For repetition {repetition}, do not read any prior repetition; write only under runs/arm-{arm.lower()}/r{repetition}.",
            "Do not mutate baseline RTL; create isolated candidates under your assigned run directory.",
            "Do not recommend any unproven candidate.",
            "Report every case in manifest order, including unsupported and no-change outcomes.",
        ],
        "max_candidates_per_case": manifest["agent_contract"]["max_candidates_per_case"],
        "case_time_budget_minutes": manifest["agent_contract"]["case_time_budget_minutes"],
        "result_schema": "schemas/rtl-advisor-plugin-abc-arm-result-v1.schema.json",
        "case_selection": "decision_disagreements_only" if case_ids else "full_manifest",
        "cases": selected_cases,
    }


def validate_arm_result(path: Path, experiment: Path = DEFAULT_EXPERIMENT) -> dict[str, Any]:
    validation = validate_manifest(experiment)
    manifest = _load(experiment / "manifest.json")
    result = _load(path)
    if result.get("schema") != "rtl-advisor-plugin-abc-arm-result-v1":
        raise FrameworkError(f"{path}: unexpected result schema")
    if result.get("experiment_id") != manifest.get("experiment_id"):
        raise FrameworkError(f"{path}: experiment_id mismatch")
    if result.get("arm") not in ARMS:
        raise FrameworkError(f"{path}: invalid arm")
    if result.get("model") != "gpt-5.6-sol" or result.get("reasoning_effort") != "xhigh":
        raise FrameworkError(f"{path}: model contract mismatch")
    if result.get("manifest_sha256") != validation["manifest_sha256"]:
        raise FrameworkError(f"{path}: stale manifest hash")
    repetition = result.get("repetition")
    all_cases = {item["case_id"]: item for item in manifest["cases"]}
    if repetition == 3:
        packet_path = (
            experiment
            / "packets"
            / f"arm-{str(result.get('arm')).lower()}-r3.json"
        )
        packet = _load(packet_path)
        expected_ids = [item["case_id"] for item in packet.get("cases", [])]
    else:
        expected_ids = [item["case_id"] for item in manifest["cases"]]
    cases = result.get("cases")
    if not isinstance(cases, list) or len(cases) != len(expected_ids):
        raise FrameworkError(
            f"{path}: result must contain exactly {len(expected_ids)} cases"
        )
    expected = {case_id: all_cases[case_id] for case_id in expected_ids}
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise FrameworkError(f"{path}: case result must be an object")
        case_id = item.get("case_id")
        if case_id not in expected or case_id in seen:
            raise FrameworkError(f"{path}: unexpected or duplicate case {case_id!r}")
        seen.add(case_id)
        if item.get("source_sha256") != expected[case_id]["source_sha256"]:
            raise FrameworkError(f"{path}: {case_id} source hash mismatch")
        if item.get("decision") not in DECISIONS:
            raise FrameworkError(f"{path}: {case_id} invalid decision")
        candidates = item.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 3:
            raise FrameworkError(f"{path}: {case_id} invalid candidates")
        if item["decision"] == "recommend_change" and not candidates:
            raise FrameworkError(f"{path}: {case_id} recommends a missing candidate")
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("final_state") not in FINAL_STATES:
                raise FrameworkError(f"{path}: {case_id} invalid candidate record")
    if [item.get("case_id") for item in cases] != expected_ids:
        raise FrameworkError(f"{path}: case order does not match assigned packet")
    return {
        "ok": True,
        "arm": result["arm"],
        "repetition": result["repetition"],
        "case_count": len(expected_ids),
    }


def _candidate_path(raw: str, *, arm: str, repetition: int) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        relative = candidate.resolve().relative_to(ROOT.resolve())
    except ValueError as exc:
        raise FrameworkError(f"candidate path is outside the repository: {raw}") from exc
    allowed_run = Path("experiments") / "plugin-abc-v1" / "runs" / f"arm-{arm.lower()}" / f"r{repetition}"
    if not (relative.is_relative_to(allowed_run) or relative.is_relative_to("artifacts")):
        raise FrameworkError(f"candidate path is outside the assigned arm/artifact store: {raw}")
    return candidate


def _yosys_quote(path: Path) -> str:
    raw = str(path.resolve())
    if any(character in raw for character in ('"', "\n", "\r", "\x00")):
        raise FrameworkError(f"unsafe Yosys path: {path}")
    return f'"{raw}"'


def _rerun_generated_p1_formal(
    baseline: Path,
    candidate: Path,
    top: str,
    output: Path,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    script = output.with_suffix(".ys")
    candidate_text = candidate.read_text(encoding="utf-8")
    module_match = re.search(r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)", candidate_text)
    if module_match is None:
        raise FrameworkError(f"candidate contains no module declaration: {candidate}")
    candidate_top = module_match.group(1)
    script.write_text(
        "\n".join((
            f"read_verilog -sv {_yosys_quote(baseline)}",
            f"prep -top {top}",
            "flatten",
            "design -stash gold",
            f"read_verilog -sv {_yosys_quote(candidate)}",
            f"prep -top {candidate_top}",
            "flatten",
            "design -stash gate",
            f"design -copy-from gold -as gold {top}",
            f"design -copy-from gate -as gate {candidate_top}",
            "equiv_make gold gate equiv",
            "prep -top equiv",
            "equiv_simple -seq 1",
            "equiv_status -assert",
            "",
        )),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["yosys", "-s", str(script)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    output.write_text(completed.stdout, encoding="utf-8")
    passed = completed.returncode == 0 and "Equivalence successfully proven" in completed.stdout
    return {
        "status": "formal_passed" if passed else "formal_failed",
        "returncode": completed.returncode,
        "script": str(script.relative_to(ROOT)),
        "log": str(output.relative_to(ROOT)),
        "script_sha256": _sha256(script),
        "log_sha256": _sha256(output),
    }


def _has_two_recipe_measurement(evidence_checks: list[dict[str, Any]]) -> bool:
    named_profiles: set[str] = set()
    for item in evidence_checks:
        if not item.get("exists"):
            continue
        evidence = Path(str(item["path"]))
        lower = evidence.name.lower()
        if "standard" in lower:
            named_profiles.add("standard")
        if "stronger" in lower:
            named_profiles.add("stronger")
        if evidence.suffix.lower() != ".json":
            continue
        try:
            document = _load(evidence)
        except FrameworkError:
            continue
        measurements = document.get("measurements")
        if isinstance(measurements, dict):
            named_profiles.update(
                profile for profile in ("standard", "stronger") if profile in measurements
            )
    return named_profiles == {"standard", "stronger"}


def audit_arm_result(
    path: Path,
    experiment: Path = DEFAULT_EXPERIMENT,
    *,
    rerun_formal: bool = True,
) -> dict[str, Any]:
    validate_arm_result(path, experiment)
    manifest = _load(experiment / "manifest.json")
    manifest_cases = {item["case_id"]: item for item in manifest["cases"]}
    result = _load(path)
    arm = result["arm"]
    repetition = result["repetition"]
    output_root = experiment / "evaluations" / f"arm-{arm.lower()}" / f"r{repetition}"
    rows: list[dict[str, Any]] = []
    for case_result in result["cases"]:
        case = manifest_cases[case_result["case_id"]]
        baseline = _resolve(case["sources"][-1])
        candidate_rows: list[dict[str, Any]] = []
        for index, candidate_result in enumerate(case_result["candidates"], start=1):
            candidate = _candidate_path(
                str(candidate_result.get("path", "")),
                arm=arm,
                repetition=repetition,
            )
            exists = candidate.is_file()
            evidence_checks = []
            for raw in candidate_result.get("evidence_paths", []):
                evidence = Path(str(raw))
                if not evidence.is_absolute():
                    evidence = ROOT / evidence
                evidence_checks.append({
                    "path": str(evidence),
                    "exists": evidence.is_file(),
                    "sha256": _sha256(evidence) if evidence.is_file() else None,
                })
            independent_formal = None
            if (
                rerun_formal
                and exists
                and case["source_kind"] == "generated"
                and case["proof_level"] == "P1"
            ):
                independent_formal = _rerun_generated_p1_formal(
                    baseline,
                    candidate,
                    case["top"],
                    output_root / f"{case['case_id']}-candidate-{index}.formal.log",
                )
            formal_supported = (
                candidate_result.get("formal_status") != "formal_passed"
                or (independent_formal is not None and independent_formal["status"] == "formal_passed")
                or (
                    independent_formal is None
                    and bool(evidence_checks)
                    and all(item["exists"] for item in evidence_checks)
                )
            )
            measurement_supported = (
                candidate_result.get("measurement_status") != "measured"
                or (
                    bool(evidence_checks)
                    and all(item["exists"] for item in evidence_checks)
                    and _has_two_recipe_measurement(evidence_checks)
                    and candidate_result.get("final_state")
                    in {"measured_improvement", "synthesis_handles", "flow_dependent", "regression"}
                )
            )
            candidate_rows.append({
                "candidate_id": candidate_result.get("candidate_id"),
                "path": str(candidate),
                "path_exists": exists,
                "evidence": evidence_checks,
                "formal_claim_supported": formal_supported,
                "measurement_claim_supported": measurement_supported,
                "independent_formal": independent_formal,
            })
        rows.append({
            "case_id": case["case_id"],
            "baseline_sha256_expected": case["source_sha256"],
            "baseline_sha256_actual": _sha256(baseline),
            "baseline_unchanged": _sha256(baseline) == case["source_sha256"],
            "candidates": candidate_rows,
            "claims_supported": all(
                item["path_exists"]
                and item["formal_claim_supported"]
                and item["measurement_claim_supported"]
                for item in candidate_rows
            ),
        })
    audit = {
        "schema": "rtl-advisor-plugin-abc-evidence-audit-v1",
        "experiment_id": "plugin-abc-v1",
        "arm": arm,
        "repetition": repetition,
        "result_path": str(path),
        "result_sha256": _sha256(path),
        "all_baselines_unchanged": all(row["baseline_unchanged"] for row in rows),
        "all_candidate_claims_supported": all(row["claims_supported"] for row in rows),
        "cases": rows,
    }
    output_path = output_root / "evidence-audit.json"
    _write_json(audit, output_path)
    return audit


def _case_score(result: dict[str, Any], oracle: dict[str, Any]) -> dict[str, Any]:
    decision = result["decision"]
    action = oracle["known_action"]
    candidates = result["candidates"]
    harmful = any(
        candidate.get("formal_status") == "formal_failed"
        or candidate.get("final_state") == "regression"
        for candidate in candidates
    ) and decision == "recommend_change"
    unproven_recommendation = decision == "recommend_change" and any(
        candidate.get("formal_status") != "formal_passed"
        or candidate.get("measurement_status") != "measured"
        for candidate in candidates
    )
    if action in {"no_change", "no_unproven_recommendation"}:
        correct = decision in {"no_change", "unsupported", "inconclusive"}
    else:
        validated_states = {
            candidate.get("final_state")
            for candidate in candidates
            if candidate.get("formal_status") == "formal_passed"
            and candidate.get("measurement_status") == "measured"
        }
        correct = bool(result.get("evidence_complete")) and bool(validated_states) and (
            (decision == "recommend_change" and "measured_improvement" in validated_states)
            or (
                decision == "no_change"
                and bool(validated_states & {"synthesis_handles", "flow_dependent", "regression"})
            )
        )
    return {
        "case_id": result["case_id"],
        "correct": bool(correct and not harmful and not unproven_recommendation),
        "harmful": harmful,
        "unproven_recommendation": unproven_recommendation,
        "evidence_complete": bool(result.get("evidence_complete")),
        "opportunity_case": action == "evaluate_candidate",
        "opportunity_validated": action == "evaluate_candidate" and bool(candidates) and bool(result.get("evidence_complete")),
        "no_change_case": action in {"no_change", "no_unproven_recommendation"},
        "correct_no_change": action in {"no_change", "no_unproven_recommendation"} and decision in {"no_change", "unsupported", "inconclusive"},
    }


def score(paths: list[Path], experiment: Path = DEFAULT_EXPERIMENT) -> dict[str, Any]:
    oracle_doc = _load(experiment / "oracle.json")
    oracle = {item["case_id"]: item for item in oracle_doc["cases"]}
    arms: list[dict[str, Any]] = []
    for path in paths:
        validate_arm_result(path, experiment)
        result = _load(path)
        audit_path = (
            experiment
            / "evaluations"
            / f"arm-{result['arm'].lower()}"
            / f"r{result['repetition']}"
            / "evidence-audit.json"
        )
        if not audit_path.is_file():
            raise FrameworkError(
                f"independent evidence audit is required before scoring: {audit_path}"
            )
        audit = _load(audit_path)
        if audit.get("result_sha256") != _sha256(path):
            raise FrameworkError(f"stale evidence audit for {path}")
        audit_cases = {item["case_id"]: item for item in audit["cases"]}
        effective_results: list[dict[str, Any]] = []
        for item in result["cases"]:
            checked = dict(item)
            case_audit = audit_cases[item["case_id"]]
            checked["evidence_complete"] = bool(item.get("evidence_complete")) and bool(
                case_audit.get("baseline_unchanged")
                and case_audit.get("claims_supported")
            )
            effective_results.append(checked)
        rows = [_case_score(item, oracle[item["case_id"]]) for item in effective_results]
        total = len(rows)
        opportunities = [row for row in rows if row["opportunity_case"]]
        no_change = [row for row in rows if row["no_change_case"]]
        actionable = [item for item in effective_results if item["decision"] in {"recommend_change", "no_change"}]
        arms.append({
            "arm": result["arm"],
            "repetition": result["repetition"],
            "validated_decision_accuracy_percent": round(sum(row["correct"] for row in rows) / total * 100, 2),
            "robust_opportunity_recall_percent": round(sum(row["opportunity_validated"] for row in opportunities) / len(opportunities) * 100, 2) if opportunities else None,
            "correct_no_change_percent": round(sum(row["correct_no_change"] for row in no_change) / len(no_change) * 100, 2) if no_change else None,
            "evidence_completion_percent": round(sum(bool(item.get("evidence_complete")) for item in actionable) / len(actionable) * 100, 2) if actionable else None,
            "harmful_recommendations": sum(row["harmful"] for row in rows),
            "unproven_recommendations": sum(row["unproven_recommendation"] for row in rows),
            "case_scores": rows,
        })
    return {"schema": "rtl-advisor-plugin-abc-scorecard-v1", "experiment_id": "plugin-abc-v1", "arms": arms}


def compare_repetitions(
    paths: list[Path], experiment: Path = DEFAULT_EXPERIMENT
) -> dict[str, Any]:
    if len(paths) < 2:
        raise FrameworkError("compare requires at least two arm results")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        validate_arm_result(path, experiment)
        result = _load(path)
        grouped.setdefault(result["arm"], []).append(result)
    comparisons: list[dict[str, Any]] = []
    for arm, results in sorted(grouped.items()):
        results.sort(key=lambda item: item["repetition"])
        if len(results) != 2:
            raise FrameworkError(
                f"arm {arm} must have exactly two results for repeat comparison"
            )
        left, right = results
        left_cases = {item["case_id"]: item for item in left["cases"]}
        right_cases = {item["case_id"]: item for item in right["cases"]}
        decision_disagreements: list[str] = []
        evidence_disagreements: list[str] = []
        for case_id in left_cases:
            first = left_cases[case_id]
            second = right_cases[case_id]
            if first["decision"] != second["decision"]:
                decision_disagreements.append(case_id)
            first_evidence = (
                first["scope_claim"],
                bool(first["evidence_complete"]),
                sorted(
                    (
                        item["formal_status"],
                        item["measurement_status"],
                        item["final_state"],
                    )
                    for item in first["candidates"]
                ),
            )
            second_evidence = (
                second["scope_claim"],
                bool(second["evidence_complete"]),
                sorted(
                    (
                        item["formal_status"],
                        item["measurement_status"],
                        item["final_state"],
                    )
                    for item in second["candidates"]
                ),
            )
            if first_evidence != second_evidence:
                evidence_disagreements.append(case_id)
        count = len(left_cases)
        comparisons.append({
            "arm": arm,
            "repetitions": [left["repetition"], right["repetition"]],
            "decision_reproducibility_percent": round(
                (count - len(decision_disagreements)) / count * 100, 2
            ),
            "normalized_evidence_reproducibility_percent": round(
                (count - len(evidence_disagreements)) / count * 100, 2
            ),
            "decision_disagreements": decision_disagreements,
            "evidence_disagreements": evidence_disagreements,
            "third_repetition_required": bool(decision_disagreements),
        })
    return {
        "schema": "rtl-advisor-plugin-abc-repeat-comparison-v1",
        "experiment_id": "plugin-abc-v1",
        "comparisons": comparisons,
    }


def resolve_tiebreaks(
    paths: list[Path], experiment: Path = DEFAULT_EXPERIMENT
) -> dict[str, Any]:
    if not paths:
        raise FrameworkError("resolve requires arm results")
    grouped: dict[str, dict[int, dict[str, Any]]] = {}
    for path in paths:
        validate_arm_result(path, experiment)
        result = _load(path)
        grouped.setdefault(result["arm"], {})[result["repetition"]] = result
    resolved_arms: list[dict[str, Any]] = []
    for arm, repetitions in sorted(grouped.items()):
        if not {1, 2}.issubset(repetitions):
            raise FrameworkError(f"arm {arm} requires repetitions 1 and 2")
        first = {item["case_id"]: item for item in repetitions[1]["cases"]}
        second = {item["case_id"]: item for item in repetitions[2]["cases"]}
        third = {
            item["case_id"]: item
            for item in repetitions.get(3, {}).get("cases", [])
        }
        rows: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for case_id, first_item in first.items():
            decisions = [first_item["decision"], second[case_id]["decision"]]
            tie_break_used = decisions[0] != decisions[1]
            if tie_break_used:
                if case_id not in third:
                    raise FrameworkError(
                        f"arm {arm} case {case_id} requires repetition 3"
                    )
                decisions.append(third[case_id]["decision"])
            counts = {decision: decisions.count(decision) for decision in set(decisions)}
            maximum = max(counts.values())
            winners = sorted(
                decision for decision, count in counts.items() if count == maximum
            )
            resolved = winners[0] if len(winners) == 1 else None
            if resolved is None:
                unresolved.append(case_id)
            rows.append({
                "case_id": case_id,
                "decisions": decisions,
                "tie_break_used": tie_break_used,
                "resolved_decision": resolved,
            })
        resolved_arms.append({
            "arm": arm,
            "case_count": len(rows),
            "tie_break_case_count": sum(row["tie_break_used"] for row in rows),
            "unresolved_cases": unresolved,
            "all_decisions_resolved": not unresolved,
            "cases": rows,
        })
    return {
        "schema": "rtl-advisor-plugin-abc-tiebreak-resolution-v1",
        "experiment_id": "plugin-abc-v1",
        "arms": resolved_arms,
    }


def _write_json(value: dict[str, Any], output: Path | None) -> None:
    payload = json.dumps(value, indent=2, sort_keys=False) + "\n"
    if output is None:
        sys.stdout.write(payload)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    packet = subparsers.add_parser("packet")
    packet.add_argument("--arm", required=True, choices=sorted(ARMS))
    packet.add_argument("--repetition", type=int, default=1)
    packet.add_argument("--case-id", action="append", dest="case_ids")
    packet.add_argument("--output", type=Path)
    arm = subparsers.add_parser("validate-arm")
    arm.add_argument("result", type=Path)
    audit = subparsers.add_parser("audit")
    audit.add_argument("result", type=Path)
    audit.add_argument("--no-rerun-formal", action="store_true")
    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("results", type=Path, nargs="+")
    score_parser.add_argument("--output", type=Path)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("results", type=Path, nargs="+")
    compare_parser.add_argument("--output", type=Path)
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("results", type=Path, nargs="+")
    resolve_parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            _write_json(validate_manifest(args.experiment), None)
        elif args.command == "packet":
            _write_json(
                make_packet(
                    args.arm,
                    args.repetition,
                    args.experiment,
                    args.case_ids,
                ),
                args.output,
            )
        elif args.command == "validate-arm":
            _write_json(validate_arm_result(args.result, args.experiment), None)
        elif args.command == "audit":
            _write_json(
                audit_arm_result(
                    args.result,
                    args.experiment,
                    rerun_formal=not args.no_rerun_formal,
                ),
                None,
            )
        elif args.command == "score":
            _write_json(score(args.results, args.experiment), args.output)
        elif args.command == "compare":
            _write_json(
                compare_repetitions(args.results, args.experiment),
                args.output,
            )
        else:
            _write_json(
                resolve_tiebreaks(args.results, args.experiment),
                args.output,
            )
    except FrameworkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
