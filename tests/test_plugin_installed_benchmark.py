import json
from pathlib import Path

import pytest

from scripts import plugin_installed_benchmark as installed_benchmark
from scripts.plugin_abc_measured import (
    MeasuredRunError,
    _instrumented_packet,
    _phase7_candidate_config,
    _phase7_source_pin,
    _prompt,
    run_measured,
)
from scripts.plugin_installed_benchmark import (
    _compact_batch_digest,
    _installed_batch_commands,
    snapshot_artifacts,
)


def test_installed_mode_does_not_inject_workspace_skill() -> None:
    assert _phase7_source_pin("B", "installed-acceptance") == ([], {})
    prompt = _prompt(packet_path=Path("packet"), output_root=Path("run"),
                     result_path=Path("result"), arm="B", phase="installed-acceptance",
                     candidate_config=Path("isolated.toml"))
    assert "installed rtl-advisor:analyze-rtl" in prompt
    assert "--jobs 4" in prompt
    assert "formal verification" in prompt
    assert "--config isolated.toml" in prompt
    assert "exactly once" in prompt
    assert "Do not open cli-contract.md" in prompt
    assert "source-pinned workspace" not in prompt


def test_runner_source_inspection_is_not_counted_as_execution() -> None:
    runner = "/installed/analyze-rtl/scripts/run_rtl_advisor.py"
    commands = [
        {"command": f"sed -n '1,20p' {runner}"},
        {"command": f"python3 {runner} --config isolated.toml workflow batch manifest.json"},
    ]
    assert _installed_batch_commands(commands, runner) == [commands[1]]


def test_compact_digest_rejects_full_artifact_payloads() -> None:
    compact = {
        "document_type": "rtl-advisor.workflow.batch-summary",
        "status": "completed",
        "items": [{"item_id": f"g{index:02d}"} for index in range(1, 25)],
    }
    command = {"aggregated_output": json.dumps(compact)}
    assert _compact_batch_digest(command)
    compact["items"][0]["source_text"] = "module generated; endmodule"
    command["aggregated_output"] = json.dumps(compact)
    assert not _compact_batch_digest(command)


def test_candidate_config_supports_separate_cold_replay_root(tmp_path: Path) -> None:
    root = tmp_path / "installed-acceptance/v1/candidate"
    first = _phase7_candidate_config(tmp_path, root=root)
    before = first.read_bytes()
    assert _phase7_candidate_config(tmp_path, root=root).read_bytes() == before
    assert str(root / "artifacts") in before.decode()
    assert not (tmp_path / "instrumented/phase7").exists()


def test_instrumented_packet_replaces_conflicting_canonical_write_rule(
    tmp_path: Path,
) -> None:
    original = {
        "cases": [{"case_id": "g01"}],
        "isolation_rules": ["write only under runs/arm-a/r2"],
    }
    effective = json.loads(_instrumented_packet(original, tmp_path).read_text())
    assert original["isolation_rules"] == ["write only under runs/arm-a/r2"]
    assert effective["cases"] == original["cases"]
    assert any("Do not read or write" in rule for rule in effective["isolation_rules"])
    assert not any("runs/arm-a/r2" in rule for rule in effective["isolation_rules"])


def test_replay_snapshot_detects_changed_proofs_but_allows_pointer_progress(tmp_path: Path) -> None:
    (tmp_path / "verification.json").write_text("proof")
    (tmp_path / "state-latest.json").write_text("pointer")
    before = snapshot_artifacts(tmp_path)
    (tmp_path / "state-latest.json").write_text("new pointer")
    assert snapshot_artifacts(tmp_path) == before
    (tmp_path / "verification.json").write_text("changed proof")
    assert snapshot_artifacts(tmp_path) != before


def test_identity_tracks_canonical_run_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = tmp_path / "experiment"
    source = tmp_path / "case.sv"
    source.write_text("module case_top; endmodule\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/plugin_abc_measured.py").write_text("# harness\n")
    (tmp_path / "rtl-advisor.toml").write_text("# config\n")
    (experiment / "runs").mkdir(parents=True)
    (experiment / "runs/evidence.json").write_text("before\n")
    (experiment / "manifest.json").write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "g01",
                        "sources": ["case.sv"],
                        "source_sha256": installed_benchmark._sha256(source),
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(installed_benchmark, "ROOT", tmp_path)
    before = installed_benchmark._identity(experiment)
    (experiment / "runs/evidence.json").write_text("after\n")
    after = installed_benchmark._identity(experiment)
    assert before["canonical_run_tree_sha256"] != after["canonical_run_tree_sha256"]


@pytest.mark.parametrize("series", ["../escape", "a/b", ""])
def test_unsafe_series_rejected_before_launch(tmp_path: Path, series: str) -> None:
    with pytest.raises(MeasuredRunError, match="safe directory"):
        run_measured(arm="A", repetition=1, phase="installed-acceptance",
                     experiment=tmp_path, series=series)
    assert not list(tmp_path.iterdir())
