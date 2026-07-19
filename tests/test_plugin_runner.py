from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = (
    ROOT
    / "plugins/rtl-advisor/skills/analyze-rtl/scripts/run_rtl_advisor.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("rtl_advisor_plugin_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _semantic_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fake_cli(tmp_path: Path, payload: dict, *, exit_code: int) -> Path:
    executable = tmp_path / "fake-rtl-advisor"
    content = dict(payload)
    content["semantic_hash"] = _semantic_hash(content)
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        f"print(json.dumps({content!r}))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_runner_validates_and_returns_capabilities(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.agent.capabilities",
        "flow_version": "rtl-advisor-agent-v1",
        "status": "ok",
        "command": ["rtl-advisor", "agent", "capabilities", "--json"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)
    completed = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "capabilities"],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert result["document_type"] == "rtl-advisor.agent.capabilities"
    assert result["semantic_hash"] == _semantic_hash(
        {key: value for key, value in result.items() if key != "semantic_hash"}
    )


def test_runner_preserves_blocked_review_exit_code(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.agent.review",
        "flow_version": "rtl-advisor-agent-v1",
        "status": "blocked",
        "decision": "failed",
        "command": ["rtl-advisor", "agent", "review", "--json"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=3)
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "review",
            "top.sv",
            "--top",
            "top",
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 3
    assert result["status"] == "blocked"
    assert result["decision"] == "failed"


def test_runner_rejects_semantic_hash_mismatch() -> None:
    runner = _load_runner()
    payload = {
        "schema_version": 1,
        "document_type": "rtl-advisor.agent.capabilities",
        "status": "ok",
        "command": [],
        "semantic_hash": "wrong",
    }

    try:
        runner._validate_payload(payload, "capabilities")
    except runner.RunnerError as exc:
        assert exc.code == "semantic_hash_mismatch"
    else:
        raise AssertionError("semantic hash mismatch was accepted")
