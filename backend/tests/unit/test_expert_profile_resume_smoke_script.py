from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / "scripts" / "smoke-expert-profile-asset-resume.py"
PACKAGE = ROOT / "scripts" / "expert_profile_resume_smoke"


def _python_sources() -> list[Path]:
    return [ENTRY, *sorted(PACKAGE.glob("*.py"))]


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_resume_smoke_is_split_by_responsibility() -> None:
    expected = {
        "__init__.py",
        "cli.py",
        "contracts.py",
        "experiments.py",
        "marker_server.py",
        "mcp_acceptance.py",
        "mcp_cli.py",
        "mcp_contracts.py",
        "mcp_experiments.py",
        "mcp_fixture.py",
        "mcp_prompts.py",
        "profile_fixture.py",
        "resume_acceptance.py",
        "runtime_edge.py",
        "target.py",
    }

    assert {path.name for path in PACKAGE.glob("*.py")} == expected
    assert len(_source(ENTRY).splitlines()) <= 30
    assert "from expert_profile_resume_smoke.cli import main" in _source(ENTRY)
    assert all(len(_source(path).splitlines()) <= 450 for path in PACKAGE.glob("*.py"))


def test_resume_smoke_is_explicitly_destructive() -> None:
    cli = _source(PACKAGE / "cli.py")

    assert 'result.add_argument("--apply", action="store_true")' in cli
    assert "creates temporary experts, Profile assets, Sessions, and real Runs" in cli
    assert ".runtime" in cli
    assert "expert-profile-resume-smoke" in cli


def test_resume_smoke_never_calls_global_reload_methods() -> None:
    forbidden: list[tuple[str, str, int]] = []
    for path in _python_sources():
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


def test_experiments_have_no_transport_or_database_imports() -> None:
    tree = ast.parse(
        _source(PACKAGE / "experiments.py"),
        filename=str(PACKAGE / "experiments.py"),
    )
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


def test_marker_server_has_no_backend_app_dependency() -> None:
    source = _source(PACKAGE / "marker_server.py")

    assert "from app." not in source
    assert "import app." not in source


def test_checkpoint_store_is_the_only_checkpoint_file_writer() -> None:
    writers = [
        path.name
        for path in _python_sources()
        if ".write_text(" in _source(path)
    ]

    assert writers == ["contracts.py"]


def test_resume_smoke_has_all_experiments_and_exact_cleanup() -> None:
    experiments = _source(PACKAGE / "experiments.py")
    runtime_edge = _source(PACKAGE / "runtime_edge.py")
    profile_fixture = _source(PACKAGE / "profile_fixture.py")

    for experiment in range(8):
        assert f'"E{experiment}"' in experiments
    assert "release_all_holds" in experiments
    assert "self.store.checkpoint.sessions" in runtime_edge
    assert "self.store.checkpoint.experts" in profile_fixture
    assert "verify_cleanup" in profile_fixture
    assert "cleanup_errors" in _source(PACKAGE / "cli.py")


def test_resume_smoke_audits_every_control_method() -> None:
    sources = "\n".join(_source(path) for path in _python_sources())

    assert "control_methods" in sources
    assert "assert_no_global_reload" in sources
    assert '"session.prepare"' in sources
    assert '"session.invalidate"' in sources
    assert '"profile.skills.replace"' in sources
    assert '"profile.mcp.replace"' in sources


def test_resume_smoke_compacts_streaming_deltas_before_checkpointing() -> None:
    runtime_edge = _source(PACKAGE / "runtime_edge.py")
    record_event = runtime_edge[runtime_edge.index("    def _record_run_event(") :]

    reasoning_guard = 'if event_type == "chat.reasoning.delta":'
    message_guard = 'if event_type == "chat.message.delta":'
    append = "evidence.events.append("
    assert record_event.index(reasoning_guard) < record_event.index(append)
    assert record_event.index(message_guard) < record_event.index(append)


def test_resume_smoke_follows_promoted_redirect_runs() -> None:
    runtime_edge = _source(PACKAGE / "runtime_edge.py")

    assert 'message_type == "run.redirect_promoted"' in runtime_edge
    assert '"type": "run.resume"' in runtime_edge
    assert '"promoted_run_id"' in runtime_edge


def test_resume_smoke_does_not_confuse_cleanup_with_business_success() -> None:
    experiments = _source(PACKAGE / "experiments.py")
    cli = _source(PACKAGE / "cli.py")

    assert "acceptance_failures" in experiments
    assert '"alpha_v2"' in experiments
    assert '"E5-next"' in experiments
    assert '"E7-v2"' in experiments
    acceptance = _source(PACKAGE / "resume_acceptance.py")
    assert '"registry-keeper"' in acceptance
    assert '"E4-same"' in acceptance
    assert "run_marker_not_observed" in acceptance
    assert "business acceptance failed" in cli
