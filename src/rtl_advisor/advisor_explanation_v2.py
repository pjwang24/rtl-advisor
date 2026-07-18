from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any

from rtl_advisor.codex_analysis import _audit_events, _event_usage
from rtl_advisor.config import ProjectConfig
from rtl_advisor.tools import ToolExecutionError, first_output_line, run_command


ADVISOR_PROMPT_VERSION = "rtl-advisor-safe-explanation-v2"
ADVISOR_RESPONSE_CONTRACT_VERSION = "rtl-advisor-safe-response-v2"
ADVISOR_EFFORT = "xhigh"


class AdvisorExplanationError(RuntimeError):
    """Raised when a v2 advisor explanation violates the fixed gate contract."""


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _response_schema(analysis: dict[str, Any]) -> dict[str, Any]:
    selected_id = analysis.get("selected_candidate_id")
    selected = next(
        (
            candidate
            for candidate in analysis.get("candidates") or []
            if candidate.get("candidate_id") == selected_id
        ),
        None,
    )
    transformation = selected.get("transformation_id") if selected else None
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "decision",
            "selected_candidate_id",
            "transformation_id",
            "predicted_directions",
            "recommendation",
            "risks",
            "verification",
        ],
        "properties": {
            "summary": {"type": "string", "minLength": 1},
            "decision": {"type": "string", "enum": [analysis["decision"]]},
            "selected_candidate_id": (
                {"type": "string", "enum": [selected_id]}
                if selected_id is not None
                else {"type": "null"}
            ),
            "transformation_id": (
                {"type": "string", "enum": [transformation]}
                if transformation is not None
                else {"type": "null"}
            ),
            "predicted_directions": {
                "type": "object",
                "additionalProperties": False,
                "required": ["delay", "area", "cell_count"],
                "properties": {
                    metric: {
                        "type": "string",
                        "enum": ["improve", "degrade", "neutral", "uncertain"],
                    }
                    for metric in ("delay", "area", "cell_count")
                },
            },
            "recommendation": {"type": "string", "minLength": 1},
            "risks": {
                "type": "array",
                "maxItems": 6,
                "items": {"type": "string", "minLength": 1},
            },
            "verification": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


def _source_excerpts(analysis: dict[str, Any], input_path: Path) -> list[dict[str, Any]]:
    try:
        design = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorExplanationError(f"invalid design input artifact: {exc}") from exc
    authorized = {str(Path(item["path"]).resolve()) for item in design["files"]}
    excerpts = []
    seen: set[tuple[str, int, int]] = set()
    for candidate in analysis.get("candidates") or []:
        for location in (candidate.get("source") or {}).get("locations") or []:
            raw_path = location.get("file")
            if not raw_path:
                continue
            path = Path(str(raw_path)).expanduser().resolve()
            if str(path) not in authorized or not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            start = max(1, int(location.get("start_line", 1)) - 12)
            end = min(len(lines), int(location.get("end_line", start)) + 12)
            key = (str(path), start, end)
            if key in seen:
                continue
            seen.add(key)
            excerpts.append(
                {
                    "file": path.name,
                    "start_line": start,
                    "end_line": end,
                    "source": "\n".join(
                        f"{line_number:5d}: {lines[line_number - 1]}"
                        for line_number in range(start, end + 1)
                    ),
                }
            )
    return excerpts[:3]


def _validate_response(
    response: Any,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AdvisorExplanationError("advisor response must be an object")
    expected_keys = {
        "summary",
        "decision",
        "selected_candidate_id",
        "transformation_id",
        "predicted_directions",
        "recommendation",
        "risks",
        "verification",
    }
    if set(response) != expected_keys:
        raise AdvisorExplanationError("advisor response keys do not match schema")
    if response["decision"] != analysis["decision"]:
        raise AdvisorExplanationError("Codex attempted to override the gate decision")
    if response["selected_candidate_id"] != analysis.get("selected_candidate_id"):
        raise AdvisorExplanationError("Codex attempted to override the selected candidate")
    selected = next(
        (
            candidate
            for candidate in analysis.get("candidates") or []
            if candidate.get("candidate_id") == analysis.get("selected_candidate_id")
        ),
        None,
    )
    transformation = selected.get("transformation_id") if selected else None
    if response["transformation_id"] != transformation:
        raise AdvisorExplanationError("Codex attempted to override the transformation")
    return response


def explain_gate_decision(
    config: ProjectConfig,
    analysis: dict[str, Any],
    analysis_path: Path,
    *,
    allow_model_source: bool,
    force: bool = False,
) -> dict[str, Any]:
    if not allow_model_source:
        return {
            "status": "blocked",
            "reason": "advisor mode requires explicit --allow-model-source",
        }
    model_input = {
        "schema_version": 2,
        "gate": analysis["gate"],
        "decision": analysis["decision"],
        "selected_candidate_id": analysis.get("selected_candidate_id"),
        "profile": analysis["profile"],
        "design_features": analysis.get("features") or {},
        "candidates": analysis.get("candidates") or [],
        "source_excerpts": _source_excerpts(
            analysis, analysis_path.parent / "input.json"
        ),
    }
    schema = _response_schema(analysis)
    prompt = f"""You are the explanation layer for a safety-gated RTL advisor.
The deterministic gate decision in the JSON input is final.
Do not change the decision, selected_candidate_id, or transformation_id.
Explain the pre-synthesis structural evidence, predicted PPA directions, uncertainty,
risks, and the lint/formal checks required before adoption.
Do not claim measured synthesis results; none are provided.
Do not invoke tools, access files, or use any context outside this prompt.
Return exactly one response matching the supplied JSON Schema.

MODEL_INPUT_JSON
{json.dumps(model_input, indent=2, sort_keys=True)}
"""
    output_dir = analysis_path.parent / "advisor"
    output_path = output_dir / "explanation.json"
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    cache_key = _json_hash(
        {
            "prompt_version": ADVISOR_PROMPT_VERSION,
            "prompt_hash": prompt_hash,
            "schema_hash": _json_hash(schema),
            "analysis_hash": _json_hash(analysis),
            "model": config.codex.model,
            "effort": ADVISOR_EFFORT,
        }
    )
    if output_path.is_file() and not force:
        try:
            cached = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("cache_key") == cache_key:
            return cached

    try:
        version_result = run_command(
            (config.tools.codex, "--version"),
            timeout_seconds=config.tools.timeout_seconds,
        )
    except ToolExecutionError as exc:
        raise AdvisorExplanationError(str(exc)) from exc
    if version_result.returncode != 0:
        raise AdvisorExplanationError(version_result.stderr or version_result.stdout)
    codex_version = first_output_line(version_result) or "unknown"
    workspace = output_dir / "model_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    schema_path = workspace / "response-schema.json"
    response_path = workspace / "response.json"
    prompt_path = output_dir / "prompt.txt"
    events_path = output_dir / "events.jsonl"
    stderr_path = output_dir / "stderr.log"
    schema_path.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
        f'model_reasoning_effort="{ADVISOR_EFFORT}"',
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
        raise AdvisorExplanationError(str(exc)) from exc
    events_path.write_text(
        completed.stdout + ("\n" if completed.stdout else ""), encoding="utf-8"
    )
    stderr_path.write_text(
        completed.stderr + ("\n" if completed.stderr else ""), encoding="utf-8"
    )
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or str(completed.returncode)
        raise AdvisorExplanationError(f"Codex explanation failed: {detail}")
    try:
        _audit_events(completed.stdout)
        response = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdvisorExplanationError(f"invalid Codex explanation: {exc}") from exc
    validated = _validate_response(response, analysis)
    result = {
        "status": "completed",
        "cache_key": cache_key,
        "prompt_version": ADVISOR_PROMPT_VERSION,
        "model": config.codex.model,
        "effort": ADVISOR_EFFORT,
        "latency_seconds": round(time.monotonic() - started, 6),
        "usage": _event_usage(completed.stdout),
        "source_shared": True,
        "codex_version": codex_version,
        "response": validated,
        "artifacts": {
            "prompt": str(prompt_path),
            "schema": str(schema_path),
            "events": str(events_path),
            "stderr": str(stderr_path),
        },
    }
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result
