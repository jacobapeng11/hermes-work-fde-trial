from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / "scripts" / "smoke-expert-profile-mcp-isolation.py"
PACKAGE = ROOT / "scripts" / "expert_profile_resume_smoke"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_mcp_isolation_smoke_has_thin_entry_and_bounded_modules() -> None:
    assert len(_source(ENTRY).splitlines()) <= 30
    assert "from expert_profile_resume_smoke.mcp_cli import main" in _source(ENTRY)
    for name in (
        "mcp_acceptance.py",
        "mcp_cli.py",
        "mcp_contracts.py",
        "mcp_experiments.py",
        "mcp_fixture.py",
        "mcp_prompts.py",
    ):
        assert (PACKAGE / name).is_file()
        assert len(_source(PACKAGE / name).splitlines()) <= 400


def test_mcp_experiments_have_no_transport_or_database_imports() -> None:
    path = PACKAGE / "mcp_experiments.py"
    tree = ast.parse(_source(path), filename=str(path))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert imported.isdisjoint({"httpx", "sqlalchemy", "websockets"})


def test_mcp_isolation_smoke_is_apply_gated_and_registry_scoped() -> None:
    cli = _source(PACKAGE / "mcp_cli.py")
    experiments = _source(PACKAGE / "mcp_experiments.py")

    assert 'result.add_argument("--apply", action="store_true")' in cli
    assert "expert-profile-mcp-isolation-smoke" in cli
    assert '"registry_incremental_registration": "out_of_scope"' in cli
    assert "creates two temporary business experts" in cli
    for experiment in range(4):
        assert f'"M{experiment}"' in experiments


def test_mcp_isolation_smoke_forbids_global_reload_and_keeps_exact_cleanup() -> None:
    sources = [ENTRY, *sorted(PACKAGE.glob("*.py"))]
    forbidden: list[tuple[str, str, int]] = []
    for path in sources:
        tree = ast.parse(_source(path), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr in {
                "reload_skills",
                "reload_mcp",
                "reload_skills_and_list",
                "reload_mcp_and_list",
            }:
                forbidden.append((path.name, function.attr, node.lineno))
    assert forbidden == []
    assert "await edge.cleanup(cleanup_errors)" in _source(PACKAGE / "mcp_cli.py")
    assert "await profiles.cleanup(cleanup_errors)" in _source(PACKAGE / "mcp_cli.py")
    assert "await profiles.verify_cleanup(cleanup_errors)" in _source(
        PACKAGE / "mcp_cli.py"
    )


def test_marker_server_exposes_two_distinct_isolation_toolsets() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from expert_profile_resume_smoke.marker_server import MarkerMcp

        marker = MarkerMcp(suffix="20260822-test", host="127.0.0.1", port=1)
        a = {item["name"] for item in marker._tool_definitions("registry", "a")}
        b = {item["name"] for item in marker._tool_definitions("registry", "b")}
    finally:
        sys.path.pop(0)

    assert a == {
        "a_marker_20260822_test",
        "a_hold_20260822_test",
    }
    assert b == {"b_marker_20260822_test"}
    assert a.isdisjoint(b)
