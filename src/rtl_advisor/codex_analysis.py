from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from rtl_advisor.config import ProjectConfig
from rtl_advisor.corpus import CaseManifest, VariantSpec, load_manifest
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


PROMPT_VERSION = "rtl-advisor-codex-v1"
ANALYSIS_SCHEMA_VERSION = 1
SUPPORTED_MODES = {"codex", "hybrid"}
SUPPORTED_EFFORTS = {"xhigh", "ultra"}

TRANSFORMATION_CATALOG = (
    {
        "id": "share_arithmetic_by_muxing_inputs",
        "description": "Share mutually exclusive arithmetic by selecting operands first.",
    },
    {
        "id": "reassociate_arithmetic_tree",
        "description": "Balance or reassociate an arithmetic/reduction dependency chain.",
    },
    {
        "id": "move_mux_across_operation",
        "description": "Move selection before or after an operation to change duplication or depth.",
    },
    {
        "id": "balance_priority_selection",
        "description": "Restructure a semantically compatible priority or selection chain.",
    },
    {
        "id": "factor_repeated_decode",
        "description": "Compute repeated decode conditions once and reuse them.",
    },
    {
        "id": "factor_comparator_selection",
        "description": "Reduce duplicated comparisons or comparison-driven selection.",
    },
    {
        "id": "bound_variable_shift",
        "description": "Constrain or restructure a wide variable shift implementation.",
    },
    {
        "id": "narrow_intermediate_width",
        "description": "Use the minimum proven-safe width and signedness for intermediates.",
    },
    {
        "id": "restructure_popcount_or_saturation",
        "description": "Balance population-count, saturation, or threshold structures.",
    },
)

_TRANSFORMATION_IDS = tuple(item["id"] for item in TRANSFORMATION_CATALOG)
_CATEGORIES = (
    "arithmetic_resource_sharing",
    "arithmetic_association",
    "mux_placement",
    "priority_selection",
    "decode_factoring",
    "comparator_selection",
    "variable_shift",
    "width_or_signedness",
    "popcount_or_saturation",
)
_DIRECTIONS = ("improve", "degrade", "neutral", "uncertain")
_COMMENT_PATTERN = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

CODEX_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "findings": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "category",
                    "source",
                    "evidence",
                    "confidence",
                    "recommendation",
                    "transformation_id",
                    "predicted_effect",
                    "risks",
                ],
                "properties": {
                    "category": {"type": "string", "enum": list(_CATEGORIES)},
                    "source": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["file", "start_line", "end_line"],
                        "properties": {
                            "file": {"type": "string", "enum": ["design.sv"]},
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                        },
                    },
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 6,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "recommendation": {"type": "string", "minLength": 1},
                    "transformation_id": {
                        "type": "string",
                        "enum": list(_TRANSFORMATION_IDS),
                    },
                    "predicted_effect": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["delay", "area", "cell_count"],
                        "properties": {
                            "delay": {"type": "string", "enum": list(_DIRECTIONS)},
                            "area": {"type": "string", "enum": list(_DIRECTIONS)},
                            "cell_count": {
                                "type": "string",
                                "enum": list(_DIRECTIONS),
                            },
                        },
                    },
                    "risks": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    },
}


class CodexAnalysisError(RuntimeError):
    """Raised when a Codex analysis run fails or violates its contract."""


@dataclass(frozen=True)
class CodexAnalysisBuild:
    result: dict[str, Any]
    cached: bool
    output_path: Path


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _strip_comments(source: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if character == "\n" else " " for character in match.group())

    return _COMMENT_PATTERN.sub(blank, source)


def _blind_source(source: str, variant: VariantSpec) -> str:
    blinded = _strip_comments(source)
    replacements = (
        (variant.wrapper_top, "design_top"),
        (variant.kernel_top, "design_kernel"),
    )
    for original, replacement in replacements:
        blinded = re.sub(rf"\b{re.escape(original)}\b", replacement, blinded)
    return blinded


def _replace_metadata(value: Any, replacements: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        for original, replacement in replacements:
            value = value.replace(original, replacement)
        return value
    if isinstance(value, list):
        return [_replace_metadata(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_metadata(item, replacements)
            for key, item in value.items()
        }
    return value


def _blind_rule_findings(
    rules_analysis: dict[str, Any],
    variant: VariantSpec,
) -> list[dict[str, Any]]:
    replacements = (
        (variant.wrapper_top, "design_top"),
        (variant.kernel_top, "design_kernel"),
        (variant.file, "design.sv"),
    )
    findings = []
    for finding in rules_analysis.get("findings", []):
        source = finding.get("source") or {}
        locations = source.get("locations") or []
        first_location = locations[0] if locations else {}
        findings.append(
            {
                "rule_id": finding["rule_id"],
                "category": finding["category"],
                "source": {
                    "file": "design.sv",
                    "start_line": first_location.get("start_line"),
                    "end_line": first_location.get("end_line"),
                },
                "confidence": finding["confidence"],
                "evidence": _replace_metadata(finding["evidence"], replacements),
                "recommendation": finding["recommendation"],
                "transformation_id": finding["transformation_id"],
                "predicted_effect": finding["predicted_effect"],
                "risks": finding["risks"],
            }
        )
    return findings


def build_model_input(
    manifest: CaseManifest,
    variant: VariantSpec,
    mode: str,
    *,
    rules_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in SUPPORTED_MODES:
        raise CodexAnalysisError(f"unsupported Codex analysis mode: {mode}")
    if mode == "hybrid" and rules_analysis is None:
        raise CodexAnalysisError("hybrid analysis requires structural rule findings")
    source = manifest.variant_path(variant).read_text(encoding="utf-8")
    model_input: dict[str, Any] = {
        "schema_version": 1,
        "rtl": {
            "filename": "design.sv",
            "top": "design_top",
            "source": _blind_source(source, variant),
        },
        "transformation_catalog": list(TRANSFORMATION_CATALOG),
    }
    if mode == "hybrid":
        model_input["structural_findings"] = _blind_rule_findings(
            rules_analysis or {},
            variant,
        )
    model_input["input_hash"] = _json_hash(model_input)
    return model_input


def _prompt(model_input: dict[str, Any], mode: str) -> str:
    context = (
        "RTL plus precomputed structural findings"
        if mode == "hybrid"
        else "RTL only"
    )
    return f"""You are a pre-synthesis RTL advisor.
Analyze only the JSON input included below ({context}).
Do not run commands, invoke tools, access files, use the network, or seek other context.
No synthesis metrics or outcome labels are provided. Do not claim measured results.
Recommend only registered transformation_id values from the supplied catalog.
Preserve cycle latency and logical behavior. Treat every timing/area effect as a prediction.
Use exact source line numbers from design.sv and return at most five ranked findings.
If there is no actionable opportunity, return an empty findings array.
Your final response must satisfy the supplied JSON Schema exactly.

MODEL_INPUT_JSON
{json.dumps(model_input, indent=2, sort_keys=True)}
"""


def _require_exact_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    actual = set(value)
    if actual != required:
        raise CodexAnalysisError(
            f"invalid Codex response at {path}: expected keys {sorted(required)}, "
            f"got {sorted(actual)}"
        )


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexAnalysisError(f"invalid Codex response at {path}: expected string")
    return value


def _validate_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise CodexAnalysisError("invalid Codex response: expected a JSON object")
    _require_exact_keys(response, {"summary", "findings"}, "$")
    _require_string(response["summary"], "$.summary")
    findings = response["findings"]
    if not isinstance(findings, list) or len(findings) > 5:
        raise CodexAnalysisError("invalid Codex response at $.findings")
    finding_keys = {
        "category",
        "source",
        "evidence",
        "confidence",
        "recommendation",
        "transformation_id",
        "predicted_effect",
        "risks",
    }
    for index, finding in enumerate(findings):
        path = f"$.findings[{index}]"
        if not isinstance(finding, dict):
            raise CodexAnalysisError(f"invalid Codex response at {path}")
        _require_exact_keys(finding, finding_keys, path)
        if finding["category"] not in _CATEGORIES:
            raise CodexAnalysisError(f"invalid category at {path}.category")
        source = finding["source"]
        if not isinstance(source, dict):
            raise CodexAnalysisError(f"invalid source at {path}.source")
        _require_exact_keys(source, {"file", "start_line", "end_line"}, f"{path}.source")
        if source["file"] != "design.sv":
            raise CodexAnalysisError(f"invalid source file at {path}.source.file")
        for field in ("start_line", "end_line"):
            if isinstance(source[field], bool) or not isinstance(source[field], int) or source[field] < 1:
                raise CodexAnalysisError(f"invalid line at {path}.source.{field}")
        if source["end_line"] < source["start_line"]:
            raise CodexAnalysisError(f"invalid source range at {path}.source")
        evidence = finding["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 6:
            raise CodexAnalysisError(f"invalid evidence at {path}.evidence")
        for item_index, item in enumerate(evidence):
            _require_string(item, f"{path}.evidence[{item_index}]")
        confidence = finding["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise CodexAnalysisError(f"invalid confidence at {path}.confidence")
        _require_string(finding["recommendation"], f"{path}.recommendation")
        if finding["transformation_id"] not in _TRANSFORMATION_IDS:
            raise CodexAnalysisError(f"invalid transformation at {path}.transformation_id")
        predicted = finding["predicted_effect"]
        if not isinstance(predicted, dict):
            raise CodexAnalysisError(f"invalid predicted effect at {path}.predicted_effect")
        _require_exact_keys(
            predicted,
            {"delay", "area", "cell_count"},
            f"{path}.predicted_effect",
        )
        if any(direction not in _DIRECTIONS for direction in predicted.values()):
            raise CodexAnalysisError(f"invalid direction at {path}.predicted_effect")
        risks = finding["risks"]
        if not isinstance(risks, list) or len(risks) > 6:
            raise CodexAnalysisError(f"invalid risks at {path}.risks")
        for risk_index, risk in enumerate(risks):
            _require_string(risk, f"{path}.risks[{risk_index}]")
    return response


def _audit_events(events: str) -> None:
    allowed_items = {"reasoning", "agent_message"}
    for line_number, line in enumerate(events.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAnalysisError(
                f"invalid Codex JSONL event at line {line_number}: {exc}"
            ) from exc
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type and item_type not in allowed_items:
                raise CodexAnalysisError(
                    f"Codex run rejected because it used item type {item_type!r}"
                )


def _event_usage(events: str) -> dict[str, int]:
    usage: dict[str, int] = {}
    for line in events.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_usage = event.get("usage")
        if event.get("type") != "turn.completed" or not isinstance(raw_usage, dict):
            continue
        usage = {
            str(key): int(value)
            for key, value in raw_usage.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
    return usage


def _failure(output_dir: Path, kind: str, detail: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "failure.json").write_text(
        json.dumps({"status": "failed", "kind": kind, "detail": detail}, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _normalize_result(
    response: dict[str, Any],
    *,
    manifest: CaseManifest,
    variant: VariantSpec,
    mode: str,
    model: str,
    effort: str,
    cache_key: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    findings = []
    for rank, raw_finding in enumerate(response["findings"], start=1):
        finding = dict(raw_finding)
        source = dict(finding["source"])
        source["model_visible_file"] = source.pop("file")
        source["file"] = variant.file
        finding["source"] = source
        finding["rank"] = rank
        finding["origin"] = mode
        identity = {
            "mode": mode,
            "source": source,
            "transformation_id": finding["transformation_id"],
            "recommendation": finding["recommendation"],
        }
        finding["finding_id"] = _json_hash(identity)[:16]
        findings.append(finding)
    core = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "case_id": manifest.case_id,
        "variant_id": variant.variant_id,
        "mode": mode,
        "model": model,
        "effort": effort,
        "summary": response["summary"],
        "findings": findings,
        "cache_key": cache_key,
        "provenance": provenance,
    }
    core["analysis_hash"] = _json_hash(core)
    return core


def analyze_with_codex(
    config: ProjectConfig,
    case: str | Path | CaseManifest,
    variant_id: str,
    *,
    mode: str,
    effort: str | None = None,
    rules_analysis: dict[str, Any] | None = None,
    force: bool = False,
    run_id: str | None = None,
) -> CodexAnalysisBuild:
    if mode not in SUPPORTED_MODES:
        raise CodexAnalysisError(f"unsupported Codex analysis mode: {mode}")
    selected_effort = effort or config.codex.default_effort
    if selected_effort not in SUPPORTED_EFFORTS:
        raise CodexAnalysisError(f"unsupported Codex effort: {selected_effort}")
    if run_id is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise CodexAnalysisError("run_id contains unsupported characters")
    manifest = case if isinstance(case, CaseManifest) else load_manifest(case)
    variant = manifest.variant(variant_id)
    model_input = build_model_input(
        manifest,
        variant,
        mode,
        rules_analysis=rules_analysis,
    )
    prompt = _prompt(model_input, mode)

    try:
        version_result = run_command(
            (config.tools.codex, "--version"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        raise CodexAnalysisError(str(exc)) from exc
    if version_result.returncode != 0:
        raise CodexAnalysisError(version_result.stderr or version_result.stdout)
    codex_version = first_output_line(version_result) or "unknown"
    cache_key = _json_hash(
        {
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "schema_sha256": _json_hash(CODEX_RESPONSE_SCHEMA),
            "input_hash": model_input["input_hash"],
            "mode": mode,
            "model": config.codex.model,
            "effort": selected_effort,
            "codex_version": codex_version,
        }
    )
    output_dir = (
        config.artifacts_dir
        / "cases"
        / manifest.case_id
        / "analysis"
        / mode
        / variant.variant_id
        / selected_effort
    )
    if run_id is not None:
        output_dir = output_dir / "runs" / run_id
    output_path = output_dir / "analysis.json"
    if output_path.is_file() and not force:
        try:
            cached = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached is not None and cached.get("cache_key") == cache_key:
            return CodexAnalysisBuild(cached, True, output_path)

    workspace = output_dir / "model_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    request_path = workspace / "request.json"
    schema_path = workspace / "response-schema.json"
    response_path = workspace / "response.json"
    prompt_path = output_dir / "prompt.txt"
    events_path = output_dir / "events.jsonl"
    stderr_path = output_dir / "stderr.log"
    request_path.write_text(
        json.dumps(model_input, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    schema_path.write_text(
        json.dumps(CODEX_RESPONSE_SCHEMA, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    response_path.unlink(missing_ok=True)
    command = (
        config.tools.codex,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--skip-git-repo-check",
        "--model",
        config.codex.model,
        "--config",
        f'model_reasoning_effort="{selected_effort}"',
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(response_path),
        "--json",
        "--color",
        "never",
        "-",
    )
    started = time.monotonic()
    try:
        completed = run_command(
            command,
            timeout_seconds=config.codex.timeout_seconds,
            cwd=workspace,
            input_text=prompt,
        )
    except ToolExecutionError as exc:
        _failure(output_dir, "infrastructure", str(exc))
        raise CodexAnalysisError(str(exc)) from exc
    events_path.write_text(completed.stdout + ("\n" if completed.stdout else ""), encoding="utf-8")
    stderr_path.write_text(completed.stderr + ("\n" if completed.stderr else ""), encoding="utf-8")
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or f"exit code {completed.returncode}"
        _failure(output_dir, "infrastructure", detail)
        raise CodexAnalysisError(f"Codex analysis failed: {detail}")
    try:
        _audit_events(completed.stdout)
        response = json.loads(response_path.read_text(encoding="utf-8"))
        validated = _validate_response(response)
    except (OSError, json.JSONDecodeError, CodexAnalysisError) as exc:
        _failure(output_dir, "schema_or_audit", str(exc))
        if isinstance(exc, CodexAnalysisError):
            raise
        raise CodexAnalysisError(f"could not parse Codex response: {exc}") from exc

    provenance = {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "input_hash": model_input["input_hash"],
        "schema_sha256": _json_hash(CODEX_RESPONSE_SCHEMA),
        "raw_response_sha256": _json_hash(validated),
        "codex_version": codex_version,
        "command": list(command),
        "request_path": str(request_path),
        "prompt_path": str(prompt_path),
        "schema_path": str(schema_path),
        "response_path": str(response_path),
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        "audited_no_tool_use": True,
        "synthesis_labels_visible": False,
        "run_id": run_id,
        "latency_seconds": round(time.monotonic() - started, 6),
        "model_usage": _event_usage(completed.stdout),
    }
    result = _normalize_result(
        validated,
        manifest=manifest,
        variant=variant,
        mode=mode,
        model=config.codex.model,
        effort=selected_effort,
        cache_key=cache_key,
        provenance=provenance,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "failure.json").unlink(missing_ok=True)
    return CodexAnalysisBuild(result, False, output_path)
