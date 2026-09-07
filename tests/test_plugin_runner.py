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


def test_runner_accepts_compact_workflow_summary(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-summary-v1",
        "document_type": "rtl-advisor.workflow.summary",
        "workflow_id": "workflow-" + "1" * 20,
        "request_semantic_hash": "2" * 64,
        "authorization_semantic_hash": "3" * 64,
        "state_semantic_hash": "4" * 64,
        "status": "completed",
        "authorized_through": "review",
        "completed_stages": ["capabilities", "review"],
        "decision": "candidate_available",
        "safe": False,
        "limitations": [],
        "next_action": "request_candidate_authorization",
        "artifacts": {"state": "/tmp/state.json"},
        "normalized_commands": [],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)
    request = tmp_path / "request.json"
    authorization = tmp_path / "authorization.json"
    request.write_text("{}\n", encoding="utf-8")
    authorization.write_text("{}\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "workflow",
            "start",
            str(request),
            "--authorization",
            str(authorization),
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert result["document_type"] == "rtl-advisor.workflow.summary"
    assert result["next_action"] == "request_candidate_authorization"


def test_runner_accepts_and_minifies_workflow_digest(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-digest-v1",
        "document_type": "rtl-advisor.workflow.digest",
        "workflow_id": "workflow-" + "1" * 20,
        "status": "completed",
        "decision": "no_change",
        "action": "no_change",
        "scope": "whole_input",
        "source": {"kind": "generated_rtl", "path": "/tmp/top.sv", "sha256": "2" * 64},
        "source_locations": [],
        "rationale": "No eligible registered transformation was found.",
        "finding": None,
        "candidate_id": None,
        "formal_status": "not_run",
        "measurement_status": "not_run",
        "profile_results": {},
        "safe": False,
        "evidence_complete": True,
        "next_action": "none",
        "parent": {"summary_semantic_hash": "3" * 64, "state_semantic_hash": "4" * 64},
        "artifacts": {"summary": "/tmp/summary.json"},
        "reproduce": ["rtl-advisor", "agent", "workflow", "status"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)
    request = tmp_path / "request.json"
    authorization = tmp_path / "authorization.json"
    request.write_text("{}\n", encoding="utf-8")
    authorization.write_text("{}\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "workflow",
            "start",
            str(request),
            "--authorization",
            str(authorization),
            "--compact",
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["document_type"] == "rtl-advisor.workflow.digest"
    assert "\n  " not in completed.stdout


def test_runner_forwards_bounded_batch_arguments(tmp_path: Path) -> None:
    runner = _load_runner()
    manifest = tmp_path / "batch.json"
    prompt = tmp_path / "prompt.txt"
    output = tmp_path / "out"
    args = runner.build_parser().parse_args(
        [
            "workflow",
            "batch",
            str(manifest),
            "--authorized-through",
            "measure",
            "--prompt-file",
            str(prompt),
            "--first-eligible",
            "--output-dir",
            str(output),
            "--jobs",
            "2",
        ]
    )

    assert runner._operation_arguments(args) == [
        "batch",
        str(manifest.resolve()),
        "--authorized-through",
        "measure",
        "--prompt-file",
        str(prompt.resolve()),
        "--output-dir",
        str(output.resolve()),
        "--first-eligible",
        "--jobs",
        "2",
    ]


def test_runner_accepts_workflow_preparation_and_forwards_bounded_intent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this generated RTL\n", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-workflow-preparation-v1",
        "document_type": "rtl-advisor.workflow.preparation",
        "status": "prepared",
        "workflow_id": "workflow-" + "1" * 20,
        "request_semantic_hash": "2" * 64,
        "authorization_semantic_hash": "3" * 64,
        "capabilities_semantic_hash": "4" * 64,
        "authorized_through": "review",
        "artifacts": {
            "request": str(tmp_path / "request.json"),
            "authorization": str(tmp_path / "authorization.json"),
            "capabilities": str(tmp_path / "capabilities.json"),
        },
        "command": ["rtl-advisor", "agent", "workflow", "prepare"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "workflow",
            "prepare",
            str(source),
            "--input-kind",
            "generated_rtl",
            "--objective",
            "balanced",
            "--authorized-through",
            "review",
            "--prompt-file",
            str(prompt),
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

    assert completed.returncode == 0
    assert result["document_type"] == "rtl-advisor.workflow.preparation"
    assert result["status"] == "prepared"


def test_runner_accepts_compact_read_only_evidence_exploration(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-evidence-exploration-v1",
        "document_type": "rtl-advisor.evidence.exploration",
        "exploration_id": "exploration-" + "1" * 20,
        "status": "ready",
        "read_only": True,
        "dataset_semantic_hash": "2" * 64,
        "filters": {"classifications": ["regressed"]},
        "summary": {
            "measured_candidate_count": 1,
            "profile_observation_count": 1,
            "decision_counts": {"regression": 1},
            "classification_counts": {"regressed": 1},
        },
        "charts": [
            {
                "chart_id": "candidate-outcomes",
                "type": "bar",
                "rows": [{"category": "regression", "count": 1}],
            }
        ],
        "artifacts": {"dataset": str(tmp_path / "chart-data.json")},
        "command": ["rtl-advisor", "agent", "evidence", "explore"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "evidence",
            "explore",
            "--classification",
            "regressed",
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert result["document_type"] == "rtl-advisor.evidence.exploration"
    assert result["read_only"] is True
    assert result["summary"]["decision_counts"] == {"regression": 1}


def test_runner_accepts_lineage_aware_corpus_coverage(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-corpus-coverage-v1",
        "document_type": "rtl-advisor.corpus.coverage",
        "coverage_id": "coverage-" + "1" * 20,
        "status": "ready",
        "read_only": True,
        "counting_unit": "independent_design_lineage",
        "population": {
            "independent_design_lineage_count": 12,
            "variant_record_count": 8,
        },
        "breakdowns": {"tiers": []},
        "artifacts": {"coverage": str(tmp_path / "coverage.json")},
        "command": ["rtl-advisor", "agent", "corpus", "coverage"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=0)

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "corpus",
            "coverage",
            "--tier",
            "A",
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert result["counting_unit"] == "independent_design_lineage"
    assert result["population"]["independent_design_lineage_count"] == 12


def test_runner_preserves_corpus_qualification_blocker_exit_code(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "schema": "rtl-advisor-corpus-qualification-v1",
        "document_type": "rtl-advisor.corpus.qualification",
        "qualification_id": "qualification-" + "1" * 20,
        "status": "completed_with_blockers",
        "read_only": False,
        "registry_mutation": "append_only",
        "source_result_semantic_hash": "2" * 64,
        "summary": {"reference_count": 2, "blocked_count": 1},
        "artifacts": {"qualification": str(tmp_path / "qualification.json")},
        "command": ["rtl-advisor", "agent", "corpus", "qualify"],
    }
    executable = _fake_cli(tmp_path, payload, exit_code=4)
    lock = tmp_path / "lock.json"
    plan = tmp_path / "plan.json"
    lock.write_text("{}\n", encoding="utf-8")
    plan.write_text("{}\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER_PATH),
            "corpus",
            "qualify",
            str(lock),
            str(plan),
        ],
        cwd=ROOT,
        env={**os.environ, "RTL_ADVISOR_BIN": str(executable)},
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads(completed.stdout)

    assert completed.returncode == 4
    assert result["status"] == "completed_with_blockers"
    assert result["registry_mutation"] == "append_only"
