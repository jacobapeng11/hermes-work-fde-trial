import pytest

from app.api.v1.user.runtime_capabilities import InMemoryRuntimeCapabilityStore


@pytest.mark.asyncio
async def test_in_memory_capabilities_hide_staged_assets_until_applied_lock() -> None:
    store = InMemoryRuntimeCapabilityStore()
    before = await store.list_capabilities(org_id=1, user_id=1, runtime_workspace_id=1)
    assert before["skills"] == []

    store.set_applied_lock(
        {
            "assets": [
                {"asset_type": "skill", "asset_id": 7, "action": "install"},
                {"asset_type": "mcp", "asset_id": 8, "action": "install"},
                {"asset_type": "knowledge_base", "asset_id": 9, "action": "remove"},
            ]
        }
    )
    after = await store.list_capabilities(org_id=1, user_id=1, runtime_workspace_id=1)
    assert [item.asset_id for item in after["skills"]] == [7]
    assert [item.asset_id for item in after["mcps"]] == [8]
    assert after["knowledge_bases"] == []
