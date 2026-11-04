from __future__ import annotations

import json

import pytest

from app.services.disposable_smoke_identity import (
    SmokeIdentityManifest,
    SmokeIdentityMismatch,
    disposable_smoke_identity,
)


class FakeStore:
    def __init__(self) -> None:
        self.next_id = 800
        self.users: dict[int, str] = {}

    async def create(self, *, purpose: str, user_code: str) -> SmokeIdentityManifest:
        self.next_id += 1
        self.users[self.next_id] = user_code
        return SmokeIdentityManifest(
            org_id=1,
            user_id=self.next_id,
            user_code=user_code,
            purpose=purpose,
            created_at="2026-08-01T00:00:00+00:00",
        )

    async def delete(self, manifest: SmokeIdentityManifest) -> None:
        if self.users.get(manifest.user_id) != manifest.user_code:
            raise SmokeIdentityMismatch(
                "cleanup identity no longer matches its creation manifest"
            )
        del self.users[manifest.user_id]


@pytest.mark.asyncio
async def test_disposable_identity_emits_manifest_and_cleans_success() -> None:
    store = FakeStore()
    cleanup: list[str] = []

    async with disposable_smoke_identity("runtime", store=store) as identity:
        async def cleanup_runtime() -> None:
            cleanup.append("runtime")

        identity.add_cleanup(cleanup_runtime)
        payload = json.loads(identity.manifest.to_json())
        assert payload["user_code"].startswith("smoke-runtime-")
        assert payload["user_id"] in store.users

    assert cleanup == ["runtime"]
    assert store.users == {}


@pytest.mark.asyncio
async def test_disposable_identity_cleans_after_body_failure() -> None:
    store = FakeStore()

    with pytest.raises(RuntimeError, match="smoke failed"):
        async with disposable_smoke_identity("failure", store=store):
            raise RuntimeError("smoke failed")

    assert store.users == {}


@pytest.mark.asyncio
async def test_disposable_identity_refuses_mismatched_cleanup() -> None:
    store = FakeStore()

    with pytest.raises(
        SmokeIdentityMismatch,
        match="cleanup identity no longer matches",
    ):
        async with disposable_smoke_identity("mismatch", store=store) as identity:
            store.users[identity.manifest.user_id] = "some-real-user"


@pytest.mark.asyncio
async def test_disposable_identity_rejects_unsafe_purpose_before_creation() -> None:
    store = FakeStore()

    with pytest.raises(ValueError, match="smoke purpose"):
        async with disposable_smoke_identity("../production", store=store):
            pass

    assert store.users == {}
