from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_advisor.mvp_schema import (
    MVPSchemaError,
    PilotProvenanceV1,
    build_pilot_manifest,
    compile_context_snapshot,
    load_pilot_manifest,
    normalized_result_projection,
    read_hashed_json,
    write_hashed_json,
)


def _provenance(tmp_path: Path) -> PilotProvenanceV1:
    license_path = tmp_path / "LICENSE"
    license_path.write_text("Apache-2.0\n", encoding="utf-8")
    return PilotProvenanceV1(
        project="fixture",
        source_url="https://example.invalid/fixture",
        revision="abc123",
        license="Apache-2.0",
        license_path=str(license_path),
    )


def test_pilot_manifest_round_trip_and_stale_hash_rejection(tmp_path: Path) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top(input [7:0] a,b,c, output [7:0] y); assign y=a+b+c; endmodule\n")
    manifest, design = build_pilot_manifest(
        top="top",
        files=(source,),
        objective="timing",
        provenance=_provenance(tmp_path),
        base=tmp_path,
    )
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n", encoding="utf-8")

    loaded, loaded_design = load_pilot_manifest(path)

    assert loaded.top == "top"
    assert loaded_design.design_hash == design.design_hash
    source.write_text("module top; endmodule\n", encoding="utf-8")
    with pytest.raises(MVPSchemaError, match="stale"):
        load_pilot_manifest(path)


def test_hashed_json_rejects_mutation(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    record = write_hashed_json(
        path,
        {"schema_version": 1, "document_type": "example", "status": "ok"},
    )
    assert read_hashed_json(path, document_type="example")["semantic_hash"] == record["semantic_hash"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "changed"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(MVPSchemaError, match="hash mismatch"):
        read_hashed_json(path)


def test_compile_context_hashes_filelists_and_include_trees(tmp_path: Path) -> None:
    include = tmp_path / "include"
    include.mkdir()
    header = include / "constants.svh"
    header.write_text("`define UNUSED 1\n", encoding="utf-8")
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    filelist = tmp_path / "sources.f"
    filelist.write_text("-I include\ntop.sv\n", encoding="utf-8")
    _, design = build_pilot_manifest(
        top="top",
        filelist=filelist,
        objective="timing",
        provenance=_provenance(tmp_path),
        base=tmp_path,
    )
    first = compile_context_snapshot(design)

    header.write_text("`define UNUSED 2\n", encoding="utf-8")
    second = compile_context_snapshot(design)
    assert first["compile_context_hash"] != second["compile_context_hash"]

    header.write_text("`define UNUSED 1\n", encoding="utf-8")
    filelist.write_text("-I include\n./top.sv\n", encoding="utf-8")
    third = compile_context_snapshot(design)
    assert first["compile_context_hash"] != third["compile_context_hash"]


@pytest.mark.parametrize(
    ("location", "field"),
    (("top", "unexpected"), ("provenance", "unexpected"), ("source", "unexpected")),
)
def test_pilot_manifest_rejects_unknown_fields(
    tmp_path: Path, location: str, field: str
) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    manifest, _ = build_pilot_manifest(
        top="top",
        files=(source,),
        objective="timing",
        provenance=_provenance(tmp_path),
        base=tmp_path,
    )
    raw = manifest.to_dict()
    if location == "top":
        raw[field] = True
    elif location == "provenance":
        raw["provenance"][field] = True
    else:
        raw["source_hashes"][0][field] = True
    path = tmp_path / f"{location}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(MVPSchemaError, match="unknown fields"):
        load_pilot_manifest(path)


def test_pilot_manifest_compile_context_binding_rejects_header_change(
    tmp_path: Path,
) -> None:
    include = tmp_path / "include"
    include.mkdir()
    header = include / "constants.svh"
    header.write_text("`define UNUSED 1\n", encoding="utf-8")
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    manifest, _ = build_pilot_manifest(
        top="top",
        files=(source,),
        include_dirs=(include,),
        objective="timing",
        provenance=_provenance(tmp_path),
        base=tmp_path,
    )
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    header.write_text("`define UNUSED 2\n", encoding="utf-8")

    with pytest.raises(MVPSchemaError) as error:
        load_pilot_manifest(path)

    assert error.value.code == "stale_compile_context"


def test_pilot_manifest_requires_exactly_one_input_form_and_round_trips_filelist(
    tmp_path: Path,
) -> None:
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    filelist = tmp_path / "sources.f"
    filelist.write_text("top.sv\n", encoding="utf-8")
    provenance = _provenance(tmp_path)

    with pytest.raises(MVPSchemaError, match="exactly one"):
        build_pilot_manifest(
            top="top",
            files=(source,),
            filelist=filelist,
            objective="timing",
            provenance=provenance,
            base=tmp_path,
        )
    with pytest.raises(MVPSchemaError, match="exactly one"):
        build_pilot_manifest(
            top="top",
            objective="timing",
            provenance=provenance,
            base=tmp_path,
        )

    manifest, design = build_pilot_manifest(
        top="top",
        filelist=filelist,
        objective="timing",
        provenance=provenance,
        base=tmp_path,
    )
    assert manifest.files == ()
    path = tmp_path / "pilot-filelist.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    loaded, loaded_design = load_pilot_manifest(path)

    assert loaded.files == ()
    assert loaded.filelist == str(filelist)
    assert loaded_design.design_hash == design.design_hash


def test_exclusive_hashed_json_never_overwrites_an_append_only_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "immutable.json"
    first = write_hashed_json(
        path,
        {"schema_version": 1, "document_type": "example", "status": "first"},
        exclusive=True,
    )

    with pytest.raises(MVPSchemaError) as error:
        write_hashed_json(
            path,
            {"schema_version": 1, "document_type": "example", "status": "second"},
            exclusive=True,
        )

    assert error.value.code == "append_only_conflict"
    assert read_hashed_json(path) == first


def test_normalized_projection_is_stable_across_relocated_run_artifacts() -> None:
    common = {
        "schema_version": 2,
        "run_schema": "rtl-advisor-run-v1",
        "document_type": "rtl-advisor.agent.v2.measurement",
        "status": "completed",
        "decision": "synthesis_handles",
        "measurements": {
            "standard": {
                "classification": "neutral",
                "baseline": {"metrics": {"critical_delay_ps": 10.0}},
            }
        },
    }
    first = {
        **common,
        "run_id": "mvp-aaaaaaaaaaaaaaaaaaaa",
        "candidate_id": "addcand_aaaaaaaaaaaaaaaa",
        "artifacts": {"root": "/first/run", "measurement": "/first/run/m.json"},
        "finding": {"source": {"file": "/first/rtl/top.sv", "line": 4}},
        "semantic_hash": "a" * 64,
    }
    second = {
        **common,
        "run_id": "mvp-bbbbbbbbbbbbbbbbbbbb",
        "candidate_id": "addcand_bbbbbbbbbbbbbbbb",
        "artifacts": {"root": "/second/run", "measurement": "/second/run/m.json"},
        "finding": {"source": {"file": "/second/rtl/top.sv", "line": 4}},
        "semantic_hash": "b" * 64,
    }

    assert normalized_result_projection(first) == normalized_result_projection(second)
