from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.plugin_abc_measured import MeasuredRunError
from scripts.plugin_installed_acceptance import _hash, _record_failure, _runner_digest


def test_runner_error_is_preserved_before_artifact_lookup() -> None:
    payload = {
        "document_type": "rtl-advisor.workflow.error",
        "error": {"code": "top_required"},
    }
    payload["semantic_hash"] = _hash(payload)
    with pytest.raises(MeasuredRunError, match="top_required"):
        _runner_digest([{
            "command": "python3 run_rtl_advisor.py workflow prepare input.sv",
            "aggregated_output": json.dumps(payload),
            "exit_code": 2,
        }])


def test_runner_rejects_tampered_digest() -> None:
    with pytest.raises(MeasuredRunError, match="semantic hash mismatch"):
        _runner_digest([{
            "command": "python3 run_rtl_advisor.py workflow prepare input.sv",
            "aggregated_output": json.dumps({
                "document_type": "rtl-advisor.workflow.digest",
                "semantic_hash": "0" * 64,
            }),
            "exit_code": 0,
        }])


@pytest.mark.parametrize("completed", [False, True])
def test_failure_retains_authoritative_usage_only(tmp_path: Path, completed: bool) -> None:
    events = [{"type": "thread.started", "thread_id": "fresh-thread"}]
    usage = {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}
    if completed:
        events.append({"type": "turn.completed", "usage": usage})
    else:
        events.append({"type": "turn.failed", "error": {"message": "usage limit"}})
    (tmp_path / "codex-events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    record = _record_failure(tmp_path, 2.0, 1, MeasuredRunError("test failure"))
    assert record["usage_available"] is completed
    if completed:
        assert record["usage"]["input_tokens"] == 100
    else:
        assert record["usage"] is None
        assert record["terminal_event"]["message"] == "usage limit"
    assert record["thread_id"] == "fresh-thread"
    # A second write must not overwrite the first attempt's failure evidence.
    with pytest.raises(FileExistsError):
        _record_failure(tmp_path, 3.0, 1, MeasuredRunError("replacement"))
