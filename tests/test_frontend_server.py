from __future__ import annotations

from rtl_advisor.frontend_server import FRONTEND_ASSET_ROOT


def test_frontend_assets_are_self_contained_and_use_v1_api() -> None:
    index = (FRONTEND_ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    styles = (FRONTEND_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
    application = (FRONTEND_ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "RTL ADVISOR" in index
    assert 'src="/app.js"' in index
    assert 'href="/styles.css"' in index
    assert "/api/v1/overview" in application
    assert "/api/v1/cases" in application
    assert "@media (max-width: 760px)" in styles
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
