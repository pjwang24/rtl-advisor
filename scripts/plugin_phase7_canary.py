#!/usr/bin/env python3
"""Verify a fresh Codex session receives the exact Phase 7 workspace skill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.plugin_abc_measured import (
    MeasuredRunError,
    _event_diagnostics,
    _parse_events,
    _phase7_source_pin,
)


def run_canary(*, codex_bin: str = "codex") -> dict[str, Any]:
    arguments, provenance = _phase7_source_pin("B", "phase7")
    root = ROOT / "experiments/plugin-abc-v1/instrumented/phase7/source-pin-canary"
    attempt = 1
    while (root / f"attempt-{attempt:03d}").exists():
        attempt += 1
    output = root / f"attempt-{attempt:03d}"
    output.mkdir(parents=True)
    result_path = output / "result.json"
    events_path = output / "codex-events.jsonl"
    stderr_path = output / "codex-stderr.log"
    schema_path = ROOT / "schemas/rtl-advisor-plugin-source-pin-canary-v1.schema.json"
    command = [
        codex_bin,
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="xhigh"',
        *arguments,
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
        (
            "Do not run tools. Return the requested JSON. source_pin_ok is true only "
            "if your developer instructions say compact workflows perform capability "
            "discovery internally. batch_supported is true only if those instructions "
            "document workflow batch. Use skill_name analyze-rtl."
        ),
    ]
    started = time.monotonic()
    with events_path.open("w", encoding="utf-8") as events, stderr_path.open(
        "w", encoding="utf-8"
    ) as errors:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=events,
            stderr=errors,
            check=False,
        )
    elapsed = round(time.monotonic() - started, 3)
    usage, thread_id = _parse_events(events_path)
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasuredRunError(f"source-pin canary returned no valid result: {exc}") from exc
    passed = completed.returncode == 0 and result == {
        "source_pin_ok": True,
        "batch_supported": True,
        "skill_name": "analyze-rtl",
    }
    record = {
        "schema_version": 1,
        "schema": "rtl-advisor-plugin-source-pin-canary-result-v1",
        "document_type": "rtl-advisor.plugin-source-pin-canary",
        "status": "passed" if passed else "failed",
        "attempt": attempt,
        "wall_time_seconds": elapsed,
        "thread_id": thread_id,
        "usage": usage,
        "result": result,
        "provenance": provenance,
        "diagnostics": _event_diagnostics(events_path),
        "artifacts": {
            "events": str(events_path),
            "stderr": str(stderr_path),
            "result": str(result_path),
        },
    }
    record_path = output / "canary.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["artifacts"]["record"] = str(record_path)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="codex")
    args = parser.parse_args(argv)
    try:
        result = run_canary(codex_bin=args.codex_bin)
    except MeasuredRunError as exc:
        print(f"Phase 7 source-pin canary failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
