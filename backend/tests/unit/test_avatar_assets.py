from __future__ import annotations

import struct

import pytest

from app.config import settings
from app.main import create_app
from app.services.avatar_assets import (
    AvatarAssetError,
    BUILTIN_AVATAR_IDS,
    validate_avatar_image,
    validate_avatar_value,
)
from app.services.expert_errors import ExpertProfileError, derived_expert_plugin_ids
from app.services.runtime_workspace import Principal


def _png(width: int = 256, height: int = 256) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height)


def test_avatar_routes_are_registered() -> None:
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in (route.methods or set())
    }
    assert ("/api/v1/user/avatar-assets", "GET") in routes
    assert ("/api/v1/user/avatar-assets", "POST") in routes


def test_builtin_avatar_registry_keeps_the_existing_eight_ids() -> None:
    assert BUILTIN_AVATAR_IDS == tuple(f"avatar-{index:02d}" for index in range(1, 9))


def test_avatar_validation_checks_content_and_declared_type() -> None:
    assert validate_avatar_image(
        content=_png(),
        declared_content_type="image/png",
    ) == ("image/png", "png")

    with pytest.raises(AvatarAssetError, match="avatar_file_type_mismatch"):
        validate_avatar_image(
            content=_png(),
            declared_content_type="image/jpeg",
        )


def test_avatar_validation_rejects_excessive_source_pixels(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AVATAR_MAX_SOURCE_PIXELS", 65_536)
    with pytest.raises(AvatarAssetError, match="avatar_dimensions_invalid"):
        validate_avatar_image(
            content=_png(width=1024, height=1024),
            declared_content_type="image/png",
        )


@pytest.mark.asyncio
async def test_builtin_avatar_value_does_not_require_a_database_lookup() -> None:
    assert await validate_avatar_value(
        object(),  # type: ignore[arg-type]
        principal=Principal(org_id=1, user_id=2),
        avatar_value="builtin:avatar-08",
    ) == "builtin:avatar-08"


@pytest.mark.asyncio
async def test_external_or_unowned_avatar_values_are_rejected() -> None:
    class MissingAvatarDb:
        async def scalar(self, _statement):
            return None

    with pytest.raises(AvatarAssetError, match="avatar_not_available"):
        await validate_avatar_value(
            MissingAvatarDb(),  # type: ignore[arg-type]
            principal=Principal(org_id=1, user_id=2),
            avatar_value="https://untrusted.example/avatar.jpg",
        )


def test_expert_plugins_are_derived_from_knowledge_and_cannot_be_forged() -> None:
    assert derived_expert_plugin_ids(
        knowledge_kinds={"personal", "enterprise"},
        enterprise_knowledge_enabled=False,
        submitted_plugin_ids=[],
    ) == ["personal-knowledge"]

    assert derived_expert_plugin_ids(
        knowledge_kinds={"personal"},
        enterprise_knowledge_enabled=True,
        submitted_plugin_ids=[],
    ) == ["enterprise-knowledge", "personal-knowledge"]

    with pytest.raises(ExpertProfileError, match="expert_plugin_selection_not_editable"):
        derived_expert_plugin_ids(
            knowledge_kinds=set(),
            enterprise_knowledge_enabled=False,
            submitted_plugin_ids=["personal-knowledge"],
        )
