from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _calls(source: str) -> set[str]:
    tree = ast.parse(source)
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Call):
                result.add(node.func.attr)
    return result


def test_user_read_and_install_routes_use_narrow_services() -> None:
    source = _source("app/api/v1/user/experts.py")
    calls = _calls(source)
    assert {
        "market_page",
        "market_detail",
        "list_mine",
        "list_runnable",
        "list_inheritance_sources",
        "list_for",
        "install",
        "copy_owned",
    } <= calls
    assert "list_market" not in calls
    assert "list_mine" in calls


def test_admin_review_reads_use_query_service() -> None:
    source = _source("app/api/v1/admin/experts.py")
    calls = _calls(source)
    assert {"list_reviews", "get_review"} <= calls
    assert "list_admin_reviews" not in calls


def test_startup_recovery_does_not_import_legacy_orchestrator() -> None:
    source = _source("app/main.py")
    assert "expert_profile_operations" in source
    assert "fail_interrupted_expert_profile_operations" in source
    assert "expert_profiles import fail_interrupted" not in source


def test_production_has_no_legacy_expert_orchestrator_reference() -> None:
    app_root = ROOT / "app"
    production_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
    )
    assert "ExpertProfileOrchestrator" not in production_sources
    assert "app.services.expert_profiles" not in production_sources
    assert not (app_root / "services" / "expert_profiles.py").exists()


def test_new_query_and_operation_modules_are_framework_free_and_focused() -> None:
    for relative in (
        "app/services/expert_profile_queries.py",
        "app/services/expert_capability_catalog.py",
        "app/services/expert_review_queries.py",
        "app/services/expert_profile_operations.py",
    ):
        source = _source(relative)
        assert "fastapi" not in source.lower()
        assert len(source.splitlines()) < 800
