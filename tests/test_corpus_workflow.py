from __future__ import annotations

import json
from pathlib import Path

import rtl_advisor.corpus_workflow as corpus_workflow
from rtl_advisor.cli import _normalized_agent_command, build_parser
from rtl_advisor.config import (
    LibertyConfig,
    ProjectConfig,
    SynthesisConfig,
    ToolConfig,
)
from rtl_advisor.corpus_registry import CorpusRegistryV1
from rtl_advisor.mvp_schema import read_hashed_json


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "examples/corpus/opentitan_arbiter_pair"
TRANCHE_LOCK = ROOT / "examples/corpus/wave2_tier_a/tranche.lock.json"


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator="verilator",
            yosys="yosys",
            codex="codex",
            timeout_seconds=5,
        ),
        synthesis=SynthesisConfig(driving_cell="BUF_X1", output_load_ff=10.0),
        liberty=LibertyConfig(
            name="test",
            path=tmp_path / "cells.lib",
            url="https://example.invalid/cells.lib",
            sha256="a" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="test",
        ),
    )


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _registry(tmp_path: Path) -> CorpusRegistryV1:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(FIXTURE_DIR / "reference.json")
    variant = json.loads((FIXTURE_DIR / "variant.json").read_text(encoding="utf-8"))
    variant["origin"] = {
        "kind": "upstream_parameter",
        "description": "Same design lineage under a second frozen parameter setting.",
        "generator": None,
        "generator_version": None,
    }
    registry.add(_write_json(tmp_path / "parameter-variant.json", variant))
    return registry


def test_coverage_uses_design_lineage_as_primary_population(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = corpus_workflow.corpus_coverage(
        config,
        registry=_registry(tmp_path),
        normalized_command=("rtl-advisor", "agent", "corpus", "coverage"),
    )

    assert result["document_type"] == "rtl-advisor.corpus.coverage"
    assert result["read_only"] is True
    assert result["counting_unit"] == "independent_design_lineage"
    assert result["population"] == {
        "independent_design_lineage_count": 1,
        "repository_lineage_count": 1,
        "containing_design_count": 1,
        "reference_record_count": 1,
        "qualified_design_lineage_count": 0,
        "variant_record_count": 1,
        "parameter_variant_count": 1,
        "naive_reference_plus_variant_count": 2,
        "naive_to_lineage_ratio": 2.0,
    }
    assert read_hashed_json(Path(result["artifacts"]["coverage"])) == result


def test_coverage_is_content_addressed_and_filters_references(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = _registry(tmp_path)
    first = corpus_workflow.corpus_coverage(
        config, registry=registry, tiers=("B",)
    )
    second = corpus_workflow.corpus_coverage(
        config, registry=registry, tiers=("B",)
    )

    assert first == second
    assert first["status"] == "empty"
    assert first["population"]["independent_design_lineage_count"] == 0
    assert first["population"]["variant_record_count"] == 0


def test_qualification_wraps_existing_engine_without_reclassifying(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    registry = _registry(tmp_path)
    source_result = {
        "plan_id": "plan-1",
        "plan_semantic_hash": "1" * 64,
        "tranche_id": "tranche-1",
        "tranche_semantic_hash": "2" * 64,
        "status": "completed_with_blockers",
        "reference_count": 2,
        "build_reproduced_count": 1,
        "blocked_count": 1,
        "source_integrity": {"verified_file_count": 7},
        "semantic_hash": "3" * 64,
    }
    monkeypatch.setattr(corpus_workflow, "qualify_tranche", lambda *args, **kwargs: source_result)

    result = corpus_workflow.qualify_corpus(
        config,
        tranche_lock_path=tmp_path / "lock.json",
        qualification_plan_path=tmp_path / "plan.json",
        registry=registry,
        normalized_command=("rtl-advisor", "agent", "corpus", "qualify"),
    )

    assert result["status"] == "completed_with_blockers"
    assert result["registry_mutation"] == "append_only"
    assert result["summary"]["blocked_count"] == 1
    assert result["source_result_semantic_hash"] == "3" * 64
    assert corpus_workflow.corpus_exit_code(result) == 4


def test_validate_and_register_frozen_tranche_are_explicit_and_idempotent(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    registry = CorpusRegistryV1(tmp_path / "new-registry")

    validation = corpus_workflow.validate_corpus_tranche(
        config,
        tranche_lock_path=TRANCHE_LOCK,
        normalized_command=("rtl-advisor", "agent", "corpus", "validate"),
    )
    first = corpus_workflow.register_corpus_tranche(
        config,
        tranche_lock_path=TRANCHE_LOCK,
        registry=registry,
        normalized_command=("rtl-advisor", "agent", "corpus", "register"),
    )
    second = corpus_workflow.register_corpus_tranche(
        config,
        tranche_lock_path=TRANCHE_LOCK,
        registry=registry,
        normalized_command=("rtl-advisor", "agent", "corpus", "register"),
    )

    assert validation["read_only"] is True
    assert validation["tranche"]["reference_count"] == 12
    assert validation["tranche"]["candidate_ppa_inspected_before_freeze"] is False
    assert first == second
    assert first["registry_mutation"] == "append_only"
    assert first["registered_count"] == 12
    assert registry.validate()["reference_count"] == 12


def test_agent_corpus_parser_and_normalized_command(tmp_path: Path) -> None:
    config = _config(tmp_path)
    args = build_parser().parse_args(
        (
            "agent",
            "corpus",
            "coverage",
            "--tier",
            "A",
            "--qualification-status",
            "active",
            "--registry-dir",
            "registry",
            "--json",
        )
    )

    command = _normalized_agent_command(config, args)

    assert args.agent_corpus_command == "coverage"
    assert command[-3:] == ("--schema-version", "1", "--json")
    assert "independent_design_lineage" not in command
    assert str((tmp_path / "registry").resolve()) in command


def test_phase5_json_schemas_match_runtime_contracts() -> None:
    coverage = json.loads(
        (ROOT / "schemas/rtl-advisor-corpus-coverage-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    qualification = json.loads(
        (ROOT / "schemas/rtl-advisor-corpus-qualification-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validation = json.loads(
        (
            ROOT
            / "schemas/rtl-advisor-corpus-tranche-validation-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    registration = json.loads(
        (ROOT / "schemas/rtl-advisor-corpus-registration-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert coverage["properties"]["counting_unit"]["const"] == (
        "independent_design_lineage"
    )
    assert qualification["properties"]["registry_mutation"]["const"] == (
        "append_only"
    )
    assert validation["properties"]["read_only"]["const"] is True
    assert registration["properties"]["registry_mutation"]["const"] == "append_only"
