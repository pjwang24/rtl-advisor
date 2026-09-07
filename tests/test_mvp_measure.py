from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import rtl_advisor.mvp_measure as mvp_measure
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.mvp_measure import (
    MVPMeasurementError,
    aggregate_measurements,
    classify_recipe,
    measure_candidate,
)
from rtl_advisor.mvp_schema import compile_context_snapshot, stable_hash
from rtl_advisor.rtl_input import DesignInputV2, normalize_design_input
from rtl_advisor.tools import sha256_file


def _metrics(delay: float, area: float, cells: int = 100) -> dict[str, float | int]:
    return {
        "critical_delay_ps": delay,
        "area_total": area,
        "cell_count": cells,
    }


@pytest.mark.parametrize(
    ("objective", "candidate", "expected"),
    (
        ("timing", _metrics(97.0, 110.0), "improved"),
        ("timing", _metrics(97.0, 110.0001), "regressed"),
        ("timing", _metrics(103.0, 100.0), "regressed"),
        ("timing", _metrics(97.0001, 100.0), "neutral"),
        ("area", _metrics(102.0, 95.0), "improved"),
        ("area", _metrics(102.0001, 95.0), "regressed"),
        ("area", _metrics(100.0, 105.0), "regressed"),
        ("area", _metrics(100.0, 104.9999), "neutral"),
        ("balanced", _metrics(97.0, 110.0), "improved"),
        ("balanced", _metrics(102.0, 95.0), "improved"),
        ("balanced", _metrics(100.0, 110.0), "neutral"),
        ("balanced", _metrics(102.0001, 95.0), "regressed"),
        ("balanced", _metrics(100.0, 110.0001), "regressed"),
        ("balanced", _metrics(100.0, 100.0), "neutral"),
    ),
)
def test_classify_recipe_uses_frozen_threshold_boundaries(
    objective: str,
    candidate: dict[str, float | int],
    expected: str,
) -> None:
    assert classify_recipe(objective, _metrics(100.0, 100.0), candidate) == expected


@pytest.mark.parametrize(
    ("standard", "stronger", "expected"),
    (
        ("improved", "improved", "measured_improvement"),
        ("neutral", "neutral", "synthesis_handles"),
        ("improved", "neutral", "flow_dependent"),
        ("neutral", "improved", "flow_dependent"),
        ("regressed", "improved", "regression"),
        ("neutral", "regressed", "regression"),
        ("regressed", "regressed", "regression"),
    ),
)
def test_aggregate_measurements_covers_every_outcome(
    standard: str,
    stronger: str,
    expected: str,
) -> None:
    assert aggregate_measurements(standard, stronger) == expected


def _config(tmp_path: Path) -> ProjectConfig:
    liberty = tmp_path / "cells.lib"
    liberty.write_text("library(test) {}\n", encoding="utf-8")
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys="yosys",
            codex="codex",
            timeout_seconds=10,
        ),
        synthesis=SynthesisConfig(
            driving_cell="BUF_X1",
            output_load_ff=10.0,
        ),
        liberty=LibertyConfig(
            name="test",
            path=liberty,
            url="https://example.invalid/cells.lib",
            sha256=sha256_file(liberty),
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="test-commit",
        ),
    )


def _design_pair(tmp_path: Path) -> tuple[DesignInputV2, DesignInputV2]:
    includes = tmp_path / "include"
    includes.mkdir()
    (includes / "width.svh").write_text("`define WIDTH 8\n", encoding="utf-8")
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    baseline_dir.mkdir()
    candidate_dir.mkdir()
    shared_text = "module helper(input logic a, output logic y); assign y = a; endmodule\n"
    (baseline_dir / "helper.sv").write_text(shared_text, encoding="utf-8")
    (candidate_dir / "helper.sv").write_text(shared_text, encoding="utf-8")
    (baseline_dir / "top.sv").write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);\n"
        "  assign y = ((a + b) + c) + d;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (candidate_dir / "top.sv").write_text(
        "module top(input logic [7:0] a,b,c,d, output logic [7:0] y);\n"
        "  assign y = (a + b) + (c + d);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    common = {
        "top": "top",
        "include_dirs": [includes],
        "defines": ["MVP_TEST=1"],
        "base": tmp_path,
    }
    baseline = normalize_design_input(
        files=[baseline_dir / "helper.sv", baseline_dir / "top.sv"],
        **common,
    )
    candidate = normalize_design_input(
        files=[candidate_dir / "helper.sv", candidate_dir / "top.sv"],
        **common,
    )
    return baseline, candidate


def _verification(
    baseline: DesignInputV2,
    candidate: DesignInputV2,
    *,
    baseline_hash: str | None = None,
    candidate_hash: str | None = None,
) -> dict:
    formal_core = {"status": "passed", "backend": "yosys-equivalence"}
    formal = {
        **formal_core,
        "proof_semantic_hash": stable_hash(formal_core),
    }
    record_core = {
        "status": "formal_passed",
        "safe": True,
        "formal": formal,
        "baseline": {
            "design_hash": baseline_hash or baseline.design_hash,
        },
        "candidate": {
            "candidate_design_hash": candidate_hash or candidate.design_hash,
        },
        "compile_context": {
            "baseline": compile_context_snapshot(baseline),
            "candidate": compile_context_snapshot(candidate),
        },
    }
    return {**record_core, "semantic_hash": stable_hash(record_core)}


def _fake_environment(config: ProjectConfig) -> dict[str, str]:
    return {
        "yosys_version": "Yosys 0.63 (test)",
        "yosys_path": "/test/yosys",
        "yosys_sha256": "1" * 64,
        "abc_version": "UC Berkeley, ABC 1.01 (test)",
        "abc_version_token": "1.01",
        "abc_path": "/test/yosys-abc",
        "abc_sha256": "2" * 64,
        "liberty_path": str(config.liberty.path),
        "liberty_name": config.liberty.name,
        "liberty_sha256": config.liberty.sha256,
        "liberty_source_commit": config.liberty.source_commit,
    }


def _fake_result(
    config: ProjectConfig,
    design: DesignInputV2,
    environment: dict,
    *,
    profile: str,
    role: str,
    profile_root: Path,
    recipe: dict,
) -> dict:
    constraints_path = profile_root / "abc.constr"
    output_dir = profile_root / role
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "netlist": output_dir / "mapped.v",
        "script": output_dir / "synthesis.ys",
        "log": output_dir / "synthesis.log",
        "stat": output_dir / "stat.json",
    }
    for label, path in artifacts.items():
        path.write_text(f"{label}\n", encoding="utf-8")
    baseline = role == "baseline"
    delay = 100.0 if baseline else (96.0 if profile == "standard" else 95.0)
    area = 100.0 if baseline else 99.0
    cells = 100 if baseline else 96
    return {
        "status": "passed",
        "role": role,
        "design_hash": design.design_hash,
        "metrics": _metrics(delay, area, cells),
        "netlist": {
            "path": str(artifacts["netlist"]),
            "sha256": sha256_file(artifacts["netlist"]),
        },
        "constraints": {
            "driving_cell": config.synthesis.driving_cell,
            "output_load_ff": config.synthesis.output_load_ff,
            "sha256": sha256_file(constraints_path),
        },
        "provenance": {
            "recipe_hash": recipe["recipe_hash"],
            "yosys_version": environment["yosys_version"],
            "yosys_path": environment["yosys_path"],
            "yosys_sha256": environment["yosys_sha256"],
            "abc_version": environment["abc_version"],
            "abc_path": environment["abc_path"],
            "abc_sha256": environment["abc_sha256"],
            "liberty_sha256": environment["liberty_sha256"],
            "constraints_path": str(constraints_path),
            "script_path": str(artifacts["script"]),
            "script_sha256": sha256_file(artifacts["script"]),
            "log_path": str(artifacts["log"]),
            "log_sha256": sha256_file(artifacts["log"]),
            "stat_path": str(artifacts["stat"]),
            "stat_sha256": sha256_file(artifacts["stat"]),
            "warnings": {"count": 0, "sha256": stable_hash([])},
        },
    }


def test_measure_candidate_requires_hash_matched_formal_proof(tmp_path: Path) -> None:
    baseline, candidate = _design_pair(tmp_path)
    verification = _verification(
        baseline,
        candidate,
        baseline_hash="0" * 64,
    )

    with pytest.raises(MVPMeasurementError) as error:
        measure_candidate(
            _config(tmp_path),
            baseline,
            candidate,
            verification,
            tmp_path / "run",
        )

    assert error.value.code == "stale_formal_proof"
    assert "baseline design hash" in str(error.value)


def test_measure_candidate_rejects_source_changed_after_formal(tmp_path: Path) -> None:
    baseline, candidate = _design_pair(tmp_path)
    verification = _verification(baseline, candidate)
    Path(candidate.files[-1].path).write_text(
        "module top; wire stale = 1'b1; endmodule\n",
        encoding="utf-8",
    )

    with pytest.raises(MVPMeasurementError) as error:
        measure_candidate(
            _config(tmp_path),
            baseline,
            candidate,
            verification,
            tmp_path / "run",
        )

    assert error.value.code == "stale_source_hashes"


def test_measure_candidate_preserves_recipe_and_constraint_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, candidate = _design_pair(tmp_path)
    config = _config(tmp_path)
    verification = _verification(baseline, candidate)
    calls: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(mvp_measure, "_environment", _fake_environment)

    def fake_run(
        config_arg,
        design,
        *,
        profile,
        role,
        profile_root,
        environment,
        recipe,
    ):
        constraints_hash = sha256_file(profile_root / "abc.constr")
        calls.append((profile, role, recipe["recipe_hash"], constraints_hash))
        return _fake_result(
            config_arg,
            design,
            environment,
            profile=profile,
            role=role,
            profile_root=profile_root,
            recipe=recipe,
        )

    monkeypatch.setattr(mvp_measure, "_run_synthesis", fake_run)

    result = measure_candidate(
        config,
        baseline,
        candidate,
        verification,
        tmp_path / "run",
        objective="timing",
    )

    assert result["decision"] == "measured_improvement"
    assert result["status"] == "measured_improvement"
    assert result["objective"] == "timing"
    assert result["source_integrity"]["baseline"]["ok"] is True
    assert result["source_integrity"]["candidate"]["ok"] is True
    assert set(result["profiles"]) == {"standard", "stronger"}
    assert result["measurements"] == result["profiles"]
    assert Path(result["artifacts"]["measurement"]).is_file()
    core = {key: value for key, value in result.items() if key != "semantic_hash"}
    assert result["semantic_hash"] == stable_hash(core)
    for profile in ("standard", "stronger"):
        pair = [call for call in calls if call[0] == profile]
        assert [call[1] for call in pair] == ["baseline", "candidate"]
        assert pair[0][2] == pair[1][2]
        assert pair[0][3] == pair[1][3]


def test_measure_candidate_fails_if_recipe_results_break_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline, candidate = _design_pair(tmp_path)
    config = _config(tmp_path)
    verification = _verification(baseline, candidate)
    monkeypatch.setattr(mvp_measure, "_environment", _fake_environment)

    def fake_run(
        config_arg,
        design,
        *,
        profile,
        role,
        profile_root,
        environment,
        recipe,
    ):
        result = _fake_result(
            config_arg,
            design,
            environment,
            profile=profile,
            role=role,
            profile_root=profile_root,
            recipe=recipe,
        )
        if profile == "standard" and role == "candidate":
            result["constraints"]["sha256"] = "f" * 64
        return result

    monkeypatch.setattr(mvp_measure, "_run_synthesis", fake_run)

    with pytest.raises(MVPMeasurementError) as error:
        measure_candidate(
            config,
            baseline,
            candidate,
            verification,
            tmp_path / "run",
        )

    assert error.value.code == "recipe_parity_failed"


def test_multi_file_script_preserves_top_include_and_define_context(
    tmp_path: Path,
) -> None:
    design, _ = _design_pair(tmp_path)
    script = mvp_measure._synthesis_script(
        design,
        profile="stronger",
        liberty=tmp_path / "cells.lib",
        abc_executable=tmp_path / "yosys-abc",
        constraints=tmp_path / "abc.constr",
        stat_json=tmp_path / "stat.json",
        netlist=tmp_path / "mapped.v",
    )

    assert f"hierarchy -check -top {design.top}" in script
    assert f'-I"{design.include_dirs[0]}"' in script
    assert '-D"MVP_TEST=1"' in script
    assert f'-exe "{tmp_path / "yosys-abc"}"' in script
    assert all(f'"{source.path}"' in script for source in design.files)
    assert "share -aggressive" in script
    assert script.count("read_verilog -sv") == 1


def test_toolchain_identity_pins_adjacent_abc_101_and_records_provenance(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    yosys = tool_dir / "yosys"
    yosys.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-V\" ]; then echo 'Yosys 0.63 (test)'; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    abc = tool_dir / "yosys-abc"
    abc.write_text(
        "#!/bin/sh\necho 'UC Berkeley, ABC 1.01 (test)'\nexit 0\n",
        encoding="utf-8",
    )
    yosys.chmod(0o755)
    abc.chmod(0o755)
    config = _config(tmp_path)
    config = replace(config, tools=replace(config.tools, yosys=str(yosys)))

    identity = mvp_measure._toolchain_identity(config)
    provenance = mvp_measure._abc_provenance(
        'ABC: ======== ABC command line "source script"\n', identity
    )

    assert identity["yosys_version"].startswith("Yosys 0.63")
    assert identity["abc_version_token"] == "1.01"
    assert identity["abc_path"] == str(abc)
    assert identity["abc_sha256"] == sha256_file(abc)
    assert provenance["executable"] == str(abc)
    assert provenance["sha256"] == sha256_file(abc)
    assert "command line" in provenance["command_line"]


@pytest.mark.parametrize(
    ("reported_version", "accepted"),
    (
        ("Yosys 0.63 (release)", True),
        ("Yosys 0.63+49 (pinned daily build)", True),
        ("Yosys 0.62+119 (older release line)", False),
        ("Yosys 0.64 (newer release line)", False),
    ),
)
def test_yosys_identity_accepts_only_the_pinned_063_release_line(
    tmp_path: Path,
    reported_version: str,
    accepted: bool,
) -> None:
    yosys = tmp_path / "yosys"
    yosys.write_text(
        f"#!/bin/sh\necho '{reported_version}'\n",
        encoding="utf-8",
    )
    yosys.chmod(0o755)
    config = _config(tmp_path)
    config = replace(config, tools=replace(config.tools, yosys=str(yosys)))

    if accepted:
        identity = mvp_measure._yosys_identity(config)
        assert identity["yosys_version"] == reported_version
        assert identity["yosys_sha256"] == sha256_file(yosys)
    else:
        with pytest.raises(MVPMeasurementError) as error:
            mvp_measure._yosys_identity(config)
        assert error.value.code == "unsupported_yosys_version"


def test_classification_rejects_invalid_or_zero_metrics() -> None:
    with pytest.raises(MVPMeasurementError, match="unsupported objective"):
        classify_recipe("power", _metrics(100.0, 100.0), _metrics(90.0, 90.0))


def test_measurement_never_classifies_if_compile_context_changes_mid_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, candidate = _design_pair(tmp_path)
    config = _config(tmp_path)
    verification = _verification(baseline, candidate)
    monkeypatch.setattr(mvp_measure, "_environment", _fake_environment)

    def fake_run(
        config_arg,
        design,
        *,
        profile,
        role,
        profile_root,
        environment,
        recipe,
    ):
        result = _fake_result(
            config_arg,
            design,
            environment,
            profile=profile,
            role=role,
            profile_root=profile_root,
            recipe=recipe,
        )
        if profile == "stronger" and role == "candidate":
            Path(candidate.include_dirs[0], "width.svh").write_text(
                "`define WIDTH 9\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(mvp_measure, "_run_synthesis", fake_run)

    with pytest.raises(MVPMeasurementError) as error:
        measure_candidate(
            config,
            baseline,
            candidate,
            verification,
            tmp_path / "run",
        )

    assert error.value.code == "flow_invalidated"
    records = list((tmp_path / "run").rglob("flow-invalidated-*.json"))
    assert len(records) == 1
    assert '"status": "flow_invalidated"' in records[0].read_text(encoding="utf-8")


def test_measurement_never_classifies_if_evidence_artifact_changes_mid_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, candidate = _design_pair(tmp_path)
    config = _config(tmp_path)
    verification = _verification(baseline, candidate)
    monkeypatch.setattr(mvp_measure, "_environment", _fake_environment)

    def fake_run(
        config_arg,
        design,
        *,
        profile,
        role,
        profile_root,
        environment,
        recipe,
    ):
        result = _fake_result(
            config_arg,
            design,
            environment,
            profile=profile,
            role=role,
            profile_root=profile_root,
            recipe=recipe,
        )
        if profile == "stronger" and role == "candidate":
            stale_log = profile_root.parent / "standard" / "baseline" / "synthesis.log"
            stale_log.write_text("tampered\n", encoding="utf-8")
        return result

    monkeypatch.setattr(mvp_measure, "_run_synthesis", fake_run)

    with pytest.raises(MVPMeasurementError) as error:
        measure_candidate(
            config,
            baseline,
            candidate,
            verification,
            tmp_path / "run",
        )

    assert error.value.code == "flow_invalidated"


def test_measurement_never_classifies_if_tool_identity_changes_mid_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, candidate = _design_pair(tmp_path)
    config = _config(tmp_path)
    verification = _verification(baseline, candidate)
    environment_calls = 0

    def changing_environment(config_arg):
        nonlocal environment_calls
        environment_calls += 1
        result = _fake_environment(config_arg)
        if environment_calls > 1:
            result["yosys_sha256"] = "2" * 64
        return result

    monkeypatch.setattr(mvp_measure, "_environment", changing_environment)

    def fake_run(
        config_arg,
        design,
        *,
        profile,
        role,
        profile_root,
        environment,
        recipe,
    ):
        return _fake_result(
            config_arg,
            design,
            environment,
            profile=profile,
            role=role,
            profile_root=profile_root,
            recipe=recipe,
        )

    monkeypatch.setattr(mvp_measure, "_run_synthesis", fake_run)

    with pytest.raises(MVPMeasurementError) as error:
        measure_candidate(
            config,
            baseline,
            candidate,
            verification,
            tmp_path / "run",
        )

    assert error.value.code == "flow_invalidated"


def test_measurement_rejects_control_characters_in_script_arguments(
    tmp_path: Path,
) -> None:
    design, _ = _design_pair(tmp_path)

    with pytest.raises(MVPMeasurementError) as error:
        mvp_measure._yosys_quote("unsafe\nread_verilog attacker.sv")

    assert error.value.code == "unsafe_compile_context"
    with pytest.raises(MVPMeasurementError, match="baseline delay must be positive"):
        classify_recipe("timing", _metrics(0.0, 100.0), _metrics(0.0, 90.0))
    with pytest.raises(MVPMeasurementError, match="classification"):
        aggregate_measurements("unknown", "neutral")
