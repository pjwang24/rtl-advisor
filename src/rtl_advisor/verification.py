from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, CorpusError, VariantSpec, load_manifest
from rtl_advisor.tools import ToolExecutionError, run_command


class VerificationError(RuntimeError):
    """Raised when a case cannot be checked."""


V2_CEC_SECONDS = 120
V2_PROOF_TIMEOUT_SECONDS = 150


@dataclass(frozen=True)
class LintResult:
    case_id: str
    variant_id: str
    status: str
    returncode: int | None
    command: tuple[str, ...]
    log_path: str
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


@dataclass(frozen=True)
class EquivalenceResult:
    case_id: str
    baseline_id: str
    baseline_sha256: str
    candidate_id: str
    candidate_sha256: str
    status: str
    expected_equivalent: bool
    expectation_met: bool
    command: tuple[str, ...]
    script_path: str
    log_path: str
    counterexample_path: str | None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact_root(config: ProjectConfig, manifest: CaseManifest) -> Path:
    return config.artifacts_dir / "cases" / manifest.case_id


def lint_case(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
) -> tuple[LintResult, ...]:
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    output_dir = _artifact_root(config, manifest) / "lint"
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[LintResult] = []

    for variant in manifest.variants:
        source = manifest.variant_path(variant)
        log_path = output_dir / f"{variant.variant_id}.log"
        command = (
            config.tools.verilator,
            "--lint-only",
            "--language",
            "1800-2017",
            "--Wall",
            "--Wno-fatal",
            "--Wno-DECLFILENAME",
            "--top-module",
            variant.wrapper_top,
            str(source),
        )
        try:
            completed = run_command(
                command,
                timeout_seconds=config.tools.timeout_seconds,
                cwd=manifest.root,
            )
            combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
            log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
            result = LintResult(
                case_id=manifest.case_id,
                variant_id=variant.variant_id,
                status="passed" if completed.returncode == 0 else "failed",
                returncode=completed.returncode,
                command=command,
                log_path=str(log_path),
                detail=None if completed.returncode == 0 else combined,
            )
        except ToolExecutionError as exc:
            log_path.write_text(f"{exc}\n", encoding="utf-8")
            result = LintResult(
                case_id=manifest.case_id,
                variant_id=variant.variant_id,
                status="error",
                returncode=None,
                command=command,
                log_path=str(log_path),
                detail=str(exc),
            )
        _write_json(output_dir / f"{variant.variant_id}.json", result.to_dict())
        results.append(result)

    summary = {
        "case_id": manifest.case_id,
        "ok": all(result.ok for result in results),
        "results": [result.to_dict() for result in results],
    }
    _write_json(output_dir / "summary.json", summary)
    return tuple(results)


def _yosys_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _equivalence_script(
    manifest: CaseManifest,
    baseline: VariantSpec,
    candidate: VariantSpec,
    counterexample_path: Path,
) -> str:
    baseline_path = manifest.variant_path(baseline)
    candidate_path = manifest.variant_path(candidate)
    return "\n".join(
        (
            f"read_verilog -sv {_yosys_quote(baseline_path)}",
            f"prep -top {baseline.kernel_top}",
            "design -stash baseline_design",
            f"read_verilog -sv {_yosys_quote(candidate_path)}",
            f"prep -top {candidate.kernel_top}",
            "design -stash candidate_design",
            (
                "design -copy-from baseline_design -as gold "
                f"{baseline.kernel_top}"
            ),
            (
                "design -copy-from candidate_design -as gate "
                f"{candidate.kernel_top}"
            ),
            "miter -equiv -make_outputs -flatten gold gate equiv_miter",
            "prep -top equiv_miter",
            (
                "sat -prove trigger 0 -show-inputs -show-outputs "
                f"-dump_json {_yosys_quote(counterexample_path)} equiv_miter"
            ),
            "",
        )
    )


def prove_equivalence(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    candidate_id: str,
) -> EquivalenceResult:
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    baseline = manifest.baseline
    candidate = manifest.variant(candidate_id)
    if candidate.variant_id == baseline.variant_id:
        raise VerificationError("the baseline cannot be checked against itself")

    output_dir = _artifact_root(config, manifest) / "equivalence"
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = output_dir / f"{candidate.variant_id}.ys"
    log_path = output_dir / f"{candidate.variant_id}.log"
    counterexample_path = output_dir / f"{candidate.variant_id}_counterexample.json"
    counterexample_path.unlink(missing_ok=True)
    script_path.write_text(
        _equivalence_script(manifest, baseline, candidate, counterexample_path),
        encoding="utf-8",
    )
    command = (config.tools.yosys, "-Q", "-s", str(script_path))

    try:
        completed = run_command(
            command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=manifest.root,
        )
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
        if completed.returncode != 0:
            status = "error"
            detail = combined or f"Yosys exited with {completed.returncode}"
        elif "SAT proof finished - no model found: SUCCESS!" in combined:
            status = "equivalent"
            detail = None
        elif "SAT proof finished - model found: FAIL!" in combined:
            status = "inequivalent"
            detail = None
        else:
            status = "error"
            detail = "Yosys completed without a recognized SAT proof result"
    except ToolExecutionError as exc:
        status = "error"
        detail = str(exc)
        log_path.write_text(f"{exc}\n", encoding="utf-8")

    actual_equivalent = status == "equivalent"
    expectation_met = status != "error" and (
        actual_equivalent == candidate.expected_equivalent
    )
    result = EquivalenceResult(
        case_id=manifest.case_id,
        baseline_id=baseline.variant_id,
        baseline_sha256=baseline.sha256,
        candidate_id=candidate.variant_id,
        candidate_sha256=candidate.sha256,
        status=status,
        expected_equivalent=candidate.expected_equivalent,
        expectation_met=expectation_met,
        command=command,
        script_path=str(script_path),
        log_path=str(log_path),
        counterexample_path=(
            str(counterexample_path) if counterexample_path.is_file() else None
        ),
        detail=detail,
    )
    _write_json(output_dir / f"{candidate.variant_id}.json", result.to_dict())
    return result


def prove_case_candidates(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    candidate_id: str = "all",
) -> tuple[EquivalenceResult, ...]:
    try:
        manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    except CorpusError as exc:
        raise VerificationError(str(exc)) from exc

    if candidate_id == "all":
        candidates = [
            variant
            for variant in manifest.variants
            if variant.variant_id != manifest.baseline_id
        ]
    else:
        candidates = [manifest.variant(candidate_id)]

    results = tuple(
        prove_equivalence(config, manifest, candidate.variant_id)
        for candidate in candidates
    )
    summary = {
        "case_id": manifest.case_id,
        "ok": all(result.expectation_met for result in results),
        "results": [result.to_dict() for result in results],
    }
    _write_json(
        _artifact_root(config, manifest) / "equivalence" / "summary.json",
        summary,
    )
    return results


def _equivalence_v2_script(
    manifest: CaseManifest,
    baseline: VariantSpec,
    candidate: VariantSpec,
    gold_aig: Path,
    gate_aig: Path,
) -> str:
    baseline_path = manifest.variant_path(baseline)
    candidate_path = manifest.variant_path(candidate)
    return "\n".join(
        (
            f"read_verilog -sv {_yosys_quote(baseline_path)}",
            f"prep -top {baseline.kernel_top}",
            "flatten",
            "techmap",
            "opt",
            "aigmap",
            f"write_aiger -symbols {_yosys_quote(gold_aig)}",
            "design -reset",
            f"read_verilog -sv {_yosys_quote(candidate_path)}",
            f"prep -top {candidate.kernel_top}",
            "flatten",
            "techmap",
            "opt",
            "aigmap",
            f"write_aiger -symbols {_yosys_quote(gate_aig)}",
            "",
        )
    )


def prove_equivalence_v2(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    candidate_id: str,
) -> EquivalenceResult:
    """Use Yosys equivalence cells for positive v2 proofs; SAT proves controls."""
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    baseline = manifest.baseline
    candidate = manifest.variant(candidate_id)
    if not candidate.expected_equivalent:
        return prove_equivalence(config, manifest, candidate_id)
    output_dir = _artifact_root(config, manifest) / "equivalence"
    output_dir.mkdir(parents=True, exist_ok=True)
    script_path = output_dir / f"{candidate.variant_id}.ys"
    log_path = output_dir / f"{candidate.variant_id}.log"
    gold_aig = output_dir / f"{candidate.variant_id}_gold.aig"
    gate_aig = output_dir / f"{candidate.variant_id}_gate.aig"
    script = _equivalence_v2_script(
        manifest, baseline, candidate, gold_aig, gate_aig
    )
    script_path.write_text(script, encoding="utf-8")
    yosys_command = (config.tools.yosys, "-Q", "-s", str(script_path))
    resolved_yosys = shutil.which(config.tools.yosys)
    abc_binary = (
        str(Path(resolved_yosys).with_name("yosys-abc"))
        if resolved_yosys is not None
        else "yosys-abc"
    )
    abc_gold = str(gold_aig).replace('"', '\\"')
    abc_gate = str(gate_aig).replace('"', '\\"')
    command = (
        abc_binary,
        "-c",
        f'cec -p -P 4 -T {V2_CEC_SECONDS} "{abc_gold}" "{abc_gate}"',
    )
    try:
        exported = run_command(
            yosys_command,
            timeout_seconds=config.tools.timeout_seconds,
            cwd=manifest.root,
        )
        export_log = "\n".join(
            part for part in (exported.stdout, exported.stderr) if part
        )
        if exported.returncode != 0:
            status = "error"
            combined = export_log
            detail = combined or f"Yosys exited with {exported.returncode}"
        else:
            completed = run_command(
                command,
                timeout_seconds=max(
                    config.tools.timeout_seconds,
                    V2_PROOF_TIMEOUT_SECONDS,
                ),
                cwd=manifest.root,
            )
            abc_log = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            )
            combined = "\n\n".join(
                part for part in (export_log, abc_log) if part
            )
            equivalent_message = "Networks are equivalent" in abc_log
            status = (
                "equivalent"
                if completed.returncode == 0 and equivalent_message
                else "error"
            )
            detail = None if status == "equivalent" else (
                abc_log or f"ABC exited with {completed.returncode}"
            )
        log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    except ToolExecutionError as exc:
        status = "error"
        detail = str(exc)
        log_path.write_text(f"{exc}\n", encoding="utf-8")
    result = EquivalenceResult(
        case_id=manifest.case_id,
        baseline_id=baseline.variant_id,
        baseline_sha256=baseline.sha256,
        candidate_id=candidate.variant_id,
        candidate_sha256=candidate.sha256,
        status=status,
        expected_equivalent=True,
        expectation_met=status == "equivalent",
        command=command,
        script_path=str(script_path),
        log_path=str(log_path),
        counterexample_path=None,
        detail=detail,
    )
    _write_json(output_dir / f"{candidate.variant_id}.json", result.to_dict())
    return result


def prove_case_candidates_v2(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
) -> tuple[EquivalenceResult, ...]:
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    results = tuple(
        prove_equivalence_v2(config, manifest, candidate.variant_id)
        for candidate in manifest.variants
        if candidate.variant_id != manifest.baseline_id
    )
    summary = {
        "case_id": manifest.case_id,
        "flow_version": "yosys-equivalence-v2",
        "ok": all(result.expectation_met for result in results),
        "results": [result.to_dict() for result in results],
    }
    _write_json(_artifact_root(config, manifest) / "equivalence/summary.json", summary)
    return results
