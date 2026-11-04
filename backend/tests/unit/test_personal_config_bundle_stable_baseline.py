from types import SimpleNamespace

import pytest

import app.services.personal_config_bundle as bundle_module
from app.services.personal_config_bundle import (
    PersonalConfigBundleBuilder,
    PersonalConfigBundleError,
)


class _Session:
    def __init__(self, values: list[object | None]) -> None:
        self.values = list(values)

    async def scalar(self, _statement):
        return self.values.pop(0)


@pytest.mark.asyncio
async def test_personal_bundle_is_asset_free_stable_profile_baseline(
    monkeypatch,
) -> None:
    profile = SimpleNamespace(
        id=41,
        org_id=7,
        user_id=9,
        name="测试工作区",
        default_model_id=13,
    )
    binding = SimpleNamespace(runtime_id="rt-stable-41", config_generation=6)
    user = SimpleNamespace(name="测试用户", user_code="employee-9")

    class _ProfileStore:
        def __init__(self, _db) -> None:
            pass

        async def ensure_default(self, *, workspace):
            assert workspace is profile
            return SimpleNamespace(id=101)

    class _ModelStore:
        def __init__(self, _db) -> None:
            pass

        async def list_visible_models(self, *, org_id: int, user_id: int):
            assert (org_id, user_id) == (7, 9)
            return [
                SimpleNamespace(
                    model_entry_id=13,
                    context_length=32_000,
                    tags=["chat"],
                    is_default=True,
                )
            ]

    monkeypatch.setattr(
        bundle_module,
        "SqlAlchemyRuntimeAgentProfileStore",
        _ProfileStore,
    )
    monkeypatch.setattr(
        bundle_module,
        "SqlAlchemyRuntimeModelStore",
        _ModelStore,
    )

    bundle = await PersonalConfigBundleBuilder(
        _Session([profile, binding, user])  # type: ignore[arg-type]
    ).build(
        profile_id=41,
        generation=6,
        require_current=True,
        config_schema=34,
    )

    assert bundle.config_revision == "generation-6"
    assert len(bundle.config_digest) == 64
    assert '"runtime_id": "rt-stable-41"' in bundle.files[
        "platform/config.snapshot.json"
    ]
    assert '"installed_skills": 0' in bundle.files[
        "platform/config.snapshot.json"
    ]
    assert '"installed_mcps": 0' in bundle.files[
        "platform/config.snapshot.json"
    ]
    assert '"bound_knowledge": 0' in bundle.files[
        "platform/config.snapshot.json"
    ]
    assert "platform/user-identity.json" in bundle.files


@pytest.mark.asyncio
async def test_personal_bundle_rejects_stale_runtime_generation() -> None:
    profile = SimpleNamespace(
        id=41,
        org_id=7,
        user_id=9,
        name="测试工作区",
        default_model_id=None,
    )
    binding = SimpleNamespace(runtime_id="rt-stable-41", config_generation=5)

    with pytest.raises(
        PersonalConfigBundleError,
        match="no longer current",
    ):
        await PersonalConfigBundleBuilder(
            _Session([profile, binding])  # type: ignore[arg-type]
        ).build(
            profile_id=41,
            generation=6,
            require_current=True,
        )
