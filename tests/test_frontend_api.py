from __future__ import annotations

import json
from pathlib import Path

from rtl_advisor.config import load_config
from rtl_advisor.frontend_api import FrontendDataStore


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_overview_exposes_frozen_v22_evidence() -> None:
    store = FrontendDataStore(load_config(ROOT / "rtl-advisor.toml"))

    overview = store.overview()

    assert overview["api_version"] == "v1"
    assert overview["evidence"]["blind_labels_used"] is False
    assert overview["evidence"]["case_count"] == 936
    assert overview["project"]["live_analysis"]["available"] is False
    assert overview["metrics"]["covered_opportunity_count"] == 86
    assert overview["metrics"]["harmful_count"] == 4
    assert overview["metrics"]["correct_no_change_count"] == 702
    assert overview["metrics"]["no_change_case_count"] == 706
    assert len(overview["families"]) == 9
    assert sum(gate["passed"] for gate in overview["gates"]) == 3
    assert {gate["label"] for gate in overview["gates"]} == {
        "Overall decision score",
        "Correct no-change decisions",
        "Incorrect recommendations",
        "OpenROAD validation",
    }


def test_frontend_case_filters_and_detail_use_generated_rtl() -> None:
    store = FrontendDataStore(load_config(ROOT / "rtl-advisor.toml"))

    result = store.cases(
        category="unsupported_family",
        limit=20,
        offset=0,
    )

    assert result["pagination"]["total"] == 13
    assert all(item["supported"] is False for item in result["items"])
    case_id = result["items"][0]["case_id"]
    detail = store.case_detail(case_id)
    assert detail["case"]["case_id"] == case_id
    assert detail["provenance"]["blind_labels_used"] is False
    assert detail["rtl"]["language"] == "systemverilog"
    assert "module" in detail["rtl"]["source"]
    assert len(detail["candidates"]) == 3
    assert all(candidate["stages"]["formal"] == "passed" for candidate in detail["candidates"])


def test_frontend_contract_is_read_only_and_versioned() -> None:
    store = FrontendDataStore(load_config(ROOT / "rtl-advisor.toml"))

    contract = store.contract()

    assert contract["api_version"] == "v1"
    assert contract["read_only"] is True
    assert contract["analysis_contract"]["next_source_version"] == "v23"
    assert {route["method"] for route in contract["routes"]} == {"GET"}


def test_frontend_payloads_are_json_serializable() -> None:
    store = FrontendDataStore(load_config(ROOT / "rtl-advisor.toml"))

    payloads = (
        store.health(),
        store.contract(),
        store.overview(),
        store.cases(limit=1),
    )

    for payload in payloads:
        assert json.loads(json.dumps(payload))["api_version"] == "v1"
