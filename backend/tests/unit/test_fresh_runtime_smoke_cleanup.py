import json
from pathlib import Path

import pytest

from app.config import settings
from scripts.fresh_runtime_smoke_cleanup import (
    FreshRuntimeCleanupPlan,
    FreshRuntimeSmokeAccount,
    FreshRuntimeSmokeCleanupError,
    _remove_runtime_root,
    acceptance_work_no,
)
from scripts import fresh_runtime_smoke_cleanup


def _plan(root: Path) -> FreshRuntimeCleanupPlan:
    acceptance_id = "a1b2c3d4e5f6"
    return FreshRuntimeCleanupPlan(
        account=FreshRuntimeSmokeAccount(
            acceptance_id=acceptance_id,
            org_id=17,
            user_id=23,
            work_no=acceptance_work_no(acceptance_id=acceptance_id),
        ),
        profile_id=31,
        binding_id=37,
        runtime_id="rt-31",
        namespace="hermes-runtimes",
        kservice_name="runtime-smoke-test",
        pvc_name="runtime-smoke-test-root",
        pvc_uid="pvc-uid-37",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root=root,
        runtime_agent_profile_ids=(41,),
    )


def test_fresh_smoke_cleanup_removes_only_a_real_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "17" / "smoke-test"
    (root / "home").mkdir(parents=True)
    (root / "home" / "state.db").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(settings, "HERMES_RUNTIME_ROOT", str(tmp_path))

    _remove_runtime_root(_plan(root))

    assert not root.exists()
    assert tmp_path.exists()


def test_fresh_smoke_cleanup_refuses_symbolic_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "17" / "smoke-test"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(settings, "HERMES_RUNTIME_ROOT", str(tmp_path))

    with pytest.raises(FreshRuntimeSmokeCleanupError, match="symbolic link"):
        _remove_runtime_root(_plan(root))

    assert root.exists()
    assert outside.exists()


def test_cleanup_checkpoint_preserves_the_acceptance_failure_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acceptance_id = "a1b2c3d4e5f6"
    account = FreshRuntimeSmokeAccount(
        acceptance_id=acceptance_id,
        org_id=17,
        user_id=23,
        work_no=acceptance_work_no(acceptance_id=acceptance_id),
    )
    monkeypatch.setattr(fresh_runtime_smoke_cleanup, "CHECKPOINT_ROOT", tmp_path)

    fresh_runtime_smoke_cleanup.write_checkpoint(
        account,
        status="acceptance_failed",
        error_type="FreshRuntimeSmokeError",
    )
    fresh_runtime_smoke_cleanup.write_checkpoint(
        account,
        status="cleanup_completed",
    )

    payload = json.loads(
        (tmp_path / f"{acceptance_id}.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "cleanup_completed"
    assert payload["acceptance_status"] == "failed"
    assert payload["acceptance_error_type"] == "FreshRuntimeSmokeError"
