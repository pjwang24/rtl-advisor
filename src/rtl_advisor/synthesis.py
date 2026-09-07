from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, VariantSpec, load_manifest
from rtl_advisor.tools import (
    ToolExecutionError,
    first_output_line,
    run_command,
    sha256_file,
)


FLOW_VERSION = "yosys-abc-nangate45-v2"
_ABC_TIMING_PATTERN = re.compile(
    r"ABC:\s+WireLoad\s*=.*?Gates\s*=\s*(?P<gates>\d+).*?"
    r"Area\s*=\s*(?P<area>[0-9]+(?:\.[0-9]+)?).*?"
    r"Delay\s*=\s*(?P<delay>[0-9]+(?:\.[0-9]+)?)\s+ps"
)


class SynthesisError(RuntimeError):
    """Raised when a variant cannot produce trustworthy synthesis evidence."""


@dataclass(frozen=True)
class SynthesisMetrics:
    critical_delay_ps: float
    area_total: float
    area_combinational: float
    area_sequential: float
    abc_area_combinational: float
    cell_count: int
    raw_cell_count: int
    abc_gate_count: int
    cells_by_type: dict[str, int]


@dataclass(frozen=True)
class SynthesisResult:
    case_id: str
    variant_id: str
    status: str
    backend: str
    cache_key: str
    cached: bool
    source_path: str
    source_sha256: str
    top: str
    metrics: SynthesisMetrics
    constraints: dict[str, Any]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SynthesisResult":
        metrics = SynthesisMetrics(**payload["metrics"])
        return cls(
            case_id=payload["case_id"],
            variant_id=payload["variant_id"],
            status=payload["status"],
            backend=payload["backend"],
            cache_key=payload["cache_key"],
            cached=bool(payload.get("cached", False)),
            source_path=payload["source_path"],
            source_sha256=payload["source_sha256"],
            top=payload["top"],
            metrics=metrics,
            constraints=payload["constraints"],
            provenance=payload["provenance"],
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _yosys_quote(path: Path) -> str:
    raw = str(path)
    if any(character in raw for character in ("\x00", "\r", "\n")):
        raise SynthesisError("Yosys arguments may not contain control characters")
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yosys_version(config: ProjectConfig) -> str:
    try:
        result = run_command(
            (config.tools.yosys, "-V"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        raise SynthesisError(str(exc)) from exc
    if result.returncode != 0:
        raise SynthesisError(result.stderr or result.stdout or "Yosys version probe failed")
    return first_output_line(result) or "unknown"


def _proof_path(config: ProjectConfig, manifest: CaseManifest, variant_id: str) -> Path:
    return (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "equivalence"
        / f"{variant_id}.json"
    )


def _require_equivalence_proof(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant: VariantSpec,
) -> None:
    if variant.variant_id == manifest.baseline_id:
        return
    proof_path = _proof_path(config, manifest, variant.variant_id)
    if not proof_path.is_file():
        raise SynthesisError(
            f"equivalence proof missing for {variant.variant_id}; run rtl-advisor equivalence first"
        )
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SynthesisError(f"could not read equivalence proof {proof_path}: {exc}") from exc
    if proof.get("status") != "equivalent" or not proof.get("expectation_met"):
        raise SynthesisError(
            f"variant {variant.variant_id} does not have a successful equivalence proof"
        )
    if proof.get("baseline_sha256") != manifest.baseline.sha256:
        raise SynthesisError(
            f"equivalence proof for {variant.variant_id} has a stale baseline hash"
        )
    if proof.get("candidate_sha256") != variant.sha256:
        raise SynthesisError(
            f"equivalence proof for {variant.variant_id} has a stale candidate hash"
        )


def _select_variants(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant_id: str,
) -> tuple[VariantSpec, ...]:
    if variant_id == "all":
        variants = tuple(
            variant
            for variant in manifest.variants
            if variant.variant_id == manifest.baseline_id
            or variant.expected_equivalent
        )
    else:
        variants = (manifest.variant(variant_id),)
    for variant in variants:
        _require_equivalence_proof(config, manifest, variant)
    return variants


def _cache_key(
    *,
    variant: VariantSpec,
    yosys_version: str,
    liberty_sha256: str,
    driving_cell: str,
    output_load_ff: float,
) -> str:
    payload = {
        "flow_version": FLOW_VERSION,
        "source_sha256": variant.sha256,
        "top": variant.wrapper_top,
        "yosys_version": yosys_version,
        "liberty_sha256": liberty_sha256,
        "driving_cell": driving_cell,
        "output_load_ff": output_load_ff,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _synthesis_script(
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
            f"synth -top {top} -flatten -noabc",
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


def _parse_abc_metrics(log: str) -> tuple[int, float, float]:
    matches = list(_ABC_TIMING_PATTERN.finditer(log))
    if not matches:
        raise SynthesisError("ABC timing summary was not found in the synthesis log")
    match = matches[-1]
    return (
        int(match.group("gates")),
        float(match.group("area")),
        float(match.group("delay")),
    )


def _parse_stat_metrics(
    path: Path,
    top: str,
) -> tuple[float, float, int, int, dict[str, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        design = payload["modules"][f"\\{top}"]
        cells_by_type = {
            str(name): int(count)
            for name, count in design["num_cells_by_type"].items()
        }
        raw_cell_count = int(design["num_cells"])
        cell_count = sum(
            count for name, count in cells_by_type.items() if not name.startswith("$")
        )
        return (
            float(design["area"]),
            float(design.get("sequential_area", 0.0)),
            cell_count,
            raw_cell_count,
            cells_by_type,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SynthesisError(f"could not parse Yosys statistics {path}: {exc}") from exc


def synthesize_variant(
    config: ProjectConfig,
    manifest: CaseManifest,
    variant: VariantSpec,
    *,
    force: bool = False,
) -> SynthesisResult:
    _require_equivalence_proof(config, manifest, variant)
    liberty_path = config.liberty.path
    if not liberty_path.is_file():
        raise SynthesisError("Liberty file is missing; run rtl-advisor setup first")
    liberty_sha256 = sha256_file(liberty_path)
    if liberty_sha256 != config.liberty.sha256:
        raise SynthesisError(
            f"Liberty checksum mismatch: expected {config.liberty.sha256}, "
            f"got {liberty_sha256}"
        )

    yosys_version = _yosys_version(config)
    cache_key = _cache_key(
        variant=variant,
        yosys_version=yosys_version,
        liberty_sha256=liberty_sha256,
        driving_cell=config.synthesis.driving_cell,
        output_load_ff=config.synthesis.output_load_ff,
    )
    output_dir = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "synthesis"
        / variant.variant_id
    )
    result_path = output_dir / "result.json"
    log_path = output_dir / "synthesis.log"
    script_path = output_dir / "synthesis.ys"
    constraints_path = output_dir / "abc.constr"
    stat_path = output_dir / "stat.json"
    netlist_path = output_dir / "mapped.v"

    if result_path.is_file() and not force:
        try:
            cached = SynthesisResult.from_dict(
                json.loads(result_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            cached = None
        if (
            cached is not None
            and cached.cache_key == cache_key
            and cached.status == "passed"
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
    script = _synthesis_script(
        source=source_path,
        top=variant.wrapper_top,
        liberty=liberty_path,
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
        raise SynthesisError(str(exc)) from exc
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    log_path.write_text(combined + ("\n" if combined else ""), encoding="utf-8")
    if completed.returncode != 0:
        raise SynthesisError(
            f"Yosys synthesis failed for {variant.variant_id}; see {log_path}"
        )

    abc_gate_count, abc_area, critical_delay_ps = _parse_abc_metrics(combined)
    area_total, area_sequential, cell_count, raw_cell_count, cells_by_type = (
        _parse_stat_metrics(stat_path, variant.wrapper_top)
    )
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
        backend="yosys-abc-liberty",
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
            "flow_version": FLOW_VERSION,
            "yosys_version": yosys_version,
            "liberty_name": config.liberty.name,
            "liberty_path": str(liberty_path),
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


def _metric_comparison(
    baseline: float,
    candidate: float,
) -> dict[str, float]:
    delta = candidate - baseline
    improvement = ((baseline - candidate) / baseline * 100.0) if baseline else 0.0
    return {
        "baseline": round(baseline, 6),
        "candidate": round(candidate, 6),
        "delta": round(delta, 6),
        "improvement_percent": round(improvement, 6),
    }


def compare_results(
    baseline: SynthesisResult,
    candidate: SynthesisResult,
) -> dict[str, Any]:
    return {
        "baseline_id": baseline.variant_id,
        "candidate_id": candidate.variant_id,
        "critical_delay_ps": _metric_comparison(
            baseline.metrics.critical_delay_ps,
            candidate.metrics.critical_delay_ps,
        ),
        "area_total": _metric_comparison(
            baseline.metrics.area_total,
            candidate.metrics.area_total,
        ),
        "cell_count": _metric_comparison(
            float(baseline.metrics.cell_count),
            float(candidate.metrics.cell_count),
        ),
    }


def synthesize_case(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    *,
    variant_id: str = "all",
    force: bool = False,
) -> tuple[tuple[SynthesisResult, ...], dict[str, Any]]:
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    variants = _select_variants(config, manifest, variant_id)
    results = tuple(
        synthesize_variant(config, manifest, variant, force=force)
        for variant in variants
    )
    by_id = {result.variant_id: result for result in results}
    baseline = by_id.get(manifest.baseline_id)
    comparisons = []
    if baseline is not None:
        comparisons = [
            compare_results(baseline, result)
            for result in results
            if result.variant_id != manifest.baseline_id
        ]
    summary = {
        "case_id": manifest.case_id,
        "status": "passed",
        "results": [result.to_dict() for result in results],
        "comparisons": comparisons,
        "delay_kind": "ABC constrained combinational estimate",
    }
    output_dir = config.artifacts_dir / "cases" / manifest.case_id / "synthesis"
    _write_json(output_dir / "summary.json", summary)
    _write_json(
        output_dir / "comparison.json",
        {"case_id": manifest.case_id, "comparisons": comparisons},
    )
    return results, summary
