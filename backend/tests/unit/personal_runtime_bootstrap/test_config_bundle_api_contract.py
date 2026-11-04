from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.internal.runtime_config_bundles as api_module
from app.api.internal.runtime_config_bundles import (
    _download_personal_config_bundle_impl,
)


class _Session:
    def __init__(self, binding: object) -> None:
        self.binding = binding

    async def scalar(self, _statement):
        return self.binding


class _ProjectionService:
    def __init__(self, _db) -> None:
        pass

    async def authenticate(self, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_personal_bundle_download_selects_revision_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = SimpleNamespace(config_generation=3, image="runtime:v0.20.1")
    captured: list[bool] = []

    class _Builder:
        def __init__(self, _db) -> None:
            pass

        async def build(self, **kwargs):
            captured.append(bool(kwargs["bootstrap"]))
            return SimpleNamespace(
                files={"config.yaml": "model: session\n", "SOUL.md": "# Runtime\n"},
                config_digest="a" * 64,
            )

    monkeypatch.setattr(
        api_module,
        "RuntimeProjectionService",
        _ProjectionService,
    )
    monkeypatch.setattr(
        api_module,
        "resolve_runtime_image_contract",
        lambda **_kwargs: SimpleNamespace(config_schema=34),
    )
    monkeypatch.setattr(api_module, "PersonalConfigBundleBuilder", _Builder)

    payload = await _download_personal_config_bundle_impl(
        profile_id=41,
        generation=3,
        authorization="Bearer runtime-key",
        runtime_id="rt-bootstrap-41",
        binding_id="191",
        config_revision="bootstrap-generation-3",
        expected_digest="a" * 64,
        db=_Session(binding),  # type: ignore[arg-type]
    )

    assert captured == [True]
    assert payload.config_revision == "bootstrap-generation-3"


@pytest.mark.asyncio
async def test_personal_bundle_download_rejects_unknown_revision_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_module,
        "RuntimeProjectionService",
        _ProjectionService,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _download_personal_config_bundle_impl(
            profile_id=41,
            generation=3,
            authorization="Bearer runtime-key",
            runtime_id="rt-bootstrap-41",
            binding_id="191",
            config_revision="generation-2",
            expected_digest="a" * 64,
            db=_Session(SimpleNamespace(config_generation=3)),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
