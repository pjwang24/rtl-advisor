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
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": "rtl-advisor.agent.v2.capabilities",
        "flow_version": "rtl-advisor-agent-v2",
        "status": "ok",
        "command": [
            "rtl-advisor",
            "agent",
            "capabilities",
            "--schema-version",
            "2",
            "--json",
        ],
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
    assert result["document_type"] == "rtl-advisor.agent.v2.capabilities"
    assert result["semantic_hash"] == _semantic_hash(
        {key: value for key, value in result.items() if key != "semantic_hash"}
    )


def test_runner_preserves_failed_formal_exit_code(tmp_path: Path) -> None:
    payload = {
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": "rtl-advisor.agent.v2.verification",
        "flow_version": "rtl-advisor-agent-v2",
        "status": "formal_failed",
        "decision": "formal_failed",
        "command": [
            "rtl-advisor",
            "agent",
            "verify",
            "mvp-00000000000000000000",
            "--candidate",
            "cand-1",
            "--schema-version",
            "2",
            "--json",
        ],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=4)
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "verify",
            "mvp-00000000000000000000",
            "--candidate",
            "cand-1",
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 4
    assert result["status"] == "formal_failed"
    assert result["decision"] == "formal_failed"


def test_runner_rejects_semantic_hash_mismatch() -> None:
    runner = _load_runner()
    payload = {
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": "rtl-advisor.agent.v2.capabilities",
        "flow_version": "rtl-advisor-agent-v2",
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


def test_runner_supports_installed_cli_outside_source_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = _load_runner()
    payload = {
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": "rtl-advisor.agent.v2.capabilities",
        "flow_version": "rtl-advisor-agent-v2",
        "status": "ok",
        "command": [
            "rtl-advisor",
            "agent",
            "capabilities",
            "--schema-version",
            "2",
            "--json",
        ],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)
    config = tmp_path / "rtl-advisor.toml"
    config.write_text("[project]\n", encoding="utf-8")
    workspace = tmp_path / "engineer-workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(runner, "_find_repo_root", lambda: None)
    monkeypatch.setenv("RTL_ADVISOR_BIN", str(executable))

    args = runner.build_parser().parse_args(
        ("--config", str(config), "capabilities")
    )
    result, exit_code = runner.run(args)

    assert exit_code == 0
    assert result["flow_version"] == "rtl-advisor-agent-v2"


def test_runner_requires_explicit_config_outside_source_checkout(
    monkeypatch,
) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "_find_repo_root", lambda: None)
    monkeypatch.delenv("RTL_ADVISOR_CONFIG", raising=False)
    args = runner.build_parser().parse_args(("capabilities",))

    try:
        runner.run(args)
    except runner.RunnerError as exc:
        assert exc.code == "config_not_found"
    else:
        raise AssertionError("runner accepted an implicit config outside a checkout")
