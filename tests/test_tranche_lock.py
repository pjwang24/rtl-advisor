from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from rtl_advisor.cli import build_parser, main
from rtl_advisor.mvp_schema import file_sha256, stable_hash
from rtl_advisor.tranche_lock import (
    TrancheLockError,
    load_tranche_lock,
    materialize_tranche_references,
    tranche_summary,
    validate_tranche_lock,
    verify_tranche_sources,
)


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "examples/corpus/wave2_tier_a/tranche.lock.json"


def _rehash(payload: dict) -> dict:
    result = deepcopy(payload)
    result.pop("semantic_hash", None)
    result["semantic_hash"] = stable_hash(result)
    return result


def test_wave2_tranche_is_frozen_before_ppa_with_required_diversity() -> None:
    lock = load_tranche_lock(LOCK_PATH)
    summary = tranche_summary(lock, path=LOCK_PATH)

    assert summary["reference_count"] == 12
    assert summary["stateful_reference_count"] == 11
    assert summary["upstream_project_count"] == 3
    assert summary["category_count"] >= 4
    assert summary["candidate_ppa_inspected_before_freeze"] is False
    assert [item["order"] for item in lock["references"]] == list(range(1, 13))


def test_tranche_rejects_tampering() -> None:
    raw = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    raw["references"][0]["top"] = "changed"

    with pytest.raises(TrancheLockError) as error:
        validate_tranche_lock(raw)
    assert error.value.code == "artifact_hash_mismatch"


def test_tranche_rejects_pre_freeze_ppa_even_when_rehashed() -> None:
    raw = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    raw["references"][0]["delay"] = 1.0
    raw = _rehash(raw)

    with pytest.raises(TrancheLockError) as error:
        validate_tranche_lock(raw)
    assert error.value.code == "ppa_visible_before_freeze"


def test_tranche_rejects_silent_candidate_replacement() -> None:
    raw = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    raw["references"].pop()
    raw = _rehash(raw)

    with pytest.raises(TrancheLockError, match="requires 12 references"):
        validate_tranche_lock(raw)


def test_validate_tranche_cli(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    parsed = parser.parse_args(("corpus", "validate-tranche", str(LOCK_PATH), "--json"))
    assert parsed.corpus_command == "validate-tranche"

    assert main(
        (
            "--config",
            str(ROOT / "rtl-advisor.toml"),
            "corpus",
            "validate-tranche",
            str(LOCK_PATH),
            "--json",
        )
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "passed"
    assert result["reference_count"] == 12


def test_tranche_materializes_twelve_license_reviewed_references() -> None:
    records = materialize_tranche_references(load_tranche_lock(LOCK_PATH))

    assert len(records) == 12
    assert all(record.qualification.state == "license_reviewed" for record in records)
    assert all(record.provenance.revision is not None for record in records)
    assert all(record.provenance.license.sha256 is not None for record in records)
    assert sum(record.proof_contract.level == "P2" for record in records) == 11


def test_register_tranche_cli_populates_registry(tmp_path: Path, capsys) -> None:
    registry_dir = tmp_path / "registry"
    assert main(
        (
            "--config",
            str(ROOT / "rtl-advisor.toml"),
            "corpus",
            "register-tranche",
            str(LOCK_PATH),
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "registered"
    assert result["registered_count"] == 12

    assert main(
        (
            "--config",
            str(ROOT / "rtl-advisor.toml"),
            "corpus",
            "summary",
            "--registry-dir",
            str(registry_dir),
            "--json",
        )
    ) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["reference_count"] == 12
    assert summary["qualification_state_counts"]["license_reviewed"] == 12


def test_tranche_source_integrity_verifies_archive_license_and_rtl(
    tmp_path: Path,
) -> None:
    raw = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    raw["selection_policy"]["candidate_count"] = 1
    raw["selection_policy"]["minimum_upstream_projects"] = 1
    raw["selection_policy"]["minimum_categories"] = 1
    raw["upstreams"] = [raw["upstreams"][0]]
    raw["references"] = [raw["references"][1]]
    raw["references"][0]["order"] = 1
    upstream_root = tmp_path / "opentitan"
    source_path = upstream_root / raw["references"][0]["primary_source"]
    source_path.parent.mkdir(parents=True)
    source_path.write_text("module prim_fifo_sync; endmodule\n", encoding="utf-8")
    archive_path = upstream_root / "source.tar.gz"
    archive_path.write_bytes(b"archive")
    license_path = upstream_root / "LICENSE"
    license_path.write_text("license\n", encoding="utf-8")
    raw["references"][0]["primary_source_sha256"] = file_sha256(source_path)
    raw["upstreams"][0]["archive_sha256"] = file_sha256(archive_path)
    raw["upstreams"][0]["license_sha256"] = file_sha256(license_path)
    raw = _rehash(raw)

    result = verify_tranche_sources(
        raw,
        {"lowrisc-opentitan": upstream_root},
    )

    assert result["status"] == "passed"
    assert result["verified_file_count"] == 3

    source_path.write_text("changed\n", encoding="utf-8")
    result = verify_tranche_sources(
        raw,
        {"lowrisc-opentitan": upstream_root},
    )
    assert result["status"] == "failed"
    assert result["failed_file_count"] == 1
