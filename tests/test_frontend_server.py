from __future__ import annotations

import pytest

from rtl_advisor.frontend_server import (
    FRONTEND_ASSET_ROOT,
    FrontendServerError,
    _is_loopback_host,
    _is_loopback_host_header,
    create_frontend_server,
)


def test_frontend_assets_are_self_contained_and_use_v1_api() -> None:
    index = (FRONTEND_ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    styles = (FRONTEND_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
    application = (FRONTEND_ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "RTL ADVISOR" in index
    assert 'src="/app.js"' in index
    assert 'href="/styles.css"' in index
    assert "/api/v1/overview" in application
    assert "/api/v1/cases" in application
    assert "/api/runs/v1" in application
    assert "Review" in index
    assert "Candidate" in index
    assert "Formal" in index
    assert "synthesis" in index.lower()
    assert "No uploads or execution" in index
    assert "type=\"file\"" not in index
    assert 'getJSON("/api/runs/v1")' in application
    assert "@media (max-width: 760px)" in styles
    assert "[hidden] { display: none !important; }" in styles
    assert "USEFUL CHANGES FOUND" in index
    assert "CORRECT NO-CHANGE DECISIONS" in index
    assert "INCORRECT RECOMMENDATIONS" in index
    for jargon in (
        "OPPORTUNITY COVERAGE",
        "ABSTENTION SPECIFICITY",
        "HARMFUL RATE",
        "Correct abstention",
        "Diagnostic only",
    ):
        assert jargon not in index + application
    assert "https://" not in index + styles + application
    assert "http://" not in index + styles + application


def test_frontend_asset_paths_are_package_local() -> None:
    assert FRONTEND_ASSET_ROOT.name == "frontend"
    assert FRONTEND_ASSET_ROOT.parent.name == "rtl_advisor"
    assert {path.name for path in FRONTEND_ASSET_ROOT.iterdir()} == {
        "index.html",
        "styles.css",
        "app.js",
    }


def test_frontend_accepts_only_loopback_bind_and_host_headers() -> None:
    assert _is_loopback_host("localhost")
    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("127.12.34.56")
    assert _is_loopback_host("::1")
    assert _is_loopback_host_header("localhost:8765")
    assert _is_loopback_host_header("127.0.0.1:8765")
    assert _is_loopback_host_header("[::1]:8765")
    assert not _is_loopback_host("0.0.0.0")
    assert not _is_loopback_host("192.168.1.20")
    assert not _is_loopback_host_header("attacker.example:8765")


def test_frontend_rejects_nonlocal_bind_before_starting_server() -> None:
    with pytest.raises(FrontendServerError, match="loopback"):
        create_frontend_server(object(), host="0.0.0.0")  # type: ignore[arg-type]
