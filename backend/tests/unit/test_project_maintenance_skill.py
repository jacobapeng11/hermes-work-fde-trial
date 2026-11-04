from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_project_maintenance_skill_routes_and_authorizes() -> None:
    skill = (ROOT / "docs/superpowers/skills/hermes-project-maintenance/SKILL.md").read_text()

    assert "先用只读信息确认" in skill
    assert "只重启 Backend、完整公网服务，还是包含 Runtime" in skill
    assert "build-runtime-v3-image-dev.sh" in skill
    assert "reload-one-runtime-from-env-dev.sh" in skill
    assert "reload-all-runtimes-from-env-dev.sh" in skill
    assert "没有本次授权时只做只读检查或 `--print-plan`" in skill
    assert "不删除 PVC/PV/锁文件/`state.db`" in skill


def test_public_dev_stop_docs_match_current_lifecycle_boundary() -> None:
    readme = (ROOT / "README.md").read_text()
    maintenance = (ROOT / "docs/architecture/current/development/project-maintenance.md").read_text()

    assert "does not scale or reload Runtime revisions" in readme
    assert "不调用 Runtime 缩容" in maintenance
    assert "Runtimes remain at zero until" not in readme
