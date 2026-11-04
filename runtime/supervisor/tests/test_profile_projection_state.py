from __future__ import annotations

from pathlib import Path

import pytest

from platform_supervisor.asset_contracts import (
    ProfileExecutionControlRequest,
    ProfileProjectionApplyRequest,
)
from platform_supervisor.profile_projection_state import (
    ProfileProjectionStateError,
    ProfileProjectionStateStore,
)


def _request(*, revision: int = 1, expected: int = 0, digest: str = "a"):
    return ProfileProjectionApplyRequest(
        operation_id=f"profile-operation-{revision:04d}",
        runtime_id="rt-119",
        binding_id=119,
        runtime_workspace_id=119,
        runtime_agent_profile_id=173,
        hermes_profile_name="expert-173",
        desired_revision=revision,
        expected_applied_revision=expected,
        expected_effective_generation=expected,
        digests={
            "profile": f"sha256:{digest * 64}",
            "skill": f"sha256:{'b' * 64}",
            "mcp": f"sha256:{'c' * 64}",
            "knowledge": f"sha256:{'d' * 64}",
        },
        profile_config={},
        artifacts=[],
    )


@pytest.mark.asyncio
async def test_pending_applied_state_is_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "profile.db"
    store = ProfileProjectionStateStore(path)
    await store.start()
    request = _request()

    pending = await store.begin(request)
    assert pending.status == "pending"
    applied = await store.applied(request)
    assert applied.status == "applied"
    assert applied.applied_revision == 1
    assert (await store.begin(request)).status == "applied"
    await store.close()

    reopened = ProfileProjectionStateStore(path)
    await reopened.start()
    observed = await reopened.get(173)
    assert observed is not None
    assert observed.status == "applied"
    assert observed.applied_revision == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_failed_same_target_can_retry_but_conflicting_digest_cannot(
    tmp_path: Path,
) -> None:
    store = ProfileProjectionStateStore(tmp_path / "profile.db")
    await store.start()
    request = _request()
    await store.begin(request)
    failed = await store.failed(
        request,
        error_code="DOWNLOAD_FAILED",
        error_message="redacted",
    )
    assert failed.status == "failed"
    assert (await store.begin(request)).status == "pending"

    with pytest.raises(
        ProfileProjectionStateError,
        match="PROFILE_PROJECTION_TARGET_CONFLICT",
    ):
        await store.begin(_request(digest="e"))
    await store.close()


@pytest.mark.asyncio
async def test_next_revision_requires_current_applied_revision(tmp_path: Path) -> None:
    store = ProfileProjectionStateStore(tmp_path / "profile.db")
    await store.start()
    first = _request()
    await store.begin(first)
    await store.applied(first)

    with pytest.raises(
        ProfileProjectionStateError,
        match="PROFILE_APPLIED_REVISION_STALE",
    ):
        await store.begin(_request(revision=2, expected=0, digest="e"))
    pending = await store.begin(_request(revision=2, expected=1, digest="e"))
    assert pending.status == "pending"
    assert pending.applied_revision == 1
    await store.close()


@pytest.mark.asyncio
async def test_execution_revoke_is_monotonic_and_enable_restores_identity(
    tmp_path: Path,
) -> None:
    store = ProfileProjectionStateStore(tmp_path / "profile.db")
    await store.start()
    projection = _request()
    await store.begin(projection)
    await store.applied(projection)

    def command(action: str, generation: int, operation: str):
        return ProfileExecutionControlRequest(
            operation_id=operation,
            action=action,
            runtime_id="rt-119",
            binding_id=119,
            runtime_workspace_id=119,
            runtime_agent_profile_id=173,
            admission_generation=generation,
            reason_code="security_test",
        )

    revoked = await store.control(command("revoke", 1, "revoke-operation-0001"))
    assert revoked.status == "applied"
    assert (await store.get(173)).execution_revoked is True
    repeated = await store.control(command("revoke", 1, "revoke-operation-0001"))
    assert repeated.status == "already_applied"
    with pytest.raises(
        ProfileProjectionStateError,
        match="PROFILE_ADMISSION_GENERATION_CONFLICT",
    ):
        await store.control(command("enable", 1, "enable-operation-0001"))
    enabled = await store.control(command("enable", 2, "enable-operation-0002"))
    assert enabled.status == "applied"
    assert (await store.get(173)).execution_revoked is False
    await store.close()
