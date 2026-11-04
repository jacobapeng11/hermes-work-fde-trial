from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.api.internal.runtime_config_bundles as bundle_module


class _Session:
    def __init__(self) -> None:
        self.rollback_calls = 0

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    (
        "Packet sequence number wrong - got 1 expected 0",
        "PendingRollbackError: transaction is inactive",
    ),
)
async def test_personal_bundle_retry_rolls_back_only_current_session(
    monkeypatch,
    message: str,
) -> None:
    session = _Session()
    reset_pool = AsyncMock()
    attempts = 0
    expected = object()

    async def implementation(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(message)
        return expected

    monkeypatch.setattr(
        bundle_module,
        "_download_personal_config_bundle_impl",
        implementation,
    )
    # Keep this guard even though the production import was removed: if a
    # future change reintroduces a global reset, this test must fail loudly.
    monkeypatch.setattr(
        bundle_module,
        "reset_connection_pool",
        reset_pool,
        raising=False,
    )

    result = await bundle_module.download_personal_config_bundle(
        profile_id=41,
        generation=3,
        authorization="Bearer token",
        runtime_id="runtime-41",
        binding_id="41",
        config_revision="generation-3",
        expected_digest="d" * 64,
        db=session,  # type: ignore[arg-type]
    )

    assert result is expected
    assert attempts == 2
    assert session.rollback_calls == 1
    reset_pool.assert_not_awaited()
