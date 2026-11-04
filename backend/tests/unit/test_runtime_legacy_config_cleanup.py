from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "backend/scripts/runtime_legacy_config_cleanup.py"


@pytest.fixture(scope="module")
def cleanup_module():
    spec = importlib.util.spec_from_file_location(
        "runtime_legacy_config_cleanup_test",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cleanup_rejects_unsafe_or_duplicate_paths(cleanup_module) -> None:
    assert cleanup_module._safe_relative_paths(
        ["skills/current/SKILL.md"],
        "test paths",
    ) == ("skills/current/SKILL.md",)
    with pytest.raises(RuntimeError, match="unsafe"):
        cleanup_module._safe_relative_paths(["../state.db"], "test paths")
    with pytest.raises(RuntimeError, match="duplicate"):
        cleanup_module._safe_relative_paths(
            ["skills/current/SKILL.md", "skills/current/SKILL.md"],
            "test paths",
        )


def test_cleanup_removes_only_legacy_difference_and_config_transaction_metadata(
    tmp_path: Path,
    cleanup_module,
) -> None:
    home = tmp_path / "home"
    state_root = tmp_path / "supervisor"
    legacy = home / "skills/legacy/SKILL.md"
    current = home / "skills/current/SKILL.md"
    legacy.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")
    current.write_text("current", encoding="utf-8")
    backup = home / ".platform-config-backup/skills/legacy/SKILL.md"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    state_root.mkdir()
    (state_root / "current-config.json").write_text("{}", encoding="utf-8")
    (state_root / "config-apply-journal.json").write_text("{}", encoding="utf-8")
    untouched_state_db = state_root / "runtime.db"
    untouched_state_db.write_text("runtime state", encoding="utf-8")
    plan = cleanup_module.CleanupPlan(
        binding=SimpleNamespace(),
        pvc_uid="pvc-uid",
        local_pv_root=tmp_path,
        home=home,
        state_root=state_root,
        old_files=("skills/current/SKILL.md", "skills/legacy/SKILL.md"),
        current_files=("skills/current/SKILL.md",),
        delete_files=("skills/legacy/SKILL.md",),
        config_digest="a" * 64,
    )

    deleted = cleanup_module._apply_cleanup(plan)

    assert deleted == 1
    assert not legacy.exists()
    assert current.read_text(encoding="utf-8") == "current"
    assert not (home / ".platform-config-backup").exists()
    assert not (state_root / "current-config.json").exists()
    assert not (state_root / "config-apply-journal.json").exists()
    assert untouched_state_db.read_text(encoding="utf-8") == "runtime state"


def test_claim_pod_scan_ignores_non_pvc_volumes(
    cleanup_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cleanup_module,
        "_kubectl_json",
        lambda *_args, **_kwargs: {
            "items": [
                {
                    "metadata": {"namespace": "runtime", "name": "target"},
                    "spec": {
                        "volumes": [
                            {"name": "temporary", "emptyDir": {}},
                            {
                                "name": "runtime-root",
                                "persistentVolumeClaim": {"claimName": "runtime-pvc"},
                            },
                        ]
                    },
                },
                {
                    "metadata": {"namespace": "runtime", "name": "unrelated"},
                    "spec": {"volumes": [{"name": "temporary", "emptyDir": {}}]},
                },
            ]
        },
    )

    assert cleanup_module._claim_pods(
        namespace="runtime",
        pvc_name="runtime-pvc",
        kubeconfig=None,
    ) == ["target"]
