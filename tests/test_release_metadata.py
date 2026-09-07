from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

import rtl_advisor


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ACTION_REVISIONS = {
    "actions/checkout": "08c6903cd8c0fde910a37f88322edcfb5dd907a8",
    "actions/setup-python": "e797f83bcb11b83ae66e0230d6156d7c80228e7c",
    "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",
}


def test_mvp_release_version_domains_are_explicit() -> None:
    manifest = json.loads(
        (ROOT / "plugins/rtl-advisor/.codex-plugin/plugin.json").read_text(
            encoding="utf-8"
        )
    )

    assert rtl_advisor.__version__ == "0.2.0a1"
    assert re.fullmatch(
        r"0\.2\.0-alpha\.1(?:\+codex\.[A-Za-z0-9][A-Za-z0-9.-]*)?",
        str(manifest["version"]),
    )
    assert manifest["name"] == "rtl-advisor"
    assert manifest["skills"] == "./skills/"
    assert "apps" not in manifest
    assert "mcpServers" not in manifest
    assert (ROOT / "plugins/rtl-advisor/skills/analyze-rtl/SKILL.md").is_file()


def test_build_backend_is_exactly_pinned() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["build-system"]["requires"] == ["flit_core==3.12.0"]
    assert "flit_core==3.12.0" in metadata["dependency-groups"]["dev"]


def test_ci_actions_are_commit_pinned() -> None:
    discovered: dict[str, str] = {}
    workflows = tuple(sorted((ROOT / ".github/workflows").glob("*.y*ml")))
    assert workflows
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        references = re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE)
        assert references, workflow
        for reference in references:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (
                workflow,
                reference,
            )
            action, revision = reference.rsplit("@", 1)
            assert revision == EXPECTED_ACTION_REVISIONS[action]
            discovered[action] = revision

    assert discovered == EXPECTED_ACTION_REVISIONS


def test_tool_integration_is_a_digest_pinned_container() -> None:
    workflow = (ROOT / ".github/workflows/tool-integration.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / ".github/docker/mvp-tools.Dockerfile").read_text(
        encoding="utf-8"
    )
    smoke = (ROOT / ".github/scripts/run-mvp-tool-smoke.sh").read_text(
        encoding="utf-8"
    )

    assert "docker build" in workflow
    assert "docker run --rm --network none" in workflow
    assert (
        "python:3.13-slim-bookworm@sha256:"
        "dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860"
        in dockerfile
    )
    assert (
        "ghcr.io/astral-sh/uv:0.11.5@sha256:"
        "bd44bb8253b99699d744ccc3db5f4d10c39a71ddbe97cf5c0361f65bf51a33f9"
        in dockerfile
    )
    assert "oss-cad-suite-build/releases/download/2026-03-06/" in dockerfile
    assert (
        "4b514b77fc85a2587fbb2784bffc18279a93f7b0fd8dd1d162f6991b10a9cbe8"
        in dockerfile
    )
    assert (
        "8d540a4d4cf6d09d27c87ad067857a9c0c2eeb023ab7a56e058cd3113db4e9b1"
        in dockerfile
    )
    assert '"Yosys 0.63"*' in smoke
    assert '"Verilator 5."*' in smoke
    assert "verilator_bin /opt/oss-cad-suite/bin/verilator" in dockerfile
    assert 'test "$(yosys-abc -q \'version; quit\' | awk \'{print $4}\')" = "1.01"' in smoke


def test_license_stays_pending_until_owner_confirmation() -> None:
    assert not (ROOT / "LICENSE").exists()


def test_plugin_explains_incomplete_run_evidence() -> None:
    skill = (
        ROOT / "plugins/rtl-advisor/skills/analyze-rtl/SKILL.md"
    ).read_text(encoding="utf-8")
    contract = (
        ROOT
        / "plugins/rtl-advisor/skills/analyze-rtl/references/cli-contract.md"
    ).read_text(encoding="utf-8")
    interpretation = (
        ROOT
        / "plugins/rtl-advisor/skills/analyze-rtl/references/result-interpretation.md"
    ).read_text(encoding="utf-8")

    assert "Evidence incomplete" in skill
    assert "`decision: incomplete`" in contract
    assert "| `incomplete` | Evidence incomplete |" in interpretation
