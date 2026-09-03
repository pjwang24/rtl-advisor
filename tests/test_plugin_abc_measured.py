from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.plugin_abc_measured import (
    MeasuredRunError,
    _event_diagnostics,
    _parse_events,
    _phase7_candidate_config,
    _phase7_source_pin,
    _terminal_failure,
    _write_attempt_failure,
    _write_telemetry,
)


def test_parse_events_reads_authoritative_usage_and_thread(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 60,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                        },
                    }
                ),
            )
        ),
        encoding="utf-8",
    )

    usage, thread_id = _parse_events(events)

    assert thread_id == "thread-1"
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cached_input_tokens"] == 60


def test_event_diagnostics_counts_parallel_command_waves_and_output(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"type": "item.started", "item": {"id": "a", "type": "command_execution", "command": "jq . a.json"}},
                {"type": "item.started", "item": {"id": "b", "type": "command_execution", "command": "shasum -a 256 x"}},
                {"type": "item.completed", "item": {"id": "a", "type": "command_execution", "command": "jq . a.json", "aggregated_output": "{}"}},
                {"type": "item.completed", "item": {"id": "b", "type": "command_execution", "command": "shasum -a 256 x", "aggregated_output": "hash"}},
                {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "done"}},
            )
        ),
        encoding="utf-8",
    )

    result = _event_diagnostics(events)

    assert result == {
        "command_count": 2,
        "command_wave_count": 1,
        "command_categories": {"artifact_read": 1, "hash": 1},
        "command_output_bytes": 6,
        "agent_message_bytes": 4,
        "output_bytes": 10,
    }


def test_phase7_plugin_arm_is_source_pinned_without_user_config() -> None:
    arguments, provenance = _phase7_source_pin("B", "phase7")

    assert arguments[:2] == ["--ignore-user-config", "--strict-config"]
    assert arguments[2] == "--config"
    assert "plugins/rtl-advisor/skills/analyze-rtl" in arguments[3]
    assert "use `--jobs 4`" in arguments[3]
    assert provenance["skill_source"] == "workspace_path"
    assert provenance["skill_path"].endswith(
        "plugins/rtl-advisor/skills/analyze-rtl"
    )
    assert len(provenance["skill_sha256"]) == 64


def test_phase6_plugin_arm_keeps_installed_plugin_route() -> None:
    arguments, provenance = _phase7_source_pin("B", "phase6")

    assert arguments == []
    assert provenance == {}


def test_phase7_candidate_config_uses_shared_isolated_artifacts(
    tmp_path: Path,
) -> None:
    config_path = _phase7_candidate_config(tmp_path)
    content = config_path.read_text(encoding="utf-8")

    assert str(tmp_path.resolve()) in content
    assert 'release-candidate-jobs4-v1/artifacts"' in content
    assert f'corpus_dir = "{(Path(__file__).resolve().parents[1] / "corpus").resolve()}"' in content


def test_telemetry_is_append_only_by_arm_and_repetition(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.json"
    record = {
        "arm": "A",
        "repetition": 1,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "wall_time_seconds": 1.0,
        "task_completed": True,
    }

    _write_telemetry(path, record)

    with pytest.raises(MeasuredRunError, match="already contains"):
        _write_telemetry(path, record)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["runs"] == [record]


def test_failed_attempt_does_not_prevent_a_successful_retry(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.json"
    failed = {
        "arm": "B",
        "repetition": 1,
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
        "wall_time_seconds": 1.0,
        "task_completed": False,
    }
    passed = {**failed, "input_tokens": 20, "total_tokens": 22, "task_completed": True}

    _write_telemetry(path, failed)
    _write_telemetry(path, passed)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [item["task_completed"] for item in payload["runs"]] == [False, True]


def test_excluded_success_does_not_prevent_a_corrected_retry(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.json"
    excluded = {
        "arm": "B",
        "repetition": 2,
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
        "wall_time_seconds": 1.0,
        "task_completed": True,
        "admitted_to_evaluation": False,
        "exclusion_reason": "deterministic batch replay defect",
    }
    corrected = {
        **excluded,
        "admitted_to_evaluation": True,
        "input_tokens": 20,
        "total_tokens": 22,
    }
    corrected.pop("exclusion_reason")

    _write_telemetry(path, excluded)
    _write_telemetry(path, corrected)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [item.get("admitted_to_evaluation", True) for item in payload["runs"]] == [
        False,
        True,
    ]


def test_parse_events_rejects_missing_completion(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        encoding="utf-8",
    )

    with pytest.raises(MeasuredRunError, match="no completed turn"):
        _parse_events(events)


def test_failed_turn_is_preserved_without_fabricated_usage(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    errors = tmp_path / "stderr.log"
    failure = tmp_path / "failure.json"
    events.write_text(
        "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {"message": "usage limit reached"},
                    }
                ),
            )
        ),
        encoding="utf-8",
    )
    errors.write_text("", encoding="utf-8")

    assert _terminal_failure(events) == {
        "event_type": "turn.failed",
        "message": "usage limit reached",
    }
    _write_attempt_failure(
        failure,
        arm="A",
        repetition=1,
        attempt=2,
        elapsed=3.0,
        exit_code=1,
        events_path=events,
        stderr_path=errors,
        parse_error="Codex JSONL has no completed turn with usage",
    )

    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert payload["usage_available"] is False
    assert "input_tokens" not in payload
    assert payload["thread_id"] == "thread-1"
    assert payload["terminal_event"]["message"] == "usage limit reached"


def test_codex_transport_schema_is_strict_and_typed() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (
            root
            / "schemas/rtl-advisor-plugin-abc-codex-output-v1.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema"]["type"] == "string"
    case = schema["properties"]["cases"]["items"]
    assert set(case["required"]) == set(case["properties"])
