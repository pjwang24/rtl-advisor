from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser, main
from rtl_advisor.corpus_registry import (
    CATEGORIES,
    CorpusRegistryError,
    CorpusRegistryV1,
    REFERENCE_SCHEMA_ID,
    VARIANT_SCHEMA_ID,
    ReferenceManifestV1,
    VariantManifestV1,
    load_corpus_manifest,
)
from rtl_advisor.schemas import read_schema


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "examples/corpus/opentitan_arbiter_pair"
REFERENCE_FIXTURE = FIXTURE_DIR / "reference.json"
VARIANT_FIXTURE = FIXTURE_DIR / "variant.json"


def _reference_payload() -> dict:
    return json.loads(REFERENCE_FIXTURE.read_text(encoding="utf-8"))


def _variant_payload() -> dict:
    return json.loads(VARIANT_FIXTURE.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_metadata_only_arbiter_pair_parses_without_claiming_qualification() -> None:
    reference = load_corpus_manifest(REFERENCE_FIXTURE)
    variant = load_corpus_manifest(VARIANT_FIXTURE)

    assert isinstance(reference, ReferenceManifestV1)
    assert reference.schema == REFERENCE_SCHEMA_ID
    assert reference.qualification.state == "discovered"
    assert reference.provenance.revision is None
    assert reference.source_hashes == ()
    assert isinstance(variant, VariantManifestV1)
    assert variant.schema == VARIANT_SCHEMA_ID
    assert variant.parent_reference_id == reference.reference_id
    assert variant.state == "declared"


def test_registry_adds_hash_linked_records_without_counting_variant_as_reference(
    tmp_path: Path,
) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")

    reference_result = registry.add(REFERENCE_FIXTURE)
    variant_result = registry.add(VARIANT_FIXTURE)
    summary = registry.summary()

    assert reference_result["kind"] == "reference"
    assert len(reference_result["semantic_hash"]) == 64
    assert variant_result["kind"] == "variant"
    assert summary["reference_count"] == 1
    assert summary["variant_count"] == 1
    assert summary["tier_reference_counts"]["A"] == 1
    assert summary["tier_qualified_counts"]["A"] == 0
    assert summary["design_lineage_count"] == 1
    assert summary["upstream_project_reference_counts"] == {
        "lowrisc-opentitan": 1
    }
    assert summary["license_expression_reference_counts"] == {"Apache-2.0": 1}
    assert summary["license_disposition_reference_counts"]["pending"] == 1
    assert summary["proof_level_reference_counts"]["P2"] == 1
    assert summary["split_reference_counts"]["unassigned"] == 1
    assert registry.validate()["status"] == "passed"


def test_many_source_files_still_count_as_one_reference(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload["compile_context"]["sources"] = [
        "rtl/top.sv",
        "rtl/dependency_a.sv",
        "rtl/dependency_b.sv",
        "rtl/dependency_c.sv",
    ]
    manifest_path = _write_json(tmp_path / "multi-file.json", payload)
    registry = CorpusRegistryV1(tmp_path / "registry")

    registry.add(manifest_path)

    assert registry.summary()["tier_reference_counts"]["A"] == 1


def test_registry_rejects_duplicate_design_lineage_under_a_new_id(
    tmp_path: Path,
) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(REFERENCE_FIXTURE)
    duplicate = _reference_payload()
    duplicate["reference_id"] = "renamed-copy"
    duplicate["display_name"] = "Renamed copy"
    duplicate_path = _write_json(tmp_path / "duplicate.json", duplicate)

    with pytest.raises(CorpusRegistryError) as error:
        registry.add(duplicate_path)

    assert error.value.code == "duplicate_design_lineage"


def test_repository_lineage_cannot_cross_dataset_splits(tmp_path: Path) -> None:
    first = _reference_payload()
    first["split"] = "development"
    first_path = _write_json(tmp_path / "first.json", first)
    second = deepcopy(first)
    second["reference_id"] = "opentitan-second-design"
    second["display_name"] = "Second design"
    second["lineage"]["design_lineage_id"] = "lowrisc-opentitan-second-design"
    second["compile_context"]["top"] = "second_top"
    second["compile_context"]["sources"] = ["hw/ip/prim/rtl/second_top.sv"]
    second["split"] = "evaluation"
    second_path = _write_json(tmp_path / "second.json", second)
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(first_path)

    with pytest.raises(CorpusRegistryError) as error:
        registry.add(second_path)

    assert error.value.code == "split_lineage_leakage"


def test_source_pinned_state_requires_revision_and_all_source_hashes(
    tmp_path: Path,
) -> None:
    payload = _reference_payload()
    payload["qualification"]["state"] = "source_pinned"
    payload["provenance"]["license"]["sha256"] = "a" * 64
    payload["provenance"]["license"]["disposition"] = "approved"
    missing_revision = _write_json(tmp_path / "missing-revision.json", payload)

    with pytest.raises(CorpusRegistryError, match="exact revision"):
        load_corpus_manifest(missing_revision)

    payload["provenance"]["revision_kind"] = "commit"
    payload["provenance"]["revision"] = "1" * 40
    missing_hash = _write_json(tmp_path / "missing-hash.json", payload)

    with pytest.raises(CorpusRegistryError, match="hash for every resolved source"):
        load_corpus_manifest(missing_hash)


def test_fully_evidenced_reference_counts_as_qualified(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload["qualification"]["state"] = "reference_qualified"
    payload["provenance"]["revision_kind"] = "commit"
    payload["provenance"]["revision"] = "1" * 40
    payload["provenance"]["license"]["sha256"] = "2" * 64
    payload["provenance"]["license"]["disposition"] = "approved"
    payload["source_hashes"] = [
        {
            "path": "hw/ip/prim/rtl/prim_arbiter_ppc.sv",
            "sha256": "3" * 64,
        }
    ]
    payload["compile_context_hashes"]["compile_context_hash"] = "4" * 64
    payload["compile_context"]["frontend"] = "verilator"
    payload["compile_context"]["frontend_version"] = "5.047"
    payload["compile_context"]["build_commands"] = ["bender script flist"]
    payload["compile_context"]["lint_commands"] = ["verilator --lint-only"]
    path = _write_json(tmp_path / "qualified.json", payload)
    registry = CorpusRegistryV1(tmp_path / "registry")

    registry.add(path)

    summary = registry.summary()
    assert summary["tier_reference_counts"]["A"] == 1
    assert summary["tier_qualified_counts"]["A"] == 1


def test_variant_requires_registered_parent(tmp_path: Path) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")

    with pytest.raises(CorpusRegistryError) as error:
        registry.add(VARIANT_FIXTURE)

    assert error.value.code == "missing_parent_reference"


def test_registry_detects_stored_record_mutation(tmp_path: Path) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    result = registry.add(REFERENCE_FIXTURE)
    record_path = Path(result["record_path"])
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["display_name"] = "tampered"
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CorpusRegistryError) as error:
        registry.validate()

    assert error.value.code == "artifact_hash_mismatch"


def test_manifest_rejects_source_path_escape(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload["compile_context"]["sources"] = ["../outside.sv"]
    path = _write_json(tmp_path / "escape.json", payload)

    with pytest.raises(CorpusRegistryError) as error:
        load_corpus_manifest(path)

    assert error.value.code == "unsafe_path"


def test_proof_level_rejects_incompatible_latency_contract(tmp_path: Path) -> None:
    payload = _reference_payload()
    payload["proof_contract"]["latency_relation"] = "combinational"
    path = _write_json(tmp_path / "bad-proof.json", payload)

    with pytest.raises(CorpusRegistryError) as error:
        load_corpus_manifest(path)

    assert error.value.code == "invalid_proof_contract"


def test_json_schema_documents_are_parseable_and_match_runtime_taxonomy() -> None:
    proof = read_schema("rtl-advisor-proof-v1")
    reference = read_schema("rtl-advisor-reference-v1")
    variant = read_schema("rtl-advisor-variant-v1")

    assert proof["properties"]["schema"]["const"] == "rtl-advisor-proof-v1"
    assert reference["properties"]["schema"]["const"] == REFERENCE_SCHEMA_ID
    assert set(reference["properties"]["categories"]["items"]["enum"]) == set(
        CATEGORIES
    )
    assert variant["properties"]["schema"]["const"] == VARIANT_SCHEMA_ID


def test_unknown_packaged_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown RTL Advisor schema"):
        read_schema("missing-schema")


def test_corpus_registry_cli_parser_exposes_registry_operations() -> None:
    parser = build_parser()

    add = parser.parse_args(("corpus", "add", "reference.json", "--json"))
    advance = parser.parse_args(
        ("corpus", "advance", "reference-v2.json", "--json")
    )
    validate = parser.parse_args(("corpus", "validate", "--json"))
    listing = parser.parse_args(
        ("corpus", "list", "--kind", "references", "--json")
    )
    summary = parser.parse_args(("corpus", "summary", "--json"))

    assert add.corpus_command == "add"
    assert advance.corpus_command == "advance"
    assert validate.corpus_command == "validate"
    assert listing.kind == "references"
    assert summary.corpus_command == "summary"


def test_registry_appends_hash_linked_qualification_progression(
    tmp_path: Path,
) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    first = registry.add(REFERENCE_FIXTURE)
    successor = _reference_payload()
    successor["qualification"]["state"] = "license_reviewed"
    successor["provenance"]["license"]["sha256"] = "a" * 64
    successor["provenance"]["license"]["disposition"] = "conditional"
    successor_path = _write_json(tmp_path / "license-reviewed.json", successor)

    second = registry.advance(successor_path)

    assert second["sequence"] == 2
    assert second["predecessor_hash"] == first["semantic_hash"]
    assert "000002-after-" in second["record_path"]
    assert registry.references()[0].qualification.state == "license_reviewed"
    validation = registry.validate()
    assert validation["reference_count"] == 1
    assert validation["record_count"] == 1
    assert validation["history_record_count"] == 2


def test_registry_rejects_non_monotonic_or_identity_changing_progression(
    tmp_path: Path,
) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(REFERENCE_FIXTURE)
    unchanged = _write_json(tmp_path / "unchanged.json", _reference_payload())

    with pytest.raises(CorpusRegistryError) as error:
        registry.advance(unchanged)
    assert error.value.code == "invalid_qualification_transition"


def test_variant_progression_cannot_change_source_locations(tmp_path: Path) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(REFERENCE_FIXTURE)
    registry.add(VARIANT_FIXTURE)
    prepared = _variant_payload()
    prepared["state"] = "prepared"
    prepared["source_locations"] = ["hw/ip/prim/rtl/different.sv"]
    prepared["artifact_hashes"] = {"candidate": "a" * 64}
    prepared_path = _write_json(tmp_path / "prepared.json", prepared)

    with pytest.raises(CorpusRegistryError) as error:
        registry.advance(prepared_path)

    assert error.value.code == "invalid_variant_transition"

    changed = _reference_payload()
    changed["qualification"]["state"] = "license_reviewed"
    changed["provenance"]["license"]["sha256"] = "a" * 64
    changed["provenance"]["license"]["disposition"] = "conditional"
    changed["lineage"]["design_lineage_id"] = "different-design"
    changed_path = _write_json(tmp_path / "changed.json", changed)

    with pytest.raises(CorpusRegistryError) as error:
        registry.advance(changed_path)
    assert error.value.code == "invalid_qualification_transition"


def test_registry_detects_broken_history_link(tmp_path: Path) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(REFERENCE_FIXTURE)
    successor = _reference_payload()
    successor["qualification"]["state"] = "license_reviewed"
    successor["provenance"]["license"]["sha256"] = "a" * 64
    successor["provenance"]["license"]["disposition"] = "conditional"
    successor_path = _write_json(tmp_path / "license-reviewed.json", successor)
    result = registry.advance(successor_path)
    record_path = Path(result["record_path"])
    record_path.rename(record_path.with_name("000002-after-deadbeefdeadbeef.json"))

    with pytest.raises(CorpusRegistryError) as error:
        registry.validate()
    assert error.value.code == "invalid_history_link"


def test_registry_allows_hash_linked_terminal_status_at_current_stage(
    tmp_path: Path,
) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(REFERENCE_FIXTURE)
    blocked = _reference_payload()
    blocked["qualification"] = {
        "state": "discovered",
        "status": "blocked",
        "reason": "Dependency context is unavailable.",
    }
    blocked_path = _write_json(tmp_path / "blocked.json", blocked)

    result = registry.advance(blocked_path)

    assert result["sequence"] == 2
    assert registry.references()[0].qualification.status == "blocked"

    resumed = _reference_payload()
    resumed_path = _write_json(tmp_path / "resumed.json", resumed)
    resume_result = registry.advance(resumed_path)
    assert resume_result["sequence"] == 3
    assert registry.references()[0].qualification.status == "active"


def test_registry_rejects_data_change_in_terminal_status_event(
    tmp_path: Path,
) -> None:
    registry = CorpusRegistryV1(tmp_path / "registry")
    registry.add(REFERENCE_FIXTURE)
    blocked = _reference_payload()
    blocked["qualification"] = {
        "state": "discovered",
        "status": "blocked",
        "reason": "Dependency context is unavailable.",
    }
    blocked["compile_context"]["top"] = "different_top"
    blocked_path = _write_json(tmp_path / "blocked.json", blocked)

    with pytest.raises(CorpusRegistryError) as error:
        registry.advance(blocked_path)
    assert error.value.code == "invalid_qualification_transition"


def test_corpus_registry_cli_add_list_summary_and_validate(
    tmp_path: Path,
    capsys,
) -> None:
    registry_dir = tmp_path / "registry"
    common = (
        "--config",
        str(ROOT / "rtl-advisor.toml"),
        "corpus",
    )

    assert main(
        (
            *common,
            "add",
            str(REFERENCE_FIXTURE),
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    add_payload = json.loads(capsys.readouterr().out)
    assert add_payload["record_id"] == "opentitan-prim-arbiter-ppc"

    assert main(
        (
            *common,
            "add",
            str(VARIANT_FIXTURE),
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    capsys.readouterr()

    assert main(
        (
            *common,
            "list",
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["count"] == 2

    assert main(
        (
            *common,
            "summary",
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["reference_count"] == 1
    assert summary["variant_count"] == 1

    assert main(
        (
            *common,
            "validate",
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["status"] == "passed"
