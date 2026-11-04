from __future__ import annotations

import pytest

from app.services.runtime_access_nonce import InMemoryRuntimeAccessNonceStore


@pytest.mark.asyncio
async def test_runtime_access_nonce_is_one_time() -> None:
    store = InMemoryRuntimeAccessNonceStore()

    assert await store.consume("nonce-0000000000000001", ttl_seconds=300)
    assert not await store.consume("nonce-0000000000000001", ttl_seconds=300)
