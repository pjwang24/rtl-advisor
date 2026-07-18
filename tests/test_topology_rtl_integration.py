from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from rtl_advisor.config import LibertyConfig, ProjectConfig, SynthesisConfig, ToolConfig
from rtl_advisor.corpus import (
    COMPARATOR_SELECTION_FAMILY,
    DECODE_FACTORING_FAMILY,
    VARIABLE_SHIFT_FAMILY,
    WIDTH_SIGNEDNESS_FAMILY,
    generate_case,
)
from rtl_advisor.topology_rtl import render_topology_variants
from rtl_advisor.v2_corpus import TOPOLOGY_DOMAINS, family_descriptors
from rtl_advisor.verification import lint_case, prove_case_candidates


VERILATOR = shutil.which("verilator")
YOSYS = shutil.which("yosys")


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        config_path=tmp_path / "rtl-advisor.toml",
        root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        corpus_dir=tmp_path / "corpus",
        tools=ToolConfig(
            verilator=VERILATOR or "verilator",
            yosys=YOSYS or "yosys",
            codex="codex",
            timeout_seconds=30,
        ),
        synthesis=SynthesisConfig(driving_cell="BUF_X1", output_load_ff=10.0),
        liberty=LibertyConfig(
            name="unused",
            path=tmp_path / "unused.lib",
            url="https://example.invalid/unused.lib",
            sha256="a" * 64,
            license_path=tmp_path / "LICENSE",
            license_url="https://example.invalid/LICENSE",
            source_commit="unused",
        ),
    )


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for topology integration proofs",
)
@pytest.mark.parametrize("family", tuple(TOPOLOGY_DOMAINS))
def test_topology_family_lints_and_proves(family: str, tmp_path: Path) -> None:
    descriptor = next(
        descriptor
        for descriptor in family_descriptors(family)
        if descriptor.width <= 8
        and descriptor.topology.get("operation") not in {"multiply"}
    )
    rendered = render_topology_variants(
        family,
        descriptor.case_id,
        descriptor.width,
        descriptor.topology,
    )
    manifest = generate_case(
        tmp_path / "corpus" / descriptor.case_id,
        family=family,
        case_id=descriptor.case_id,
        width=descriptor.width,
        seed=descriptor.seed,
        rendered_override=rendered,
    )

    lint = lint_case(_config(tmp_path), manifest)
    proofs = prove_case_candidates(_config(tmp_path), manifest)

    assert all(result.ok for result in lint)
    assert [result.status for result in proofs] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proofs)


@pytest.mark.skipif(
    VERILATOR is None or YOSYS is None,
    reason="Verilator and Yosys are required for topology integration proofs",
)
@pytest.mark.parametrize(
    ("family", "topology"),
    (
        (
            VARIABLE_SHIFT_FAMILY,
            {
                "direction": "arithmetic_right",
                "width": 8,
                "amount_excess": 0,
                "guarded": True,
                "signed": True,
            },
        ),
        (
            WIDTH_SIGNEDNESS_FAMILY,
            {
                "operation": "compare",
                "width": 8,
                "extension": 8,
                "signedness_mix": "su",
                "truncate_result": False,
            },
        ),
        (
            WIDTH_SIGNEDNESS_FAMILY,
            {
                "operation": "compare",
                "width": 8,
                "extension": 4,
                "signedness_mix": "ss",
                "truncate_result": True,
            },
        ),
        (
            DECODE_FACTORING_FAMILY,
            {
                "opcode_width": 4,
                "match_count": 16,
                "reuse_count": 2,
                "decode_style": "masked",
                "width": 8,
            },
        ),
        (
            COMPARATOR_SELECTION_FAMILY,
            {
                "relation": "ge",
                "width": 8,
                "signed": False,
                "fanout": 2,
                "constant_shape": "zero",
            },
        ),
    ),
)
def test_topology_semantic_edges_prove_and_negative_controls_fail(
    family: str,
    topology: dict,
    tmp_path: Path,
) -> None:
    case_id = f"edge_{family}"
    rendered = render_topology_variants(family, case_id, 8, topology)
    manifest = generate_case(
        tmp_path / "corpus" / case_id,
        family=family,
        case_id=case_id,
        width=8,
        seed=20260714,
        rendered_override=rendered,
    )

    proofs = prove_case_candidates(_config(tmp_path), manifest)

    assert [result.status for result in proofs] == [
        "equivalent",
        "equivalent",
        "equivalent",
        "inequivalent",
    ]
    assert all(result.expectation_met for result in proofs)
